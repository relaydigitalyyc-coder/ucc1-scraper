"""Google Maps business phone scraper for MCA lead enrichment.

Uses Playwright to automate Google Maps searches and extract phone numbers,
websites, and other business data from listing results.

Anti-detection features:
  - Stealth scripts to hide automation flags
  - User agent rotation (4 rotating agents)
  - Random delays between actions (2-4s)
  - Captcha detection with auto-pause
  - Browser context rotation signals

Extraction strategies (in priority order):
  1. Click phone button on listing info panel  (data-item-id="phone:tel:NNNN")
  2. Extract from aria-label attributes
  3. Fallback regex extraction from page body text

Every result is cached in a local SQLite database so the same business is
never re-queried across runs.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

from models.lead import MCALead

__all__ = [
    "GoogleMapsEnricher",
    "GmapsCache",
    "clean_phone",
    "make_business_key",
]

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────

DEFAULT_CACHE_PATH = Path("data/gmaps_cache.db")

# How many queries before rotating browser session
QUERIES_PER_BROWSER = 50

# Pause duration when captcha detected (seconds)
CAPTCHA_PAUSE_SECONDS = 60

# Random delay range between actions
MIN_DELAY = 2.0
MAX_DELAY = 4.0

# Rotating user agents to reduce fingerprinting
USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
]

# Phone regex for fallback extraction from raw text
US_PHONE_RE = re.compile(r"(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}")

PHONE_CLEAN_RE = re.compile(r"[^\d]")

CAPTCHA_INDICATORS = [
    "unusual traffic",
    "captcha",
    "not a robot",
    "verify you're human",
    "sorry, please try again later",
]


# ── Phone utilities ────────────────────────────────────────────────


def clean_phone(raw: str) -> str | None:
    """Extract and format a US phone number to E.164 (+1xxxxxxxxxx).

    Handles extensions by trimming to the first 10 or 11 digits.

    Args:
        raw: Raw text potentially containing a phone number.

    Returns:
        E.164 formatted string (e.g. +13055550123) or None if unparseable.
    """
    nums = PHONE_CLEAN_RE.sub("", raw)

    # Exact matches first (before trimming)
    if len(nums) == 10 and nums[0] in "23456789":
        return f"+1{nums}"
    if len(nums) == 11 and nums[0] == "1":
        return f"+{nums}"

    # Handle extensions / extra trailing digits
    if len(nums) > 11 and nums[0] == "1":
        return f"+{nums[:11]}"
    if len(nums) > 10:
        nums = nums[:10]
        if nums[0] in "23456789":
            return f"+1{nums}"

    return None


def make_business_key(name: str, city: str, state: str) -> str:
    """Create a normalized cache key for a business lookup.

    Strips common entity suffixes (LLC, INC, CORP, etc.) and normalizes
    whitespace and case so that equivalent business names produce the
    same cache key.

    Args:
        name: Business name (e.g. "MIAMI RESTAURANT GROUP LLC")
        city: City name
        state: Two-letter state code

    Returns:
        Normalized key string: "NAME|CITY|STATE"
    """
    clean = re.sub(r"[,.\s]+", " ", name.strip()).upper().strip()
    clean = re.sub(
        r"\s+(LLC|L\.L\.C\.|INC|INC\.|CORP|CORP\.|CORPORATION|L\.P\.|LP)\s*$",
        "",
        clean,
    ).strip()
    return f"{clean}|{city.strip().upper()}|{state.strip().upper()}"


# ── SQLite cache ───────────────────────────────────────────────────


class GmapsCache:
    """SQLite-backed cache for Google Maps enrichment results.

    Prevents re-scraping the same business across runs. Thread-safe for
    single-process async usage.

    Usage:
        cache = GmapsCache(Path("data/gmaps_cache.db"))
        cache.open()
        cache.set("BIZ|CITY|ST", {"phone": "+15551234567"})
        result = cache.get("BIZ|CITY|ST")
        cache.close()
    """

    def __init__(self, db_path: Path = DEFAULT_CACHE_PATH):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def open(self):
        """Open (or create) the database and ensure the schema exists."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gmaps_cache (
                business_key TEXT PRIMARY KEY,
                phone TEXT,
                website TEXT,
                address TEXT,
                rating REAL,
                confidence TEXT,
                source TEXT,
                queried_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def get(self, business_key: str) -> dict | None:
        """Retrieve a cached enrichment result.

        Args:
            business_key: Normalized business key from make_business_key().

        Returns:
            Dict with phone/website/address/rating/confidence/source keys,
            or None if not cached.
        """
        if not self._conn:
            return None
        cursor = self._conn.execute(
            """
            SELECT phone, website, address, rating, confidence, source
            FROM gmaps_cache WHERE business_key = ?
            """,
            (business_key,),
        )
        row = cursor.fetchone()
        if row:
            return {
                "phone": row[0],
                "website": row[1],
                "address": row[2],
                "rating": row[3],
                "confidence": row[4],
                "source": row[5],
            }
        return None

    def set(self, business_key: str, result: dict):
        """Insert or update a cached enrichment result.

        Args:
            business_key: Normalized business key.
            result: Dict with phone/website/address/rating/confidence/source.
        """
        if not self._conn:
            return
        self._conn.execute(
            """
            INSERT OR REPLACE INTO gmaps_cache
                (business_key, phone, website, address, rating, confidence, source, queried_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                business_key,
                result.get("phone"),
                result.get("website"),
                result.get("address"),
                result.get("rating"),
                result.get("confidence", "low"),
                result.get("source", "google_maps"),
                datetime.now().isoformat(),
            ),
        )
        self._conn.commit()

    def stats(self) -> dict:
        """Return summary statistics about the cache.

        Returns:
            Dict with 'total' (entries) and 'with_phone' (entries with phone).
        """
        if not self._conn:
            return {"total": 0, "with_phone": 0}
        cursor = self._conn.execute(
            """SELECT COUNT(*),
               COALESCE(SUM(CASE WHEN phone IS NOT NULL AND phone != '' THEN 1 ELSE 0 END), 0)
               FROM gmaps_cache"""
        )
        row = cursor.fetchone()
        return {"total": row[0], "with_phone": row[1]}

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()


# ── GoogleMapsEnricher ─────────────────────────────────────────────


class GoogleMapsEnricher:
    """Enrich leads with phone numbers from Google Maps search results.

    Uses Playwright to automate a headless (or headed) Chromium browser,
    searches Google Maps for a business by name+city+state, and extracts
    the phone number from the result listing.

    Features:
      - Multiple extraction strategies with fallback chain
      - Persistent SQLite cache (never re-scrapes a known business)
      - User agent rotation
      - Anti-detection stealth scripts
      - Captcha detection with auto-pause
      - Compatible with existing LeadEnricher pipeline

    Usage:
        async with GoogleMapsEnricher(headless=True) as enricher:
            result = await enricher.find_phone("Joe's Pizza", "New York", "NY")
            print(result["phone"])  # "+12125551234"
    """

    def __init__(
        self,
        headless: bool = True,
        max_concurrent: int = 3,
        cache_path: Path = DEFAULT_CACHE_PATH,
    ):
        self.headless = headless
        self.max_concurrent = max_concurrent
        self._query_count = 0
        self._user_agent_index = 0
        self._cache = GmapsCache(cache_path)
        self._cache.open()

    # ── Lifecycle ──────────────────────────────────────────────────

    async def close(self):
        """Close the cache database connection."""
        self._cache.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── User agent rotation ─────────────────────────────────────────

    def _next_user_agent(self) -> str:
        """Return the next user agent in the rotation list (round-robin)."""
        ua = USER_AGENTS[self._user_agent_index % len(USER_AGENTS)]
        self._user_agent_index += 1
        return ua

    # ── Captcha detection ──────────────────────────────────────────

    @staticmethod
    def _is_captcha(page_text: str, page_url: str) -> bool:
        """Detect if Google is showing a captcha/blocked page.

        Checks both page body text and URL for known block signals.

        Args:
            page_text: Page body text content.
            page_url: Current page URL.

        Returns:
            True if a captcha or block page is detected.
        """
        text_lower = page_text.lower()
        for indicator in CAPTCHA_INDICATORS:
            if indicator in text_lower:
                return True
        if "consent.google" in page_url or "sorry" in page_url:
            return True
        return False

    # ── Regex extraction (Strategy 3 fallback) ─────────────────────

    @staticmethod
    def _extract_via_regex(text: str) -> str | None:
        """Strategy 3: Extract phone number from raw text via regex.

        Last-resort fallback when DOM-based strategies don't yield a phone.

        Args:
            text: Page body text content.

        Returns:
            E.164 formatted phone string or None.
        """
        for match in US_PHONE_RE.finditer(text):
            phone = clean_phone(match.group())
            if phone:
                return phone
        return None

    # ── DOM extraction strategies ──────────────────────────────────

    @staticmethod
    async def _extract_via_click(page) -> str | None:
        """Strategy 1: Click the phone button on the listing info panel.

        Google Maps renders phone numbers inside a button with
        data-item-id="phone:tel:NNNN". Clicking opens a detail panel or
        reveals the number in the aria-label.

        Args:
            page: Playwright Page object (must be on a Maps search page).

        Returns:
            E.164 formatted phone string or None.
        """
        try:
            # Wait for result list to render
            await page.wait_for_timeout(2000)

            # Click on first search result to open the side info panel
            first_result = page.locator('a[href*="maps/place/"]').first
            if await first_result.count() > 0:
                await first_result.click()
                await asyncio.sleep(random.uniform(1.0, 2.0))

            # Find the phone button in the info panel
            phone_btn = page.locator('button[data-item-id*="phone:tel:"]').first
            if await phone_btn.count() > 0:
                # Try aria-label first (often contains the full number)
                phone_text = await phone_btn.get_attribute("aria-label") or ""
                if not phone_text:
                    phone_text = await phone_btn.text_content() or ""

                numbers = PHONE_CLEAN_RE.sub("", phone_text)
                if len(numbers) >= 10:
                    return clean_phone(numbers)
        except Exception:
            logger.debug("Strategy 1 (click phone button) failed", exc_info=True)
        return None

    @staticmethod
    async def _extract_via_aria(page) -> str | None:
        """Strategy 2: Extract phone from aria-label attributes.

        Some Google Maps layouts put the full phone number in the
        aria-label of buttons rather than requiring a click.

        Args:
            page: Playwright Page object.

        Returns:
            E.164 formatted phone string or None.
        """
        try:
            buttons = await page.locator(
                'button[aria-label*="Phone"], button[aria-label*="phone"]'
            ).all()
            for btn in buttons:
                label = await btn.get_attribute("aria-label") or ""
                numbers = PHONE_CLEAN_RE.sub("", label)
                if len(numbers) >= 10:
                    return clean_phone(numbers)
        except Exception:
            logger.debug("Strategy 2 (aria-label) failed", exc_info=True)
        return None

    @staticmethod
    async def _extract_website(page) -> str | None:
        """Extract business website URL from the info panel.

        Args:
            page: Playwright Page object.

        Returns:
            Website URL string or None.
        """
        try:
            # Link with data-item-id containing "authority"
            site_btn = page.locator('a[data-item-id*="authority"]').first
            if await site_btn.count() > 0:
                href = await site_btn.get_attribute("href") or ""
                if href:
                    return href

            # Generic link pointing to an external site
            site_link = page.locator('a[role="link"][href*="http"]').first
            if await site_link.count() > 0:
                href = await site_link.get_attribute("href") or ""
                if href and "google.com" not in href:
                    return href
        except Exception:
            logger.debug("Website extraction failed", exc_info=True)
        return None

    @staticmethod
    async def _extract_address(page) -> str | None:
        """Extract business address from the info panel.

        Args:
            page: Playwright Page object.

        Returns:
            Address string or None.
        """
        try:
            addr_btn = page.locator('button[data-item-id*="address:"]').first
            if await addr_btn.count() > 0:
                text = await addr_btn.text_content() or ""
                return text.strip()
        except Exception:
            logger.debug("Address extraction failed", exc_info=True)
        return None

    # ── Core extraction pipeline ───────────────────────────────────

    async def _extract_phone(self, page) -> dict:
        """Run extraction strategies in priority order.

        Args:
            page: Playwright Page object on a Maps search result page.

        Returns:
            Dict with keys: phone, website, address, rating, confidence, source.
        """
        phone = None
        confidence = "low"

        # Strategy 1: Click phone button on info panel (highest accuracy)
        phone = await self._extract_via_click(page)
        if phone:
            confidence = "high"

        # Strategy 2: Extract from aria-label
        if not phone:
            phone = await self._extract_via_aria(page)
            if phone:
                confidence = "medium"

        # Strategy 3: Fallback regex extraction from page text
        if not phone:
            page_text = await page.text_content("body") or ""
            phone = self._extract_via_regex(page_text)
            if phone:
                confidence = "low"

        # Extract additional data
        website = None
        address = None
        try:
            website = await self._extract_website(page)
        except Exception:
            pass
        try:
            address = await self._extract_address(page)
        except Exception:
            pass

        return {
            "phone": phone,
            "website": website,
            "address": address,
            "rating": None,
            "confidence": confidence,
            "source": "google_maps",
        }

    # ── Main public API ────────────────────────────────────────────

    async def find_phone(
        self, business_name: str, city: str, state: str
    ) -> dict:
        """Search Google Maps and extract phone number for a business.

        Checks the local cache first. If not cached, launches a headless
        Playwright Chromium, navigates to Google Maps, and runs extraction
        strategies.

        Args:
            business_name: Business name (e.g. "MIAMI RESTAURANT GROUP LLC")
            city: Business city
            state: Two-letter state code

        Returns:
            Dict with keys:
              - phone (str|None): E.164 formatted phone number
              - website (str|None): Business website URL
              - address (str|None): Street address from Maps
              - rating (float|None): Google Maps star rating
              - confidence (str): "high", "medium", or "low"
              - source (str): Always "google_maps"
        """
        # Check cache first
        key = make_business_key(business_name, city, state)
        cached = self._cache.get(key)
        if cached:
            logger.debug("Cache hit for %s | %s, %s", business_name, city, state)
            return cached

        query = f"{business_name} {city} {state}".strip()
        encoded = re.sub(r"[^\w\s\-.,]", "", query)
        encoded = re.sub(r"\s+", "+", encoded)
        url = f"https://www.google.com/maps/search/{encoded}"

        logger.info(
            "Scraping Google Maps for %s | %s, %s", business_name, city, state
        )

        result = await self._scrape_phone(url, business_name)

        # Cache result
        self._cache.set(key, result)
        self._query_count += 1

        logger.debug(
            "Result for %s: phone=%s confidence=%s",
            business_name,
            result.get("phone"),
            result.get("confidence"),
        )

        # Rate limit: random delay between queries
        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        return result

    async def _scrape_phone(self, url: str, business_name: str) -> dict:
        """Core Playwright scraping logic.

        Launches a Chromium browser, navigates to the Maps URL, and
        extracts data. Extracted as a separate method for testability.

        Args:
            url: Fully-constructed Google Maps search URL.
            business_name: Business name (for logging).

        Returns:
            Dict with phone/website/address/rating/confidence/source.
        """
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)

            ua = self._next_user_agent()
            context = await browser.new_context(
                user_agent=ua,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )

            page = await context.new_page()

            # Anti-detection: hide automation flags before any JS runs
            await page.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });
                try { delete navigator.__proto__.webdriver; } catch (e) {}
                """
            )

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(random.uniform(2.0, 4.0))

                # Check for captcha / block page
                page_text = await page.text_content("body") or ""
                if self._is_captcha(page_text, page.url):
                    logger.warning(
                        "Captcha detected for %s — pausing %ds",
                        business_name,
                        CAPTCHA_PAUSE_SECONDS,
                    )
                    await asyncio.sleep(CAPTCHA_PAUSE_SECONDS)
                    return {
                        "phone": None,
                        "website": None,
                        "address": None,
                        "rating": None,
                        "confidence": "low",
                        "source": "google_maps",
                    }

                # Run extraction strategies
                result = await self._extract_phone(page)

                # Check captcha again after extraction (some pages load it late)
                if not result.get("phone"):
                    page_text2 = await page.text_content("body") or ""
                    if self._is_captcha(page_text2, page.url):
                        logger.warning(
                            "Delayed captcha detected for %s", business_name
                        )
                        await asyncio.sleep(CAPTCHA_PAUSE_SECONDS)

                return result

            except Exception:
                logger.error(
                    "Error scraping %s", business_name, exc_info=True
                )
                return {
                    "phone": None,
                    "website": None,
                    "address": None,
                    "rating": None,
                    "confidence": "low",
                    "source": "google_maps",
                }
            finally:
                await browser.close()

    # ── Pipeline integration ───────────────────────────────────────

    def cache_stats(self) -> dict:
        """Return cache statistics.

        Returns:
            Dict with 'total' (entries) and 'with_phone' (entries with phone).
        """
        return self._cache.stats()
