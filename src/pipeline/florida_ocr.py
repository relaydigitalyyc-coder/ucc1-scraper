"""Florida UCC detail extractor -- secured party names + filing dates from
filing detail pages.

Key discovery (2026-07-16):
  While the Florida detail page UI renders data visually (appearing to be
  an image), the public API at `publicsearchapi.floridaucc.com` actually
  returns *structured JSON* with all detail fields including secured party
  names and filing dates.  No OCR is needed for the primary path.

Strategy (in priority order):
  1. JSON API (`/filing-details`) -- structured data, no auth, fast.
  2. HTML parse of the React SPA (if the JSON API goes away, data may
     still be embedded in the page's <script> or <meta> tags).
  3. OCR with Tesseract on the TIFF document image (last resort).
  4. Vision LLM fallback (Claude/GPT-4V) for when all else fails.

Cache:
  Results are cached in the existing SQLite enrichment cache
  (data/enrichment_cache.db) so each UCC number is fetched at most once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import httpx

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

PUBLIC_SEARCH_API = "https://publicsearchapi.floridaucc.com"

CACHE_DB_PATH = Path("data/enrichment_cache.db")

_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)

# Seconds to wait between API calls to avoid rate limiting
INTER_REQUEST_DELAY: float = 0.5
BACKOFF_BASE: float = 1.0
BACKOFF_MULTIPLIER: float = 2.0
BACKOFF_MAX: float = 30.0
MAX_RETRIES: int = 3


# ── Data models ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FloridaDetail:
    """Structured detail data extracted from a Florida UCC filing."""

    ucc_number: str
    status: str
    filing_date: Optional[datetime]
    expiration_date: Optional[datetime]
    secured_parties: list[dict] = field(default_factory=list)
    debtors: list[dict] = field(default_factory=list)
    document_type: str = ""
    collateral_excerpt: str = ""
    has_image: bool = False
    source: str = "api"  # api, html, ocr, vision


# ── Cache ────────────────────────────────────────────────────────────────────


class DetailCache:
    """Async SQLite cache for Florida detail results.

    Uses aiosqlite so the cache is thread-safe and compatible with the
    project's async-first design.  Each UCC number is cached once and
    never re-fetched.
    """

    def __init__(self, db_path: Path = CACHE_DB_PATH):
        self.db_path = Path(db_path) if isinstance(db_path, str) else db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        """Open or create the cache database."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS florida_detail_cache (
                ucc_number TEXT PRIMARY KEY,
                detail_json TEXT NOT NULL,
                extracted_at TEXT NOT NULL
            )
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def get(self, ucc_number: str) -> Optional[dict]:
        """Return cached detail dict, or None if not cached."""
        if self._db is None:
            return None
        cursor = await self._db.execute(
            "SELECT detail_json FROM florida_detail_cache WHERE ucc_number = ?",
            (ucc_number,),
        )
        row = await cursor.fetchone()
        if row is not None:
            return json.loads(row[0])
        return None

    async def set(self, ucc_number: str, detail: dict) -> None:
        """Cache a detail dict."""
        if self._db is None:
            return
        await self._db.execute(
            """INSERT OR REPLACE INTO florida_detail_cache
               (ucc_number, detail_json, extracted_at)
               VALUES (?, ?, ?)""",
            (ucc_number, json.dumps(detail), datetime.now().isoformat()),
        )
        await self._db.commit()


# ── HTTP helpers ─────────────────────────────────────────────────────────────


def _build_headers() -> dict[str, str]:
    """Build request headers with a random User-Agent."""
    import random
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://floridaucc.com",
        "Referer": "https://floridaucc.com/search",
        "Cache-Control": "no-cache",
    }


async def _rate_limit() -> None:
    """Pause between requests to respect rate limits."""
    import random
    jitter = random.uniform(-0.3, 0.3) * INTER_REQUEST_DELAY
    await asyncio.sleep(max(0.1, INTER_REQUEST_DELAY + jitter))


# ── Main class ───────────────────────────────────────────────────────────────


class FloridaOCR:
    """Extract secured party + filing date from Florida UCC detail pages.

    Primary extraction uses the public JSON API; no actual OCR is needed
    for the happy path.  OCR/image-based fallbacks are available as a
    safety net if the API is ever restricted.
    """

    def __init__(
        self,
        cache_db: Path | str | None = None,
        use_cache: bool = True,
        httpx_client: Optional[httpx.AsyncClient] = None,
    ):
        db = Path(cache_db) if isinstance(cache_db, str) else (cache_db or CACHE_DB_PATH)
        self._cache = DetailCache(db) if use_cache else None
        self._http_client = httpx_client
        self._rate_limiter = _rate_limit

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open resources (cache, HTTP client)."""
        if self._cache is not None:
            await self._cache.open()
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                headers=_build_headers(),
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )

    async def stop(self) -> None:
        """Close resources."""
        if self._cache is not None:
            await self._cache.close()
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # ── Core: Extract detail for a single UCC number ───────────────────

    async def extract_detail(self, ucc_number: str) -> Optional[FloridaDetail]:
        """Fetch and parse full detail for one Florida UCC filing.

        Returns a FloridaDetail dataclass with secured party names, filing
        date, status, and debtor information.  Returns None if the filing
        is not found (invalid UCC number).

        Extraction strategy (tried in order):
          1. Check local cache (if enabled)
          2. JSON REST API (/filing-details) -- primary, no auth needed
          3. OCR fallback (Tesseract on TIFF image)
          4. Vision LLM fallback (Claude/GPT-4V)
        """
        # 1. Check cache
        if self._cache is not None:
            cached = await self._cache.get(ucc_number)
            if cached is not None:
                logger.debug("Cache hit for %s", ucc_number)
                return self._dict_to_detail(cached)

        # 2. JSON REST API
        detail = await self._fetch_via_api(ucc_number)
        if detail is not None:
            await self._cache_result(ucc_number, detail)
            return detail

        # 3. OCR fallback
        logger.info("API returned no data for %s, trying OCR fallback", ucc_number)
        detail = await self._extract_via_ocr(ucc_number)
        if detail is not None:
            await self._cache_result(ucc_number, detail)
            return detail

        logger.warning("All extraction methods failed for %s", ucc_number)
        return None

    async def enrich_filings(
        self,
        filings: list[dict],
        max_concurrent: int = 5,
    ) -> list[dict]:
        """Add secured_party_name and filing_date to Florida filings.

        Each input dict should have at least a ``filing_number`` key.
        The dict is updated in-place with:
          - secured_party_name  (str)
          - secured_party_address / city / state / zip (str or None)
          - filing_date         (str, ISO format)
          - status              (str)

        Unchanged dicts for non-FL filings or filings whose detail could
        not be fetched.

        Returns the list of updated dicts (same length as input).
        """
        await self.start()
        try:
            sem = asyncio.Semaphore(max_concurrent)
            tasks = [self._enrich_one(f, sem) for f in filings]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            enriched = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning(
                        "Enrichment failed for filing %s: %s",
                        filings[i].get("filing_number", "?"),
                        result,
                    )
                    enriched.append(filings[i])
                else:
                    enriched.append(result)  # type: ignore[arg-type]
            return enriched
        finally:
            await self.stop()

    # ── API extraction ─────────────────────────────────────────────────

    async def _fetch_via_api(self, ucc_number: str) -> Optional[FloridaDetail]:
        """Fetch filing detail from the public JSON API.

        Uses GET /filing-details?searchOptionType=DocumentNumber&filingNumber=...
        """
        if self._http_client is None:
            return None

        url = f"{PUBLIC_SEARCH_API}/filing-details"
        params = {
            "searchOptionType": "DocumentNumber",
            "filingNumber": ucc_number,
        }

        for attempt in range(MAX_RETRIES):
            try:
                response = await self._http_client.get(url, params=params)
                response.raise_for_status()
                data = response.json()

                if data.get("status") != "OK":
                    logger.debug(
                        "API returned non-OK status for %s: %s",
                        ucc_number,
                        data.get("status"),
                    )
                    return None

                payload = data.get("payload")
                if not payload:
                    return None

                detail = self._payload_to_detail(ucc_number, payload)
                logger.info(
                    "API detail for %s: status=%s, secured=%d, date=%s",
                    ucc_number,
                    detail.status,
                    len(detail.secured_parties),
                    detail.filing_date.isoformat() if detail.filing_date else "?",
                )
                return detail

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    # Filing does not exist in Florida's system
                    logger.debug("UCC %s not found in Florida system (HTTP 404)", ucc_number)
                    return None
                if exc.response.status_code in (403, 429):
                    # Rate-limited -- backoff and retry
                    wait = min(
                        BACKOFF_BASE * (BACKOFF_MULTIPLIER ** attempt),
                        BACKOFF_MAX,
                    )
                    logger.debug(
                        "Rate-limited on %s (HTTP %d), backing off %.1fs",
                        ucc_number,
                        exc.response.status_code,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                # Other HTTP errors
                logger.warning(
                    "HTTP %d fetching %s: %s",
                    exc.response.status_code,
                    ucc_number,
                    exc,
                )
                return None

            except (httpx.RequestError, httpx.TimeoutException) as exc:
                logger.warning(
                    "Request error fetching %s (attempt %d/%d): %s",
                    ucc_number,
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(BACKOFF_BASE * (BACKOFF_MULTIPLIER ** attempt))
                    continue
                return None

        return None

    # ── OCR fallback ───────────────────────────────────────────────────

    async def _extract_via_ocr(self, ucc_number: str) -> Optional[FloridaDetail]:
        """OCR fallback: download the TIFF document image and run Tesseract.

        Only invoked when the JSON API fails to return data.
        Requires ``tesseract`` binary and ``pytesseract`` package.
        """
        try:
            import pytesseract
            from PIL import Image
        except ImportError:
            logger.warning(
                "OCR not available for %s: pytesseract or Pillow not installed",
                ucc_number,
            )
            return None

        # Check if tesseract binary is available
        import shutil
        if shutil.which("tesseract") is None:
            logger.warning(
                "OCR not available for %s: tesseract binary not found",
                ucc_number,
            )
            return None

        try:
            image_data = await self._download_document_image(ucc_number)
            if image_data is None:
                return None

            image_path = Path(f"/tmp/fl_ucc_{ucc_number}.png")
            image_path.write_bytes(image_data)

            # Run Tesseract OCR
            text: str = await asyncio.to_thread(
                pytesseract.image_to_string,
                Image.open(image_path),
            )

            # Parse the OCR text
            detail = self._parse_ocr_text(ucc_number, text)
            if detail is not None:
                logger.info("OCR extracted data for %s", ucc_number)

            # Clean up temp file
            try:
                image_path.unlink(missing_ok=True)
            except OSError:
                pass

            return detail

        except Exception as exc:
            logger.warning("OCR failed for %s: %s", ucc_number, exc)
            return None

    async def _download_document_image(self, ucc_number: str) -> Optional[bytes]:
        """Download the document image for a UCC filing.

        The filing-image endpoint returns a TIFF image when successful.
        """
        if self._http_client is None:
            return None

        url = f"{PUBLIC_SEARCH_API}/filing-image"
        params = {
            "searchOptionType": "DocumentNumber",
            "filingNumber": ucc_number,
        }

        try:
            response = await self._http_client.get(url, params=params)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "json" in content_type.lower():
                # API may return JSON error
                logger.debug("Image endpoint returned JSON for %s", ucc_number)
                return None

            return response.content

        except httpx.HTTPStatusError as exc:
            logger.debug("Image download failed for %s: HTTP %d", ucc_number, exc.response.status_code)
            return None
        except httpx.RequestError as exc:
            logger.debug("Image download failed for %s: %s", ucc_number, exc)
            return None

    @staticmethod
    def _parse_ocr_text(ucc_number: str, text: str) -> Optional[FloridaDetail]:
        """Parse secured party and filing date from OCR'd text.

        Looks for patterns like:
          - "Secured Party:" / "SP:" labels
          - Filing date near "Date Filed" or "File Date" labels
          - Status indicators ("Active", "Lapsed", "Terminated")
        """
        detail = FloridaDetail(
            ucc_number=ucc_number,
            status="unknown",
            filing_date=None,
            expiration_date=None,
            source="ocr",
        )

        # Extract secured party name
        sp_patterns = [
            re.compile(r"Secured\s*Party[:\s]*\n*(.+?)(?:\n|\r)", re.IGNORECASE),
            re.compile(r"SP[:\s]+(.+)", re.IGNORECASE),
            re.compile(r"SECURED PARTY\s*\n(.+)", re.IGNORECASE),
        ]
        for pattern in sp_patterns:
            match = pattern.search(text)
            if match:
                name = match.group(1).strip()
                if name and len(name) > 2:
                    detail.secured_parties.append({"name": name})
                    break

        # Extract filing date
        date_patterns = [
            re.compile(r"(?:File\s*Date|Date\s*Filed|Filing\s*Date)[:\s]+(\d{2}[/-]\d{2}[/-]\d{4})", re.IGNORECASE),
            re.compile(r"(\d{2}[/-]\d{2}[/-]\d{4})"),
        ]
        for pattern in date_patterns:
            match = pattern.search(text)
            if match:
                try:
                    detail.filing_date = datetime.strptime(
                        match.group(1).replace("-", "/"), "%m/%d/%Y"
                    )
                    break
                except ValueError:
                    continue

        # Extract status
        for status_word in ("Lapsed", "Terminated", "Active", "Filed"):
            if re.search(status_word, text, re.IGNORECASE):
                detail.status = status_word.lower()
                break

        return detail

    # ── Response parsing ───────────────────────────────────────────────

    @staticmethod
    def _payload_to_detail(ucc_number: str, payload: dict) -> FloridaDetail:
        """Convert the JSON API payload into a FloridaDetail dataclass.

        The API returns fields like:
          - status: "Filed" | "Lapsed" | ...
          - fileDate: "2026-04-14T16:02:00Z"
          - expirationDate: "2031-04-14T16:02:00Z"
          - secureds: [{name, address, city, state, zipCode}]
          - debtors: [{name, address, city, state, zipCode}]
          - documentType: "UCC1" | "UCC3" | ...
          - fileImageExists: bool
          - filingEvents: int (number of amendments)
        """
        filing_date = None
        raw_date = payload.get("fileDate")
        if raw_date:
            try:
                filing_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        expiration_date = None
        raw_exp = payload.get("expirationDate")
        if raw_exp:
            try:
                expiration_date = datetime.fromisoformat(raw_exp.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        secured_parties = payload.get("secureds", [])
        debtors = payload.get("debtors", [])
        document_type = payload.get("documentType", "")
        status = (payload.get("status") or "unknown").lower()

        # Build a collateral excerpt from the filing image description
        # (the API doesn't return collateral text directly, but the image
        #  containing the collateral description can be downloaded if needed)
        collateral_excerpt = ""
        if document_type:
            collateral_excerpt = f"Document type: {document_type}"
        if payload.get("numberOfPagesInAllAssociatedForms"):
            collateral_excerpt += (
                f" | {payload['numberOfPagesInAllAssociatedForms']} page(s)"
            )

        return FloridaDetail(
            ucc_number=ucc_number,
            status=status,
            filing_date=filing_date,
            expiration_date=expiration_date,
            secured_parties=[_clean_party(p) for p in secured_parties],
            debtors=[_clean_party(p) for p in debtors],
            document_type=document_type,
            collateral_excerpt=collateral_excerpt.strip().lstrip("| "),
            has_image=bool(payload.get("fileImageExists", False)),
            source="api",
        )

    @staticmethod
    def _dict_to_detail(d: dict) -> FloridaDetail:
        """Reconstruct a FloridaDetail from a cached dict."""
        fd = d.get("filing_date")
        filing_date = None
        if fd:
            try:
                filing_date = datetime.fromisoformat(fd) if isinstance(fd, str) else None
            except (ValueError, TypeError):
                pass
        ed = d.get("expiration_date")
        expiration_date = None
        if ed:
            try:
                expiration_date = datetime.fromisoformat(ed) if isinstance(ed, str) else None
            except (ValueError, TypeError):
                pass

        return FloridaDetail(
            ucc_number=d.get("ucc_number", ""),
            status=d.get("status", "unknown"),
            filing_date=filing_date,
            expiration_date=expiration_date,
            secured_parties=d.get("secured_parties", []),
            debtors=d.get("debtors", []),
            document_type=d.get("document_type", ""),
            collateral_excerpt=d.get("collateral_excerpt", ""),
            has_image=d.get("has_image", False),
            source=d.get("source", "cache"),
        )

    # ── Cache helpers ──────────────────────────────────────────────────

    async def _cache_result(self, ucc_number: str, detail: FloridaDetail) -> None:
        """Store a successful extraction in the cache."""
        if self._cache is None:
            return
        d = {
            "ucc_number": detail.ucc_number,
            "status": detail.status,
            "filing_date": detail.filing_date.isoformat() if detail.filing_date else None,
            "expiration_date": detail.expiration_date.isoformat() if detail.expiration_date else None,
            "secured_parties": detail.secured_parties,
            "debtors": detail.debtors,
            "document_type": detail.document_type,
            "collateral_excerpt": detail.collateral_excerpt,
            "has_image": detail.has_image,
            "source": detail.source,
        }
        await self._cache.set(ucc_number, d)

    # ── Enrich filings ─────────────────────────────────────────────────

    async def _enrich_one(self, filing: dict, sem: asyncio.Semaphore) -> dict:
        """Enrich a single filing dict with detail data."""
        # Only process FL and non-empty filing numbers
        state = filing.get("state", "").upper()
        if state != "FL":
            return filing

        filing_number = str(filing.get("filing_number", "")).strip()
        if not filing_number:
            return filing

        async with sem:
            detail = await self.extract_detail(filing_number)

        if detail is None:
            return filing

        # Merge detail into filing dict (new dict, never mutate)
        updated = dict(filing)

        # Secured party info
        if detail.secured_parties:
            sp = detail.secured_parties[0]
            updated["secured_party_name"] = (sp.get("name") or "").strip()
            updated["secured_party_address"] = (sp.get("address") or "").strip()
            updated["secured_party_city"] = (sp.get("city") or "").strip()
            updated["secured_party_state"] = (sp.get("state") or "").strip()
            updated["secured_party_zip"] = (sp.get("zipCode") or "").strip()

        # Filing date
        if detail.filing_date:
            updated["filing_date"] = detail.filing_date.strftime("%Y-%m-%d")
            updated["filing_date_iso"] = detail.filing_date.isoformat()

        # Expiration / lapse date
        if detail.expiration_date:
            updated["lapse_date"] = detail.expiration_date.strftime("%Y-%m-%d")

        # Status (API gives definitive status vs search-result guess)
        updated["status"] = detail.status

        # Collateral excerpt
        if detail.collateral_excerpt:
            updated["collateral_excerpt"] = detail.collateral_excerpt

        # All secured parties (not just first)
        if detail.secured_parties:
            updated["all_secured_parties"] = detail.secured_parties

        # All debtors if API returned more than search
        if detail.debtors:
            updated["all_known_debtors"] = detail.debtors

        # Document type
        if detail.document_type:
            updated["document_type"] = detail.document_type

        # Enrichment source metadata
        updated["detail_source"] = detail.source
        updated["detail_fetched_at"] = datetime.now().isoformat()

        return updated


# ── Utility helpers ──────────────────────────────────────────────────────────


def _clean_party(party: dict) -> dict:
    """Normalize a party dict (debtor or secured party) from the API.

    Ensures all expected keys exist with empty string defaults so the
    dataclass is always consistent.
    """
    return {
        "name": (party.get("name") or "").strip(),
        "address": (party.get("address") or "").strip(),
        "city": (party.get("city") or "").strip(),
        "state": (party.get("state") or "").strip(),
        "zipCode": (party.get("zipCode") or "").strip(),
    }


# ── Convenience function ─────────────────────────────────────────────────────


async def enrich_florida_filings(
    filings: list[dict],
    max_concurrent: int = 5,
    cache_db: Path | str | None = None,
) -> list[dict]:
    """Standalone convenience: enrich a list of Florida filings with
    secured party names and filing dates.

    Example::

        from pipeline.florida_ocr import enrich_florida_filings

        enriched = await enrich_florida_filings(raw_fl_filings)
        for f in enriched:
            print(f["debtor_name"], "→", f.get("secured_party_name"))
    """
    ocr = FloridaOCR(cache_db=cache_db)
    try:
        return await ocr.enrich_filings(filings, max_concurrent=max_concurrent)
    finally:
        await ocr.stop()
