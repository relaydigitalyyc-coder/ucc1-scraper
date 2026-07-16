"""Georgia UCC Scraper — GSCCCA (Georgia Superior Court Clerks' Cooperative Authority).

Portal: https://search.gsccca.org/UCC_Search/
Type: JavaScript SPA with React-like interface
Approach: Playwright browser automation
"""

from datetime import datetime
from typing import AsyncIterator, Optional

from playwright.async_api import Page

from scrapers.base import BaseStateScraper
from scrapers.registry import register_scraper


@register_scraper("GA")
class GeorgiaScraper(BaseStateScraper):
    state = "GA"
    state_name = "Georgia"
    base_url = "https://search.gsccca.org/UCC_Search/"

    NAME_INPUT_SELECTOR = 'input[name*="debtor"], input[placeholder*="Debtor"], input[id*="Debtor"]'
    SEARCH_BUTTON_SELECTOR = 'button[type="submit"], input[type="submit"], button:has-text("Search")'
    RESULTS_TABLE_SELECTOR = 'table, [role="grid"]'
    RESULT_ROW_SELECTOR = 'tr:has(td), [role="row"]:has([role="gridcell"])'

    async def search_by_date_range(
        self, start_date: datetime, end_date: datetime, page: Optional[Page] = None
    ) -> AsyncIterator[dict]:
        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._safe_goto(page, self.base_url)
            await page.wait_for_timeout(3000)

            # Georgia GSCCCA: the portal allows date-range search
            # If available, fill dates; otherwise use wildcard debtor search
            date_inputs = page.locator('input[type="date"], input[placeholder*="date" i]')
            if await date_inputs.count() >= 2:
                await date_inputs.nth(0).fill(start_date.strftime("%m/%d/%Y"))
                await date_inputs.nth(1).fill(end_date.strftime("%m/%d/%Y"))
            else:
                # Fallback: search by debtor name with common wildcard
                name_input = page.locator(self.NAME_INPUT_SELECTOR).first
                if await name_input.count() > 0:
                    await name_input.fill("A")  # Broad wildcard; results filtered by date post-hoc

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
        while True:
            await page.wait_for_timeout(1500)

            rows = page.locator(self.RESULT_ROW_SELECTOR)
            count = await rows.count()

            for i in range(count):
                row = rows.nth(i)
                cells = row.locator("td, [role='gridcell']")
                cell_count = await cells.count()

                if cell_count < 2:
                    continue

                cell_texts = []
                for j in range(cell_count):
                    cell_texts.append((await cells.nth(j).text_content() or "").strip())

                # GA typically: Filing#, FilingDate, Debtor, SecuredParty, Status
                detail_link = row.locator("a").first
                detail_url = ""
                if await detail_link.count() > 0:
                    detail_url = (await detail_link.get_attribute("href")) or ""

                yield {
                    "state": "GA",
                    "filing_number": cell_texts[0] if cell_texts else "",
                    "filing_date": cell_texts[1] if len(cell_texts) > 1 else "",
                    "debtor_name": cell_texts[2] if len(cell_texts) > 2 else "",
                    "secured_party_name": cell_texts[3] if len(cell_texts) > 3 else "",
                    "status": cell_texts[4] if len(cell_texts) > 4 else "unknown",
                    "detail_url": detail_url,
                    "raw_cells": cell_texts,
                }

            # Pagination
            next_btn = page.locator('a:has-text("Next"), button:has-text("Next"), [aria-label*="next" i]').first
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

            num_input = page.locator('input[name*="filing"], input[id*="filing"], input[name*="number"]').first
            if await num_input.count() > 0:
                await num_input.fill(filing_number)
                search_btn = page.locator(self.SEARCH_BUTTON_SELECTOR).first
                if await search_btn.count() > 0:
                    await search_btn.click()
                    await page.wait_for_timeout(3000)

            fields = {}
            labels = page.locator("dt, label, th, .label, strong")
            label_count = await labels.count()
            for i in range(label_count):
                key = ((await labels.nth(i).text_content()) or "").strip().rstrip(":").lower().replace(" ", "_")
                parent = labels.nth(i).locator("..")
                sibling_text = (await parent.text_content()) or ""
                fields[key] = sibling_text.replace(key, "").strip()

            body_text = (await page.text_content("body")) or ""
            fields["_raw_body_text"] = body_text[:5000]
            return fields

        finally:
            if should_close:
                await page.close()

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
