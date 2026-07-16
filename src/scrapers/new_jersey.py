"""New Jersey UCC Scraper — njportal.com/UCC.

Portal: https://www.njportal.com/UCC/
Type: ASP.NET Web Forms (server-rendered with __VIEWSTATE)
Approach: Playwright automation with ASP.NET form handling.
"""

from datetime import datetime
from typing import AsyncIterator, Optional
from urllib.parse import urljoin

from playwright.async_api import Page

from scrapers.base import BaseStateScraper
from scrapers.registry import register_scraper


@register_scraper("NJ")
class NewJerseyScraper(BaseStateScraper):
    state = "NJ"
    state_name = "New Jersey"
    base_url = "https://www.njportal.com/UCC/"

    # NJ uses ASP.NET — forms have name="aspnetForm" with __VIEWSTATE
    FORM_SELECTOR = 'form[name="aspnetForm"], form#aspnetForm, form'
    SEARCH_TYPE_SELECTOR = 'select[name*="SearchType"], select[id*="SearchType"]'
    DEBTOR_INPUT_SELECTOR = 'input[name*="DebtorName"], input[id*="DebtorName"], input[name*="debtor"]'
    DATE_FROM_SELECTOR = 'input[name*="FromDate"], input[id*="FromDate"], input[name*="from"]'
    DATE_TO_SELECTOR = 'input[name*="ToDate"], input[id*="ToDate"], input[name*="to"]'
    SEARCH_BUTTON_SELECTOR = 'input[type="submit"][value*="Search"], button[id*="Search"], button:has-text("Search")'
    RESULTS_TABLE_SELECTOR = 'table[id*="Results"], table[id*="SearchResults"], table.Grid, table'
    RESULT_ROW_SELECTOR = 'tr:has(td)'
    NEXT_PAGE_SELECTOR = 'a:has-text("Next"), input[value="Next"], a[id*="Next"]'

    async def search_by_date_range(
        self, start_date: datetime, end_date: datetime, page: Optional[Page] = None
    ) -> AsyncIterator[dict]:
        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._safe_goto(page, self.base_url)
            await page.wait_for_timeout(2000)

            # NJ portal: find the search form and set up a date-range search
            # The portal typically has a search type dropdown
            search_type = page.locator(self.SEARCH_TYPE_SELECTOR).first
            if await search_type.count() > 0:
                # Try to select "Filing Date" or "Date Range" search
                options = search_type.locator("option")
                opt_count = await options.count()
                for i in range(opt_count):
                    opt_text = ((await options.nth(i).text_content()) or "").lower()
                    if "date" in opt_text or "filing" in opt_text:
                        await search_type.select_option(index=i)
                        await page.wait_for_timeout(1000)
                        break

            # Fill date range
            date_from = page.locator(self.DATE_FROM_SELECTOR).first
            date_to = page.locator(self.DATE_TO_SELECTOR).first

            if await date_from.count() > 0:
                await date_from.fill(start_date.strftime("%m/%d/%Y"))
            if await date_to.count() > 0:
                await date_to.fill(end_date.strftime("%m/%d/%Y"))

            # Submit the form (ASP.NET postback)
            search_btn = page.locator(self.SEARCH_BUTTON_SELECTOR).first
            if await search_btn.count() > 0:
                await search_btn.click()
                await page.wait_for_timeout(4000)

            async for filing in self._parse_results(page):
                yield filing

        finally:
            if should_close:
                await page.close()

    async def _parse_results(self, page: Page) -> AsyncIterator[dict]:
        """Parse ASP.NET GridView results."""
        while True:
            await page.wait_for_timeout(1500)

            # Find the results grid
            table = page.locator(self.RESULTS_TABLE_SELECTOR).first
            rows = table.locator(self.RESULT_ROW_SELECTOR)
            count = await rows.count()

            for i in range(count):
                row = rows.nth(i)
                cells = row.locator("td")
                cell_count = await cells.count()

                if cell_count < 2:
                    continue

                cell_texts = []
                for j in range(cell_count):
                    cell_texts.append((await cells.nth(j).text_content() or "").strip())

                # NJ typically displays: Filing#, FilingDate, DebtorName, SecuredParty, Status
                detail_link = row.locator("a").first
                detail_url = ""
                filing_number = ""
                if await detail_link.count() > 0:
                    detail_url = (await detail_link.get_attribute("href")) or ""
                    filing_number = (await detail_link.text_content() or "").strip()

                filing = {
                    "state": "NJ",
                    "filing_number": filing_number or cell_texts[0] if cell_texts else "",
                    "filing_date": cell_texts[1] if len(cell_texts) > 1 else "",
                    "debtor_name": cell_texts[2] if len(cell_texts) > 2 else "",
                    "secured_party_name": cell_texts[3] if len(cell_texts) > 3 else "",
                    "status": cell_texts[4] if len(cell_texts) > 4 else "unknown",
                    "detail_url": urljoin(self.base_url, detail_url) if detail_url else "",
                    "raw_cells": cell_texts,
                }
                yield filing

            # Pagination
            next_btn = page.locator(self.NEXT_PAGE_SELECTOR).first
            if await next_btn.count() > 0 and await next_btn.is_enabled():
                await next_btn.click()
                await page.wait_for_timeout(2000)
            else:
                break

    async def get_filing_detail(self, filing_number: str, page: Optional[Page] = None) -> dict:
        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._safe_goto(page, self.base_url)
            await page.wait_for_timeout(2000)

            # NJ portal: search by filing number
            num_input = page.locator('input[name*="FilingNumber"], input[id*="FilingNumber"]').first
            if await num_input.count() > 0:
                await num_input.fill(filing_number)
                search_btn = page.locator(self.SEARCH_BUTTON_SELECTOR).first
                if await search_btn.count() > 0:
                    await search_btn.click()
                    await page.wait_for_timeout(3000)

                    # Click into the detail page if we got a results list
                    first_link = page.locator(f'a:has-text("{filing_number}")').first
                    if await first_link.count() > 0:
                        await first_link.click()
                        await page.wait_for_timeout(2000)

            return await self._extract_detail_fields(page)

        finally:
            if should_close:
                await page.close()

    async def _extract_detail_fields(self, page: Page) -> dict:
        """Extract detail fields from NJ's detail page."""
        fields = {}

        # NJ typically shows key-value pairs in divs or table rows
        rows = page.locator("tr")
        row_count = await rows.count()
        for i in range(row_count):
            label = rows.nth(i).locator("td:first-child, th")
            value = rows.nth(i).locator("td:last-child")
            if await label.count() > 0 and await value.count() > 0:
                key = ((await label.text_content()) or "").strip().rstrip(":").lower().replace(" ", "_")
                val = ((await value.text_content()) or "").strip()
                fields[key] = val

        body_text = (await page.text_content("body")) or ""
        fields["_raw_body_text"] = body_text[:5000]
        return fields

    async def check_status(self, filing_number: str, page: Optional[Page] = None) -> str:
        detail = await self.get_filing_detail(filing_number, page)
        status_text = (detail.get("status", "") + " " + detail.get("_raw_body_text", "")).lower()
        if "terminat" in status_text:
            return "terminated"
        elif "lapsed" in status_text:
            return "lapsed"
        elif "continu" in status_text or "amend" in status_text:
            return "amended"
        elif "active" in status_text:
            return "active"
        return "unknown"
