"""Tests for New York UCC scraper (SODA API fallback)."""

from datetime import datetime, timedelta

import pytest

from scrapers.new_york import NewYorkScraper
from scrapers.registry import get_scraper, list_available_states


@pytest.fixture
def ny_scraper():
    """Create a NY scraper in SODA fallback mode."""
    return NewYorkScraper(use_soda_fallback=True)


@pytest.mark.asyncio
async def test_health_check_soda(ny_scraper):
    """Health check should succeed with SODA fallback (no browser needed)."""
    result = await ny_scraper.health_check()
    assert result["ok"] is True
    assert result["soda_fallback_available"] is True


@pytest.mark.asyncio
async def test_search_by_date_range(ny_scraper):
    """Search by date range should return results from SODA API."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=10)

    count = 0
    debtor_names = []
    source = None
    async for filing in ny_scraper.search_by_date_range(start_date, end_date):
        count += 1
        if count <= 5:
            debtor_names.append(filing.get("debtor_name", ""))
        source = filing.get("data_source")

        if count >= 20:
            break

    assert count > 0, "Should return at least 1 filing"
    assert source == "soda_api", "Should use SODA API as data source"
    assert all(isinstance(n, str) and len(n) > 0 for n in debtor_names), (
        "All filings should have debtor names"
    )


@pytest.mark.asyncio
async def test_search_by_date_range_yields_required_fields(ny_scraper):
    """Each filing dict must contain the standard fields."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5)

    required_fields = [
        "state",
        "filing_number",
        "filing_date",
        "debtor_name",
        "entity_type",
        "filing_type",
        "data_source",
    ]

    count = 0
    async for filing in ny_scraper.search_by_date_range(start_date, end_date):
        count += 1
        assert filing.get("state") == "NY"
        assert filing.get("filing_number"), "filing_number must not be empty"
        assert filing.get("filing_date"), "filing_date must not be empty"
        assert filing.get("data_source") == "soda_api"
        for field in required_fields:
            assert field in filing, f"Missing required field: {field}"

        if count >= 5:
            break


@pytest.mark.asyncio
async def test_search_by_debtor_name(ny_scraper):
    """Search by debtor name should return matching entities."""
    count = 0
    async for filing in ny_scraper.search_by_debtor_name("CONSULTING"):
        count += 1
        debtor = str(filing.get("debtor_name", "")).upper()
        assert "CONSULTING" in debtor, f"Expected CONSULTING in {debtor}"
        if count >= 5:
            break

    assert count > 0, "Should find at least 1 CONSULTING entity"


@pytest.mark.asyncio
async def test_get_filing_detail_valid(ny_scraper):
    """Get detail for a filing number should return non-empty dict."""
    # First find a real filing number
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)
    filing_number = None
    async for filing in ny_scraper.search_by_date_range(start_date, end_date):
        filing_number = filing.get("filing_number")
        if filing_number:
            break

    assert filing_number is not None, "Need at least one filing to test detail lookup"

    detail = await ny_scraper.get_filing_detail(filing_number)
    assert detail is not None
    assert detail.get("filing_number") == filing_number
    assert detail.get("state") == "NY"


@pytest.mark.asyncio
async def test_get_filing_detail_invalid(ny_scraper):
    """Get detail for a non-existent filing should return error dict."""
    detail = await ny_scraper.get_filing_detail("999999999999")
    assert detail is not None
    assert detail.get("filing_number") == "999999999999"
    assert detail.get("data_source") == "soda_api"


@pytest.mark.asyncio
async def test_check_status(ny_scraper):
    """check_status should return a known status string."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)
    filing_number = None
    async for filing in ny_scraper.search_by_date_range(start_date, end_date):
        filing_number = filing.get("filing_number")
        if filing_number:
            break

    if filing_number:
        status = await ny_scraper.check_status(filing_number)
        valid_statuses = ["active", "terminated", "lapsed", "continued", "amended", "unknown"]
        assert status in valid_statuses, f"Status '{status}' not in {valid_statuses}"


def test_registry_integration():
    """NY scraper should be registered and retrievable by state code."""
    assert "NY" in list_available_states()

    scraper = get_scraper("NY")
    assert scraper is not None
    assert scraper.state == "NY"
    assert scraper.state_name == "New York"
    assert isinstance(scraper, NewYorkScraper)

    # Verify lowercase also works
    scraper_lower = get_scraper("ny")
    assert scraper_lower is not None
    assert scraper_lower.state == "NY"


@pytest.mark.asyncio
async def test_soda_row_normalization(ny_scraper):
    """Normalize a sample SODA row to verify correct field mapping."""
    sample_row = {
        "film_num": "260624002374",
        "dos_id": "7950476",
        "filing_date": "2026-06-24T00:00:00.000",
        "approved_date": "2026-06-15T00:00:00.000",
        "eff_date": "2026-06-15T00:00:00.000",
        "corp_name": "TEST COMPANY LLC",
        "fictitious_name": "TEST DBA",
        "pre_corp_name": "OLD NAME INC",
        "entity_type": "DOMESTIC LIMITED LIABILITY COMPANY",
        "filing_type": "ARTICLES OF ORGANIZATION",
        "for_juris": "NY",
        "cnty_prin_ofc": "Albany",
        "law": "LIMITED LIABILITY COMPANY LAW - 203",
        "filer_name": "JOHN DOE",
        "filer_addr1": "123 MAIN ST",
        "filer_city": "ALBANY",
        "filer_state": "NY",
        "filer_zip5": "12201",
        "sop_name": "REGISTERED AGENT LLC",
        "sop_addr1": "456 STATE ST",
        "sop_city": "ALBANY",
        "sop_state": "NY",
        "sop_zip5": "12203",
    }

    result = ny_scraper._normalize_soda_row(sample_row)

    assert result["state"] == "NY"
    assert result["filing_number"] == "260624002374"
    assert result["filing_date"] == "2026-06-24"
    assert result["debtor_name"] == "TEST COMPANY LLC"
    assert result["fictitious_name"] == "TEST DBA"
    assert result["previous_name"] == "OLD NAME INC"
    assert result["entity_type"] == "DOMESTIC LIMITED LIABILITY COMPANY"
    assert result["filing_type"] == "ARTICLES OF ORGANIZATION"
    assert result["jurisdiction"] == "NY"
    assert result["county"] == "Albany"
    assert result["filer_name"] == "JOHN DOE"
    assert result["filer_state"] == "NY"
    assert result["sop_name"] == "REGISTERED AGENT LLC"
    assert result["data_source"] == "soda_api"
    assert result["is_ucc_filing"] is False
    # SODA doesn't have secured party data
    assert result["secured_party_name"] == ""


@pytest.mark.asyncio
async def test_many_filings_structure_consistency(ny_scraper):
    """Verify that a larger sample of filings all conform to expected structure."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5)

    count = 0
    async for filing in ny_scraper.search_by_date_range(start_date, end_date):
        count += 1
        # Every filing must have these non-empty fields
        assert filing.get("filing_number"), f"Empty filing_number at idx {count}"
        assert filing.get("debtor_name"), f"Empty debtor_name at idx {count}"
        assert filing.get("filing_type"), f"Empty filing_type at idx {count}"
        assert filing.get("entity_type"), f"Empty entity_type at idx {count}"
        assert filing.get("data_source") in ("soda_api", "ucc_portal")
        assert filing.get("state") == "NY"

        if count >= 200:
            break

    assert count > 0, "Should return at least one filing"
