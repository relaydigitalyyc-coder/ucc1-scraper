"""Phone number enrichment for MCA leads — skip-trace phone, website, and
industry data by trying multiple public-data strategies in priority order.

Strategies (in order):
  1. Google Places API — best results, free monthly credit
  2. OpenCorporates API — free business registry lookup
  3. Web search fallback — parse phone from instant-answer snippet
  4. LLM-based extraction — optional Claude/OpenAI call

Every result is cached in a local SQLite database so the same business is
never re-queried across runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from models.lead import MCALead

__all__ = [
    "LeadEnricher",
    "PhoneParsingError",
    "EnrichmentCache",
    "GooglePlacesEnricher",
    "OpenCorporatesEnricher",
    "WebSearchEnricher",
    "LLMEnricher",
]

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────────────────────────────

DEFAULT_CACHE_PATH = Path("data/enrichment_cache.db")

# Maximum requests per second to Google Places (free-tier quota)
GOOGLE_RATE_LIMIT = 10.0

# Exponential-backoff base (seconds)
BACKOFF_BASE = 1.0
BACKOFF_MAX = 60.0
BACKOFF_FACTOR = 2.0

# ──────────────────────────────────────────────────────────────────────
#  Phone number parsing
# ──────────────────────────────────────────────────────────────────────


class PhoneParsingError(ValueError):
    """Raised when a phone number cannot be parsed or validated."""


# Liberal regexes to extract candidate phone numbers from raw text
PHONE_PATTERNS = [
    # (555) 123-4567
    re.compile(r"\((\d{3})\)\s*(\d{3})[-.](\d{4})"),
    # 555-123-4567 or 555.123.4567
    re.compile(r"(?<!\d)(\d{3})[-.](\d{3})[-.](\d{4})(?!\d)"),
    # +1 555 123 4567 or +1 (555) 123-4567
    re.compile(r"\+\s*1\s*\(?(\d{3})\)?[\s.-]*(\d{3})[\s.-]*(\d{4})"),
    # 15551234567 (no formatting)
    re.compile(r"(?<!\d)1(\d{3})(\d{3})(\d{4})(?!\d)"),
    # International without +1: 44 20 7946 0958 etc.
    re.compile(r"\+\s*(\d{1,3})\s*\(?(\d{1,4})\)?[\s.-]*(\d{1,4})[\s.-]*(\d{1,10})"),
]


def find_phone_numbers(text: str) -> list[str]:
    """Extract candidate phone numbers from free-form text using regex.

    Returns a list of E.164-formatted phone numbers (e.g. +15551234567),
    deduplicated and preserving insertion order.
    """
    seen: set[str] = set()
    results: list[str] = []

    for pattern in PHONE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groups()
            if len(groups) == 3:
                candidate = f"+1{groups[0]}{groups[1]}{groups[2]}"
            elif len(groups) == 4:
                candidate = f"+{groups[0]}{groups[1]}{groups[2]}{groups[3]}"
            else:
                continue

            if candidate not in seen:
                seen.add(candidate)
                results.append(candidate)

    return results


def validate_phone(e164: str) -> bool:
    """Validate a phone number using the phonenumbers library.

    Uses ``is_possible_number`` (a permissive check that accepts most
    well-formed numbers) rather than ``is_valid_number`` (which requires
    a real number from the carrier database).  Falls back to a basic
    length/format check if the library is unavailable.
    """
    try:
        import phonenumbers

        parsed = phonenumbers.parse(e164, None)
        return phonenumbers.is_possible_number(parsed)
    except ImportError:
        # phonenumbers not installed — basic sanity check
        return len(e164) >= 10 and len(e164) <= 16 and e164.startswith("+")
    except Exception:
        return False


def normalize_phone(raw: str) -> str | None:
    """Parse a raw phone number string and return E.164 format, or None."""
    # If already in E.164-ish form, clean it up
    cleaned = re.sub(r"[^\d+]", "", raw)
    if cleaned.startswith("+"):
        e164 = cleaned
    elif cleaned.startswith("1") and len(cleaned) == 11:
        e164 = "+" + cleaned
    elif len(cleaned) == 10:
        e164 = "+1" + cleaned
    else:
        # Try regex extraction
        found = find_phone_numbers(raw)
        if found:
            # Return the longest match (often the most complete)
            return max(found, key=len)
        return None

    if validate_phone(e164):
        return e164
    return None


def format_for_display(e164: str) -> str:
    """Convert E.164 to a human-readable US format (555) 123-4567."""
    try:
        import phonenumbers

        parsed = phonenumbers.parse(e164, None)
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
    except ImportError:
        if e164.startswith("+1") and len(e164) == 12:
            return f"({e164[2:5]}) {e164[5:8]}-{e164[8:]}"
        return e164


# ──────────────────────────────────────────────────────────────────────
#  SQLite cache
# ──────────────────────────────────────────────────────────────────────


class EnrichmentCache:
    """Local SQLite cache for enrichment results so the same business is
    never re-queried across runs.

    Schema
    ------
    enrichment_cache (
        business_key TEXT PRIMARY KEY,  -- normalized name|city|state
        data          TEXT NOT NULL,      -- JSON blob of enrichment result
        source        TEXT NOT NULL,      -- strategy that produced the result
        queried_at    TEXT NOT NULL       -- ISO timestamp
    )
    """

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS enrichment_cache (
            business_key TEXT PRIMARY KEY,
            data         TEXT NOT NULL,
            source       TEXT NOT NULL,
            queried_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cache_queried
            ON enrichment_cache(queried_at);
    """

    def __init__(self, db_path: str | Path = DEFAULT_CACHE_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """Open the database connection and ensure the table exists."""
        if self._conn is not None:
            return
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get(self, business_key: str) -> dict[str, Any] | None:
        """Return cached enrichment data, or *None* if not found / expired.

        Results older than 90 days are discarded so stale data doesn't
        persist forever.
        """
        self.open()
        cursor = self._conn.execute(
            "SELECT data, queried_at FROM enrichment_cache WHERE business_key = ?",
            (business_key,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        queried = datetime.fromisoformat(row["queried_at"])
        if datetime.now() - queried > timedelta(days=90):
            self._conn.execute(
                "DELETE FROM enrichment_cache WHERE business_key = ?",
                (business_key,),
            )
            self._conn.commit()
            return None

        return json.loads(row["data"])

    def set(self, business_key: str, data: dict[str, Any], source: str) -> None:
        """Store enrichment data for a business key."""
        self.open()
        self._conn.execute(
            """INSERT OR REPLACE INTO enrichment_cache
               (business_key, data, source, queried_at)
               VALUES (?, ?, ?, ?)""",
            (
                business_key,
                json.dumps(data),
                source,
                datetime.now().isoformat(),
            ),
        )
        self._conn.commit()

    def stats(self) -> dict[str, Any]:
        """Return cache statistics."""
        self.open()
        cursor = self._conn.execute(
            "SELECT source, COUNT(*) as count FROM enrichment_cache GROUP BY source"
        )
        rows = cursor.fetchall()
        return {row["source"]: row["count"] for row in rows}

    def clear_expired(self) -> int:
        """Remove entries older than 90 days. Returns count removed."""
        self.open()
        cutoff = (datetime.now() - timedelta(days=90)).isoformat()
        cursor = self._conn.execute(
            "DELETE FROM enrichment_cache WHERE queried_at < ?", (cutoff,)
        )
        self._conn.commit()
        return cursor.rowcount

    def __enter__(self) -> "EnrichmentCache":
        self.open()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ──────────────────────────────────────────────────────────────────────
#  Enrichment strategies
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EnrichmentResult:
    """Result from a single enrichment strategy."""

    phone: str | None = None
    website: str | None = None
    email: str | None = None
    industry: str | None = None
    years_in_business: float | None = None
    address: str | None = None
    source: str = "unknown"


def _make_business_key(name: str, city: str, state: str) -> str:
    """Normalized cache key: upper-case name minus common suffixes."""
    import re as _re

    key = name.upper().strip()
    for suffix in [
        ", LLC", " LLC", ", L.L.C.", " L.L.C.", ", INC", " INC",
        ", INC.", " INC.", ", CORP", " CORP", ", CORPORATION", " CORPORATION",
        ", LP", " LP", ", L.P.", " L.P.", ", THE", " THE ", ", CO", " CO",
    ]:
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    key = _re.sub(r"[^\w\s]", "", key)
    key = _re.sub(r"\s+", " ", key).strip()
    return f"{key}|{city.upper().strip()}|{state.upper().strip()}"


class GooglePlacesEnricher:
    """Search Google Places for a business and return phone, website, etc.

    Uses the Places Text Search endpoint (free tier includes $200/mo credit).
    Rate-limited to 10 QPS on the free tier.
    """

    BASE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
    DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
    TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=15)
        self._sem = asyncio.Semaphore(int(GOOGLE_RATE_LIMIT))

    async def enrich(self, name: str, city: str, state: str) -> EnrichmentResult | None:
        """Look up a business on Google Places.

        Uses ``findplacefromtext`` with the business name + city + state,
        then calls Place Details on the first match to retrieve phone and
        website.
        """
        query = f"{name} {city} {state}"
        fields = "place_id,name,formatted_address,types"

        async with self._sem:
            params = {
                "input": query,
                "inputtype": "textquery",
                "fields": fields,
                "key": self._api_key,
            }
            try:
                resp = await self._client.get(self.BASE_URL, params=params)
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPStatusError as exc:
                logger.warning("Google Places HTTP error: %s", exc)
                return None
            except httpx.TimeoutException:
                logger.warning("Google Places request timed out for %s", query)
                return None
            except json.JSONDecodeError:
                logger.warning("Google Places returned non-JSON for %s", query)
                return None

        candidates = body.get("candidates", [])
        if not candidates:
            # Fall back to text search which is more lenient
            return await self._text_search_fallback(name, city, state)

        place_id = candidates[0].get("place_id")
        if not place_id:
            return None

        return await self._get_place_details(place_id)

    async def _text_search_fallback(self, name: str, city: str, state: str) -> EnrichmentResult | None:
        """Fallback: use the textsearch endpoint for broader matching."""
        query = f"{name} {city} {state}"
        async with self._sem:
            params = {
                "query": query,
                "key": self._api_key,
            }
            try:
                resp = await self._client.get(self.TEXT_SEARCH_URL, params=params)
                resp.raise_for_status()
                body = resp.json()
            except (httpx.HTTPStatusError, httpx.TimeoutException, json.JSONDecodeError):
                return None

        results = body.get("results", [])
        if not results:
            return None

        place_id = results[0].get("place_id")
        if not place_id:
            return EnrichmentResult(
                industry=self._classify_types(results[0].get("types", [])),
                source="google_places",
            )

        return await self._get_place_details(place_id)

    async def _get_place_details(self, place_id: str) -> EnrichmentResult | None:
        """Retrieve phone, website, etc. from Place Details."""
        fields = "place_id,name,formatted_address,formatted_phone_number,"
        fields += "international_phone_number,website,types,url"

        async with self._sem:
            params = {
                "place_id": place_id,
                "fields": fields,
                "key": self._api_key,
            }
            try:
                resp = await self._client.get(self.DETAILS_URL, params=params)
                resp.raise_for_status()
                body = resp.json()
            except (httpx.HTTPStatusError, httpx.TimeoutException, json.JSONDecodeError):
                return None

        result = body.get("result", {})
        raw_phone = result.get("international_phone_number") or result.get("formatted_phone_number")
        website = result.get("website")
        industry = self._classify_types(result.get("types", []))

        phone = normalize_phone(raw_phone) if raw_phone else None

        return EnrichmentResult(
            phone=phone,
            website=website,
            industry=industry,
            address=result.get("formatted_address"),
            source="google_places",
        )

    @staticmethod
    def _classify_types(types: list[str]) -> str | None:
        """Map Google Place types to industry categories."""
        type_to_industry = {
            "restaurant": "Food & Dining",
            "food": "Food & Dining",
            "cafe": "Food & Dining",
            "bakery": "Food & Dining",
            "bar": "Food & Dining",
            "night_club": "Food & Dining",
            "meal_delivery": "Food & Dining",
            "meal_takeaway": "Food & Dining",
            "store": "Retail",
            "retail": "Retail",
            "shopping_mall": "Retail",
            "clothing_store": "Retail",
            "electronics_store": "Retail",
            "grocery_or_supermarket": "Retail",
            "liquor_store": "Retail",
            "hardware_store": "Retail",
            "car_dealer": "Automotive",
            "car_rental": "Automotive",
            "car_repair": "Automotive",
            "car_wash": "Automotive",
            "auto_parts_store": "Automotive",
            "gas_station": "Automotive",
            "doctor": "Healthcare",
            "dentist": "Healthcare",
            "hospital": "Healthcare",
            "pharmacy": "Healthcare",
            "health": "Healthcare",
            "physiotherapist": "Healthcare",
            "lodging": "Hospitality",
            "hotel": "Hospitality",
            "motel": "Hospitality",
            "travel_agency": "Hospitality",
            "campground": "Hospitality",
            "construction": "Construction",
            "plumber": "Construction",
            "electrician": "Construction",
            "roofing_contractor": "Construction",
            "general_contractor": "Construction",
            "moving_company": "Transportation",
            "transit_station": "Transportation",
            "airport": "Transportation",
            "taxi_stand": "Transportation",
            "train_station": "Transportation",
            "warehouse": "Logistics & Distribution",
            "storage": "Logistics & Distribution",
            "laundry": "Personal Services",
            "beauty_salon": "Personal Services",
            "hair_care": "Personal Services",
            "spa": "Personal Services",
            "gym": "Fitness",
            "fitness_center": "Fitness",
            "lawyer": "Professional Services",
            "accounting": "Professional Services",
            "insurance_agency": "Professional Services",
            "real_estate_agency": "Professional Services",
            "finance": "Financial Services",
            "bank": "Financial Services",
            "manufacturing": "Manufacturing",
            "school": "Education",
            "university": "Education",
        }
        for t in types:
            industry = type_to_industry.get(t)
            if industry:
                return industry
        return None

    async def close(self) -> None:
        await self._client.aclose()


class OpenCorporatesEnricher:
    """Look up a business on OpenCorporates (free business registry).

    The free tier is rate-limited and returns basic company info including
    registered address. Phone numbers are rarely present, but jurisdiction
    data is useful for industry classification.
    """

    BASE_URL = "https://api.opencorporates.com/companies/search"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=15)
        # Free tier allows ~1 request per 2 seconds
        self._last_request = 0.0

    async def enrich(self, name: str, city: str, state: str) -> EnrichmentResult | None:
        """Search OpenCorporates for the business.

        Unfortunately the free tier does not include phone numbers, but
        we can derive jurisdiction-based industry classification and
        sometimes find a registered address that helps other strategies.
        """
        # Rate limit: 1 request per 2 seconds
        now = asyncio.get_event_loop().time()
        delay = 2.0 - (now - self._last_request)
        if delay > 0:
            await asyncio.sleep(delay)
        self._last_request = asyncio.get_event_loop().time()

        state_code = state.lower()
        # OpenCorporates uses ISO jurisdiction codes
        params = {
            "q": name,
            "jurisdiction_code": f"us_{state_code}",
            "per_page": 1,
        }

        try:
            resp = await self._client.get(self.BASE_URL, params=params)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPStatusError, httpx.TimeoutException, json.JSONDecodeError):
            return None

        companies = body.get("results", {}).get("companies", [])
        if not companies:
            return None

        company = companies[0].get("company", {})
        registered_address = company.get("registered_address", {})

        # OpenCorporates sometimes includes industry/NAICS info
        industry = None
        industry_codes = company.get("industry_codes", [])
        if industry_codes:
            for code in industry_codes:
                desc = code.get("description", "")
                if desc:
                    industry = desc
                    break

        # Company registry often includes incorporation date
        years_in_business = None
        incorporation_date = company.get("incorporation_date")
        if incorporation_date:
            try:
                inc_date = datetime.strptime(incorporation_date, "%Y-%m-%d")
                years_in_business = round((datetime.now() - inc_date).days / 365.25, 1)
            except (ValueError, TypeError):
                pass

        address_parts = [
            registered_address.get("street_address", ""),
            registered_address.get("locality", ""),
            registered_address.get("region", ""),
            registered_address.get("postal_code", ""),
        ]
        address = ", ".join(p for p in address_parts if p) or None

        return EnrichmentResult(
            industry=industry,
            years_in_business=years_in_business,
            address=address,
            source="opencorporates",
        )

    async def close(self) -> None:
        await self._client.aclose()


class WebSearchEnricher:
    """Search the web for business contact info using DuckDuckGo instant
    answers or direct Google search snippets.

    This strategy parses phone numbers from search result snippets and
    structured data on business-directory pages.
    """

    # Known phone directories that often have clean data
    DIRECTORY_DOMAINS = [
        "yellowpages.com",
        "whitepages.com",
        "manta.com",
        "bbb.org",
        "superpages.com",
        "merchantcircle.com",
        "mapquest.com",
        "cylex.us.com",
        "hotfrog.com",
    ]

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    async def enrich(self, name: str, city: str, state: str) -> EnrichmentResult | None:
        """Search for ``{name} {city} {state} phone`` and parse results."""
        return await self._search_directories(name, city, state)

    async def _search_directories(self, name: str, city: str, state: str) -> EnrichmentResult | None:
        """Try DuckDuckGo instant answer and fetch directory pages."""
        query = f"{name} {city} {state} phone number"
        search_url = f"https://lite.duckduckgo.com/lite/"
        params = {"q": query}

        try:
            resp = await self._client.get(search_url, params=params)
            resp.raise_for_status()
            html = resp.text
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            return None

        # Parse phone numbers from the response HTML
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator=" ", strip=True)

        phones = find_phone_numbers(text)
        validated = []
        for p in phones:
            if validate_phone(p):
                validated.append(p)

        phone = validated[0] if validated else None

        # Try to extract website from result links
        website = None
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if href.startswith("http") and "duckduckgo" not in href:
                website = href
                break

        # Sometimes DuckDuckGo refers via a redirect URL
        if website and "duckduckgo.com" in website:
            website = None

        if phone or website:
            return EnrichmentResult(phone=phone, website=website, source="web_search")

        return None

    async def close(self) -> None:
        await self._client.aclose()


class LLMEnricher:
    """Use an LLM (Claude or OpenAI) to search the web and extract
    business phone numbers.

    This is the last-resort strategy: it's slow, potentially expensive,
    and only used when no other strategy found a result.
    """

    # System prompt for the LLM
    SYSTEM_PROMPT = """You are a business research assistant. Given a business name, city, and state,
search the web for their contact information. Return ONLY a JSON object with these fields:

{
  "phone": "best phone number in E.164 format (e.g. +15551234567) or null",
  "website": "business website URL or null",
  "email": "business email or null",
  "industry": "industry category or null",
  "years_in_business": "estimated years in business as number or null"
}

If you cannot find a phone number, return null for all fields.
Do NOT include any text outside the JSON object."""

    OPENAI_MODEL = "gpt-4o-mini"
    CLAUDE_MODEL = "claude-sonnet-4-20250514"
    DEEPSEEK_MODEL = "deepseek-chat"

    def __init__(self, api_key: str | None = None, provider: str | None = None) -> None:
        """Initialize LLM enricher.

        Parameters
        ----------
        api_key : str, optional
            API key for the LLM provider. Defaults to DEEPSEEK_API_KEY,
            OPENAI_API_KEY, or ANTHROPIC_API_KEY from environment (in that order).
        provider : str, optional
            'deepseek', 'openai', or 'anthropic'. Auto-detected from env vars
            if not provided.
        """
        self._api_key = api_key
        self._provider = provider

        if not self._api_key:
            self._api_key = (os.environ.get("DEEPSEEK_API_KEY")
                          or os.environ.get("OPENAI_API_KEY")
                          or os.environ.get("ANTHROPIC_API_KEY"))
            if not self._api_key:
                self._available = False
                return

        if not self._provider:
            # Detect from env vars: deepseek > openai > anthropic
            if os.environ.get("DEEPSEEK_API_KEY"):
                self._provider = "deepseek"
            elif os.environ.get("OPENAI_API_KEY"):
                self._provider = "openai"
            elif self._api_key.startswith("sk-proj-") or self._api_key.startswith("sk-"):
                self._provider = "openai"
            elif self._api_key.startswith("sk-ant-"):
                self._provider = "anthropic"
            else:
                self._provider = "openai" if os.environ.get("OPENAI_API_KEY") else "anthropic"

        self._client = httpx.AsyncClient(timeout=30)
        self._available = True

    async def enrich(self, name: str, city: str, state: str) -> EnrichmentResult | None:
        """Ask the LLM to find contact info for this business."""
        if not self._available:
            return None

        if self._provider == "deepseek":
            return await self._call_deepseek(name, city, state)
        elif self._provider == "openai":
            return await self._call_openai(name, city, state)
        else:
            return await self._call_anthropic(name, city, state)

    async def _call_deepseek(self, name: str, city: str, state: str) -> EnrichmentResult | None:
        """Call DeepSeek Chat API (OpenAI-compatible, supports web search)."""
        prompt = f"Find contact information for this business:\n\nName: {name}\nCity: {city}\nState: {state}"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 300,
        }

        try:
            resp = await self._client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

            raw = data["choices"][0]["message"]["content"]
            cleaned = raw.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned.removeprefix("```json").removesuffix("```").strip()
            elif cleaned.startswith("```"):
                cleaned = cleaned.removeprefix("```").removesuffix("```").strip()

            result = json.loads(cleaned)
            if result.get("phone"):
                return EnrichmentResult(
                    phone=result["phone"],
                    website=result.get("website"),
                    email=result.get("email"),
                    industry=result.get("industry"),
                    years_in_business=result.get("years_in_business"),
                    source="deepseek",
                )
        except Exception as exc:
            logger.warning("DeepSeek enrichment failed for %s: %s", name, exc)

        return None

    async def _call_openai(self, name: str, city: str, state: str) -> EnrichmentResult | None:
        """Call OpenAI Chat Completions API."""
        prompt = f"Find contact information for this business:\n\nName: {name}\nCity: {city}\nState: {state}"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 300,
        }

        try:
            resp = await self._client.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            result = resp.json()
            content = result["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.warning("OpenAI enrichment failed for %s: %s", name, exc)
            return None

        return self._parse_llm_response(content)

    async def _call_anthropic(self, name: str, city: str, state: str) -> EnrichmentResult | None:
        """Call Anthropic Messages API."""
        prompt = f"Find contact information for this business:\n\nName: {name}\nCity: {city}\nState: {state}"

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.CLAUDE_MODEL,
            "max_tokens": 300,
            "system": self.SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": prompt},
            ],
        }

        try:
            resp = await self._client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            result = resp.json()
            content = result["content"][0]["text"]
        except Exception as exc:
            logger.warning("Anthropic enrichment failed for %s: %s", name, exc)
            return None

        return self._parse_llm_response(content)

    @staticmethod
    def _parse_llm_response(content: str) -> EnrichmentResult | None:
        """Parse JSON from the LLM response string."""
        # Try to extract JSON from markdown code fences first
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if json_match:
            content = json_match.group(1)

        content = content.strip()
        # Remove leading/trailing non-JSON if there's a JSON object
        obj_start = content.find("{")
        obj_end = content.rfind("}")
        if obj_start >= 0 and obj_end > obj_start:
            content = content[obj_start : obj_end + 1]

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Try to find any phone numbers in the text
            phones = find_phone_numbers(content)
            validated = [p for p in phones if validate_phone(p)]
            if validated:
                return EnrichmentResult(phone=validated[0], source="llm_extraction")
            return None

        phone = None
        if data.get("phone"):
            phone = normalize_phone(data["phone"])

        years = data.get("years_in_business")
        if years is not None:
            try:
                years = float(years)
            except (ValueError, TypeError):
                years = None

        return EnrichmentResult(
            phone=phone,
            website=data.get("website"),
            email=data.get("email"),
            industry=data.get("industry"),
            years_in_business=years,
            source="llm_extraction",
        )

    async def close(self) -> None:
        await self._client.aclose()


# ──────────────────────────────────────────────────────────────────────
#  Main LeadEnricher — orchestrates strategies
# ──────────────────────────────────────────────────────────────────────


class LeadEnricher:
    """Orchestrates phone-number / website / industry enrichment for MCA leads.

    Strategies are tried in priority order:
        1. Google Places API  (requires ``GOOGLE_PLACES_API_KEY`` env var)
        2. OpenCorporates API (free)
        3. Web search          (free — DuckDuckGo + directory pages)
        4. LLM extraction      (requires ``OPENAI_API_KEY`` or ``ANTHROPIC_API_KEY``)

    Results are cached in a local SQLite database.
    """

    def __init__(
        self,
        google_api_key: str | None = None,
        openai_api_key: str | None = None,
        cache_path: str | Path = DEFAULT_CACHE_PATH,
    ) -> None:
        self._google_api_key = google_api_key or os.environ.get("GOOGLE_PLACES_API_KEY")
        self._openai_api_key = openai_api_key or os.environ.get("OPENAI_API_KEY")

        self._cache = EnrichmentCache(cache_path)

        # Strategy instantiation is lazy (on first use)
        self._google: GooglePlacesEnricher | None = None
        self._opencorporates: OpenCorporatesEnricher | None = None
        self._web_search: WebSearchEnricher | None = None
        self._llm: LLMEnricher | None = None

    # ── Public API ────────────────────────────────────────────────────

    async def enrich(self, lead: MCALead) -> MCALead:
        """Enrich a single lead with contact information.

        The lead is returned with ``phone_number``, ``website``, ``email``,
        ``industry``, and ``years_in_business`` populated (or left as-is
        if no data is found).

        Parameters
        ----------
        lead : MCALead
            A scored lead with at least ``business_name``, ``business_city``,
            and ``business_state`` populated.

        Returns
        -------
        MCALead
            The same lead (new instance) with enrichment fields filled.
        """
        name = lead.business_name
        city = lead.business_city or ""
        state = lead.business_state or lead.source_filing.state

        result = await self.skip_trace(name, city, state)

        # Build a new lead with enrichment fields populated
        enriched = lead.model_copy(deep=True) if hasattr(lead, "model_copy") else lead

        if result.get("phone"):
            enriched.phone_number = result["phone"]
        if result.get("website"):
            enriched.website = result["website"]
        if result.get("email"):
            enriched.email = result["email"]
        if result.get("industry"):
            enriched.industry = result["industry"]
        if result.get("years_in_business") is not None:
            enriched.years_in_business = result["years_in_business"]

        if result.get("source"):
            enriched.notes.append(f"Enriched via {result['source']}")

        return enriched

    async def enrich_batch(
        self, leads: list[MCALead], max_concurrent: int = 5
    ) -> list[MCALead]:
        """Enrich multiple leads concurrently.

        Parameters
        ----------
        leads : list[MCALead]
            Leads to enrich.
        max_concurrent : int
            Maximum concurrent enrichment requests (default 5).

        Returns
        -------
        list[MCALead]
            Enriched leads in the same order as the input.
        """
        sem = asyncio.Semaphore(max_concurrent)

        async def _enrich_one(lead: MCALead) -> MCALead:
            async with sem:
                return await self.enrich(lead)

        tasks = [_enrich_one(lead) for lead in leads]
        return await asyncio.gather(*tasks)

    async def skip_trace(self, business_name: str, city: str, state: str) -> dict[str, Any]:
        """Perform a full skip trace for a business.

        Tries each strategy in priority order and returns the best result
        across all strategies.

        Parameters
        ----------
        business_name : str
        city : str
        state : str

        Returns
        -------
        dict[str, Any]
            A dictionary with keys ``phone``, ``website``, ``email``,
            ``industry``, ``years_in_business``, ``address``, ``source``.
        """
        key = _make_business_key(business_name, city, state)

        # Check cache first
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        # Try each strategy in priority order
        result = await self._try_strategies(business_name, city, state)

        # Cache the result (even if empty — avoids re-querying)
        cache_data: dict[str, Any] = {
            "phone": result.phone if result else None,
            "website": result.website if result else None,
            "email": result.email if result else None,
            "industry": result.industry if result else None,
            "years_in_business": result.years_in_business if result else None,
            "address": result.address if result else None,
            "source": result.source if result else "none",
        }
        self._cache.set(key, cache_data, source=cache_data["source"])

        return cache_data

    async def _try_strategies(
        self, name: str, city: str, state: str
    ) -> EnrichmentResult | None:
        """Try each strategy in order, returning the first successful result.

        Each strategy is called independently so that if one fails or returns
        empty data, the next can be tried.
        """
        best: EnrichmentResult | None = None

        # 1. Google Places
        if self._google_api_key:
            try:
                result = await self._ensure_google().enrich(name, city, state)
                if result:
                    best = self._merge_results(best, result)
                    # If we got a phone, we're done — Google is the most reliable source
                    if best and best.phone:
                        return best
            except Exception as exc:
                logger.warning("Google Places enrichment failed: %s", exc)

        # 2. OpenCorporates
        try:
            result = await self._ensure_opencorporates().enrich(name, city, state)
            if result:
                best = self._merge_results(best, result)
        except Exception as exc:
            logger.warning("OpenCorporates enrichment failed: %s", exc)

        # 3. Web search
        try:
            result = await self._ensure_web_search().enrich(name, city, state)
            if result:
                best = self._merge_results(best, result)
                if best and best.phone:
                    return best
        except Exception as exc:
            logger.warning("Web search enrichment failed: %s", exc)

        # 4. LLM (last resort — checks DEEPSEEK_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY)
        llm_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if llm_key:
            try:
                result = await self._ensure_llm().enrich(name, city, state)
                if result:
                    best = self._merge_results(best, result)
                    if best and best.phone:
                        return best
            except Exception as exc:
                logger.warning("LLM enrichment failed: %s", exc)

        return best

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _merge_results(
        current: EnrichmentResult | None, new: EnrichmentResult
    ) -> EnrichmentResult:
        """Merge a new result into an existing result, keeping the first
        non-None value for each field.
        """
        if current is None:
            return new
        return EnrichmentResult(
            phone=current.phone or new.phone,
            website=current.website or new.website,
            email=current.email or new.email,
            industry=current.industry or new.industry,
            years_in_business=current.years_in_business or new.years_in_business,
            address=current.address or new.address,
            source=current.source,
        )

    def _ensure_google(self) -> GooglePlacesEnricher:
        if self._google is None:
            self._google = GooglePlacesEnricher(self._google_api_key)
        return self._google

    def _ensure_opencorporates(self) -> OpenCorporatesEnricher:
        if self._opencorporates is None:
            self._opencorporates = OpenCorporatesEnricher()
        return self._opencorporates

    def _ensure_web_search(self) -> WebSearchEnricher:
        if self._web_search is None:
            self._web_search = WebSearchEnricher()
        return self._web_search

    def _ensure_llm(self) -> LLMEnricher:
        if self._llm is None:
            from pipeline.enricher import LLMEnricher
            import os
            llm_key = (os.environ.get("DEEPSEEK_API_KEY")
                      or os.environ.get("OPENAI_API_KEY")
                      or os.environ.get("ANTHROPIC_API_KEY"))
            self._llm = LLMEnricher(llm_key)
        return self._llm

    async def close(self) -> None:
        """Release all HTTP client connections and close the cache."""
        if self._google:
            await self._google.close()
        if self._opencorporates:
            await self._opencorporates.close()
        if self._web_search:
            await self._web_search.close()
        if self._llm:
            await self._llm.close()
        self._cache.close()

    def cache_stats(self) -> dict[str, Any]:
        """Return cache statistics (counts per source)."""
        return self._cache.stats()

    async def __aenter__(self) -> "LeadEnricher":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
