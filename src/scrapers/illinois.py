"""Illinois UCC Scraper — IL Secretary of State.

Portal: https://apps.ilsos.gov/uccsearch/
Type: Modern SPA with anti-bot protection (403 on direct HTTP)
Approach: Playwright with stealth scripts required. Illinois uses Cloudflare-like
protection; rotating residential proxies may be needed at scale.
"""

from datetime import datetime
from typing import AsyncIterator, Optional

from playwright.async_api import Page

from scrapers.base import BaseStateScraper
from scrapers.registry import register_scraper


@register_scraper("IL")
class IllinoisScraper(BaseStateScraper):
    state = "IL"
    state_name = "Illinois"
    base_url = "https://apps.ilsos.gov/uccsearch/"

    requests_per_second: float = 0.5

    async def search_by_date_range(
        self, start_date: datetime, end_date: datetime, page: Optional[Page] = None
    ) -> AsyncIterator[dict]:
        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._safe_goto(page, self.base_url)
            await page.wait_for_timeout(4000)  # IL is protected, give it time

            # IL uccsearch is a SPA — find search controls
            # Look for date range inputs
            date_inputs = page.locator('input[type="date"], input[name*="date" i]')
            input_count = await date_inputs.count()

            if input_count >= 2:
                await date_inputs.nth(0).fill(start_date.strftime("%m/%d/%Y"))
                await date_inputs.nth(1).fill(end_date.strftime("%m/%d/%Y"))

            # Search button
            search_btn = page.locator(
                'button[type="submit"], input[type="submit"], '
                'button:has-text("Search"), a:has-text("Search")'
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
            await page.wait_for_timeout(2000)

            rows = page.locator("tr:has(td), [role='row']:has([role='gridcell'])")
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

                detail_link = row.locator("a").first
                detail_url = ""
                if await detail_link.count() > 0:
                    detail_url = (await detail_link.get_attribute("href")) or ""

                yield {
                    "state": "IL",
                    "filing_number": cell_texts[0] if cell_texts else "",
                    "filing_date": cell_texts[1] if len(cell_texts) > 1 else "",
                    "debtor_name": cell_texts[2] if len(cell_texts) > 2 else "",
                    "secured_party_name": cell_texts[3] if len(cell_texts) > 3 else "",
                    "status": cell_texts[4] if len(cell_texts) > 4 else "unknown",
                    "detail_url": detail_url,
                    "raw_cells": cell_texts,
                }

            next_btn = page.locator('a:has-text("Next"), button:has-text("Next")').first
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
            await page.wait_for_timeout(3000)

            num_input = page.locator('input[name*="number"], input[id*="number"], input[name*="filing"]').first
            if await num_input.count() > 0:
                await num_input.fill(filing_number)
                search_btn = page.locator('button[type="submit"], input[type="submit"]').first
                if await search_btn.count() > 0:
                    await search_btn.click()
                    await page.wait_for_timeout(3000)

            return await self._extract_fields(page)

        finally:
            if should_close:
                await page.close()

    async def _extract_fields(self, page: Page) -> dict:
        fields = {}
        labels = page.locator("label, dt, .label, strong, th")
        label_count = await labels.count()
        for i in range(label_count):
            key = ((await labels.nth(i).text_content()) or "").strip().rstrip(":").lower().replace(" ", "_")
            parent = labels.nth(i).locator("..")
            text = (await parent.text_content()) or ""
            fields[key] = text.replace(key, "").strip()

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
