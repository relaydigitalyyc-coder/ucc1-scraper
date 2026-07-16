"""Base scraper class that all state-specific UCC scrapers inherit from."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import AsyncIterator, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page


class BaseStateScraper(ABC):
    """Abstract base for a state UCC filing scraper.

    Each state gets a subclass that implements the scraping logic for that
    state's specific search portal (whether it's an API, ASP.NET app, SPA, etc.).
    """

    state: str  # Two-letter state code, set by subclass
    state_name: str  # Full state name
    base_url: str  # State UCC search portal URL

    # Rate limiting
    requests_per_second: float = 1.0
    max_retries: int = 3
    retry_delay: float = 5.0

    def __init__(self, headless: bool = True, proxy: Optional[str] = None):
        self.headless = headless
        self.proxy = proxy
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    # ── Browser lifecycle ──────────────────────────────────────────

    async def start(self):
        """Launch browser and create context."""
        self._playwright = await async_playwright().start()
        launch_args = {"headless": self.headless}
        if self.proxy:
            launch_args["proxy"] = {"server": self.proxy}
        self._browser = await self._playwright.chromium.launch(**launch_args)
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
        )

    async def stop(self):
        """Clean up browser resources."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def new_page(self) -> Page:
        """Create a new page with stealth settings."""
        page = await self._context.new_page()
        # Basic stealth: hide automation flags
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)
        return page

    # ── Abstract interface ─────────────────────────────────────────

    @abstractmethod
    async def search_by_date_range(
        self, start_date: datetime, end_date: datetime, page: Optional[Page] = None
    ) -> AsyncIterator[dict]:
        """Search for UCC filings filed between start_date and end_date.

        Yields raw filing dicts (before normalization). Each dict should
        contain all fields visible in the search results for that state.
        """
        ...

    @abstractmethod
    async def get_filing_detail(self, filing_number: str, page: Optional[Page] = None) -> dict:
        """Fetch full detail for a single filing by its filing number.

        Returns a raw dict with all available fields.
        """
        ...

    @abstractmethod
    async def check_status(self, filing_number: str, page: Optional[Page] = None) -> str:
        """Check if a filing is active, terminated, lapsed, etc."""
        ...

    # ── Normalization (can be overridden per state) ─────────────────

    async def normalize_filing(self, raw: dict) -> dict:
        """Convert raw state-specific filing dict into standard UCCFiling fields.

        Override this if the state has unusual data formats.
        """
        return raw

    # ── Health check / connectivity ──────────────────────────────────

    async def health_check(self) -> dict:
        """Verify the state portal is reachable without doing a full scrape.

        Returns {"ok": bool, "status_code": int, "url": str, "error": str | None}
        """
        page = await self.new_page()
        try:
            response = await page.goto(self.base_url, timeout=30000, wait_until="domcontentloaded")
            status = response.status if response else 0
            return {
                "ok": status >= 200 and status < 500,
                "status_code": status,
                "url": self.base_url,
                "error": None if status < 500 else f"HTTP {status}",
            }
        except Exception as e:
            return {
                "ok": False,
                "status_code": 0,
                "url": self.base_url,
                "error": str(e)[:200],
            }
        finally:
            await page.close()

    # ── Helpers ─────────────────────────────────────────────────────

    async def _safe_goto(self, page: Page, url: str, timeout: int = 30000):
        """Navigate to URL with retry logic."""
        from tenacity import retry, stop_after_attempt, wait_exponential

        @retry(stop=stop_after_attempt(self.max_retries), wait=wait_exponential(multiplier=1))
        async def _goto():
            response = await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            if response and response.status >= 500:
                raise Exception(f"Server error: {response.status}")
            return response

        return await _goto()

    async def _safe_click(self, page: Page, selector: str, timeout: int = 10000):
        """Click an element with retry."""
        await page.wait_for_selector(selector, timeout=timeout, state="visible")
        await page.click(selector)

    async def _safe_fill(self, page: Page, selector: str, value: str, timeout: int = 10000):
        """Fill an input field with retry."""
        await page.wait_for_selector(selector, timeout=timeout, state="visible")
        await page.fill(selector, value)

    async def _wait_for_navigation(self, page: Page, timeout: int = 30000):
        """Wait for page navigation to complete."""
        await page.wait_for_load_state("networkidle", timeout=timeout)
