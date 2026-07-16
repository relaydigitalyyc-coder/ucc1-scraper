"""Colorado UCC Scraper — CO Secretary of State.

Portal: https://www.sos.state.co.us/ucc/pages/home.xhtml
Type: JavaServer Faces (JSF) / XHTML — server-rendered with JSF lifecycle
Approach: Playwright — CO uses JSF which requires proper form POST handling.
"""

from datetime import datetime
from typing import AsyncIterator, Optional

from playwright.async_api import Page

from scrapers.base import BaseStateScraper
from scrapers.registry import register_scraper


@register_scraper("CO")
class ColoradoScraper(BaseStateScraper):
    state = "CO"
    state_name = "Colorado"
    base_url = "https://www.sos.state.co.us/ucc/pages/home.xhtml"

    async def search_by_date_range(
        self, start_date: datetime, end_date: datetime, page: Optional[Page] = None
    ) -> AsyncIterator[dict]:
        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._safe_goto(page, self.base_url)
            await page.wait_for_timeout(3000)

            # CO uses JSF — form with javax.faces.ViewState
            # Navigate to search
            search_link = page.locator('a:has-text("Search"), a[href*="search" i]').first
            if await search_link.count() > 0:
                await search_link.click()
                await page.wait_for_timeout(2000)

            date_inputs = page.locator('input[type="text"][name*="date" i], input[type="text"][name*="Date"]')
            if await date_inputs.count() >= 2:
                await date_inputs.nth(0).fill(start_date.strftime("%m/%d/%Y"))
                await date_inputs.nth(1).fill(end_date.strftime("%m/%d/%Y"))

            search_btn = page.locator(
                'input[type="submit"], button[type="submit"], '
                'a:has-text("Search"), button:has-text("Search")'
            ).first
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
            rows = page.locator("tr:has(td)")
            count = await rows.count()

            for i in range(count):
                row = rows.nth(i)
                cells = row.locator("td")
                cell_count = await cells.count()
                if cell_count < 2:
                    continue

                cell_texts = [(await cells.nth(j).text_content() or "").strip() for j in range(cell_count)]
                detail_link = row.locator("a").first
                detail_url = ""
                if await detail_link.count() > 0:
                    detail_url = (await detail_link.get_attribute("href")) or ""

                yield {
                    "state": "CO",
                    "filing_number": cell_texts[0] if cell_texts else "",
                    "filing_date": cell_texts[1] if len(cell_texts) > 1 else "",
                    "debtor_name": cell_texts[2] if len(cell_texts) > 2 else "",
                    "secured_party_name": cell_texts[3] if len(cell_texts) > 3 else "",
                    "status": cell_texts[4] if len(cell_texts) > 4 else "unknown",
                    "detail_url": detail_url,
                    "raw_cells": cell_texts,
                }

            next_btn = page.locator('a:has-text("Next"), a:has-text(">")').first
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
            search_link = page.locator('a:has-text("Search")').first
            if await search_link.count() > 0:
                await search_link.click()
                await page.wait_for_timeout(2000)

            num_input = page.locator('input[name*="number"], input[name*="filing"]').first
            if await num_input.count() > 0:
                await num_input.fill(filing_number)
                search_btn = page.locator('input[type="submit"], button[type="submit"]').first
                if await search_btn.count() > 0:
                    await search_btn.click()
                    await page.wait_for_timeout(3000)

            fields = {}
            labels = page.locator("label, .label, th, strong")
            for i in range(await labels.count()):
                key = ((await labels.nth(i).text_content()) or "").strip().rstrip(":").lower().replace(" ", "_")
                parent = labels.nth(i).locator("..")
                text = (await parent.text_content()) or ""
                fields[key] = text.replace(key, "").strip()

            body_text = (await page.text_content("body")) or ""
            fields["_raw_body_text"] = body_text[:5000]
            return fields

        finally:
            if should_close:
                await page.close()

    async def check_status(self, filing_number: str, page: Optional[Page] = None) -> str:
        detail = await self.get_filing_detail(filing_number, page)
        status_text = (detail.get("_raw_body_text", "")).lower()
        if "terminat" in status_text:
            return "terminated"
        elif "lapsed" in status_text:
            return "lapsed"
        elif "active" in status_text:
            return "active"
        return "unknown"
