"""Tests for Pydantic data models — UCCFiling, MCALead, LeadScore."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from models.filing import UCCFiling, FilingStatus, DebtorInfo, SecuredPartyInfo
from models.lead import MCALead, LeadScore, LeadTier


class TestDebtorInfo:
    def test_creates_with_minimum_fields(self):
        """DebtorInfo requires only legal_name."""
        debtor = DebtorInfo(legal_name="ACME CORP")
        assert debtor.legal_name == "ACME CORP"
        assert debtor.dba_name is None
        assert debtor.city is None
        assert debtor.entity_type is None

    def test_creates_with_all_fields(self):
        debtor = DebtorInfo(
            legal_name="ACME CORP",
            dba_name="Acme Widgets",
            address_line1="123 Main St",
            city="New York",
            state="NY",
            zip_code="10001",
            entity_type="CORP",
            raw_text="ACME CORP, 123 Main St, New York, NY 10001",
        )
        assert debtor.dba_name == "Acme Widgets"
        assert debtor.entity_type == "CORP"
        assert debtor.city == "New York"

    def test_legal_name_is_required(self):
        with pytest.raises(ValidationError):
            DebtorInfo()


class TestSecuredPartyInfo:
    def test_creates_with_name_only(self):
        sp = SecuredPartyInfo(legal_name="BANK OF AMERICA")
        assert sp.legal_name == "BANK OF AMERICA"
        assert sp.is_mca_funder is False
        assert sp.funder_tier is None

    def test_creates_mca_funder(self):
        sp = SecuredPartyInfo(
            legal_name="YELLOWSTONE CAPITAL LLC",
            is_mca_funder=True,
            funder_db_id="mca-001",
            funder_tier=1,
        )
        assert sp.is_mca_funder is True
        assert sp.funder_tier == 1

    def test_defaults_is_mca_funder_to_false(self):
        sp = SecuredPartyInfo(legal_name="UNKNOWN LENDER")
        assert sp.is_mca_funder is False


class TestFilingStatus:
    def test_all_statuses_exist(self):
        """Verify all expected statuses are available."""
        assert FilingStatus.ACTIVE == "active"
        assert FilingStatus.TERMINATED == "terminated"
        assert FilingStatus.AMENDED == "amended"
        assert FilingStatus.CONTINUED == "continued"
        assert FilingStatus.LAPSED == "lapsed"
        assert FilingStatus.UNKNOWN == "unknown"

    def test_status_is_str_enum(self):
        """FilingStatus values should be strings."""
        assert isinstance(FilingStatus.ACTIVE.value, str)


class TestUCCFiling:
    def test_creates_valid_filing(self, sample_filing):
        assert sample_filing.state == "FL"
        assert sample_filing.filing_number == "2024000123456"
        assert sample_filing.debtor.legal_name == "MIAMI RESTAURANT GROUP LLC"
        assert len(sample_filing.secured_parties) == 1
        assert sample_filing.secured_parties[0].legal_name == "YELLOWSTONE CAPITAL LLC"

    def test_filing_number_is_required(self):
        with pytest.raises(ValidationError):
            UCCFiling(state="CA", debtor=DebtorInfo(legal_name="TEST"))

    def test_state_must_be_two_chars(self):
        with pytest.raises(ValidationError):
            UCCFiling(
                filing_number="123",
                state="CAL",  # too long
                debtor=DebtorInfo(legal_name="TEST"),
                filing_date=datetime.now(),
            )

    def test_default_status_is_unknown(self):
        filing = UCCFiling(
            filing_number="123",
            state="NY",
            debtor=DebtorInfo(legal_name="TEST"),
            filing_date=datetime.now(),
        )
        assert filing.status == FilingStatus.UNKNOWN

    def test_scraped_at_auto_populated(self):
        filing = UCCFiling(
            filing_number="123",
            state="NY",
            debtor=DebtorInfo(legal_name="TEST"),
            filing_date=datetime.now(),
        )
        assert filing.scraped_at is not None
        assert isinstance(filing.scraped_at, datetime)

    def test_collateral_type_is_optional(self):
        filing = UCCFiling(
            filing_number="123",
            state="NY",
            debtor=DebtorInfo(legal_name="TEST"),
            filing_date=datetime.now(),
        )
        assert filing.collateral_type is None

    def test_secured_parties_defaults_to_empty_list(self):
        filing = UCCFiling(
            filing_number="123",
            state="NY",
            debtor=DebtorInfo(legal_name="TEST"),
            filing_date=datetime.now(),
        )
        assert filing.secured_parties == []


class TestLeadScore:
    def test_total_must_be_between_0_and_100(self):
        LeadScore(total=50)
        LeadScore(total=0)
        LeadScore(total=100)

    def test_total_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            LeadScore(total=150)
        with pytest.raises(ValidationError):
            LeadScore(total=-5)

    def test_individual_scores_have_correct_max(self):
        score = LeadScore(
            total=85,
            funder_match=25,
            recency=20,
            term_maturity=20,
            stacking=10,
            industry=5,
            vintage=2,
            filing_status=3,
        )
        assert score.funder_match <= 25
        assert score.recency <= 20
        assert score.term_maturity <= 20
        assert score.stacking <= 15
        assert score.industry <= 10
        assert score.vintage <= 5
        assert score.filing_status <= 5

    def test_individual_scores_out_of_range(self):
        with pytest.raises(ValidationError):
            LeadScore(total=85, funder_match=30)  # max 25
        with pytest.raises(ValidationError):
            LeadScore(total=85, recency=25)  # max 20


class TestLeadTier:
    def test_tier_values(self):
        assert LeadTier.A == "A"
        assert LeadTier.B == "B"
        assert LeadTier.C == "C"
        assert LeadTier.D == "D"

    def test_tier_is_str_enum(self):
        assert isinstance(LeadTier.A.value, str)


class TestMCALead:
    def test_creates_valid_lead(self, sample_filing):
        score = LeadScore(total=85, funder_match=25, recency=20, term_maturity=15,
                          stacking=10, industry=10, vintage=3, filing_status=2)
        lead = MCALead(
            lead_id="abc123def456",
            business_name="MIAMI RESTAURANT GROUP LLC",
            dba_name="Joe's Seafood Shack",
            mca_funder_name="YELLOWSTONE CAPITAL LLC",
            mca_funder_tier=1,
            score=score,
            tier=LeadTier.A,
            source_filing=sample_filing,
        )
        assert lead.tier == LeadTier.A
        assert lead.score.total == 85
        assert lead.business_name == "MIAMI RESTAURANT GROUP LLC"

    def test_to_csv_row_returns_dict(self, sample_filing):
        score = LeadScore(total=50, funder_match=10, recency=10, term_maturity=10,
                          stacking=5, industry=5, vintage=5, filing_status=5)
        lead = MCALead(
            lead_id="test123",
            business_name="TEST CO",
            mca_funder_name="TEST FUNDER",
            mca_funder_tier=1,
            score=score,
            tier=LeadTier.B,
            source_filing=sample_filing,
        )
        row = lead.to_csv_row()
        assert isinstance(row, dict)
        assert row["lead_id"] == "test123"
        assert row["business_name"] == "TEST CO"
        assert row["tier"] == "B"
        assert row["filing_date"] == sample_filing.filing_date.isoformat()

    def test_default_stack_count_is_one(self, sample_filing):
        score = LeadScore(total=50)
        lead = MCALead(
            lead_id="test",
            business_name="TEST",
            mca_funder_name="TEST",
            mca_funder_tier=1,
            score=score,
            tier=LeadTier.B,
            source_filing=sample_filing,
        )
        assert lead.stack_count == 1

    def test_lead_id_is_required(self):
        score = LeadScore(total=50)
        with pytest.raises(ValidationError):
            MCALead(
                business_name="TEST",
                mca_funder_name="TEST",
                mca_funder_tier=1,
                score=score,
                tier=LeadTier.B,
                source_filing=UCCFiling(
                    filing_number="1",
                    state="NY",
                    debtor=DebtorInfo(legal_name="TEST"),
                    filing_date=datetime.now(),
                ),
            )
