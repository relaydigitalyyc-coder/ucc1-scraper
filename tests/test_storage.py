"""Tests for async SQLite storage layer."""

from datetime import datetime

import pytest

from models.filing import UCCFiling, FilingStatus, DebtorInfo, SecuredPartyInfo
from models.lead import MCALead, LeadScore, LeadTier


@pytest.mark.asyncio
class TestStorageInit:
    async def test_init_creates_tables(self, storage):
        """Tables should be created without error."""
        # If no exception, tables exist
        count = await storage.get_lead_count()
        assert count == 0


@pytest.mark.asyncio
class TestFilingStorage:
    async def test_save_and_check_exists(self, storage, sample_filing):
        await storage.save_filing(sample_filing)
        exists = await storage.filing_exists(sample_filing.filing_number, sample_filing.state)
        assert exists is True

    async def test_filing_not_exists(self, storage):
        exists = await storage.filing_exists("NONEXIST", "ZZ")
        assert exists is False

    async def test_save_multiple_filings(self, storage, sample_filing):
        filing2 = UCCFiling(
            filing_number="FL-002",
            state="FL",
            filing_date=datetime(2024, 7, 10),
            debtor=DebtorInfo(legal_name="ANOTHER CORP", city="Tampa", state="FL"),
            secured_parties=[SecuredPartyInfo(legal_name="FUNDER X")],
        )
        await storage.save_filings([sample_filing, filing2])
        exists1 = await storage.filing_exists(sample_filing.filing_number, sample_filing.state)
        exists2 = await storage.filing_exists("FL-002", "FL")
        assert exists1 and exists2

    async def test_upsert_filing(self, storage, sample_filing):
        """Saving the same filing twice should be an upsert (no error)."""
        await storage.save_filing(sample_filing)
        # Modify and re-save
        sample_filing.status = FilingStatus.TERMINATED
        await storage.save_filing(sample_filing)
        # Should not raise
        assert True

    async def test_save_filing_with_mca_funder(self, storage, sample_filing):
        await storage.save_filing(sample_filing)
        exists = await storage.filing_exists(sample_filing.filing_number, sample_filing.state)
        assert exists


@pytest.mark.asyncio
class TestLeadStorage:
    async def test_save_and_count_lead(self, storage, sample_filing):
        score = LeadScore(total=85, funder_match=25, recency=20, term_maturity=15,
                          stacking=10, industry=10, vintage=3, filing_status=2)
        lead = MCALead(
            lead_id="test-lead-001",
            business_name="TEST BUSINESS LLC",
            mca_funder_name="YELLOWSTONE CAPITAL LLC",
            mca_funder_tier=1,
            stack_count=1,
            score=score,
            tier=LeadTier.A,
            source_filing=sample_filing,
        )
        await storage.save_lead(lead)
        count = await storage.get_lead_count()
        assert count == 1

    async def test_save_multiple_leads(self, storage, sample_filing):
        for i in range(5):
            score = LeadScore(total=60 + i, funder_match=15, recency=10, term_maturity=10,
                              stacking=5, industry=5, vintage=5, filing_status=5)
            lead = MCALead(
                lead_id=f"lead-{i:03d}",
                business_name=f"BUSINESS {i}",
                mca_funder_name="FUNDER LLC",
                mca_funder_tier=1,
                score=score,
                tier=LeadTier.B if i < 3 else LeadTier.C,
                source_filing=sample_filing,
            )
            await storage.save_lead(lead)
        count = await storage.get_lead_count()
        assert count == 5

    async def test_get_tier_counts(self, storage, sample_filing):
        tiers_data = [
            ("lead-a1", LeadTier.A, 90),
            ("lead-a2", LeadTier.A, 85),
            ("lead-b1", LeadTier.B, 70),
            ("lead-b2", LeadTier.B, 65),
            ("lead-c1", LeadTier.C, 50),
        ]
        for lead_id, tier, total in tiers_data:
            score = LeadScore(total=total)
            lead = MCALead(
                lead_id=lead_id,
                business_name="TEST",
                mca_funder_name="FUNDER",
                mca_funder_tier=1,
                score=score,
                tier=tier,
                source_filing=sample_filing,
            )
            await storage.save_lead(lead)

        counts = await storage.get_tier_counts()
        assert counts.get("A", 0) == 2
        assert counts.get("B", 0) == 2
        assert counts.get("C", 0) == 1

    async def test_get_leads_by_tier(self, storage, sample_filing):
        score = LeadScore(total=85)
        lead = MCALead(
            lead_id="hot-lead-1",
            business_name="HOT LEAD CORP",
            dba_name="Hot Restaurant",
            business_city="Miami",
            business_state="FL",
            mca_funder_name="YELLOWSTONE CAPITAL LLC",
            mca_funder_tier=1,
            score=score,
            tier=LeadTier.A,
            source_filing=sample_filing,
        )
        await storage.save_lead(lead)

        tier_a = await storage.get_leads_by_tier(LeadTier.A, limit=10)
        assert len(tier_a) == 1
        assert tier_a[0]["business_name"] == "HOT LEAD CORP"
        assert tier_a[0]["tier"] == "A"

    async def test_tier_filter_empty(self, storage):
        """Querying a tier with no leads should return empty list."""
        empty = await storage.get_leads_by_tier(LeadTier.A, limit=10)
        assert empty == []

    async def test_upsert_lead(self, storage, sample_filing):
        """Saving same lead twice should upsert (no error)."""
        score = LeadScore(total=80)
        lead = MCALead(
            lead_id="same-lead",
            business_name="SAME CORP",
            mca_funder_name="FUNDER",
            mca_funder_tier=1,
            score=score,
            tier=LeadTier.A,
            source_filing=sample_filing,
        )
        await storage.save_lead(lead)
        lead.tier = LeadTier.B
        await storage.save_lead(lead)
        count = await storage.get_lead_count()
        assert count == 1


@pytest.mark.asyncio
class TestRecentFilingNumbers:
    async def test_recent_filing_lookup(self, storage, sample_filing):
        await storage.save_filing(sample_filing)
        recent = await storage.get_recent_filing_numbers(days=30)
        assert len(recent) >= 1
        key = f"{sample_filing.filing_number}|{sample_filing.state}"
        assert key in recent
