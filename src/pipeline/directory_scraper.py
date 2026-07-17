"""Playwright-powered directory scraper for business phone numbers.

Tries multiple free online directories in priority order to find a business
phone number.  Each directory is scraped headlessly via Playwright with
stealth patches, cookie-acceptance handling, random delays, and user-agent
rotation.  Results are cached in a local SQLite database so the same
business is never re-scraped across runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

# ---------------------------------------------------------------------------
#  Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

DEFAULT_CACHE_PATH = Path("data/directory_cache.db")

DIRECTORY_PRIORITY = [
    "yellowpages",
    "yellowpages_mip",
    "whitepages",
    "merchantcircle",
    "cylex",
]

NUMPY_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
PHONE_CLEAN_RE = re.compile(r"[^\d]")

TOLL_FREE_PREFIXES = {"800", "888", "877", "866", "855", "844", "833"}

USER_AGENTS = [
    # Windows Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # macOS Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Linux Chrome
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Windows Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]

# JavaScript that patches common automation-detection properties.
STEALTH_SCRIPT = """
// Hide webdriver
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// Fake plugins array (headless Chromium has 0)
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});

// Set language preferences
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en']
});

// Fake the chrome runtime object
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {}
};

// Override permissions so 'notifications' is denied
if (window.navigator.permissions) {
    const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
    window.navigator.permissions.query = (params) => {
        if (params && params.name === 'notifications') {
            return Promise.resolve({ state: 'denied' });
        }
        return origQuery(params);
    };
}

// Override connection type
Object.defineProperty(navigator, 'connection', {
    get: () => ({
        effectiveType: '4g',
        rtt: 50,
        downlink: 10,
        saveData: false,
    })
});
"""

# ---------------------------------------------------------------------------
#  Phone-number helpers
# ---------------------------------------------------------------------------


def clean_phone(raw: str) -> str | None:
    """Extract and format a US phone number as ``(XXX) XXX-XXXX``."""
    nums = PHONE_CLEAN_RE.sub("", raw)
    if len(nums) == 10 and nums[0] in "23456789":
        return f"({nums[:3]}) {nums[3:6]}-{nums[6:]}"
    if len(nums) == 11 and nums[0] == "1":
        return f"({nums[1:4]}) {nums[4:7]}-{nums[7:]}"
    return None


def extract_phones(text: str) -> list[str]:
    """Extract all US phone numbers from a block of text.

    Returns a deduplicated list of formatted phone numbers like
    ``(555) 123-4567``.  Toll-free numbers (800, 888, etc.) are
    excluded since they rarely represent the business itself.
    """
    seen: set[str] = set()
    results: list[str] = []

    for match in NUMPY_PHONE_RE.finditer(text):
        phone = clean_phone(match.group())
        if phone is None:
            continue
        area = phone[1:4]
        if area in TOLL_FREE_PREFIXES:
            continue
        if phone not in seen:
            seen.add(phone)
            results.append(phone)

    return results


def clean_business_name(name: str) -> str:
    """Remove legal entity suffixes for better directory matching."""
    cleaned = re.sub(
        r"\s+(LLC|L\.L\.C\.|INC|INC\.|CORP|CORP\.|CORPORATION|"
        r"LP|L\.P\.|LTD|LTD\.|PLC|P\.L\.C\.|PC|P\.C\.|PA|P\.A\.|"
        r"LLP|L\.L\.P\.)\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^THE\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[#&/\'\"]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ---------------------------------------------------------------------------
#  SQLite cache
# ---------------------------------------------------------------------------


class DirectoryCache:
    """Persistent cache for directory-scraper results.

    Prevents re-scraping the same business across runs.
    Results expire after 90 days.
    """

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS directory_cache (
            business_key TEXT PRIMARY KEY,
            phone        TEXT,
            source       TEXT,
            data         TEXT,
            queried_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dir_cache_ts
            ON directory_cache(queried_at);
    """

    def __init__(self, db_path: str | Path = DEFAULT_CACHE_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        if self._conn is not None:
            return
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get(self, business_key: str) -> dict[str, Any] | None:
        self.open()
        cursor = self._conn.execute(
            "SELECT phone, source, data, queried_at FROM directory_cache "
            "WHERE business_key = ?",
            (business_key,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        queried = datetime.fromisoformat(row["queried_at"])
        if datetime.now() - queried > __import__("datetime").timedelta(days=90):
            self._conn.execute(
                "DELETE FROM directory_cache WHERE business_key = ?",
                (business_key,),
            )
            self._conn.commit()
            return None
        return {
            "phone": row["phone"],
            "source": row["source"],
        }

    def set(self, business_key: str, phone: str | None, source: str) -> None:
        self.open()
        self._conn.execute(
            "INSERT OR REPLACE INTO directory_cache "
            "(business_key, phone, source, data, queried_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                business_key,
                phone,
                source,
                json.dumps({"phone": phone, "source": source}),
                datetime.now().isoformat(),
            ),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
#  Main scraper
# ---------------------------------------------------------------------------


class DirectoryScraper:
    """Scrape business phone numbers from free online directories.

    Parameters
    ----------
    headless : bool
        Run browser in headless mode (default ``True``).
    cache_path : str | Path
        Location of the SQLite cache database.
    timeout : int
        Default page-load timeout in milliseconds (default 25 000).
    """

    def __init__(
        self,
        headless: bool = True,
        cache_path: str | Path = DEFAULT_CACHE_PATH,
        timeout: int = 25000,
    ) -> None:
        self.headless = headless
        self.timeout = timeout
        self._cache = DirectoryCache(cache_path)
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the browser and create a stealth context."""
        self._pw = await async_playwright().start()
        ua = random.choice(USER_AGENTS)
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=ua,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/New_York",
            # Block images & fonts for speed
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        # Apply stealth patches to every page
        await self._context.add_init_script(STEALTH_SCRIPT)

    async def stop(self) -> None:
        """Release browser resources."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._cache.close()

    # ── Page helpers ───────────────────────────────────────────────────

    async def _random_delay(self, min_s: float = 0.8, max_s: float = 2.5) -> float:
        delay = random.uniform(min_s, max_s)
        await asyncio.sleep(delay)
        return delay

    async def _accept_cookies(self, page: Page) -> None:
        """Try to click common cookie-accept buttons."""
        selectors = [
            "button:has-text('Accept All')",
            "button:has-text('Accept all')",
            "button:has-text('Accept Cookies')",
            "button:has-text('I Accept')",
            "button:has-text('Got it')",
            "button:has-text('OK')",
            "button#onetrust-accept-btn-handler",
            ".cookie-accept",
            "#cookieConsent button",
            "[data-testid='cookie-accept']",
            "button:has-text('Allow All')",
            "button:has-text('Allow all')",
            "button:has-text('Agree')",
        ]
        for sel in selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=3000, state="visible")
                if btn:
                    await btn.click()
                    await asyncio.sleep(0.3)
                    return
            except (PlaywrightTimeout, Exception):
                continue

    async def _scrape_page(
        self,
        url: str,
        wait_selector: str | None = None,
        wait_ms: int = 3000,
    ) -> str | None:
        """Navigate to *url*, wait briefly, and return the visible page text.

        Returns ``None`` if the page fails to load or returns an error.
        """
        page = await self._context.new_page()
        try:
            response = await page.goto(
                url, timeout=self.timeout, wait_until="domcontentloaded"
            )
            if response and response.status >= 400:
                logger.debug("HTTP %d for %s", response.status, url)
                return None

            # Wait a beat for JS-rendered content
            await asyncio.sleep(wait_ms / 1000)

            # Accept any cookie dialog
            await self._accept_cookies(page)

            if wait_selector:
                try:
                    await page.wait_for_selector(
                        wait_selector, timeout=8000, state="visible"
                    )
                except PlaywrightTimeout:
                    pass

            # Give JS a moment to render phone numbers
            await asyncio.sleep(0.5)

            text = await page.inner_text("body")
            return text
        except PlaywrightTimeout:
            logger.debug("Timeout loading %s", url)
            return None
        except Exception as exc:
            logger.debug("Error scraping %s: %s", url, exc)
            return None
        finally:
            await page.close()

    # ── Directory implementations ──────────────────────────────────────

    async def _yellowpages(self, biz: str, city: str, state: str) -> str | None:
        """Search YellowPages.com — main search results."""
        search = quote(f"{biz} {city} {state}")
        url = f"https://www.yellowpages.com/search?search_terms={search}"
        text = await self._scrape_page(url, wait_ms=4000)
        if not text:
            return None
        phones = extract_phones(text)
        return phones[0] if phones else None

    async def _yellowpages_mip(self, biz: str, city: str, state: str) -> str | None:
        """Try YellowPages mobile (mip) page — simpler HTML structure."""
        slug = re.sub(r"[^a-z0-9]+", "-", biz.lower()).strip("-")
        city_slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
        state_slug = state.lower().strip()
        # Try a few variants
        variants = [
            f"https://www.yellowpages.com/mip/{slug}_{city_slug}_{state_slug}",
            f"https://www.yellowpages.com/mip/{slug}-{city_slug}-{state_slug}",
        ]
        for url in variants:
            text = await self._scrape_page(url, wait_ms=3000)
            if text:
                phones = extract_phones(text)
                if phones:
                    return phones[0]
        return None

    async def _whitepages(self, biz: str, city: str, state: str) -> str | None:
        """Search WhitePages business directory."""
        slug = re.sub(r"[^a-z0-9]+", "-", biz.lower()).strip("-")
        city_slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
        state_slug = state.lower().strip()
        url = (
            f"https://www.whitepages.com/business/{slug}/{city_slug}-{state_slug}"
        )
        text = await self._scrape_page(url, wait_ms=4000)
        if not text:
            return None
        phones = extract_phones(text)
        return phones[0] if phones else None

    async def _merchantcircle(self, biz: str, city: str, state: str) -> str | None:
        """Search MerchantCircle.com."""
        q = quote(biz)
        loc = quote(f"{city} {state}")
        url = f"https://www.merchantcircle.com/search?q={q}&l={loc}"
        text = await self._scrape_page(url, wait_ms=4000)
        if not text:
            return None
        phones = extract_phones(text)
        return phones[0] if phones else None

    async def _cylex(self, biz: str, city: str, state: str) -> str | None:
        """Search Cylex.us.com business directory."""
        query = quote(f"{biz} {city} {state}")
        url = f"https://www.cylex.us.com/s/{query}"
        text = await self._scrape_page(url, wait_ms=4000)
        if not text:
            return None
        phones = extract_phones(text)
        return phones[0] if phones else None

    # ── Public API ─────────────────────────────────────────────────────

    async def find_phone(
        self,
        biz_name: str,
        city: str,
        state: str,
        skip_cache: bool = False,
    ) -> dict[str, Any]:
        """Try every directory in priority order; return the first phone found.

        Parameters
        ----------
        biz_name : str
            Full business name (e.g. ``"Central Oregon Residential Services, LLC"``).
        city : str
            City name.
        state : str
            Two-letter state code.
        skip_cache : bool
            If ``True``, force a fresh scrape even if cached.

        Returns
        -------
        dict
            ``{"phone": "...", "source": "yellowpages", "cached": False}``
            or ``{"phone": None, "source": "none"}`` if nothing found.
        """
        key = f"{clean_business_name(biz_name).upper()}|{city.upper().strip()}|{state.upper().strip()}"

        # Check cache
        if not skip_cache:
            cached = self._cache.get(key)
            if cached is not None:
                return {**cached, "cached": True}

        clean_name = clean_business_name(biz_name)

        methods: list[tuple[str, Any]] = [
            ("yellowpages", self._yellowpages),
            ("yellowpages_mip", self._yellowpages_mip),
            ("whitepages", self._whitepages),
            ("merchantcircle", self._merchantcircle),
            ("cylex", self._cylex),
        ]

        for source_name, method in methods:
            try:
                await self._random_delay(1.0, 3.0)
                phone = await method(clean_name, city, state)
                if phone:
                    result = {"phone": phone, "source": source_name, "cached": False}
                    self._cache.set(key, phone, source_name)
                    return result
            except Exception as exc:
                logger.debug("%s failed for %s: %s", source_name, biz_name, exc)
                continue

        self._cache.set(key, None, "none")
        return {"phone": None, "source": "none", "cached": False}

    async def find_phones_all(
        self,
        biz_name: str,
        city: str,
        state: str,
    ) -> dict[str, dict[str, Any]]:
        """Try **all** directories and return results for each.

        Useful for diagnostics / comparative analysis.
        """
        clean_name = clean_business_name(biz_name)
        methods: list[tuple[str, Any]] = [
            ("yellowpages", self._yellowpages),
            ("yellowpages_mip", self._yellowpages_mip),
            ("whitepages", self._whitepages),
            ("merchantcircle", self._merchantcircle),
            ("cylex", self._cylex),
        ]

        results: dict[str, dict[str, Any]] = {}
        for source_name, method in methods:
            try:
                await self._random_delay(0.5, 1.5)
                phone = await method(clean_name, city, state)
                results[source_name] = {
                    "phone": phone,
                    "found": phone is not None,
                }
            except Exception as exc:
                results[source_name] = {"phone": None, "found": False, "error": str(exc)}

        return results


# ---------------------------------------------------------------------------
#  Convenience function
# ---------------------------------------------------------------------------


async def find_business_phone(
    biz_name: str,
    city: str,
    state: str,
    headless: bool = True,
) -> dict[str, Any]:
    """One-shot: launch a scraper, find a phone, close, and return result."""
    scraper = DirectoryScraper(headless=headless)
    await scraper.start()
    try:
        return await scraper.find_phone(biz_name, city, state)
    finally:
        await scraper.stop()
