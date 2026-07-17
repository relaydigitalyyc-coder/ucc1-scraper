"""Tests for the FilingNormalizer — raw data → UCCFiling conversion."""

from datetime import datetime

import pytest

from models.filing import FilingStatus


class TestDateParsing:
    def test_parse_mm_dd_yyyy(self, normalizer):
        result = normalizer._parse_date("07/15/2024")
        assert result == datetime(2024, 7, 15)

    def test_parse_mm_dd_yy(self, normalizer):
        result = normalizer._parse_date("07/15/24")
        assert result is not None
        assert result.year == 2024

    def test_parse_yyyy_mm_dd(self, normalizer):
        result = normalizer._parse_date("2024-07-15")
        assert result == datetime(2024, 7, 15)

    def test_parse_mm_dd_yyyy_with_time(self, normalizer):
        result = normalizer._parse_date("07/15/2024 10:30:00")
        assert result == datetime(2024, 7, 15, 10, 30, 0)

    def test_parse_full_month_name(self, normalizer):
        result = normalizer._parse_date("July 15, 2024")
        assert result == datetime(2024, 7, 15)

    def test_parse_day_month_year(self, normalizer):
        result = normalizer._parse_date("15 July 2024")
        assert result == datetime(2024, 7, 15)

    def test_parse_dash_format(self, normalizer):
        result = normalizer._parse_date("07-15-2024")
        assert result == datetime(2024, 7, 15)

    def test_parse_none_returns_now(self, normalizer):
        """Missing dates default to now() to prevent pipeline failures."""
        from datetime import datetime
        assert isinstance(normalizer._parse_date(None), datetime)
        assert isinstance(normalizer._parse_date(""), datetime)

    def test_parse_unparseable_returns_now(self, normalizer):
        """Unparseable dates default to now()."""
        from datetime import datetime
        result = normalizer._parse_date("not a date at all")
        assert isinstance(result, datetime)


class TestStatusParsing:
    def test_active_status(self, normalizer):
        assert normalizer._parse_status("active") == FilingStatus.ACTIVE
        assert normalizer._parse_status("Active") == FilingStatus.ACTIVE
        assert normalizer._parse_status("filed") == FilingStatus.ACTIVE

    def test_terminated_status(self, normalizer):
        assert normalizer._parse_status("terminated") == FilingStatus.TERMINATED
        assert normalizer._parse_status("Termination") == FilingStatus.TERMINATED

    def test_lapsed_status(self, normalizer):
        assert normalizer._parse_status("lapsed") == FilingStatus.LAPSED
        assert normalizer._parse_status("expired") == FilingStatus.LAPSED

    def test_continued_status(self, normalizer):
        assert normalizer._parse_status("continuation") == FilingStatus.AMENDED

    def test_unknown_status(self, normalizer):
        assert normalizer._parse_status("unknown") == FilingStatus.UNKNOWN
        assert normalizer._parse_status("xyz") == FilingStatus.UNKNOWN


class TestDebtorParsing:
    def test_extracts_debtor_name(self, normalizer, sample_raw_fl):
        filing = normalizer.normalize(sample_raw_fl)
        assert filing.debtor.legal_name == "MIAMI RESTAURANT GROUP LLC"

    def test_extracts_dba_name(self, normalizer, sample_raw_fl):
        filing = normalizer.normalize(sample_raw_fl)
        assert filing.debtor.dba_name == "Joe's Seafood Shack"

    def test_extracts_debtor_address(self, normalizer, sample_raw_fl):
        filing = normalizer.normalize(sample_raw_fl)
        assert filing.debtor.city == "Miami"
        assert filing.debtor.state == "FL"

    def test_detects_entity_type_llc(self, normalizer, sample_raw_fl):
        filing = normalizer.normalize(sample_raw_fl)
        assert filing.debtor.entity_type == "LLC"

    def test_detects_entity_type_inc(self, normalizer, sample_raw_ny):
        filing = normalizer.normalize(sample_raw_ny)
        assert filing.debtor.entity_type == "INC"

    def test_falls_back_to_alternative_name_fields(self, normalizer):
        raw = {
            "state": "TX",
            "filing_number": "TX123",
            "filing_date": "01/01/2024",
            "debtorName": "ALT NAME CORP",
        }
        filing = normalizer.normalize(raw)
        assert filing.debtor.legal_name == "ALT NAME CORP"

    def test_uses_organization_name_fallback(self, normalizer):
        raw = {
            "state": "TX",
            "filing_number": "TX456",
            "filing_date": "01/01/2024",
            "organization_name": "ORG NAME LLC",
        }
        filing = normalizer.normalize(raw)
        assert filing.debtor.legal_name == "ORG NAME LLC"


class TestSecuredPartyParsing:
    def test_extracts_single_secured_party(self, normalizer, sample_raw_fl):
        filing = normalizer.normalize(sample_raw_fl)
        assert len(filing.secured_parties) == 1
        assert filing.secured_parties[0].legal_name == "YELLOWSTONE CAPITAL LLC"

    def test_extracts_multiple_secured_parties(self, normalizer):
        raw = {
            "state": "CA",
            "filing_number": "CA123",
            "filing_date": "01/01/2024",
            "debtor_name": "TEST LLC",
            "secured_parties": [
                {"name": "LENDER ONE LLC"},
                {"name": "LENDER TWO INC"},
            ],
        }
        filing = normalizer.normalize(raw)
        assert len(filing.secured_parties) == 2
        assert filing.secured_parties[0].legal_name == "LENDER ONE LLC"
        assert filing.secured_parties[1].legal_name == "LENDER TWO INC"

    def test_handles_string_secured_parties_list(self, normalizer):
        raw = {
            "state": "CA",
            "filing_number": "CA456",
            "filing_date": "01/01/2024",
            "debtor_name": "TEST LLC",
            "secured_parties": ["LENDER A", "LENDER B"],
        }
        filing = normalizer.normalize(raw)
        assert len(filing.secured_parties) == 2
        assert filing.secured_parties[0].legal_name == "LENDER A"


class TestFullNormalization:
    def test_normalizes_florida_filing(self, normalizer, sample_raw_fl):
        filing = normalizer.normalize(sample_raw_fl)
        assert filing.state == "FL"
        assert filing.filing_number == "2024000123456"
        assert filing.debtor.legal_name == "MIAMI RESTAURANT GROUP LLC"
        assert filing.debtor.dba_name == "Joe's Seafood Shack"
        assert filing.filing_date == datetime(2024, 7, 1)
        assert filing.status == FilingStatus.ACTIVE
        assert filing.collateral_description is not None

    def test_normalizes_ny_filing(self, normalizer, sample_raw_ny):
        filing = normalizer.normalize(sample_raw_ny)
        assert filing.state == "NY"
        assert filing.filing_number == "2024071500123"
        assert filing.debtor.legal_name == "BROOKLYN AUTO REPAIR INC"
        assert filing.status == FilingStatus.ACTIVE

    def test_preserves_collateral_description(self, normalizer, sample_raw_fl):
        filing = normalizer.normalize(sample_raw_fl)
        assert "future receivables" in filing.collateral_description.lower()

    def test_preserves_source_url(self, normalizer, sample_raw_fl):
        filing = normalizer.normalize(sample_raw_fl)
        assert filing.source_url == "https://www.floridaucc.com/filing/123456"

    def test_raw_json_preserved(self, normalizer, sample_raw_fl):
        filing = normalizer.normalize(sample_raw_fl)
        assert filing.raw_json is not None
        assert filing.raw_json["state"] == "FL"
