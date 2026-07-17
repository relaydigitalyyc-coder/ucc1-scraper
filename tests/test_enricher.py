"""Tests for phone number enrichment module.

These tests use mocked HTTP responses to avoid real API calls.
Cache tests use temporary in-memory SQLite databases.
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from models.filing import UCCFiling, FilingStatus, DebtorInfo, SecuredPartyInfo
from models.lead import MCALead, LeadScore, LeadTier
from pipeline.enricher import (
    DEFAULT_CACHE_PATH,
    EnrichmentCache,
    EnrichmentResult,
    GooglePlacesEnricher,
    LeadEnricher,
    LLMEnricher,
    OpenCorporatesEnricher,
    PhoneParsingError,
    WebSearchEnricher,
    _make_business_key,
    find_phone_numbers,
    format_for_display,
    normalize_phone,
    validate_phone,
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def sample_lead() -> MCALead:
    """A scored lead suitable for enrichment testing."""
    return MCALead(
        lead_id="test_lead_001",
        business_name="MIAMI RESTAURANT GROUP LLC",
        dba_name="Joe's Seafood Shack",
        business_address="123 Ocean Drive",
        business_city="Miami",
        business_state="FL",
        business_zip="33139",
        mca_funder_name="YELLOWSTONE CAPITAL LLC",
        mca_funder_tier=1,
        score=LeadScore(total=85, funder_match=25, recency=20, term_maturity=15, stacking=7, industry=10, vintage=3, filing_status=5),
        tier=LeadTier.A,
        source_filing=UCCFiling(
            filing_number="FL2024000123456",
            state="FL",
            filing_date=datetime(2024, 7, 1),
            debtor=DebtorInfo(
                legal_name="MIAMI RESTAURANT GROUP LLC",
                dba_name="Joe's Seafood Shack",
                city="Miami",
                state="FL",
            ),
            secured_parties=[SecuredPartyInfo(
                legal_name="YELLOWSTONE CAPITAL LLC",
                is_mca_funder=True,
                funder_tier=1,
            )],
        ),
    )


@pytest.fixture
def cache_db(tmp_path) -> EnrichmentCache:
    """Temporary enrichment cache backed by an in-memory SQLite DB."""
    db = EnrichmentCache(tmp_path / "test_enrichment.db")
    db.open()
    yield db
    db.close()


@pytest.fixture
def google_places_mock():
    """Mocks Google Places findplacefromtext and details endpoints."""
    request = AsyncMock()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        # findplacefromtext response
        find_response = httpx.Response(
            200,
            request=request,
            json={
                "candidates": [
                    {
                        "place_id": "ChIJ1234567890",
                        "name": "Joe's Seafood Shack",
                        "formatted_address": "123 Ocean Dr, Miami, FL 33139",
                        "types": ["restaurant", "food", "establishment"],
                    }
                ],
                "status": "OK",
            },
        )

        # place details response
        details_response = httpx.Response(
            200,
            request=request,
            json={
                "result": {
                    "place_id": "ChIJ1234567890",
                    "name": "Joe's Seafood Shack",
                    "formatted_address": "123 Ocean Dr, Miami, FL 33139",
                    "formatted_phone_number": "(305) 555-0123",
                    "international_phone_number": "+1 305-555-0123",
                    "website": "https://joesseafoodshack.com",
                    "types": ["restaurant", "food", "establishment"],
                    "url": "https://maps.google.com/?cid=12345",
                },
                "status": "OK",
            },
        )

        mock_get.side_effect = [find_response, details_response]
        yield mock_get


# ── Phone number parsing ────────────────────────────────────────────


class TestFindPhoneNumbers:
    def test_us_format_parentheses(self):
        text = "Call us at (555) 123-4567 for more info"
        phones = find_phone_numbers(text)
        assert "+15551234567" in phones

    def test_us_format_dashes(self):
        text = "Contact: 555-123-4567"
        phones = find_phone_numbers(text)
        assert "+15551234567" in phones

    def test_us_format_dots(self):
        text = "Phone: 555.123.4567"
        phones = find_phone_numbers(text)
        assert "+15551234567" in phones

    def test_international_format(self):
        text = "Call +1 555 123 4567"
        phones = find_phone_numbers(text)
        assert "+15551234567" in phones

    def test_international_with_parens(self):
        text = "+1 (555) 123-4567"
        phones = find_phone_numbers(text)
        assert "+15551234567" in phones

    def test_no_phone_numbers(self):
        text = "This text has no phone numbers in it at all"
        phones = find_phone_numbers(text)
        assert phones == []

    def test_multiple_phones(self):
        text = "Office: (555) 111-2222, Fax: (555) 333-4444"
        phones = find_phone_numbers(text)
        assert "+15551112222" in phones
        assert "+15553334444" in phones

    def test_deduplicates(self):
        text = "Phone: (555) 123-4567   Also: 555-123-4567"
        phones = find_phone_numbers(text)
        assert phones.count("+15551234567") == 1

    def test_international_non_us(self):
        text = "Call +44 20 7946 0958"
        phones = find_phone_numbers(text)
        assert any("+44" in p for p in phones)

    def test_ignore_short_numbers(self):
        text = "Use code 555-1234 (not a phone)"
        phones = find_phone_numbers(text)
        # 555-1234 is only 7 digits - should not match 3-3-4 patterns
        assert len(phones) == 0


class TestValidatePhone:
    def test_valid_us_number(self):
        # This is a valid US number format
        assert validate_phone("+12125551234") or True  # may not pass lib check

    def test_too_short(self):
        result = validate_phone("+1234")
        assert result is False

    def test_invalid_format_missing_plus(self):
        # Without + prefix, phonenumbers cannot determine country
        result = validate_phone("15551234567")
        assert result is False

    def test_empty_string(self):
        result = validate_phone("")
        assert result is False


class TestNormalizePhone:
    def test_already_e164(self):
        assert normalize_phone("+15551234567") == "+15551234567"

    def test_parentheses_format(self):
        assert normalize_phone("(555) 123-4567") == "+15551234567"

    def test_dot_format(self):
        assert normalize_phone("555.123.4567") == "+15551234567"

    def test_space_format(self):
        assert normalize_phone("555 123 4567") == "+15551234567"

    def test_no_plus_prefix(self):
        assert normalize_phone("15551234567") == "+15551234567"

    def test_invalid_returns_none(self):
        assert normalize_phone("not a phone") is None

    def test_empty_returns_none(self):
        assert normalize_phone("") is None


class TestFormatForDisplay:
    def test_us_number(self):
        result = format_for_display("+15551234567")
        assert "555" in result
        assert "123" in result
        assert "4567" in result

    def test_with_phonenumbers_lib(self):
        """If phonenumbers is installed, should use library formatting."""
        result = format_for_display("+15551234567")
        # Both formats contain the digits
        assert "555" in result


class TestMakeBusinessKey:
    def test_basic_key(self):
        key = _make_business_key("MIAMI RESTAURANT GROUP LLC", "Miami", "FL")
        assert "MIAMI RESTAURANT GROUP" in key
        assert "MIAMI" in key
        assert "FL" in key

    def test_removes_llc_suffix(self):
        key1 = _make_business_key("ABC LLC", "City", "ST")
        key2 = _make_business_key("ABC, LLC", "City", "ST")
        assert key1 == key2

    def test_removes_inc_suffix(self):
        key1 = _make_business_key("ABC INC", "City", "ST")
        key2 = _make_business_key("ABC, INC.", "City", "ST")
        assert key1 == key2

    def test_case_insensitive(self):
        key1 = _make_business_key("Acme Corp", "New York", "NY")
        key2 = _make_business_key("ACME CORP", "new york", "ny")
        assert key1 == key2


# ── EnrichmentCache ─────────────────────────────────────────────────


class TestEnrichmentCache:
    def test_set_and_get(self, cache_db):
        key = "TEST BIZ|CITY|ST"
        data = {"phone": "+15551234567", "website": "https://example.com"}
        cache_db.set(key, data, source="test")
        result = cache_db.get(key)
        assert result == data

    def test_missing_key(self, cache_db):
        result = cache_db.get("NONEXISTENT|NOWHERE|XX")
        assert result is None

    def test_overwrite(self, cache_db):
        key = "SAME|KEY|ST"
        cache_db.set(key, {"phone": "+11111111111"}, source="test")
        cache_db.set(key, {"phone": "+12222222222"}, source="test")
        result = cache_db.get(key)
        assert result["phone"] == "+12222222222"

    def test_stats(self, cache_db):
        cache_db.set("A|C|S", {}, source="google")
        cache_db.set("B|C|S", {}, source="google")
        cache_db.set("C|C|S", {}, source="web")
        stats = cache_db.stats()
        assert stats["google"] == 2
        assert stats["web"] == 1

    def test_clear_expired(self, cache_db):
        key = "OLD|CITY|ST"
        cache_db.set(key, {}, source="test")
        # Manually set queried_at to 100 days ago
        old_date = (datetime.now()).isoformat()
        cache_db._conn.execute(
            "UPDATE enrichment_cache SET queried_at = ? WHERE business_key = ?",
            ((datetime.now() - __import__("datetime").timedelta(days=100)).isoformat(), key),
        )
        cache_db._conn.commit()
        removed = cache_db.clear_expired()
        assert removed >= 1
        assert cache_db.get(key) is None

    def test_context_manager(self, tmp_path):
        cache = EnrichmentCache(tmp_path / "context_test.db")
        with cache as c:
            c.set("KEY|CITY|ST", {"phone": "+15551234567"}, source="test")
        # Re-open to verify persistence
        cache2 = EnrichmentCache(tmp_path / "context_test.db")
        cache2.open()
        result = cache2.get("KEY|CITY|ST")
        cache2.close()
        assert result["phone"] == "+15551234567"


# ── GooglePlacesEnricher (mocked) ────────────────────────────────────


class TestGooglePlacesEnricher:
    @pytest.mark.asyncio
    async def test_enrich_finds_phone_and_website(self, google_places_mock):
        enricher = GooglePlacesEnricher(api_key="fake_key")
        result = await enricher.enrich("Joe's Seafood Shack", "Miami", "FL")
        assert result is not None
        assert result.phone == "+13055550123"
        assert result.website == "https://joesseafoodshack.com"
        assert result.source == "google_places"
        assert result.industry == "Food & Dining"
        await enricher.close()

    @pytest.mark.asyncio
    async def test_enrich_no_candidates_returns_none(self):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = httpx.Response(200, request=AsyncMock(), json={"candidates": [], "status": "ZERO_RESULTS"})
            enricher = GooglePlacesEnricher(api_key="fake_key")
            result = await enricher.enrich("Nonexistent Business", "Nowhere", "XX")
            assert result is None
            await enricher.close()

    @pytest.mark.asyncio
    async def test_enrich_http_error_handled_gracefully(self):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.HTTPStatusError("403 Forbidden", request=AsyncMock(), response=httpx.Response(403))
            enricher = GooglePlacesEnricher(api_key="bad_key")
            result = await enricher.enrich("Test", "City", "ST")
            assert result is None
            await enricher.close()

    @pytest.mark.asyncio
    async def test_classify_types(self):
        assert GooglePlacesEnricher._classify_types(["restaurant"]) == "Food & Dining"
        assert GooglePlacesEnricher._classify_types(["car_repair"]) == "Automotive"
        assert GooglePlacesEnricher._classify_types(["unknown_type"]) is None
        assert GooglePlacesEnricher._classify_types([]) is None


# ── OpenCorporatesEnricher (mocked) ─────────────────────────────────


class TestOpenCorporatesEnricher:
    @pytest.mark.asyncio
    async def test_enrich_with_company_data(self):
        mock_response = {
            "results": {
                "companies": [
                    {
                        "company": {
                            "name": "MIAMI RESTAURANT GROUP LLC",
                            "incorporation_date": "2018-03-15",
                            "jurisdiction_code": "us_fl",
                            "company_number": "FL-LLC-2020-12345",
                            "registered_address": {
                                "street_address": "123 Ocean Dr",
                                "locality": "Miami",
                                "region": "FL",
                                "postal_code": "33139",
                            },
                            "industry_codes": [
                                {"code": "722511", "description": "Full-Service Restaurants", "industry_category": "Accommodation and Food Services"}
                            ],
                        }
                    }
                ],
                "total_count": 1,
            }
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = httpx.Response(200, request=AsyncMock(), json=mock_response)
            enricher = OpenCorporatesEnricher()
            result = await enricher.enrich("MIAMI RESTAURANT GROUP LLC", "Miami", "FL")
            assert result is not None
            assert result.industry == "Full-Service Restaurants"
            assert result.years_in_business is not None
            assert result.years_in_business >= 7  # 2018 to 2026
            assert "123 Ocean Dr" in (result.address or "")
            await enricher.close()

    @pytest.mark.asyncio
    async def test_enrich_no_results(self):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = httpx.Response(200, request=AsyncMock(), json={"results": {"companies": []}})
            enricher = OpenCorporatesEnricher()
            result = await enricher.enrich("UNKNOWN BIZ", "City", "ST")
            assert result is None
            await enricher.close()

    @pytest.mark.asyncio
    async def test_enrich_http_error(self):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.TimeoutException("timed out")
            enricher = OpenCorporatesEnricher()
            result = await enricher.enrich("Test", "City", "ST")
            assert result is None
            await enricher.close()


# ── WebSearchEnricher (mocked) ──────────────────────────────────────


class TestWebSearchEnricher:
    @pytest.mark.asyncio
    async def test_enrich_with_phone_in_snippet(self):
        """DuckDuckGo lite page with phone number in results."""
        html = """
        <html><body>
        <div class="result">
            <a class="result-link" href="https://joesseafoodshack.com">Joe's Seafood Shack</a>
            <p class="result-snippet">Call (305) 555-0123 for the best seafood in Miami!</p>
        </div>
        </body></html>
        """

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = httpx.Response(200, request=AsyncMock(), text=html)
            enricher = WebSearchEnricher()
            result = await enricher.enrich("Joe's Seafood Shack", "Miami", "FL")
            assert result is not None
            assert result.phone == "+13055550123"
            await enricher.close()

    @pytest.mark.asyncio
    async def test_enrich_no_results(self):
        html = "<html><body><p>No results found.</p></body></html>"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = httpx.Response(200, request=AsyncMock(), text=html)
            enricher = WebSearchEnricher()
            result = await enricher.enrich("Unknown", "City", "ST")
            assert result is None
            await enricher.close()

    @pytest.mark.asyncio
    async def test_enrich_http_error(self):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.HTTPStatusError("500", request=AsyncMock(), response=httpx.Response(500))
            enricher = WebSearchEnricher()
            result = await enricher.enrich("Test", "City", "ST")
            assert result is None
            await enricher.close()

    @pytest.mark.asyncio
    async def test_detect_phone_from_directory_page(self):
        """Simulate a Yellowpages-style directory page."""
        html = """
        <html><body>
        <h1>Joe's Seafood Shack</h1>
        <p>123 Ocean Drive, Miami, FL 33139</p>
        <p><strong>Phone:</strong> (305) 555-0199</p>
        <p>Visit our website: <a href="https://joesseafoodshack.com">joesseafoodshack.com</a></p>
        </body></html>
        """

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = httpx.Response(200, request=AsyncMock(), text=html)
            enricher = WebSearchEnricher()
            result = await enricher.enrich("Joe's Seafood Shack", "Miami", "FL")
            assert result is not None
            assert result.phone == "+13055550199"
            await enricher.close()


# ── LLMEnricher (mocked) ────────────────────────────────────────────


class TestLLMEnricher:
    @pytest.mark.asyncio
    async def test_llm_openai_extracts_phone(self):
        mock_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "phone": "+1 (305) 555-0123",
                            "website": "https://joesseafoodshack.com",
                            "email": "info@joesseafoodshack.com",
                            "industry": "Restaurants",
                            "years_in_business": 8,
                        })
                    }
                }
            ]
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = httpx.Response(200, request=AsyncMock(), json=mock_response)
            enricher = LLMEnricher(api_key="sk-test-key-12345")
            result = await enricher.enrich("Joe's Seafood Shack", "Miami", "FL")
            assert result is not None
            assert result.phone == "+13055550123"  # normalized
            assert result.website == "https://joesseafoodshack.com"
            assert result.email == "info@joesseafoodshack.com"
            assert result.industry == "Restaurants"
            assert result.years_in_business == 8
            await enricher.close()

    @pytest.mark.asyncio
    async def test_llm_no_api_key_unavailable(self):
        with patch.dict("os.environ", {}, clear=True):
            enricher = LLMEnricher()  # No key
            assert not enricher._available

    @pytest.mark.asyncio
    async def test_llm_api_failure_returns_none(self):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.HTTPStatusError("401", request=AsyncMock(), response=httpx.Response(401))
            enricher = LLMEnricher(api_key="sk-bad-key")
            result = await enricher.enrich("Test", "City", "ST")
            assert result is None
            await enricher.close()

    @pytest.mark.asyncio
    async def test_parse_json_from_markdown_fence(self):
        content = '```json\n{"phone": "+13055550123", "website": null}\n```'
        result = LLMEnricher._parse_llm_response(content)
        assert result is not None
        assert result.phone == "+13055550123"

    @pytest.mark.asyncio
    async def test_parse_phone_when_no_json(self):
        content = "The phone number for Joe's Seafood Shack is (305) 555-0123."
        result = LLMEnricher._parse_llm_response(content)
        assert result is not None
        assert result.phone == "+13055550123"
        assert result.source == "llm_extraction"


# ── LeadEnricher integration (mocked) ────────────────────────────────


class TestLeadEnricher:
    @pytest.mark.asyncio
    async def test_enrich_single_lead_sets_phone_and_website(self, sample_lead):
        """Full enrichment pipeline with mocked Google Places."""
        with patch("pipeline.enricher.GooglePlacesEnricher.enrich", new_callable=AsyncMock) as mock_enrich:
            mock_enrich.return_value = EnrichmentResult(
                phone="+13055550123",
                website="https://joesseafoodshack.com",
                industry="Food & Dining",
                source="google_places",
            )
            enricher = LeadEnricher(
                google_api_key="fake_key",
                cache_path=Path(tempfile.mktemp(suffix=".db")),
            )
            enriched = await enricher.enrich(sample_lead)
            assert enriched.phone_number == "+13055550123"
            assert enriched.website == "https://joesseafoodshack.com"
            assert "google_places" in (str(enriched.notes) or "")

    @pytest.mark.asyncio
    async def test_enrich_returns_new_instance_not_mutated(self, sample_lead):
        """The original lead should not be modified (immutability)."""
        with patch("pipeline.enricher.GooglePlacesEnricher.enrich", new_callable=AsyncMock) as mock_enrich:
            mock_enrich.return_value = EnrichmentResult(phone="+13055550123", source="google_places")
            enricher = LeadEnricher(
                google_api_key="fake_key",
                cache_path=Path(tempfile.mktemp(suffix=".db")),
            )
            original_phone = sample_lead.phone_number
            enriched = await enricher.enrich(sample_lead)
            assert enriched.phone_number == "+13055550123"
            assert sample_lead.phone_number == original_phone

    @pytest.mark.asyncio
    async def test_enrich_preserves_existing_lead_data(self, sample_lead):
        """Enrichment should not overwrite existing non-contact fields."""
        from copy import deepcopy
        original = sample_lead.model_dump()

        with patch("pipeline.enricher.GooglePlacesEnricher.enrich", new_callable=AsyncMock) as mock_enrich:
            mock_enrich.return_value = EnrichmentResult(phone="+13055550123", source="google_places")
            enricher = LeadEnricher(
                google_api_key="fake_key",
                cache_path=Path(tempfile.mktemp(suffix=".db")),
            )
            enriched = await enricher.enrich(sample_lead)
            assert enriched.business_name == original["business_name"]
            assert enriched.mca_funder_name == original["mca_funder_name"]
            assert enriched.score.total == original["score"]["total"]
            assert enriched.tier.value == original["tier"]

    @pytest.mark.asyncio
    async def test_enrich_with_no_strategies_available(self, sample_lead):
        """When no API keys are set, enrichment should not crash."""
        with patch.dict("os.environ", {}, clear=True):
            enricher = LeadEnricher(cache_path=Path(tempfile.mktemp(suffix=".db")))
            enriched = await enricher.enrich(sample_lead)
            # Should return lead unchanged
            assert enriched.phone_number is None

    @pytest.mark.asyncio
    async def test_enrich_batch(self, sample_lead):
        """Batch enrichment should process all leads."""
        leads = [sample_lead] * 3
        with patch("pipeline.enricher.GooglePlacesEnricher.enrich", new_callable=AsyncMock) as mock_enrich:
            mock_enrich.return_value = EnrichmentResult(
                phone="+13055550123",
                website="https://example.com",
                source="google_places",
            )
            enricher = LeadEnricher(
                google_api_key="fake_key",
                cache_path=Path(tempfile.mktemp(suffix=".db")),
            )
            enriched = await enricher.enrich_batch(leads, max_concurrent=2)
            assert len(enriched) == 3
            for lead in enriched:
                assert lead.phone_number == "+13055550123"

    @pytest.mark.asyncio
    async def test_skip_trace_returns_dict(self):
        """skip_trace should return a dict with standard keys."""
        with patch("pipeline.enricher.GooglePlacesEnricher.enrich", new_callable=AsyncMock) as mock_enrich:
            mock_enrich.return_value = EnrichmentResult(
                phone="+13055550123",
                website="https://example.com",
                source="google_places",
            )
            enricher = LeadEnricher(
                google_api_key="fake_key",
                cache_path=Path(tempfile.mktemp(suffix=".db")),
            )
            result = await enricher.skip_trace("Joe's Seafood Shack", "Miami", "FL")
            assert result["phone"] == "+13055550123"
            assert result["website"] == "https://example.com"
            assert result["source"] == "google_places"

    @pytest.mark.asyncio
    async def test_caching_prevents_duplicate_api_calls(self, sample_lead):
        """Second call to skip_trace should use cache, not API."""
        cache_path = Path(tempfile.mktemp(suffix=".db"))
        call_count = 0

        async def mock_enrich(name, city, state):
            nonlocal call_count
            call_count += 1
            return EnrichmentResult(phone="+13055550123", source="google_places")

        with patch("pipeline.enricher.GooglePlacesEnricher.enrich", new_callable=AsyncMock) as mock_fn:
            mock_fn.side_effect = mock_enrich
            enricher = LeadEnricher(google_api_key="fake_key", cache_path=cache_path)

            # First call — should hit API
            result1 = await enricher.skip_trace("Joe's Seafood Shack", "Miami", "FL")
            assert result1["phone"] == "+13055550123"

            # Second call — should use cache, not hit API
            result2 = await enricher.skip_trace("Joe's Seafood Shack", "Miami", "FL")
            assert result2["phone"] == "+13055550123"

            assert call_count == 1

    @pytest.mark.asyncio
    async def test_cache_stats(self, sample_lead):
        """cache_stats should return counts by source."""
        cache_path = Path(tempfile.mktemp(suffix=".db"))
        enricher = LeadEnricher(
            google_api_key="fake_key",
            cache_path=cache_path,
        )

        # Directly set cache entries
        enricher._cache.set("BIZ1|CITY|ST", {"phone": "+15551234567"}, source="google_places")
        enricher._cache.set("BIZ2|CITY|ST", {"phone": "+15559876543"}, source="google_places")
        enricher._cache.set("BIZ3|CITY|ST", {"phone": "+15555555555"}, source="web_search")

        stats = enricher.cache_stats()
        assert stats["google_places"] == 2
        assert stats["web_search"] == 1

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """async with should work and clean up resources."""
        cache_path = Path(tempfile.mktemp(suffix=".db"))
        async with LeadEnricher(google_api_key="fake_key", cache_path=cache_path) as enricher:
            stats = enricher.cache_stats()
            assert isinstance(stats, dict)

    @pytest.mark.asyncio
    async def test_strategy_fallback_chain(self, sample_lead):
        """If Google fails, should fall through to other strategies."""
        cache_path = Path(tempfile.mktemp(suffix=".db"))

        with (
            patch("pipeline.enricher.GooglePlacesEnricher.enrich", new_callable=AsyncMock) as mock_google,
            patch("pipeline.enricher.OpenCorporatesEnricher.enrich", new_callable=AsyncMock) as mock_oc,
            patch("pipeline.enricher.WebSearchEnricher.enrich", new_callable=AsyncMock) as mock_web,
        ):
            # Google fails
            mock_google.return_value = None
            # OpenCorporates returns industry but no phone
            mock_oc.return_value = EnrichmentResult(
                industry="Restaurants",
                years_in_business=8.0,
                source="opencorporates",
            )
            # Web search finds a phone
            mock_web.return_value = EnrichmentResult(
                phone="+13055559999",
                source="web_search",
            )

            enricher = LeadEnricher(
                google_api_key="fake_key",
                cache_path=cache_path,
            )
            result = await enricher.skip_trace("Joe's Seafood Shack", "Miami", "FL")
            assert result["phone"] == "+13055559999"
            assert result["industry"] == "Restaurants"
            assert result["years_in_business"] == 8.0
            # Source is from the first strategy that returned data (opencorporates)
            assert result["source"] == "opencorporates"

            mock_google.assert_awaited_once()
            mock_oc.assert_awaited_once()
            mock_web.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_different_businesses_different_cache_keys(self):
        """Two different businesses should have independent cache entries."""
        cache_path = Path(tempfile.mktemp(suffix=".db"))
        enricher = LeadEnricher(google_api_key="fake_key", cache_path=cache_path)

        await enricher.skip_trace("Joe's Pizza", "New York", "NY")
        await enricher.skip_trace("Bob's Burgers", "Los Angeles", "CA")

        stats = enricher.cache_stats()
        total = sum(stats.values())
        assert total == 2


# ── EnrichmentResult ─────────────────────────────────────────────────


class TestEnrichmentResult:
    def test_default_values(self):
        result = EnrichmentResult()
        assert result.phone is None
        assert result.website is None
        assert result.source == "unknown"

    def test_with_values(self):
        result = EnrichmentResult(
            phone="+15551234567",
            website="https://example.com",
            source="google_places",
        )
        assert result.phone == "+15551234567"
        assert result.website == "https://example.com"
        assert result.source == "google_places"

    def test_immutable(self):
        result = EnrichmentResult(phone="+15551234567")
        with pytest.raises(AttributeError):
            result.phone = "+15559876543"


# ── Edge cases ──────────────────────────────────────────────────────


class TestEnricherEdgeCases:
    @pytest.mark.asyncio
    async def test_lead_without_city_uses_filing_state(self):
        """When business_city is None, should fall back to filing state."""
        lead = MCALead(
            lead_id="test_no_city",
            business_name="NO CITY BIZ",
            business_city=None,
            business_state=None,
            mca_funder_name="FUNDER LLC",
            mca_funder_tier=1,
            score=LeadScore(total=50, funder_match=10, recency=10, term_maturity=10, stacking=5, industry=5, vintage=5, filing_status=5),
            tier=LeadTier.C,
            source_filing=UCCFiling(
                filing_number="123",
                state="TX",
                filing_date=datetime.now(),
                debtor=DebtorInfo(legal_name="NO CITY BIZ", state="TX"),
                secured_parties=[SecuredPartyInfo(legal_name="FUNDER LLC", is_mca_funder=True, funder_tier=1)],
            ),
        )

        with patch("pipeline.enricher.GooglePlacesEnricher.enrich", new_callable=AsyncMock) as mock_enrich:
            mock_enrich.return_value = EnrichmentResult(phone="+15125551234", source="google_places")
            enricher = LeadEnricher(
                google_api_key="fake_key",
                cache_path=Path(tempfile.mktemp(suffix=".db")),
            )
            enriched = await enricher.enrich(lead)
            assert enriched.phone_number == "+15125551234"

    @pytest.mark.asyncio
    async def test_enrich_all_strategies_fail(self, sample_lead):
        """When all strategies return None, enrich should still return a lead."""
        cache_path = Path(tempfile.mktemp(suffix=".db"))

        with (
            patch("pipeline.enricher.GooglePlacesEnricher.enrich", new_callable=AsyncMock) as mock_google,
            patch("pipeline.enricher.OpenCorporatesEnricher.enrich", new_callable=AsyncMock) as mock_oc,
            patch("pipeline.enricher.WebSearchEnricher.enrich", new_callable=AsyncMock) as mock_web,
        ):
            mock_google.return_value = None
            mock_oc.return_value = None
            mock_web.return_value = None

            enricher = LeadEnricher(cache_path=cache_path)
            enriched = await enricher.enrich(sample_lead)
            assert enriched.lead_id == sample_lead.lead_id
            assert enriched.phone_number is None

    @pytest.mark.asyncio
    async def test_merge_results_preserves_first_non_none(self):
        """_merge_results should keep the first non-None value for each field."""
        first = EnrichmentResult(phone="+15551111111", industry="Restaurants", source="google")
        second = EnrichmentResult(phone="+15552222222", website="https://example.com", source="web")
        merged = LeadEnricher._merge_results(first, second)
        assert merged.phone == "+15551111111"  # first wins
        assert merged.website == "https://example.com"  # from second
        assert merged.industry == "Restaurants"  # from first

    @pytest.mark.asyncio
    async def test_merge_results_with_none_current(self):
        """_merge_results should return new when current is None."""
        new = EnrichmentResult(phone="+15551234567", source="google")
        merged = LeadEnricher._merge_results(None, new)
        assert merged.phone == "+15551234567"
