"""Delaware UCC Scraper -- DE Division of Corporations.

IMPORTANT: Delaware has NO public UCC filing search. All UCC searches require a
certified "Authorized Searcher" account (see https://corp.delaware.gov/uccsearch/).

The only publicly accessible data is the ICIS Business Entity Search at:
https://icis.corp.delaware.gov/Ecorp/EntitySearch/NameSearch.aspx

This scraper uses ICIS entity search as a best-effort proxy. Entity registration
data is mapped to the filing dict format. UCC-specific fields (secured_party_name,
collateral_description) are always empty because UCC data is not public in Delaware.

Search strategy:
1. Search by year/month patterns in entity names (e.g. '2026 LLC', 'JULY')
2. Visit entity detail pages to get incorporation dates
3. Filter by requested date range

Note: ICIS is ASP.NET WebForms. go_back() corrupts view state, so each entity
detail visit requires a fresh search + click cycle.
"""

from datetime import datetime, timedelta
from typing import AsyncIterator, Optional

from playwright.async_api import Page

from scrapers.base import BaseStateScraper
from scrapers.registry import register_scraper

# ── ICIS selectors ────────────────────────────────────────────────────

SEARCH_URL = "https://icis.corp.delaware.gov/Ecorp/EntitySearch/NameSearch.aspx"
ENTITY_NAME_INPUT = "#ctl00_ContentPlaceHolder1_frmEntityName"
FILE_NUMBER_INPUT = "#ctl00_ContentPlaceHolder1_frmFileNumber"
SEARCH_BUTTON = "#ctl00_ContentPlaceHolder1_btnSubmit"

RESULT_FILE_NUMBERS = 'span[id*="rptSearchResults"][id*="lblFileNumber"]'
RESULT_ENTITY_LINKS = 'a[id*="rptSearchResults"][id*="lnkbtnEntityName"]'

DETAIL_INC_DATE = "#ctl00_ContentPlaceHolder1_lblIncDate"
DETAIL_ENTITY_NAME = "#ctl00_ContentPlaceHolder1_lblEntityName"
DETAIL_ENTITY_KIND = "#ctl00_ContentPlaceHolder1_lblEntityKind"
DETAIL_ENTITY_TYPE = "#ctl00_ContentPlaceHolder1_lblEntityType"
DETAIL_RESIDENCY = "#ctl00_ContentPlaceHolder1_lblResidency"
DETAIL_AGENT_NAME = "#ctl00_ContentPlaceHolder1_lblAgentName"
DETAIL_AGENT_ADDR = "#ctl00_ContentPlaceHolder1_lblAgentAddress1"
DETAIL_AGENT_CITY = "#ctl00_ContentPlaceHolder1_lblAgentCity"
DETAIL_AGENT_COUNTY = "#ctl00_ContentPlaceHolder1_lblAgentCounty"
DETAIL_AGENT_STATE = "#ctl00_ContentPlaceHolder1_lblAgentState"
DETAIL_AGENT_ZIP = "#ctl00_ContentPlaceHolder1_lblAgentPostalCode"
DETAIL_AGENT_PHONE = "#ctl00_ContentPlaceHolder1_lblAgentPhone"

# Limits (per-search and total)
# Tunable: number of entity detail pages to visit per search term / total.
# Each detail visit takes ~10s (search + click). Increase for broader
# coverage but longer run time. 30 total visits = ~5 min for all search terms.
MAX_VISITS_PER_TERM = 10
MAX_TOTAL_VISITS = 30


def _generate_search_terms(start_date: datetime, end_date: datetime) -> list[str]:
    """Generate entity name search terms for finding recently formed entities.

    Returns prioritized, deduplicated list of search terms.
    """
    terms: list[str] = []

    # Year + suffix combinations (many entities include formation year)
    years_seen: set[int] = set()
    current = start_date
    while current <= end_date:
        years_seen.add(current.year)
        current = current + timedelta(days=30)

    for year in sorted(years_seen):
        for suffix in ["LLC", "INC", "LP", "CORP"]:
            terms.append(f"{year} {suffix}")
        terms.append(str(year))

    # Month names
    month_names = [
        "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
        "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
    ]
    month_abbrs = [
        "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
        "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
    ]
    months_in_range: set[int] = set()
    current = start_date
    while current <= end_date:
        months_in_range.add(current.month)
        current = current + timedelta(days=1)
    for m in months_in_range:
        terms.append(month_names[m - 1])
        terms.append(month_abbrs[m - 1])

    seen: set[str] = set()
    unique: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


async def _extract_text(page: Page, selector: str) -> str:
    """Safely extract text from a single element."""
    el = page.locator(selector)
    if await el.count() > 0:
        return ((await el.first.text_content()) or "").strip()
    return ""


def _parse_date(date_str: str) -> Optional[datetime]:
    """Parse MM/DD/YYYY format."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y")
    except ValueError:
        return None


async def _scrape_entity_detail(page: Page) -> dict:
    """Extract all fields from the current entity detail page."""
    return {
        "incorporation_date": await _extract_text(page, DETAIL_INC_DATE),
        "entity_name": await _extract_text(page, DETAIL_ENTITY_NAME),
        "entity_kind": await _extract_text(page, DETAIL_ENTITY_KIND),
        "entity_type": await _extract_text(page, DETAIL_ENTITY_TYPE),
        "residency": await _extract_text(page, DETAIL_RESIDENCY),
        "registered_agent": await _extract_text(page, DETAIL_AGENT_NAME),
        "registered_agent_address": await _extract_text(page, DETAIL_AGENT_ADDR),
        "registered_agent_city": await _extract_text(page, DETAIL_AGENT_CITY),
        "registered_agent_county": await _extract_text(page, DETAIL_AGENT_COUNTY),
        "registered_agent_state": await _extract_text(page, DETAIL_AGENT_STATE),
        "registered_agent_zip": await _extract_text(page, DETAIL_AGENT_ZIP),
        "registered_agent_phone": await _extract_text(page, DETAIL_AGENT_PHONE),
        "detail_url": page.url,
    }


@register_scraper("DE")
class DelawareScraper(BaseStateScraper):
    state = "DE"
    state_name = "Delaware"
    base_url = SEARCH_URL

    async def search_by_date_range(
        self, start_date: datetime, end_date: datetime, page: Optional[Page] = None
    ) -> AsyncIterator[dict]:
        """Search for Delaware entities formed between start_date and end_date.

        Uses ICIS entity name search as a proxy (no public UCC search exists).
        Note: secured_party_name and collateral_description are always empty.
        """
        should_close = page is None
        page = page or await self.new_page()

        try:
            search_terms = _generate_search_terms(start_date, end_date)
            seen_numbers: set[str] = set()
            total_visits = 0

            for term in search_terms:
                if total_visits >= MAX_TOTAL_VISITS:
                    break

                # Execute name search
                await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1500)
                await page.fill(ENTITY_NAME_INPUT, term)
                await page.click(SEARCH_BUTTON)
                await page.wait_for_timeout(4000)

                body = (await page.text_content("body")) or ""
                if "No Records Found" in body:
                    continue

                # Collect file numbers and entity names from results
                fn_els = page.locator(RESULT_FILE_NUMBERS)
                link_els = page.locator(RESULT_ENTITY_LINKS)
                num = min(await fn_els.count(), await link_els.count())

                candidates: list[tuple[str, str]] = []
                for i in range(num):
                    fn = (await fn_els.nth(i).text_content() or "").strip()
                    en = (await link_els.nth(i).text_content() or "").strip()
                    if fn and fn not in seen_numbers:
                        candidates.append((fn, en))

                # Visit detail pages for candidates
                term_visits = 0
                for file_number, entity_name in candidates:
                    if total_visits >= MAX_TOTAL_VISITS:
                        break
                    if term_visits >= MAX_VISITS_PER_TERM:
                        break

                    seen_numbers.add(file_number)
                    total_visits += 1
                    term_visits += 1

                    # Fresh search: file number lookup
                    detail = await self._lookup_by_file_number(page, file_number)

                    if detail is None:
                        continue

                    inc_date = _parse_date(detail.get("incorporation_date", ""))
                    if inc_date is None:
                        continue

                    if not (start_date <= inc_date <= end_date):
                        continue

                    yield {
                        "state": "DE",
                        "filing_number": file_number,
                        "filing_date": inc_date.strftime("%m/%d/%Y"),
                        "debtor_name": entity_name,
                        "secured_party_name": "",
                        "status": detail.get("residency", "unknown"),
                        "detail_url": detail.get("detail_url", ""),
                        "collateral_description": "",
                        "entity_kind": detail.get("entity_kind", ""),
                        "entity_type": detail.get("entity_type", ""),
                        "registered_agent": detail.get("registered_agent", ""),
                        "registered_agent_address": detail.get(
                            "registered_agent_address", ""
                        ),
                        "registered_agent_city": detail.get(
                            "registered_agent_city", ""
                        ),
                        "registered_agent_state": detail.get(
                            "registered_agent_state", ""
                        ),
                        "registered_agent_zip": detail.get(
                            "registered_agent_zip", ""
                        ),
                        "data_source": "icis_entity_search",
                        "is_ucc_filing": False,
                    }

        finally:
            if should_close:
                await page.close()

    async def _lookup_by_file_number(
        self, page: Page, file_number: str
    ) -> Optional[dict]:
        """Search by file number and click through to the entity detail page.

        ICIS returns search results for file-number queries, so we click
        the first result to get to the entity detail page."""
        try:
            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
            await page.fill(FILE_NUMBER_INPUT, file_number)
            await page.click(SEARCH_BUTTON)
            await page.wait_for_timeout(4000)

            body = (await page.text_content("body")) or ""

            # File number search can land directly on Entity Details or search results
            if "Entity Details" in body:
                return await _scrape_entity_detail(page)

            # Click first result link
            link = page.locator(RESULT_ENTITY_LINKS).first
            if await link.count() > 0:
                await link.click()
                await page.wait_for_timeout(4000)

                body = (await page.text_content("body")) or ""
                if "Entity Details" in body:
                    return await _scrape_entity_detail(page)

            return None

        except Exception:
            return None

    async def get_filing_detail(
        self, filing_number: str, page: Optional[Page] = None
    ) -> dict:
        """Fetch entity detail for a Delaware file number."""
        should_close = page is None
        page = page or await self.new_page()

        try:
            detail = await self._lookup_by_file_number(page, filing_number)
            if detail:
                detail["file_number"] = filing_number
                return detail
            return {"file_number": filing_number, "error": "Entity not found"}

        finally:
            if should_close:
                await page.close()

    async def check_status(
        self, filing_number: str, page: Optional[Page] = None
    ) -> str:
        """Check entity status. DE does not expose status publicly without
        a paid lookup ($10-$20). Returns residency as best proxy."""
        detail = await self.get_filing_detail(filing_number, page)
        residency = (detail.get("residency", "")).lower()
        entity_state = (detail.get("registered_agent_state", "")).upper()

        if residency == "domestic" and entity_state in ("DE", "DELAWARE"):
            return "active"
        elif residency == "foreign":
            return "foreign"
        return "unknown"

    async def search_by_name(
        self,
        entity_name: str,
        page: Optional[Page] = None,
        max_results: int = 20,
    ) -> AsyncIterator[dict]:
        """Search for Delaware entities by name (partial match).

        Returns entity summary dicts. Each dict contains keys:
        state, filing_number, filing_date, debtor_name, secured_party_name,
        status, detail_url, collateral_description.
        """
        should_close = page is None
        page = page or await self.new_page()

        try:
            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
            await page.fill(ENTITY_NAME_INPUT, entity_name)
            await page.click(SEARCH_BUTTON)
            await page.wait_for_timeout(4000)

            # Collect all results at once (ASP.NET page state is fragile)
            fn_els = page.locator(RESULT_FILE_NUMBERS)
            link_els = page.locator(RESULT_ENTITY_LINKS)
            num = min(await fn_els.count(), await link_els.count(), max_results)

            results: list[dict] = []
            for i in range(num):
                try:
                    fn = (await fn_els.nth(i).text_content() or "").strip()
                    en = (await link_els.nth(i).text_content() or "").strip()
                    if fn:
                        results.append({
                            "state": "DE",
                            "filing_number": fn,
                            "filing_date": "",
                            "debtor_name": en,
                            "secured_party_name": "",
                            "status": "unknown",
                            "detail_url": "",
                            "collateral_description": "",
                            "data_source": "icis_entity_search",
                            "is_ucc_filing": False,
                        })
                except Exception:
                    continue

            for result in results:
                yield result

        finally:
            if should_close:
                await page.close()
