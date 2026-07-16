"""Texas UCC Scraper — SOSDirect (Texas Secretary of State).

Portal: https://direct.sos.state.tx.us/
Type: Classic ASP application with session-based login
Approach: Playwright — Texas SOSDirect requires a free account for UCC searches.
Free registration is available. Paid account needed for bulk downloads.

IMPORTANT: Texas SOSDirect requires a user account. The scraper must:
1. Log in with credentials (set via env vars TX_SOS_USER / TX_SOS_PASS)
2. Navigate to UCC search
3. Perform searches with rate limits (Texas enforces aggressively)
"""

import os
from datetime import datetime
from typing import AsyncIterator, Optional

from playwright.async_api import Page

from scrapers.base import BaseStateScraper
from scrapers.registry import register_scraper


@register_scraper("TX")
class TexasScraper(BaseStateScraper):
    state = "TX"
    state_name = "Texas"
    base_url = "https://direct.sos.state.tx.us/"

    requests_per_second: float = 0.33  # Texas rate-limits aggressively

    # Texas SOSDirect selectors
    LOGIN_USER_SELECTOR = 'input[name="UserID"], input[name="username"], input[id="UserID"]'
    LOGIN_PASS_SELECTOR = 'input[name="Password"], input[name="password"], input[id="Password"]'
    LOGIN_BUTTON_SELECTOR = 'input[type="submit"][value*="Log"], button:has-text("Log")'
    UCC_LINK_SELECTOR = 'a[href*="ucc" i], a:has-text("UCC")'

    @property
    def username(self) -> Optional[str]:
        return os.environ.get("TX_SOS_USER")

    @property
    def password(self) -> Optional[str]:
        return os.environ.get("TX_SOS_PASS")

    async def _login(self, page: Page):
        """Log into Texas SOSDirect."""
        if not self.username or not self.password:
            raise RuntimeError(
                "Texas SOSDirect requires credentials. "
                "Set TX_SOS_USER and TX_SOS_PASS environment variables. "
                "Register for free at https://direct.sos.state.tx.us/"
            )

        await self._safe_goto(page, "https://direct.sos.state.tx.us/acct/acct-login.asp")
        await page.wait_for_timeout(2000)

        await page.locator(self.LOGIN_USER_SELECTOR).first.fill(self.username)
        await page.locator(self.LOGIN_PASS_SELECTOR).first.fill(self.password)
        await page.locator(self.LOGIN_BUTTON_SELECTOR).first.click()
        await page.wait_for_timeout(3000)

    async def search_by_date_range(
        self, start_date: datetime, end_date: datetime, page: Optional[Page] = None
    ) -> AsyncIterator[dict]:
        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._login(page)

            # Navigate to UCC section
            ucc_link = page.locator(self.UCC_LINK_SELECTOR).first
            if await ucc_link.count() > 0:
                await ucc_link.click()
                await page.wait_for_timeout(2000)

            # Texas SOSDirect uses a form with date fields
            date_inputs = page.locator('input[type="text"][name*="date" i], input[name*="Date"]')
            if await date_inputs.count() >= 2:
                # Texas uses MM/DD/YYYY format
                await date_inputs.nth(0).fill(start_date.strftime("%m/%d/%Y"))
                await date_inputs.nth(1).fill(end_date.strftime("%m/%d/%Y"))

            search_btn = page.locator('input[type="submit"], button[type="submit"]').first
            if await search_btn.count() > 0:
                await search_btn.click()
                await page.wait_for_timeout(5000)

            async for filing in self._parse_results(page):
                yield filing

        finally:
            if should_close:
                await page.close()

    async def _parse_results(self, page: Page) -> AsyncIterator[dict]:
        while True:
            await page.wait_for_timeout(2000)

            rows = page.locator("tr:has(td)")
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

                detail_link = row.locator("a").first
                detail_url = ""
                if await detail_link.count() > 0:
                    detail_url = (await detail_link.get_attribute("href")) or ""

                yield {
                    "state": "TX",
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
                await page.wait_for_timeout(3000)
            else:
                break

    async def get_filing_detail(self, filing_number: str, page: Optional[Page] = None) -> dict:
        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._login(page)
            await self._safe_goto(page, f"https://direct.sos.state.tx.us/ucc/ucc-search.asp")
            await page.wait_for_timeout(2000)

            num_input = page.locator('input[name*="DocNumber"], input[name*="FilingNumber"]').first
            if await num_input.count() > 0:
                await num_input.fill(filing_number)
                search_btn = page.locator('input[type="submit"]').first
                if await search_btn.count() > 0:
                    await search_btn.click()
                    await page.wait_for_timeout(3000)

            fields = {}
            labels = page.locator("td.label, th, b, strong")
            label_count = await labels.count()
            for i in range(label_count):
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
