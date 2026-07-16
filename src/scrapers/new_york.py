"""New York UCC Scraper -- NY Department of State.

IMPORTANT: NY uses F5 TSPD (JavaScript bot detection) on apps.dos.ny.gov.
This blocks ALL headless browsers including Playwright with stealth patches.
To use this scraper, you MUST run with headless=False OR through a residential
proxy service that can solve the TSPD challenge.

Portal: https://apps.dos.ny.gov/ucc-search/
Type: Oracle PL/SQL web app behind F5 TSPD JS challenge
Alternative legacy URL: https://appext20.dos.ny.gov/pls/ucc_public/ (DECOMMISSIONED - 404)

Known search types (from legacy Oracle portal and NY regulations):
- Debtor Name search (by individual or organization name)
- Filing Number search (by document/filing number)
- Secured Party Name search
- Filing Date range search (by date range or year)

Fallback data source: NY Open Data (data.ny.gov) corporation database
- "Daily Corporation and Other Entity Filing Data" (k4vb-judh)
- "Corporations and Other Entities: All Filings - Name Status History" (ekwr-p59j)
- "Active Corporations: Beginning 1800" (n9v6-gdp6)

These provide debtor-level entity data but NOT UCC lien/secured party information.

Architecture:
    This scraper supports two modes:
    1. PLAYWRIGHT mode (default): Uses a real browser to pass the TSPD challenge
       and interact with the Oracle web app search form.
    2. SODA mode (fallback): Uses the NY Open Data SODA API to fetch corporation
       filings as a proxy for debtor data. No secured party info.
"""

from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Optional

import httpx
from playwright.async_api import Page

from scrapers.base import BaseStateScraper
from scrapers.registry import register_scraper

# ── NY portal URLs ───────────────────────────────────────────────────────

UCC_SEARCH_URL = "https://apps.dos.ny.gov/ucc-search/"
UCC_PUBLIC_URL = "https://apps.dos.ny.gov/ucc-public/"

# SODA API endpoints for NY Open Data fallback
SODA_BASE = "https://data.ny.gov/resource"
SODA_DAILY_FILINGS = f"{SODA_BASE}/k4vb-judh.json"  # Daily corporation filings
SODA_ACTIVE_CORPS = f"{SODA_BASE}/n9v6-gdp6.json"  # Active corporations

# Known selectors for the Oracle PL/SQL web app (behind TSPD challenge).
# These are educated guesses based on the old appext20 portal structure and
# typical Oracle web app patterns. They may need adjustment once access is obtained.
SELECTORS = {
    "search_type_debtor": 'input[type="radio"][value*="debtor" i], '
    'input[type="radio"][value*="name" i], '
    'label:has-text("Debtor"), label:has-text("Name")',
    "search_type_filing_number": 'input[type="radio"][value*="filing" i], '
    'input[type="radio"][value*="number" i], '
    'label:has-text("Filing Number"), label:has-text("Document Number")',
    "search_type_secured_party": 'input[type="radio"][value*="secured" i], '
    'input[type="radio"][value*="party" i], '
    'label:has-text("Secured Party")',
    "search_type_date": 'input[type="radio"][value*="date" i], '
    'label:has-text("Filing Date"), label:has-text("Date Range")',
    "debtor_name_input": 'input[name*="debtor" i], input[id*="debtor" i], '
    'input[name*="name" i], input[id*="name" i]',
    "filing_number_input": 'input[name*="filing" i], input[id*="filing" i], '
    'input[name*="number" i], input[id*="number" i]',
    "secured_party_input": 'input[name*="secured" i], input[id*="secured" i], '
    'input[name*="party" i], input[id*="party" i]',
    "date_from_input": 'input[name*="from" i], input[id*="from" i], '
    'input[name*="start" i], input[id*="start" i], '
    'input[type="date"]:first-of-type',
    "date_to_input": 'input[name*="to" i], input[id*="to" i], '
    'input[name*="end" i], input[id*="end" i], '
    'input[type="date"]:nth-of-type(2)',
    "search_button": 'input[type="submit"][value*="Search"], '
    'button[type="submit"]:has-text("Search"), '
    'button:has-text("Search"), '
    'input[type="submit"]',
    "results_table": "table:has(tr:has(td))",
    "result_rows": "tr:has(td)",
    "next_page": 'a:has-text("Next"), a:has-text(">"), '
    'a[href*="next" i], a[href*="Next" i]',
    "detail_link": "a",
}


@register_scraper("NY")
class NewYorkScraper(BaseStateScraper):
    """NY UCC filing scraper.

    Requires a real browser (headless=False) or residential proxy to pass
    the F5 TSPD JS challenge. Falls back to NY Open Data corporation database
    when the portal is inaccessible.
    """

    state = "NY"
    state_name = "New York"
    base_url = UCC_SEARCH_URL

    # NY is #1 MCA market but their server is fragile and has JS challenges
    requests_per_second: float = 0.5
    max_retries: int = 3
    retry_delay: float = 10.0

    def __init__(
        self,
        headless: bool = True,
        proxy: Optional[str] = None,
        use_soda_fallback: bool = False,
    ):
        super().__init__(headless=headless, proxy=proxy)
        self.use_soda_fallback = use_soda_fallback
        self._tspd_passed = False

    # ── Browser lifecycle override ──────────────────────────────────────

    async def start(self):
        """Launch browser with anti-detection arguments for F5 TSPD."""
        await super().start()

        # Add additional stealth to the default context
        if self._context:
            await self._context.add_init_script("""
                // Remove webdriver flag (most important for TSPD)
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                // Fake plugins
                Object.defineProperty(navigator, 'plugins', {
                    get: () => {
                        const plugins = [
                            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
                            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1 },
                            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 1 },
                        ];
                        plugins.item = (i) => plugins[i] || null;
                        plugins.namedItem = (name) => plugins.find(p => p.name === name) || null;
                        return plugins;
                    },
                });
                // Fake chrome runtime
                window.chrome = { runtime: {}, app: {} };
                // Fake hardware concurrency
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            """)

    # ── Health check override ───────────────────────────────────────────

    async def health_check(self) -> dict:
        """Check if the NY portal is reachable.

        Tries the main portal first, then falls back to SODA API.
        Can be called without starting the browser (checks SODA only)."""
        result: dict[str, Any] = {
            "ok": False,
            "status_code": 0,
            "url": self.base_url,
            "error": None,
        }

        # Try main portal (only if browser is started)
        if self._context is not None:
            try:
                page = await self.new_page()
                try:
                    response = await page.goto(
                        self.base_url, timeout=30000, wait_until="domcontentloaded"
                    )
                    result["status_code"] = response.status if response else 0

                    await page.wait_for_timeout(8000)
                    body_text = (await page.text_content("body")) or ""
                    content = await page.content()

                    if "TSPD" in content or len(body_text.strip()) < 50:
                        result["ok"] = False
                        result["error"] = (
                            "F5 TSPD challenge detected — real browser required"
                        )
                        self._tspd_passed = False
                    else:
                        result["ok"] = True
                        self._tspd_passed = True
                finally:
                    await page.close()
            except Exception as e:
                result["ok"] = False
                result["error"] = f"Browser error: {str(e)[:200]}"
        else:
            result["error"] = "Browser not started — checking SODA only"

        # Check SODA fallback
        soda_ok = False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                soda_resp = await client.get(
                    f"{SODA_DAILY_FILINGS}?$limit=1",
                    headers={"Accept": "application/json"},
                )
                soda_ok = soda_resp.status_code == 200
        except Exception:
            soda_ok = False

        result["soda_fallback_available"] = soda_ok
        if not result["ok"] and soda_ok:
            result["ok"] = True
            result["note"] = "Using SODA API fallback (corporation data, not UCC)"

        return result

    # ── Search by date range ────────────────────────────────────────────

    async def search_by_date_range(
        self, start_date: datetime, end_date: datetime, page: Optional[Page] = None
    ) -> AsyncIterator[dict]:
        """Search for NY UCC filings between start_date and end_date.

        Uses SODA API fallback when the portal is inaccessible (default).
        Switch to Playwright mode by setting use_soda_fallback=False and
        ensuring headless=False or using a residential proxy.
        """
        if self.use_soda_fallback or not self._tspd_passed:
            async for result in self._search_soda_date_range(start_date, end_date):
                yield result
            return

        # Playwright mode (needs real browser to pass TSPD)
        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._safe_goto(page, self.base_url)
            await page.wait_for_timeout(6000)

            # Check if TSPD challenge passed
            body_text = (await page.text_content("body")) or ""
            if len(body_text.strip()) < 50:
                # TSPD challenge failed - switch to SODA automatically
                if should_close:
                    await page.close()
                async for result in self._search_soda_date_range(
                    start_date, end_date
                ):
                    yield result
                return

            # Select "Filing Date" search type
            await self._select_search_type(page, "date")

            # Fill date range
            await self._fill_date_range(page, start_date, end_date)

            # Submit search
            await self._click_search(page)

            # Parse results
            async for filing in self._parse_search_results(page):
                yield filing

        finally:
            if should_close:
                await page.close()

    # ── Search by debtor name ──────────────────────────────────────────

    async def search_by_debtor_name(
        self, name: str, page: Optional[Page] = None
    ) -> AsyncIterator[dict]:
        """Search for UCC filings by debtor name.

        This is the primary search method for MCA lead generation."""
        if self.use_soda_fallback or not self._tspd_passed:
            async for result in self._search_soda_name(name):
                yield result
            return

        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._safe_goto(page, self.base_url)
            await page.wait_for_timeout(6000)

            body_text = (await page.text_content("body")) or ""
            if len(body_text.strip()) < 50:
                if should_close:
                    await page.close()
                async for result in self._search_soda_name(name):
                    yield result
                return

            await self._select_search_type(page, "debtor")
            await self._fill_debtor_name(page, name)
            await self._click_search(page)

            async for filing in self._parse_search_results(page):
                yield filing

        finally:
            if should_close:
                await page.close()

    # ── Get filing detail ───────────────────────────────────────────────

    async def get_filing_detail(
        self, filing_number: str, page: Optional[Page] = None
    ) -> dict:
        """Fetch full detail for a single NY UCC filing."""
        if self.use_soda_fallback or not self._tspd_passed:
            return await self._get_soda_detail(filing_number)

        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._safe_goto(page, self.base_url)
            await page.wait_for_timeout(4000)

            await self._select_search_type(page, "filing_number")
            await self._fill_filing_number(page, filing_number)
            await self._click_search(page)
            await page.wait_for_timeout(4000)

            return await self._extract_detail(page)
        finally:
            if should_close:
                await page.close()

    # ── Check filing status ────────────────────────────────────────────

    async def check_status(
        self, filing_number: str, page: Optional[Page] = None
    ) -> str:
        """Check if a NY UCC filing is active, terminated, lapsed, etc."""
        detail = await self.get_filing_detail(filing_number, page)
        status_text = (
            (detail.get("status", "") or "")
            + " "
            + (detail.get("_raw_body_text", "") or "")
        ).lower()

        if "terminat" in status_text:
            return "terminated"
        elif "lapsed" in status_text:
            return "lapsed"
        elif "continu" in status_text:
            return "continued"
        elif "amend" in status_text:
            return "amended"
        elif "active" in status_text:
            return "active"
        return "unknown"

    # ── Private: Playwright form interaction ────────────────────────────

    async def _select_search_type(self, page: Page, search_type: str):
        """Select the search type radio button or tab."""
        selector_map = {
            "debtor": SELECTORS["search_type_debtor"],
            "filing_number": SELECTORS["search_type_filing_number"],
            "secured_party": SELECTORS["search_type_secured_party"],
            "date": SELECTORS["search_type_date"],
        }
        selector = selector_map.get(search_type, "")
        if not selector:
            return

        radio = page.locator(selector).first
        if await radio.count() > 0:
            try:
                await radio.click()
                await page.wait_for_timeout(800)
            except Exception:
                pass

    async def _fill_debtor_name(self, page: Page, name: str):
        """Fill the debtor name input field."""
        inp = page.locator(SELECTORS["debtor_name_input"]).first
        if await inp.count() > 0:
            await inp.fill(name)
            await page.wait_for_timeout(300)

    async def _fill_filing_number(self, page: Page, filing_number: str):
        """Fill the filing number input field."""
        inp = page.locator(SELECTORS["filing_number_input"]).first
        if await inp.count() > 0:
            await inp.fill(filing_number)
            await page.wait_for_timeout(300)

    async def _fill_date_range(
        self, page: Page, start_date: datetime, end_date: datetime
    ):
        """Fill date range inputs."""
        date_format = "%m/%d/%Y"

        # Try date-type inputs first
        date_inputs = page.locator('input[type="date"]')
        count = await date_inputs.count()
        if count >= 2:
            await date_inputs.nth(0).fill(start_date.strftime("%Y-%m-%d"))
            await date_inputs.nth(1).fill(end_date.strftime("%Y-%m-%d"))
            return

        # Fall back to text inputs by name pattern
        from_inputs = [
            SELECTORS["date_from_input"],
            'input[name*="fromdate" i]',
            'input[id*="fromdate" i]',
        ]
        for sel in from_inputs:
            inp = page.locator(sel).first
            if await inp.count() > 0:
                await inp.fill(start_date.strftime(date_format))
                break

        to_inputs = [
            SELECTORS["date_to_input"],
            'input[name*="todate" i]',
            'input[id*="todate" i]',
        ]
        for sel in to_inputs:
            inp = page.locator(sel).first
            if await inp.count() > 0:
                await inp.fill(end_date.strftime(date_format))
                break

    async def _click_search(self, page: Page):
        """Click the search button."""
        btn = page.locator(SELECTORS["search_button"]).first
        if await btn.count() > 0:
            await btn.click()
            await page.wait_for_timeout(6000)

    async def _parse_search_results(self, page: Page) -> AsyncIterator[dict]:
        """Parse the Oracle-generated search results table."""
        page_num = 0
        max_pages = 50  # Safety limit

        while page_num < max_pages:
            await page.wait_for_timeout(2000)

            # Find the results table
            table = page.locator(SELECTORS["results_table"]).first
            if await table.count() == 0:
                break

            rows = table.locator(SELECTORS["result_rows"])
            row_count = await rows.count()

            for i in range(row_count):
                row = rows.nth(i)
                cells = row.locator("td")
                cell_count = await cells.count()

                if cell_count < 2:
                    continue

                cell_texts: list[str] = []
                for j in range(cell_count):
                    ct = (await cells.nth(j).text_content()) or ""
                    cell_texts.append(ct.strip())

                # Extract detail link
                detail_link = row.locator(SELECTORS["detail_link"]).first
                detail_url = ""
                if await detail_link.count() > 0:
                    detail_url = (await detail_link.get_attribute("href")) or ""
                    if detail_url and not detail_url.startswith("http"):
                        from urllib.parse import urljoin
                        detail_url = urljoin(self.base_url, detail_url)

                # Map columns (typical NY Oracle portal order):
                # Filing#, FileDate, Debtor, SecuredParty, Status/Type, Pages
                yield {
                    "state": "NY",
                    "filing_number": cell_texts[0] if cell_texts else "",
                    "filing_date": cell_texts[1] if len(cell_texts) > 1 else "",
                    "debtor_name": cell_texts[2] if len(cell_texts) > 2 else "",
                    "secured_party_name": cell_texts[3] if len(cell_texts) > 3 else "",
                    "status": cell_texts[4] if len(cell_texts) > 4 else "unknown",
                    "filing_type": cell_texts[5] if len(cell_texts) > 5 else "",
                    "detail_url": detail_url,
                    "raw_cells": cell_texts,
                    "data_source": "ucc_portal",
                }

            # Pagination
            next_links = page.locator(SELECTORS["next_page"])
            if await next_links.count() > 0:
                next_btn = next_links.first
                if await next_btn.is_enabled():
                    await next_btn.click()
                    await page.wait_for_timeout(5000)
                    page_num += 1
                else:
                    break
            else:
                break

    async def _extract_detail(self, page: Page) -> dict:
        """Extract filing detail fields from the detail page."""
        fields: dict[str, str] = {}
        rows = await page.locator("tr:has(td), tr:has(th)").all()
        for row in rows:
            cells = await row.locator("td, th").all()
            if len(cells) >= 2:
                key = (
                    ((await cells[0].text_content()) or "")
                    .strip()
                    .rstrip(":")
                    .lower()
                    .replace(" ", "_")
                )
                val = ((await cells[1].text_content()) or "").strip()
                if key:
                    fields[key] = val

        body_text = (await page.text_content("body")) or ""
        fields["_raw_body_text"] = body_text[:5000]
        fields["state"] = "NY"
        fields["data_source"] = "ucc_portal"
        return fields

    # ── Private: SODA API fallback ──────────────────────────────────────

    async def _soda_client(self) -> httpx.AsyncClient:
        """Create an httpx client for SODA API calls."""
        return httpx.AsyncClient(
            timeout=30,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        )

    async def _search_soda_date_range(
        self, start_date: datetime, end_date: datetime
    ) -> AsyncIterator[dict]:
        """Search NY Open Data corporation filings by date range.

        Uses the SODA API with date filtering. This provides corporation
        filing data as a proxy — NOT UCC lien data. Secured party information
        is not available through this source.
        """
        from urllib.parse import urlencode

        # SODA API uses ISO date format
        date_from = start_date.strftime("%Y-%m-%dT00:00:00.000")
        date_to = end_date.strftime("%Y-%m-%dT23:59:59.000")

        # Query the daily filings dataset
        base_url = SODA_DAILY_FILINGS
        offset = 0
        limit = 1000
        max_results = 10000

        async with await self._soda_client() as client:
            while offset < max_results:
                params = {
                    "$where": f"filing_date between '{date_from}' and '{date_to}'",
                    "$limit": str(limit),
                    "$offset": str(offset),
                    "$order": "filing_date DESC",
                }
                url = f"{base_url}?{urlencode(params)}"

                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        break

                    rows = resp.json()
                    if not rows:
                        break

                    for row in rows:
                        yield self._normalize_soda_row(row)

                    if len(rows) < limit:
                        break
                    offset += limit

                except httpx.RequestError:
                    break

    async def _search_soda_name(self, name: str) -> AsyncIterator[dict]:
        """Search NY Open Data corporation filings by entity name."""
        from urllib.parse import urlencode

        async with await self._soda_client() as client:
            # SODA uses SoQL - search by corp_name
            base_url = SODA_DAILY_FILINGS
            offset = 0
            limit = 200
            max_results = 2000

            while offset < max_results:
                params = {
                    "$where": f"upper(corp_name) like '%{name.upper()}%'",
                    "$limit": str(limit),
                    "$offset": str(offset),
                    "$order": "filing_date DESC",
                }
                url = f"{base_url}?{urlencode(params)}"

                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        break

                    rows = resp.json()
                    if not rows:
                        break

                    for row in rows:
                        yield self._normalize_soda_row(row)

                    if len(rows) < limit:
                        break
                    offset += limit

                except httpx.RequestError:
                    break

    async def _get_soda_detail(self, filing_number: str) -> dict:
        """Get detail for a single SODA record by film_num."""
        from urllib.parse import urlencode

        async with await self._soda_client() as client:
            params = {
                "$where": f"film_num = '{filing_number}'",
                "$limit": "1",
            }
            url = f"{SODA_DAILY_FILINGS}?{urlencode(params)}"

            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    rows = resp.json()
                    if rows:
                        return self._normalize_soda_row(rows[0])
            except httpx.RequestError:
                pass

        return {
            "filing_number": filing_number,
            "error": "not found",
            "data_source": "soda_api",
            "state": "NY",
        }

    def _normalize_soda_row(self, row: dict) -> dict:
        """Convert a SODA API row into the standard filing dict format."""
        filing_date = row.get("filing_date", "")
        if filing_date and "T" in str(filing_date):
            filing_date = str(filing_date)[:10]

        return {
            "state": "NY",
            "filing_number": row.get("film_num", ""),
            "dos_id": row.get("dos_id", ""),
            "filing_date": filing_date,
            "approved_date": str(row.get("approved_date", ""))[:10]
            if row.get("approved_date")
            else "",
            "effective_date": str(row.get("eff_date", ""))[:10]
            if row.get("eff_date")
            else "",
            "debtor_name": row.get("corp_name", ""),
            "fictitious_name": row.get("fictitious_name", ""),
            "previous_name": row.get("pre_corp_name", ""),
            "entity_type": row.get("entity_type", ""),
            "filing_type": row.get("filing_type", ""),
            "jurisdiction": row.get("for_juris", ""),
            "county": row.get("cnty_prin_ofc", ""),
            "law": row.get("law", ""),
            # Secured party data is NOT available from SODA
            "secured_party_name": "",
            "status": "active",  # SODA only shows processed filings
            "detail_url": f"https://apps.dos.ny.gov/publicInquiry/PublicInquiry"
            if not row.get("film_num")
            else "",
            "collateral_description": "",
            # Filer info
            "filer_name": row.get("filer_name", ""),
            "filer_address": row.get("filer_addr1", ""),
            "filer_city": row.get("filer_city", ""),
            "filer_state": row.get("filer_state", ""),
            "filer_zip": row.get("filer_zip5", ""),
            # Service of process
            "sop_name": row.get("sop_name", ""),
            "sop_address": row.get("sop_addr1", ""),
            "sop_city": row.get("sop_city", ""),
            "sop_state": row.get("sop_state", ""),
            "sop_zip": row.get("sop_zip5", ""),
            "data_source": "soda_api",
            "is_ucc_filing": False,
            "raw_record": row,
        }
