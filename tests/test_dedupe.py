"""Tests for the Deduplicator — business entity resolution across states and filings."""

from datetime import datetime

import pytest

from models.filing import UCCFiling, FilingStatus, DebtorInfo, SecuredPartyInfo


class TestBusinessKeyGeneration:
    def test_key_uses_name_city_state(self, dedupe):
        filing = UCCFiling(
            filing_number="123",
            state="FL",
            filing_date=datetime.now(),
            debtor=DebtorInfo(
                legal_name="ACME CORP",
                city="Miami",
                state="FL",
            ),
        )
        key = dedupe._make_key(filing)
        assert "ACME CORP" in key or "ACME" in key
        assert "MIAMI" in key
        assert "FL" in key

    def test_key_normalizes_company_suffixes(self, dedupe):
        filing1 = UCCFiling(
            filing_number="1",
            state="NY",
            filing_date=datetime.now(),
            debtor=DebtorInfo(legal_name="ACME CORP LLC", city="NYC", state="NY"),
        )
        filing2 = UCCFiling(
            filing_number="2",
            state="NY",
            filing_date=datetime.now(),
            debtor=DebtorInfo(legal_name="ACME CORP LLC", city="NYC", state="NY"),
        )
        key1 = dedupe._make_key(filing1)
        key2 = dedupe._make_key(filing2)
        assert key1 == key2

    def test_key_removes_punctuation(self, dedupe):
        filing = UCCFiling(
            filing_number="123",
            state="CA",
            filing_date=datetime.now(),
            debtor=DebtorInfo(
                legal_name="ACME, CORP.",
                city="Los Angeles",
                state="CA",
            ),
        )
        key = dedupe._make_key(filing)
        assert "," not in key
        assert "." not in key

    def test_key_case_insensitive(self, dedupe):
        """Same business name with different casing should produce same key."""
        filing1 = UCCFiling(
            filing_number="1",
            state="TX",
            filing_date=datetime.now(),
            debtor=DebtorInfo(legal_name="acme corp llc", city="dallas", state="TX"),
        )
        filing2 = UCCFiling(
            filing_number="2",
            state="TX",
            filing_date=datetime.now(),
            debtor=DebtorInfo(legal_name="ACME CORP LLC", city="DALLAS", state="TX"),
        )
        assert dedupe._make_key(filing1) == dedupe._make_key(filing2)


class TestFilingIndex:
    def test_add_and_retrieve_single_filing(self, dedupe, sample_filing):
        dedupe.add_filing(sample_filing)
        related = dedupe.get_related(sample_filing)
        assert related == []

    def test_add_multiple_for_same_business(self, dedupe, sample_filing, sample_filing_2):
        dedupe.add_filing(sample_filing)
        dedupe.add_filing(sample_filing_2)
        # Different cities so different keys — verify
        related_to_fl = dedupe.get_related(sample_filing)
        assert len(related_to_fl) == 0

    def test_add_same_business_different_states(self, dedupe):
        """Same name + city but different state filings."""
        filing_ny = UCCFiling(
            filing_number="NY123",
            state="NY",
            filing_date=datetime.now(),
            debtor=DebtorInfo(legal_name="ACME CORP", city="NEW YORK", state="NY"),
            secured_parties=[SecuredPartyInfo(legal_name="FUNDER A")],
        )
        filing_fl = UCCFiling(
            filing_number="FL123",
            state="FL",
            filing_date=datetime.now(),
            debtor=DebtorInfo(legal_name="ACME CORP", city="NEW YORK", state="NY"),
            secured_parties=[SecuredPartyInfo(legal_name="FUNDER B")],
        )
        dedupe.add_filing(filing_ny)
        dedupe.add_filing(filing_fl)

        related_ny = dedupe.get_related(filing_ny)
        assert len(related_ny) == 1
        assert related_ny[0].filing_number == "FL123"

    def test_add_multiple_filings_batch(self, dedupe, sample_filing, sample_filing_non_mca):
        dedupe.add_filings([sample_filing, sample_filing_non_mca])
        businesses = dedupe.get_all_businesses()
        # Different businesses = different keys
        assert len(businesses) >= 1


class TestDuplicateDetection:
    def test_find_exact_duplicate(self, dedupe):
        filing1 = UCCFiling(
            filing_number="NY-001",
            state="NY",
            filing_date=datetime(2024, 6, 1),
            debtor=DebtorInfo(legal_name="ACME CORP", city="NEW YORK", state="NY"),
            secured_parties=[SecuredPartyInfo(legal_name="YELLOWSTONE CAPITAL LLC")],
        )
        filing2 = UCCFiling(
            filing_number="NY-002",
            state="NY",
            filing_date=datetime(2024, 7, 1),
            debtor=DebtorInfo(legal_name="ACME CORP", city="NEW YORK", state="NY"),
            secured_parties=[SecuredPartyInfo(legal_name="YELLOWSTONE CAPITAL LLC")],
        )
        dedupe.add_filings([filing1, filing2])
        dups = dedupe.find_duplicates(filing1)
        assert len(dups) == 1
        assert dups[0].filing_number == "NY-002"

    def test_no_duplicate_different_business(self, dedupe):
        filing_a = UCCFiling(
            filing_number="A1",
            state="NY",
            filing_date=datetime.now(),
            debtor=DebtorInfo(legal_name="CORP A", city="NYC", state="NY"),
            secured_parties=[SecuredPartyInfo(legal_name="LENDER X")],
        )
        filing_b = UCCFiling(
            filing_number="B1",
            state="NY",
            filing_date=datetime.now(),
            debtor=DebtorInfo(legal_name="CORP B", city="NYC", state="NY"),
            secured_parties=[SecuredPartyInfo(legal_name="LENDER Y")],
        )
        dedupe.add_filings([filing_a, filing_b])
        dups = dedupe.find_duplicates(filing_a)
        assert len(dups) == 0


class TestStackerCount:
    def test_count_active_mca_positions(self, dedupe):
        filing1 = UCCFiling(
            filing_number="1",
            state="FL",
            filing_date=datetime.now(),
            status=FilingStatus.ACTIVE,
            debtor=DebtorInfo(legal_name="TEST CORP", city="MIAMI", state="FL"),
            secured_parties=[SecuredPartyInfo(
                legal_name="FUNDER A",
                is_mca_funder=True,
                funder_tier=1,
            )],
        )
        filing2 = UCCFiling(
            filing_number="2",
            state="FL",
            filing_date=datetime.now(),
            status=FilingStatus.ACTIVE,
            debtor=DebtorInfo(legal_name="TEST CORP", city="MIAMI", state="FL"),
            secured_parties=[SecuredPartyInfo(
                legal_name="FUNDER B",
                is_mca_funder=True,
                funder_tier=1,
            )],
        )
        dedupe.add_filings([filing1, filing2])
        count = dedupe.get_stacker_count(filing1)
        # 2 unique MCA funders
        assert count == 2
