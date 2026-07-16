"""Maryland UCC Scraper with CapSolver Cloudflare Turnstile bypass.

Portal: https://egov.maryland.gov/SDAT/UCCFiling/UCCMainPage.aspx
Type: Tyler Technologies ASP.NET Web Forms + Cloudflare Turnstile captcha

Why Maryland is the #2 state for MCA lead generation (after Oregon):
  - Secured Party search is a first-class radio button option
  - Organization name search is supported
  - The ONLY barrier is Cloudflare Turnstile captcha

Architecture:
  1. Playwright navigates the ASP.NET wizard (__VIEWSTATE handled automatically)
  2. Extract Cloudflare Turnstile sitekey from the page HTML
  3. Solve the Turnstile challenge via CapSolver API (capsolver.com)
  4. Inject the token into cf-turnstile-response and submit the form
  5. Parse the ASP.NET GridView results table with pagination

CapSolver cost: ~$0.50-2/month for this volume (pay-per-solve, ~$0.001/solve).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime
from typing import AsyncIterator, Optional
from urllib.parse import urljoin

import httpx
from playwright.async_api import Page

from scrapers.base import BaseStateScraper
from scrapers.registry import register_scraper

logger = logging.getLogger(__name__)

# ── CapSolver constants ─────────────────────────────────────────────────────
CAPSOLVER_API_KEY_VAR = "CAPSOLVER_API_KEY"
CAPSOLVER_BASE = "https://api.capsolver.com"
CAPSOLVER_POLL_INTERVAL = 2  # seconds between polling
CAPSOLVER_MAX_POLLS = 30  # 60 seconds max wait

# ── Module-level Turnstile solver (stateless, reusable) ────────────────────


async def solve_turnstile(page_url: str, sitekey: str, api_key: str) -> str:
    """Solve Cloudflare Turnstile via CapSolver AntiTurnstileTaskProxyLess.

    This is a stateless function — it does NOT use Playwright.  It calls the
    CapSolver API which solves the Turnstile challenge in their cloud and
    returns a valid token.

    Args:
        page_url: The full URL of the page with the Turnstile widget.
        sitekey: The Turnstile sitekey (value of the data-sitekey attribute).
        api_key: CapSolver API key.

    Returns:
        The Turnstile token string, ready to inject into the
        cf-turnstile-response hidden input.

    Raises:
        ValueError: If the API key is empty or the API returns an error.
        TimeoutError: If CapSolver doesn't return a solution in time.
        httpx.HTTPStatusError: On HTTP-level failures.
    """
    if not api_key:
        raise ValueError(
            f"Set {CAPSOLVER_API_KEY_VAR} environment variable. "
            "Get one at capsolver.com"
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Step 1: Create a Turnstile solving task
        create_resp = await client.post(
            f"{CAPSOLVER_BASE}/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type": "AntiTurnstileTaskProxyLess",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                },
            },
        )
        create_resp.raise_for_status()
        create_data = create_resp.json()

        if create_data.get("errorId") != 0:
            raise ValueError(
                f"CapSolver createTask error: {create_data.get('errorCode', 'unknown')} "
                f"- {create_data.get('errorDescription', '')}"
            )

        task_id = create_data["taskId"]
        logger.info("CapSolver task created: %s", task_id)

        # Step 2: Poll for the solution
        for attempt in range(CAPSOLVER_MAX_POLLS):
            await asyncio.sleep(CAPSOLVER_POLL_INTERVAL)

            poll_resp = await client.post(
                f"{CAPSOLVER_BASE}/getTaskResult",
                json={
                    "clientKey": api_key,
                    "taskId": task_id,
                },
            )
            poll_resp.raise_for_status()
            poll_data = poll_resp.json()

            if poll_data.get("errorId") != 0:
                raise ValueError(
                    f"CapSolver getTaskResult error: "
                    f"{poll_data.get('errorCode', 'unknown')}"
                )

            status = poll_data.get("status", "")
            if status == "ready":
                token = poll_data["solution"]["token"]
                logger.info(
                    "CapSolver solved Turnstile in %.1fs",
                    (attempt + 1) * CAPSOLVER_POLL_INTERVAL,
                )
                return token

            if status == "failed":
                raise RuntimeError(
                    f"CapSolver task failed: {poll_data}"
                )

        raise TimeoutError(
            f"CapSolver did not return a solution after "
            f"{CAPSOLVER_MAX_POLLS * CAPSOLVER_POLL_INTERVAL}s"
        )


# ── Helper to extract Turnstile sitekey from page HTML ─────────────────────


async def extract_turnstile_sitekey(page: Page) -> str:
    """Extract the Turnstile sitekey from the current page.

    Tries multiple strategies in order of reliability:
    1. ``data-sitekey`` attribute on ``.cf-turnstile`` or any element
    2. ``turnstile.render()`` calls in ``<script>`` tags
    3. Full HTML regex fallback

    Returns:
        The sitekey string.

    Raises:
        ValueError: If no sitekey can be found on the page.
    """
    # Strategy 1: data-sitekey attribute on DOM elements
    sitekey = await page.evaluate(
        """() => {
            const el = document.querySelector(
                '.cf-turnstile, [data-sitekey]'
            );
            return el ? el.getAttribute('data-sitekey') : null;
        }"""
    )
    if sitekey:
        return sitekey

    # Strategy 2: Search inline scripts for turnstile.render() calls
    sitekey = await page.evaluate(
        """() => {
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const t = s.textContent || '';

                // turnstile.render('container', {sitekey: '...'})
                const match = t.match(
                    /turnstile\\s*\\.\\s*render\\s*\\([^)]+sitekey\\s*:\\s*['"]([^'"]+)['"]/
                );
                if (match) return match[1];

                // sitekey: '...' standalone
                const alt = t.match(/sitekey\\s*:\\s*['"]([^'"]+)['"]/);
                if (alt) return alt[1];
            }
            return null;
        }"""
    )
    if sitekey:
        return sitekey

    # Strategy 3: Full HTML text regex
    html = await page.content()
    m = re.search(r'data-sitekey=["\']([^"\']+)["\']', html)
    if m:
        return m.group(1)
    m = re.search(
        r'turnstile\.render\([^)]+sitekey:\s*["\']([^"\']+)["\']',
        html,
    )
    if m:
        return m.group(1)

    raise ValueError(
        "Could not find Turnstile sitekey on the page. "
        "The portal may have changed its captcha implementation."
    )


# ── Token injection into the page ──────────────────────────────────────────


async def inject_turnstile_token(page: Page, token: str) -> None:
    """Inject a Turnstile token into the page's cf-turnstile-response field.

    This sets the hidden input that the ASP.NET form submit will include,
    and fires any registered callbacks so the framework knows the captcha
    is solved.

    Args:
        page: The Playwright page to inject into.
        token: The Turnstile token from CapSolver.
    """
    await page.evaluate(
        """(token) => {
            // 1. Find or create the cf-turnstile-response hidden input
            let input = document.getElementById('cf-turnstile-response');
            if (!input) {
                input = document.querySelector(
                    'input[name="cf-turnstile-response"]'
                );
            }
            if (!input) {
                input = document.createElement('input');
                input.type = 'hidden';
                input.id = 'cf-turnstile-response';
                input.name = 'cf-turnstile-response';
                const form = document.forms[0];
                if (form) form.appendChild(input);
            }
            input.value = token;

            // 2. Trigger any registered Turnstile callback
            const el = document.querySelector(
                '.cf-turnstile, [data-sitekey]'
            );
            if (el) {
                const cbName = el.getAttribute('data-callback');
                if (cbName && typeof window[cbName] === 'function') {
                    try { window[cbName](token); } catch (_) {}
                }
            }

            // 3. Dispatch events so the framework notices
            input.dispatchEvent(
                new Event('input', { bubbles: true })
            );
            input.dispatchEvent(
                new Event('change', { bubbles: true })
            );
        }""",
        token,
    )


# ── Scraper class ─────────────────────────────────────────────────────────


@register_scraper("MD")
class MarylandScraper(BaseStateScraper):
    """Maryland UCC scraper — ASP.NET portal with Cloudflare Turnstile.

    Flow:
      1. Navigate through the public portal wizard
      2. Configure search form (Party = Secured Party, Org Name)
      3. Solve Turnstile captcha via CapSolver
      4. Submit form and parse ASP.NET GridView results
      5. Paginate through all result pages
    """

    state = "MD"
    state_name = "Maryland"
    base_url = "https://egov.maryland.gov/SDAT/UCCFiling/UCCMainPage.aspx"
    menu_url = "https://egov.maryland.gov/SDAT/UCCFiling/MainMenu.aspx"
    name_search_url = (
        "https://egov.maryland.gov/SDAT/UCCFiling/UCCPartyNameSearchMainPage.aspx"
    )

    # ── ASP.NET / CSS selectors ────────────────────────────────────────────
    NON_SUBSCRIBER_BTN = "#MainContentPlaceHolder_NonSubscriberButton"
    NAME_SEARCH_LINK = 'a:has-text("Name Search")'
    FILING_NUMBER_SEARCH_LINK = 'a:has-text("Filing Number Search")'

    PARTY_DEBTOR_RADIO = 'input[value="DebtorPartyRadioButton"]'
    PARTY_SECURED_RADIO = 'input[value="SecuredPartyRadioButton"]'
    ORG_NAME_TYPE_RADIO = 'input[value="OrganizationNameTypeRadioButton"]'
    INDIV_NAME_TYPE_RADIO = 'input[value="IndividualNameTypeRadioButton"]'
    STANDARD_SEARCH_RADIO = 'input[value="StandardSearchTypeRadioButton"]'
    NON_STANDARD_SEARCH_RADIO = (
        'input[value="NonStandardSearchTypeRadioButton"]'
    )
    ALL_FILINGS_RADIO = 'input[value="ByAllFilingStatusRadioButton"]'
    UNLAPSED_FILINGS_RADIO = 'input[value="ByUnlapsedFilingStatusRadioButton"]'
    ALL_TYPES_RADIO = 'input[value="ByFilingTypeAllRadioButton"]'
    UCC1_ONLY_RADIO = 'input[value="ByFilingTypeUCC1OnlyRadioButton"]'

    PARTY_NAME_INPUT = (
        "#MainContentPlaceHolder_NameSearchControl1_PartyNameTextBox"
    )
    ORG_NAME_INPUT = (
        "#MainContentPlaceHolder_NameSearchControl1_OrganizationTextBox"
    )
    INDIV_LAST_NAME_INPUT = (
        "#MainContentPlaceHolder_NameSearchControl1_IndividualLastNameTextBox"
    )
    INDIV_FIRST_NAME_INPUT = (
        "#MainContentPlaceHolder_NameSearchControl1_IndividualFirstNameTextBox"
    )

    CONTINUE_BTN = "#MainContentPlaceHolder_ContinueButton"
    BACK_BTN = "#MainContentPlaceHolder_BackButton"

    RESULTS_TABLE = "table"
    RESULT_ROWS = "tr:has(td)"
    NEXT_PAGE_SELECTOR = 'a:has-text("Next"), a[id*="Next"]'

    requests_per_second: float = 0.5

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def __init__(
        self,
        headless: bool = True,
        proxy: Optional[str] = None,
    ):
        super().__init__(headless=headless, proxy=proxy)
        self._capsolver_api_key: str = ""

    async def start(self):
        """Start the browser and read the CapSolver API key from env."""
        await super().start()
        self._capsolver_api_key = os.environ.get(CAPSOLVER_API_KEY_VAR, "")

    # ── API key guard ──────────────────────────────────────────────────────

    def _ensure_api_key(self) -> None:
        """Raise a clear error if the CapSolver API key is not configured."""
        if not self._capsolver_api_key:
            raise ValueError(
                f"Set the {CAPSOLVER_API_KEY_VAR} environment variable. "
                "Get one at capsolver.com"
            )

    # ── Turnstile workflow ─────────────────────────────────────────────────

    async def _solve_and_inject_turnstile(self, page: Page) -> None:
        """Full Turnstile bypass: extract sitekey, solve, inject.

        This is the key method that makes Maryland scrapable.
        """
        self._ensure_api_key()

        current_url = page.url
        sitekey = await extract_turnstile_sitekey(page)
        logger.info("Turnstile sitekey found: %s", sitekey)

        token = await solve_turnstile(
            page_url=current_url,
            sitekey=sitekey,
            api_key=self._capsolver_api_key,
        )
        logger.info("Turnstile token obtained (%d chars)", len(token))

        await inject_turnstile_token(page, token)
        await page.wait_for_timeout(500)

    # ── Portal navigation ──────────────────────────────────────────────────

    async def _navigate_to_name_search(self, page: Page) -> None:
        """Navigate the ASP.NET wizard to reach the Name Search form.

        Flow: MainPage -> "PUBLIC FILING AND SEARCHES" -> "Name Search" link
        """
        await self._safe_goto(page, self.base_url)
        await page.wait_for_timeout(3000)

        # Click "PUBLIC FILING AND SEARCHES" (non-subscriber path)
        public_btn = page.locator(self.NON_SUBSCRIBER_BTN).first
        if await public_btn.count() > 0:
            await public_btn.click()
            await page.wait_for_timeout(5000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

        # Click "Name Search" on the MainMenu page
        name_link = page.locator(self.NAME_SEARCH_LINK).first
        if await name_link.count() > 0:
            await name_link.click()
            await page.wait_for_timeout(5000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

    # ── Form configuration ─────────────────────────────────────────────────

    async def _click_radio(self, page: Page, selector: str) -> None:
        """Click a hidden ASP.NET radio button via JS evaluation.

        ASP.NET WebForms renders radio buttons with ``display:none`` and
        relies on a styled ``<label>`` element for visual interaction and
        JavaScript event handlers (``onclick``) for the actual selection.
        Playwright's ``click()`` even with ``force=True`` cannot interact
        with elements that have ``display:none``, so we use
        ``page.evaluate`` to dispatch the click programmatically.
        """
        clicked = await page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                el.click();
                return true;
            }""",
            selector,
        )
        if clicked:
            await page.wait_for_timeout(300)

    async def _setup_name_search(
        self,
        page: Page,
        *,
        search_by_secured: bool = True,
        org_name: bool = True,
        search_type: str = "standard",
        filing_status: str = "all",
        filing_type: str = "all",
    ) -> None:
        """Configure all radio-button options on the Name Search form.

        Args:
            page: The Playwright page with the form loaded.
            search_by_secured: True = Secured Party, False = Debtor.
            org_name: True = Organization, False = Individual.
            search_type: ``"standard"`` or ``"non_standard"``.
            filing_status: ``"all"`` or ``"unlapsed"``.
            filing_type: ``"all"`` or ``"ucc1_only"``.
        """
        # Party type
        selector = (
            self.PARTY_SECURED_RADIO if search_by_secured
            else self.PARTY_DEBTOR_RADIO
        )
        await self._click_radio(page, selector)

        # Name type
        selector = (
            self.ORG_NAME_TYPE_RADIO if org_name
            else self.INDIV_NAME_TYPE_RADIO
        )
        await self._click_radio(page, selector)

        # Search type (Standard / Non-Standard)
        if search_type == "non_standard":
            await self._click_radio(page, self.NON_STANDARD_SEARCH_RADIO)

        # Filing status (All / Unlapsed)
        if filing_status == "unlapsed":
            await self._click_radio(page, self.UNLAPSED_FILINGS_RADIO)

        # Filing type (All / UCC-1 Only)
        if filing_type == "ucc1_only":
            await self._click_radio(page, self.UCC1_ONLY_RADIO)

    # ── Date-range search ─────────────────────────────────────────────────

    async def search_by_date_range(
        self,
        start_date: datetime,
        end_date: datetime,
        page: Optional[Page] = None,
    ) -> AsyncIterator[dict]:
        """Search Maryland UCC filings filed between *start_date* and *end_date*.

        The MD portal does **not** have a date-range search field, so we use
        the following approximation:

        1. Iterate over known MCA funder names (Tier 1+2 first words).
        2. Search each as a Secured Party name via the ASP.NET form.
        3. Solve a Turnstile captcha for each search (CapSolver).
        4. Filter returned results client-side by filing date.

        Each funder-word search yields its own Turnstile solve — about 50-80
        solves per full scrape run.

        Yields:
            Raw filing dicts matching the date range.
        """
        funder_words = self._get_funder_first_words()
        if not funder_words:
            logger.warning("No Tier 1-2 funder names available for search")
            return

        should_close = page is None
        page = page or await self.new_page()

        try:
            for i, funder_word in enumerate(funder_words):
                logger.info(
                    "Searching secured party [%d/%d]: %s",
                    i + 1,
                    len(funder_words),
                    funder_word,
                )

                async for filing in self._search_single_sp(
                    secured_party_name=funder_word,
                    page=page,
                    navigate=(i == 0),  # navigate only on first search
                ):
                    if self._filing_in_date_range(filing, start_date, end_date):
                        yield filing

                await asyncio.sleep(1.0 / self.requests_per_second)
        finally:
            if should_close:
                await page.close()

    async def _search_single_sp(
        self,
        secured_party_name: str,
        page: Page,
        *,
        navigate: bool = True,
    ) -> AsyncIterator[dict]:
        """Execute a single secured-party name search with Turnstile bypass.

        This is the inner search loop used by ``search_by_date_range``.
        It does NOT create/close the page — the caller manages that.

        Args:
            secured_party_name: Name/organization to search for.
            page: An already-initialized Playwright page.
            navigate: If True, navigate to the name search form first.
                      Set to False when reusing the same page for
                      consecutive searches.
        """
        if navigate:
            await self._navigate_to_name_search(page)

        await self._setup_name_search(
            page,
            search_by_secured=True,
            org_name=True,
            search_type="standard",
            filing_status="all",
            filing_type="all",
        )

        # Fill the party name input
        name_input = page.locator(self.PARTY_NAME_INPUT).first
        if await name_input.count() == 0:
            logger.warning(
                "Party name input not found for %r", secured_party_name
            )
            return

        await name_input.fill(secured_party_name)
        await page.wait_for_timeout(300)

        # --- Turnstile bypass ---
        # We try up to 2 times in case the first token expires or fails.
        max_attempts = 2
        for attempt in range(max_attempts):
            try:
                await self._solve_and_inject_turnstile(page)
            except Exception as exc:
                logger.error(
                    "Turnstile solving failed (attempt %d/%d) for %r: %s",
                    attempt + 1,
                    max_attempts,
                    secured_party_name,
                    exc,
                )
                if attempt < max_attempts - 1:
                    await page.wait_for_timeout(2000)
                    continue
                return

            await page.wait_for_timeout(500)

            # Click Continue to submit the form
            continue_btn = page.locator(self.CONTINUE_BTN).first
            if await continue_btn.count() == 0:
                logger.warning("Continue button not found for %r", secured_party_name)
                return

            await continue_btn.click()
            await page.wait_for_timeout(5000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            # Check for captcha failure
            body_text = await page.text_content("body") or ""
            if "Invalid Captcha" in body_text:
                logger.warning(
                    "Captcha invalid for %r (attempt %d/%d) — retrying",
                    secured_party_name,
                    attempt + 1,
                    max_attempts,
                )
                # Navigate back to the search form and retry
                if attempt < max_attempts - 1:
                    back_btn = page.locator(self.BACK_BTN).first
                    if await back_btn.count() > 0:
                        await back_btn.click()
                        await page.wait_for_timeout(3000)
                    continue
                return

            break  # captcha OK, proceed to parse

        # Check for "no records" message
        body_text = await page.text_content("body") or ""
        if "No records" in body_text or "no results" in body_text.lower():
            logger.info("No records found for: %s", secured_party_name)
            return

        async for filing in self._parse_results(page):
            yield filing

    async def search_by_secured_party(
        self,
        secured_party_name: str,
        search_type: str = "standard",
        filing_status: str = "all",
        filing_type: str = "all",
        page: Optional[Page] = None,
    ) -> AsyncIterator[dict]:
        """Search by a single secured party name with Turnstile bypass.

        This is a convenience entry point for manual/ad-hoc searches.
        For batch scraping, prefer ``search_by_date_range()``.

        Args:
            secured_party_name: Name to search (e.g. "YELLOWSTONE").
            search_type: ``"standard"`` or ``"non_standard"``.
            filing_status: ``"all"`` or ``"unlapsed"``.
            filing_type: ``"all"`` or ``"ucc1_only"``.
            page: Optional reusable page instance.
        """
        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._navigate_to_name_search(page)
            await self._setup_name_search(
                page,
                search_by_secured=True,
                org_name=True,
                search_type=search_type,
                filing_status=filing_status,
                filing_type=filing_type,
            )

            name_input = page.locator(self.PARTY_NAME_INPUT).first
            if await name_input.count() == 0:
                logger.warning("Party name input not found")
                return

            await name_input.fill(secured_party_name)
            await page.wait_for_timeout(300)

            await self._solve_and_inject_turnstile(page)
            await page.wait_for_timeout(500)

            continue_btn = page.locator(self.CONTINUE_BTN).first
            if await continue_btn.count() > 0:
                await continue_btn.click()
                await page.wait_for_timeout(5000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass

            body_text = await page.text_content("body") or ""
            if "Invalid Captcha" in body_text:
                logger.error(
                    "Captcha validation failed for %r", secured_party_name
                )
                return
            if "No records" in body_text:
                return

            async for filing in self._parse_results(page):
                yield filing

        finally:
            if should_close:
                await page.close()

    # ── Result parsing (ASP.NET GridView) ──────────────────────────────────

    async def _parse_results(self, page: Page) -> AsyncIterator[dict]:
        """Parse the ASP.NET GridView search-results table.

        Handles multi-page results via ``__doPostBack``-style Next links.
        Maryland results typically contain columns:
            Organization Name | City | Filing Number | Status |
            Filing Date | Lapse Date | Page Count

        Yields:
            Raw filing dicts with fields:
            ``state``, ``filing_number``, ``debtor_name``,
            ``debtor_city``, ``secured_party_name``, ``filing_date``,
            ``status``, ``detail_url``, ``raw_cells``.
        """
        max_pages = 50

        for page_num in range(max_pages):
            await page.wait_for_timeout(2000)

            tables = page.locator(self.RESULTS_TABLE)
            table_count = await tables.count()
            found_data = False

            for t_idx in range(table_count):
                table = tables.nth(t_idx)
                rows = table.locator(self.RESULT_ROWS)
                row_count = await rows.count()

                if row_count < 2:
                    continue

                for r_idx in range(1, row_count):  # skip header row
                    row = rows.nth(r_idx)
                    cells = row.locator("td")
                    cell_count = await cells.count()

                    if cell_count < 2:
                        continue

                    cell_texts: list[str] = []
                    for c_idx in range(cell_count):
                        text = (
                            await cells.nth(c_idx).text_content() or ""
                        ).strip()
                        cell_texts.append(text[:300])

                    # Detect column layout
                    # Typical MD columns (0-based):
                    # 0=OrgName, 1=City, 2=FilingNumber, 3=Status,
                    # 4=FilingDate, 5=LapseDate, 6=PageCount
                    detail_link = row.locator("a").first
                    filing_number = ""
                    detail_url = ""
                    if await detail_link.count() > 0:
                        href = await detail_link.get_attribute("href") or ""
                        detail_url = urljoin(self.base_url, href) if href else ""
                        filing_number = (
                            await detail_link.text_content() or ""
                        ).strip()

                    # Flexible column mapping — try to figure out which
                    # column holds what based on position and content.
                    if not filing_number and cell_texts:
                        filing_number = cell_texts[0]

                    filing: dict[str, object] = {
                        "state": "MD",
                        "filing_number": filing_number,
                        "debtor_name": (
                            cell_texts[0] if len(cell_texts) > 0 else ""
                        ),
                        "debtor_city": (
                            cell_texts[1] if len(cell_texts) > 1 else ""
                        ),
                        "secured_party_name": "",
                        "filing_date": "",
                        "status": "unknown",
                        "detail_url": detail_url,
                        "raw_cells": cell_texts,
                    }

                    # Guess column positions based on the number of columns
                    if len(cell_texts) >= 5:
                        # 5+ columns: likely Org, City, Filing#, Status,
                        # Date
                        filing["debtor_name"] = cell_texts[0]
                        filing["debtor_city"] = cell_texts[1]
                        filing["filing_number"] = filing_number or cell_texts[2]
                        filing["status"] = cell_texts[3]
                        filing["filing_date"] = cell_texts[4]

                    found_data = True
                    yield filing

            if not found_data:
                break

            # Paginate to the next page via __doPostBack-style link
            next_btn = page.locator(self.NEXT_PAGE_SELECTOR).first
            if (
                await next_btn.count() > 0
                and await next_btn.is_enabled()
            ):
                await next_btn.click()
                await page.wait_for_timeout(3000)
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=15000
                    )
                except Exception:
                    pass
            else:
                break

    # ── Filing detail ─────────────────────────────────────────────────────

    async def get_filing_detail(
        self,
        filing_number: str,
        page: Optional[Page] = None,
    ) -> dict:
        """Fetch full detail for a single filing by filing number.

        Navigates to the Filing Number Search path and retrieves all
        available fields (debtor, secured party, collateral, dates, etc.).

        Note: The detail page may also require Turnstile solving.
        """
        should_close = page is None
        page = page or await self.new_page()

        try:
            await self._navigate_to_name_search(page)

            # Switch to Filing Number Search
            await page.goto(
                self.menu_url,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await page.wait_for_timeout(3000)

            fn_link = page.locator(self.FILING_NUMBER_SEARCH_LINK).first
            if await fn_link.count() > 0:
                await fn_link.click()
                await page.wait_for_timeout(5000)
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=15000
                    )
                except Exception:
                    pass

            # Fill filing number
            fn_input = page.locator(
                'input[name*="FilingNumber"], input[id*="FilingNumber"]'
            ).first
            if await fn_input.count() > 0:
                await fn_input.fill(filing_number)
                await page.wait_for_timeout(300)

            # Try Turnstile solving if it exists on this page
            try:
                await self._solve_and_inject_turnstile(page)
            except (ValueError, RuntimeError, TimeoutError) as exc:
                logger.warning(
                    "Turnstile solve failed for detail page: %s", exc
                )

            # Submit
            search_btn = page.locator(
                'input[value*="Search"], input[value*="Continue"]'
            ).first
            if await search_btn.count() > 0:
                await search_btn.click()
                await page.wait_for_timeout(5000)
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=15000
                    )
                except Exception:
                    pass

            return await self._extract_detail_fields(page)

        finally:
            if should_close:
                await page.close()

    async def _extract_detail_fields(self, page: Page) -> dict:
        """Extract all visible fields from a filing detail page.

        Maryland's detail page uses ASP.NET key-value label/value rows.
        This method harvests all of them and also captures the raw body
        text as a fallback.
        """
        fields: dict[str, str] = {}

        # Extract from table rows with alternating label/value cells
        rows = page.locator("tr")
        row_count = await rows.count()
        for i in range(min(row_count, 100)):
            label_cell = rows.nth(i).locator("td:first-child, th").first
            value_cell = rows.nth(i).locator("td:last-child").first
            if await label_cell.count() > 0 and await value_cell.count() > 0:
                key = (
                    (await label_cell.text_content() or "")
                    .strip()
                    .rstrip(":")
                    .lower()
                    .replace(" ", "_")
                )
                val = (await value_cell.text_content() or "").strip()
                if key and len(key) < 80:
                    fields[key] = val[:2000]

        # Also scan definition-list patterns
        dts = page.locator("dt")
        dt_count = await dts.count()
        for i in range(min(dt_count, 50)):
            label_text = (await dts.nth(i).text_content() or "").strip()
            if label_text:
                key = label_text.rstrip(":").lower().replace(" ", "_")
                dd = dts.nth(i).locator(".. dd").first
                if await dd.count() > 0:
                    val = (await dd.text_content() or "").strip()
                    if len(key) < 80:
                        fields[key] = val[:2000]

        # Raw body fallback
        body_text = (await page.text_content("body")) or ""
        fields["_raw_body_text"] = body_text[:5000]

        # Extract collateral description if present
        for keyword in ("collateral", "cover", "description", "property"):
            for key, val in fields.items():
                if keyword in key and len(val) > 20:
                    fields["collateral_description"] = val
                    break

        return fields

    # ── Status check ──────────────────────────────────────────────────────

    async def check_status(
        self,
        filing_number: str,
        page: Optional[Page] = None,
    ) -> str:
        """Determine the current status of a UCC filing.

        Returns one of: ``"active"``, ``"terminated"``, ``"lapsed"``,
        ``"amended"``, ``"unknown"``.
        """
        detail = await self.get_filing_detail(filing_number, page)
        status_text = (
            detail.get("status", "")
            + " "
            + detail.get("filing_status", "")
            + " "
            + detail.get("_raw_body_text", "")
        ).lower()

        if "terminat" in status_text:
            return "terminated"
        if "lapsed" in status_text:
            return "lapsed"
        if "continu" in status_text or "amend" in status_text:
            return "amended"
        if "active" in status_text or "filed" in status_text:
            return "active"
        return "unknown"

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _get_funder_first_words() -> list[str]:
        """Return unique first words of Tier 1+2 MCA funder legal names.

        These words are used as secured-party search terms.
        """
        try:
            from pipeline.classifier import MCAClassifier

            classifier = MCAClassifier()
            words: set[str] = set()
            for funder in classifier._funders:
                if funder.get("tier") in (1, 2):
                    name = funder["legal_name"].strip()
                    first_word = name.split()[0] if name.split() else name
                    if len(first_word) >= 3:
                        words.add(first_word.upper())
            return sorted(words)
        except Exception as exc:
            logger.warning("Could not load funder names: %s", exc)
            return []

    @staticmethod
    def _filing_in_date_range(
        filing: dict,
        start_date: datetime,
        end_date: datetime,
    ) -> bool:
        """Check if a filing's date falls within *start_date*..*end_date*.

        If the filing has no date or the date cannot be parsed, it is
        included (assume we'd rather over-include than miss a lead).
        """
        filing_date_str = filing.get("filing_date", "")
        if not filing_date_str:
            return True

        try:
            from dateutil import parser as date_parser

            fd = date_parser.parse(filing_date_str, fuzzy=True)
            return start_date <= fd <= end_date
        except Exception:
            return True
