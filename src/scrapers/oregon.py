"""Oregon UCC Scraper — Secretary of State Secured Transaction Registry.

Portal: https://secure.sos.state.or.us/ucc/searchHome.action
Type: Java/Struts (POST-based, no JS required)

FLOW:
  1. GET /ucc/searchHome.action → extract CSRF token
  2. POST /ucc/nsSearch.action with MCA-industry debtor search → results table
  3. For each lien number, POST /ucc/doLienNumberWebSearch.action → detail
     page with debtor name, secured party name, collateral description
  4. Yield standardized dicts → classifier pipeline scores them

DEBTOR SEARCH: Oregon matches "begins with" organization name.
We search 26 common MCA-industry prefixes (RESTAURANT, TRUCKING, etc.)
Each search returns up to 500 results from the last 2 years.
Total: ~500-2000 unique filings per day from Oregon alone.
"""

import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from scrapers.base import BaseStateScraper
from scrapers.registry import register_scraper

OR_BASE = "https://secure.sos.state.or.us"
SEARCH_URL = f"{OR_BASE}/ucc/searchHome.action"
NS_SEARCH_URL = f"{OR_BASE}/ucc/nsSearch.action"
DETAIL_URL = f"{OR_BASE}/ucc/doLienNumberWebSearch.action"

MCA_DEBTOR_PREFIXES = [
    "RESTAURANT", "DINER", "CAFE", "PIZZA", "GRILL", "SUSHI", "BAKERY",
    "TRUCKING", "TRANSPORT", "LOGISTICS", "FREIGHT", "CARRIER", "DISPATCH",
    "CONSTRUCTION", "CONTRACTOR", "BUILDER", "RENOVATION", "ROOFING", "PLUMBING",
    "MEDICAL", "HEALTHCARE", "DENTAL", "PHARMACY", "CLINIC", "CHIROPRACTIC",
    "AUTO REPAIR", "AUTOMOTIVE", "COLLISION", "TIRE", "CAR WASH",
    "HOTEL", "MOTEL", "INN", "LODGING", "HOSPITALITY",
    "SALON", "BARBER", "BEAUTY", "SPA", "NAIL",
    "RETAIL", "MARKET", "GROCERY", "LIQUOR", "CONVENIENCE",
    "MANUFACTURING", "DISTRIBUTION", "WHOLESALE",
    "LAUNDRY", "DRY CLEAN", "CLEANERS",
    "GYM", "FITNESS", "LANDSCAPING", "JANITORIAL", "SECURITY",
    "TAXI", "LIMOUSINE", "TOWING",
    "STAFFING", "TEMP", "CONSULTING", "TECHNOLOGY",
]


@register_scraper("OR")
class OregonScraper(BaseStateScraper):
    state = "OR"
    state_name = "Oregon"
    base_url = SEARCH_URL
    requests_per_second: float = 1.0

    def __init__(self, headless: bool = True, proxy: Optional[str] = None):
        super().__init__(headless=headless, proxy=proxy)
        self._client: Optional[httpx.Client] = None
        self._csrf: str = ""

    async def start(self):
        self._client = httpx.Client(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Origin": OR_BASE,
                "Referer": SEARCH_URL,
            },
            timeout=30,
            follow_redirects=True,
        )
        self._refresh_csrf()

    async def stop(self):
        if self._client:
            self._client.close()
            self._client = None

    async def new_page(self):
        raise NotImplementedError("Oregon uses HTTP POST, not Playwright")

    def _refresh_csrf(self):
        for attempt in range(3):
            try:
                r = self._client.get(SEARCH_URL)
                r.raise_for_status()
                inp = BeautifulSoup(r.text, "html.parser").find("input", {"name": "CSRFToken"})
                self._csrf = inp.get("value", "") if inp else ""
                return
            except Exception:
                if attempt == 2:
                    raise
                import time
                time.sleep(2 ** attempt)

    def _post_with_retry(self, url: str, data: dict) -> "httpx.Response":
        """POST with exponential backoff on connection errors."""
        import time
        last_err = None
        for attempt in range(3):
            try:
                r = self._client.post(url, data=data)
                r.raise_for_status()
                return r
            except httpx.ConnectError as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    self._refresh_csrf()
                    data["CSRFToken"] = self._csrf
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt < 2:
                    time.sleep(3 ** attempt)
                    continue
                raise
        raise last_err

    # ── MAIN SEARCH LOOP ────────────────────────────────────────────

    async def search_by_date_range(self, start_date: datetime, end_date: datetime, page=None):
        """Search Oregon UCC using TWO strategies:

        Strategy A: Secured Party search for all known MCA funders (first word)
        Strategy B: Debtor search for MCA-industry businesses

        Each matched lien gets a detail lookup to extract secured party.
        """
        start_str = start_date.strftime("%m/%d/%Y")
        end_str = end_date.strftime("%m/%d/%Y")
        seen: set[str] = set()

        # ── Strategy A: Secured Party search by MCA funder ──────────
        funder_words = self._get_funder_first_words()
        for word in funder_words:
            results = self._search_secured(word, start_str, end_str)
            for r in results:
                fn = r["lien_number"]
                if fn in seen:
                    continue
                seen.add(fn)
                detail = self._get_detail(fn)
                if detail:
                    # SP search: table shows funder info; detail gives us real debtor
                    r["debtor_name"] = detail.get("debtor_name", r["debtor_name"])
                    r["secured_party_name"] = detail.get("secured_party_name", r["debtor_name"])
                yield r

        # ── Strategy B: Debtor search for MCA-industry businesses ───
        for prefix in MCA_DEBTOR_PREFIXES:
            results = self._search_debtor(prefix, start_str, end_str)
            for r in results:
                fn = r["lien_number"]
                if fn in seen:
                    continue
                seen.add(fn)
                detail = self._get_detail(fn)
                if detail:
                    # Debtor search: table shows debtor; detail gives us SP
                    r["secured_party_name"] = detail.get("secured_party_name", "")
                yield r

    def _search_debtor(self, org_name: str, start_date: str, end_date: str) -> list[dict]:
        """POST debtor search, return filing dicts from results table.

        Search results contain: Name, Address, Lien Number, Lien Type,
        Filed (date), Terminate Date, Lapse Date.

        Does NOT contain secured party — we only get that from detail.
        """
        data = {
            "nonStandardEntityType": "Organization",
            "nonStandardSearchOrgName": org_name,
            "assocNameType": "Search by Debtor",
            "lapseStatus": "statusAll",
            "beginningDate": start_date,
            "endingDate": end_date,
            "CSRFToken": self._csrf,
        }

        try:
            r = self._post_with_retry(NS_SEARCH_URL, data)
        except Exception:
            self._refresh_csrf()
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        self._csrf = _extract_csrf(soup)
        return self._parse_results_table(soup)

    def _search_secured(self, org_name: str, start_date: str, end_date: str) -> list[dict]:
        """POST secured party search, return filing dicts from results table."""
        data = {
            "nonStandardEntityType": "Organization",
            "nonStandardSearchOrgName": org_name,
            "assocNameType": "Search by Secured Party",
            "lapseStatus": "statusAll",
            "beginningDate": start_date,
            "endingDate": end_date,
            "CSRFToken": self._csrf,
        }

        try:
            r = self._post_with_retry(NS_SEARCH_URL, data)
        except Exception:
            self._refresh_csrf()
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        self._csrf = _extract_csrf(soup)
        return self._parse_results_table(soup)

    @staticmethod
    def _parse_results_table(soup: BeautifulSoup) -> list[dict]:
        """Parse securedTable results (same format for debtor + SP search)."""
        if "No file entries" in soup.get_text():
            return []

        table = soup.find("table", id="securedTable")
        if not table:
            return []

        results = []
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            texts = [c.get_text(strip=True) for c in cells]
            lien_num = ""
            link = row.find("a", href=lambda h: h and "generateFileNumberSearchResult" in str(h))
            if link:
                m = re.search(r"generateFileNumberSearchResult\((\d+)\)", link.get("href", ""))
                if m:
                    lien_num = m.group(1)

            results.append({
                "state": "OR",
                "lien_number": lien_num,
                "filing_number": lien_num,
                "debtor_name": texts[0] if texts else "",
                "debtor_address": texts[1] if len(texts) > 1 else "",
                "filing_date": texts[4] if len(texts) > 4 else "",
                "lapse_date": texts[6] if len(texts) > 6 else "",
                "status": "unknown",
                "secured_party_name": "",
                "collateral_description": "",
                "source": "oregon-sos",
            })

        return results

    @staticmethod
    def _get_funder_first_words() -> list[str]:
        """Unique first words from Tier 1+2 MCA funders."""
        from pipeline.classifier import MCAClassifier
        c = MCAClassifier()
        words: set[str] = set()
        for f in c._funders:
            if f.get("tier") in (1, 2):
                name = f["legal_name"].strip()
                first = name.split()[0] if name.split() else name
                if len(first) >= 3:
                    words.add(first.upper())
        return sorted(words)

    def _get_detail(self, lien_number: str) -> Optional[dict]:
        """POST lien number search → extract filing detail via CSS classes."""
        data = {
            "inputLienNumberStr": lien_number,
            "CSRFToken": self._csrf,
        }

        try:
            r = self._post_with_retry(DETAIL_URL, data)
        except Exception:
            self._refresh_csrf()
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        self._csrf = _extract_csrf(soup)

        # Oregon detail page uses CSS classes: dName, spName, dAddress, spAddress
        debtor_name = ""
        secured_party_name = ""
        filing_date = ""

        # Debtor name: <td class="dName">
        dn_el = soup.find("td", class_="dName")
        if dn_el:
            debtor_name = dn_el.get_text(strip=True)

        # Secured party name: <td class="spName">
        sp_el = soup.find("td", class_="spName")
        if sp_el:
            secured_party_name = sp_el.get_text(strip=True)

        # Filing date: in the header row, third <th> or <td class="borderedTd">
        for cell in soup.find_all(["th", "td"]):
            if cell.get_text(strip=True) == "Filing Date":
                # Next td in the same row is the date
                row = cell.find_parent("tr")
                if row:
                    tds = row.find_all("td", class_="borderedTd")
                    if len(tds) >= 3:
                        filing_date = tds[2].get_text(strip=True)
                break

        # Fallback: parse from text
        body = soup.get_text("\n", strip=True)
        if not debtor_name:
            dns = soup.find_all("td", class_="dName")
            if dns:
                debtor_name = dns[0].get_text(strip=True)
        if not secured_party_name:
            sps = soup.find_all("td", class_="spName")
            if sps:
                secured_party_name = sps[0].get_text(strip=True)

        if not debtor_name:
            return None

        return {
            "state": "OR",
            "filing_number": lien_number,
            "filing_date": filing_date,
            "debtor_name": debtor_name,
            "secured_party_name": secured_party_name,
            "status": "unknown",
            "collateral_description": "",
            "detail_url": f"{OR_BASE}/ucc/doLienNumberWebSearch.action",
            "source": "oregon-sos",
        }

    async def get_filing_detail(self, filing_number: str, page=None) -> dict:
        detail = self._get_detail(filing_number)
        return detail or {"filing_number": filing_number, "_error": "Not found"}

    async def check_status(self, filing_number: str, page=None) -> str:
        detail = await self.get_filing_detail(filing_number)
        text = detail.get("_raw_body_text", "").lower()
        if "terminat" in text:
            return "terminated"
        elif "lapsed" in text:
            return "lapsed"
        elif "active" in text or "filed" in text:
            return "active"
        return "unknown"

    async def health_check(self) -> dict:
        try:
            r = httpx.get(SEARCH_URL, timeout=15)
            return {
                "ok": r.status_code == 200,
                "status_code": r.status_code,
                "url": SEARCH_URL,
                "error": None if r.status_code == 200 else f"HTTP {r.status_code}",
            }
        except Exception as e:
            return {"ok": False, "status_code": 0, "url": SEARCH_URL, "error": str(e)[:200]}


def _extract_csrf(soup: BeautifulSoup) -> str:
    inp = soup.find("input", {"name": "CSRFToken"})
    return inp.get("value", "") if inp else ""
