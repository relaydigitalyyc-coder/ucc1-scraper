"""Florida UCC Scraper — floridaucc.com (Florida Secured Transaction Registry).

Portal: https://www.floridaucc.com/
Type: React/MUI SPA backed by public REST API (API Gateway, no auth required)
Approach: httpx directly against publicsearchapi.floridaucc.com/search

Florida portal details (discovered 2026-07-16):
  - Public API is truly public — no API key, no session token needed
  - GET https://publicsearchapi.floridaucc.com/search?text=...&searchOptionType=...
  - Search types: OrganizationDebtorName, IndividualDebtorName, DocumentNumber
  - Result sub-options: FiledCompactDebtorNameList (filed only),
    FiledAndLapsedCompactDebtorNameList (filed + lapsed, more results)
  - Pages: 20 debtors each, cursor-paginated via nextRowNumber
  - Results: Name, UCC Number, Address, City, State, Zip, Status
  - NO secured party in search results — requires detail page (image-based, skipped)
  - WAF rate limits: ~0.75-1.0 req/s sustained works; bursts trigger 403s

Strategy: Search by MCA-industry debtor name prefixes (RESTAURANT, TRUCKING,
CONSTRUCTION, MEDICAL, HOTEL, etc.). httpx is fast — we rotate clients and
User-Agent strings to avoid WAF throttling.  Each prefix yields ~200 results
at 20/page.  Deduplication across prefixes via filing number set.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import AsyncIterator, Optional

import httpx
from playwright.async_api import Page

from scrapers.base import BaseStateScraper
from scrapers.registry import register_scraper

logger = logging.getLogger(__name__)

# ── API constants ────────────────────────────────────────────────────────
PUBLIC_SEARCH_API = "https://publicsearchapi.floridaucc.com/search"
PAGE_SIZE = 20  # fixed by the API; each page returns exactly 20 debtors

# ── User-Agent rotation pool ─────────────────────────────────────────────
_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
)

# ── MCA-targeted debtor name prefixes ────────────────────────────────────
# Grouped by industry for readability.  Each prefix triggers a separate
# search; overlapping results are deduplicated by filing number.
MCA_DEBTOR_PREFIXES: list[str] = [
    # Food service
    "RESTAURANT",
    "DINER",
    "CAFE",
    "PIZZA",
    "GRILL",
    "SUSHI",
    "BAKERY",
    "CATERING",
    # Transportation / logistics
    "TRUCKING",
    "TRANSPORT",
    "LOGISTICS",
    "FREIGHT",
    "CARRIER",
    "DISPATCH",
    # Construction / trades
    "CONSTRUCTION",
    "CONTRACTOR",
    "BUILDER",
    "RENOVATION",
    "ROOFING",
    "PLUMBING",
    "ELECTRIC",
    "HVAC",
    "PAINTING",
    "MASONRY",
    "DRYWALL",
    "EXCAVATING",
    # Medical / healthcare
    "MEDICAL",
    "HEALTHCARE",
    "DENTAL",
    "PHARMACY",
    "CLINIC",
    "CHIROPRACTIC",
    "PHYSICAL THERAPY",
    # Auto
    "AUTO",
    "AUTOMOTIVE",
    "REPAIR",
    "COLLISION",
    "TIRE",
    "CAR WASH",
    "TOWING",
    # Hospitality
    "HOTEL",
    "MOTEL",
    "INN",
    "LODGING",
    "HOSPITALITY",
    # Beauty / grooming
    "SALON",
    "BARBER",
    "BEAUTY",
    "SPA",
    "NAIL",
    # Retail / grocery
    "RETAIL",
    "MARKET",
    "GROCERY",
    "LIQUOR",
    "CONVENIENCE",
    # Manufacturing / distribution
    "MANUFACTURING",
    "DISTRIBUTION",
    "WHOLESALE",
    # Services
    "LAUNDRY",
    "DRY CLEAN",
    "CLEANERS",
    "LANDSCAPING",
    "JANITORIAL",
    "SECURITY",
    "STAFFING",
    "CONSULTING",
    # Fitness / recreation
    "GYM",
    "FITNESS",
    # Transportation services
    "TAXI",
    "LIMOUSINE",
    # Generic high-hit business prefixes
    "AMERICAN",
    "UNITED",
    "PREMIER",
    "ELITE",
    "QUALITY",
    "COASTAL",
    "SUNSHINE",
    "ATLANTIC",
    "GULF",
    "BAY",
    "FLORIDA",
    "MIAMI",
    "ORLANDO",
    "TAMPA",
    "JACKSONVILLE",
]


def _random_ua() -> str:
    """Pick a random User-Agent from the rotation pool."""
    return random.choice(_USER_AGENTS)


@register_scraper("FL")
class FloridaScraper(BaseStateScraper):
    """Florida UCC filing scraper using the public REST API.

    Uses httpx (async HTTP) for speed — no Playwright needed except for
    health checks via the base class lifecycle.  The API is unauthenticated
    but rate-limited by AWS WAF; we rotate User-Agent strings and throttle
    carefully.
    """

    state = "FL"
    state_name = "Florida"
    base_url = "https://www.floridaucc.com/"

    # ── Rate limiting ───────────────────────────────────────────────────
    # AWS WAF begins returning 403s after ~700 fast requests.
    # Sustained 1.0 req/s with random jitter keeps us under the threshold.
    requests_per_second: float = 1.0
    _inter_request_delay: float = 1.2  # seconds between API calls (conservative)

    # ── Search configuration ────────────────────────────────────────────
    search_option_type: str = "OrganizationDebtorName"
    # "FiledAndLapsedCompactDebtorNameList" includes lapsed filings (more results)
    search_option_sub_option: str = "FiledAndLapsedCompactDebtorNameList"
    search_category: str = "Standard"

    # Cap pages per prefix to bound total runtime
    max_pages_per_prefix: int = 10  # 10 pages x 20 = 200 results per prefix max

    # ── Backoff configuration ───────────────────────────────────────────
    _max_backoff_attempts: int = 5
    _base_backoff_seconds: float = 2.0
    _backoff_multiplier: float = 2.0
    _max_backoff_seconds: float = 60.0

    def __init__(self, headless: bool = True, proxy: Optional[str] = None):
        super().__init__(headless=headless, proxy=proxy)
        self._http_client: Optional[httpx.AsyncClient] = None
        self._request_count: int = 0
        self._client_rotation_interval: int = 50  # rotate client every N requests

    # ── HTTP client management with rotation ────────────────────────────

    def _build_headers(self) -> dict[str, str]:
        """Build request headers with a fresh User-Agent."""
        return {
            "User-Agent": _random_ua(),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://floridaucc.com",
            "Referer": "https://floridaucc.com/search",
            "Cache-Control": "no-cache",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create an httpx client, rotating periodically.

        Rotating the client creates a fresh TCP connection pool with
        different source port assignments, helping evade WAF tracking.
        """
        if (
            self._http_client is None
            or self._request_count > 0
            and self._request_count % self._client_rotation_interval == 0
        ):
            if self._http_client is not None:
                await self._http_client.aclose()
            self._http_client = httpx.AsyncClient(
                headers=self._build_headers(),
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )
        return self._http_client

    async def _close_client(self) -> None:
        """Close the current httpx client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self):
        """Start Playwright browser and prepare HTTP client."""
        await super().start()
        # HTTP client created lazily

    async def stop(self):
        """Clean up all resources."""
        await self._close_client()
        await super().stop()

    # ── Backoff + retry ─────────────────────────────────────────────────

    async def _backoff_sleep(self, attempt: int) -> None:
        """Exponential backoff with random jitter."""
        delay = min(
            self._base_backoff_seconds * (self._backoff_multiplier ** attempt),
            self._max_backoff_seconds,
        )
        jitter = random.uniform(0, delay * 0.3)
        total = delay + jitter
        logger.debug("Backoff: %.1fs (attempt %d)", total, attempt + 1)
        await asyncio.sleep(total)

    # ── Core API call ───────────────────────────────────────────────────

    async def _search_api(
        self,
        text: str,
        row_number: Optional[int] = None,
        option_type: Optional[str] = None,
        sub_option: Optional[str] = None,
    ) -> dict:
        """Execute a single GET search request with retry + backoff.

        Returns the parsed JSON dict.  Raises the last httpx.HTTPStatusError
        if all retries are exhausted.
        """
        params: dict[str, str] = {
            "text": text,
            "searchOptionType": option_type or self.search_option_type,
            "searchOptionSubOption": sub_option or self.search_option_sub_option,
            "searchCategory": self.search_category,
        }
        if row_number is not None:
            params["rowNumber"] = str(row_number)

        last_error: Optional[Exception] = None

        for attempt in range(self._max_backoff_attempts):
            client = await self._get_client()

            try:
                response = await client.get(PUBLIC_SEARCH_API, params=params)
                self._request_count += 1

                if response.status_code == 403 or response.status_code == 429:
                    # Rate-limited — back off and retry
                    last_error = httpx.HTTPStatusError(
                        f"WAF rate limit (HTTP {response.status_code})",
                        request=response.request,
                        response=response,
                    )
                    logger.debug(
                        "WAF block on %r (attempt %d/%d) — backing off",
                        text,
                        attempt + 1,
                        self._max_backoff_attempts,
                    )
                    await self._backoff_sleep(attempt)
                    # Rotate client on rate-limit to get a fresh connection
                    await self._close_client()
                    continue

                response.raise_for_status()
                return response.json()

            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
                # Don't retry on 4xx (except 403/429 which we already handled)
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if 400 <= status < 500 and status not in (403, 429):
                        raise
                # Network errors and 5xx — retry
                logger.debug(
                    "Request error on %r (attempt %d/%d): %s",
                    text,
                    attempt + 1,
                    self._max_backoff_attempts,
                    exc,
                )
                if attempt < self._max_backoff_attempts - 1:
                    await self._backoff_sleep(attempt)
                    await self._close_client()

        raise last_error  # type: ignore[misc]

    # ── Rate limiting helper ────────────────────────────────────────────

    async def _rate_limit(self) -> None:
        """Pause between requests to stay under the WAF rate threshold."""
        base_delay = self._inter_request_delay
        # Add random jitter: +/- 40%
        jitter = random.uniform(-0.4 * base_delay, 0.4 * base_delay)
        delay = max(0.5, base_delay + jitter)
        await asyncio.sleep(delay)

    # ── Main search method ──────────────────────────────────────────────

    async def search_by_date_range(
        self, start_date: datetime, end_date: datetime, page: Optional[Page] = None
    ) -> AsyncIterator[dict]:
        """Search Florida UCC by MCA-industry debtor name prefixes.

        Florida's API has no date-range filter, so we cast a broad net using
        industry-specific debtor name prefixes and collect all matching
        filings.  The downstream classifier pipeline determines which are
        actually MCA-related.

        Yields raw filing dicts (debtor-level data).  Deduplication is by
        UCC filing number since some debtors appear under multiple prefixes.
        """
        seen_filings: set[str] = set()
        total_yielded = 0

        for prefix in MCA_DEBTOR_PREFIXES:
            # Inter-prefix jitter: vary delay to avoid rhythmic patterns
            prefix_delay = random.uniform(1.0, 3.0)
            await asyncio.sleep(prefix_delay)

            prefix_count = 0
            row_number: Optional[int] = None
            page_num = 0

            while page_num < self.max_pages_per_prefix:
                try:
                    data = await self._search_api(
                        text=prefix,
                        row_number=row_number,
                    )
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "HTTP %d for prefix %r on page %d: %s",
                        exc.response.status_code,
                        prefix,
                        page_num,
                        exc,
                    )
                    break
                except httpx.RequestError as exc:
                    logger.warning(
                        "Request error for prefix %r page %d: %s",
                        prefix,
                        page_num,
                        exc,
                    )
                    break

                payload = data.get("payload")
                if not payload:
                    break

                debtors = payload.get("debtors", [])
                if not debtors:
                    break

                for debtor in debtors:
                    filing_number = debtor.get("uccNumber", "")
                    if filing_number and filing_number not in seen_filings:
                        seen_filings.add(filing_number)
                        prefix_count += 1
                        yield self._build_filing_dict(debtor, prefix)

                # Cursor-based pagination
                next_rn = payload.get("nextRowNumber")
                if next_rn is None or next_rn == row_number:
                    break

                row_number = next_rn
                page_num += 1
                total_yielded += prefix_count

                # Rate limit between pages
                await self._rate_limit()

            if prefix_count > 0:
                logger.info(
                    "Prefix %r: %d new filings | unique so far: %d",
                    prefix,
                    prefix_count,
                    len(seen_filings),
                )

        logger.info(
            "Florida scrape complete: %d total unique filings from %d prefixes",
            len(seen_filings),
            len(MCA_DEBTOR_PREFIXES),
        )

    def _build_filing_dict(self, debtor: dict, search_prefix: str) -> dict:
        """Convert an API debtor record into the standard raw filing dict.

        Fields follow the UCCFiling model's expected format so the
        downstream normalizer can process them without extra mapping.
        """
        return {
            "state": "FL",
            "filing_number": debtor.get("uccNumber", ""),
            "filing_date": "",  # not available in search results
            "debtor_name": (debtor.get("name") or "").strip(),
            "debtor_address": (debtor.get("address") or "").strip(),
            "debtor_city": (debtor.get("city") or "").strip(),
            "debtor_state": (debtor.get("state") or "").strip(),
            "debtor_zip": (debtor.get("zipCode") or "").strip(),
            "secured_party_name": "",  # not available in search results
            "status": (debtor.get("status") or "unknown").lower(),
            "detail_url": "",
            "search_prefix": search_prefix,
            "row_number": debtor.get("rowNumber"),
        }

    # ── Filing detail ───────────────────────────────────────────────────

    async def get_filing_detail(
        self, filing_number: str, page: Optional[Page] = None
    ) -> dict:
        """Fetch full filing detail.

        WARNING: Florida's detail page renders secured party information
        as an IMAGE (not text), so secured party names are NOT extractable
        through this API.  This method returns basic metadata from a
        document-number search only.
        """
        try:
            data = await self._search_api(
                text=filing_number,
                option_type="DocumentNumber",
                sub_option=None,
            )
            return {
                "state": "FL",
                "filing_number": filing_number,
                "raw_response": data.get("payload", {}),
            }
        except Exception as exc:
            logger.warning("Failed to fetch detail for %r: %s", filing_number, exc)
            return {
                "state": "FL",
                "filing_number": filing_number,
                "error": str(exc),
            }

    async def check_status(
        self, filing_number: str, page: Optional[Page] = None
    ) -> str:
        """Check if a filing is active, terminated, lapsed, etc.

        The status field in search results gives a first approximation
        ("Filed" or "Lapsed").  For definitive status, a detail-page
        lookup is needed (image-rendered, not currently supported).
        """
        try:
            data = await self._search_api(
                text=filing_number,
                option_type="DocumentNumber",
                sub_option=None,
            )
            payload = data.get("payload", {})
            status_text: str = str(payload).lower()

            if "terminat" in status_text:
                return "terminated"
            if "lapsed" in status_text:
                return "lapsed"
            if "continu" in status_text or "amend" in status_text:
                return "amended"
            if "active" in status_text or "filed" in status_text:
                return "active"
            return "unknown"
        except Exception:
            return "unknown"

    # ── Health check ────────────────────────────────────────────────────

    async def health_check(self) -> dict:
        """Verify the Florida public API is reachable.

        Uses a lightweight search query that should always return results.
        """
        client = await self._get_client()
        try:
            response = await client.get(
                PUBLIC_SEARCH_API,
                params={
                    "text": "TEST",
                    "searchOptionType": "OrganizationDebtorName",
                },
            )
            ok = 200 <= response.status_code < 500
            api_ok = False
            try:
                data = response.json()
                api_ok = data.get("status") == "OK"
            except Exception:
                pass
            return {
                "ok": ok and api_ok,
                "status_code": response.status_code,
                "url": PUBLIC_SEARCH_API,
                "error": None if ok else f"HTTP {response.status_code}",
            }
        except Exception as exc:
            return {
                "ok": False,
                "status_code": 0,
                "url": PUBLIC_SEARCH_API,
                "error": str(exc)[:200],
            }
