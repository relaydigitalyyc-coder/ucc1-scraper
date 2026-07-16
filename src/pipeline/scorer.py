"""Lead scoring engine — ranks UCC filings by MCA lead quality.

Composite 0-100 score based on:
- Funder match quality (25 pts) — is the lender a known MCA funder?
- Recency (20 pts) — how recently was the filing made?
- Term maturity (20 pts) — estimated % of MCA term elapsed
- Stacking (15 pts) — multiple active MCA positions
- Industry (10 pts) — high-MCA-uptake industry?
- Vintage (5 pts) — time in business
- Filing status (5 pts) — active vs terminated
"""

from datetime import datetime, timedelta
from typing import Optional

from models.filing import UCCFiling, FilingStatus
from models.lead import MCALead, LeadScore, LeadTier


class LeadScorer:
    """Scores UCC filings as MCA leads."""

    # Industries with highest MCA uptake (by NAICS category keywords)
    HIGH_MCA_INDUSTRIES = [
        "restaurant", "food", "dining", "cafe", "catering",
        "trucking", "transport", "logistics", "freight", "carrier",
        "retail", "store", "shop", "merchant",
        "construction", "contractor", "builder", "renovation",
        "medical", "healthcare", "dental", "physician", "clinic", "pharmacy",
        "auto", "automotive", "repair", "body shop",
        "hotel", "motel", "hospitality", "lodging",
        "manufacturing", "wholesale", "distribution",
        "salon", "barber", "beauty", "spa",
        "gym", "fitness", "laundry", "dry clean",
    ]

    # Typical MCA terms in days
    TYPICAL_MCA_TERM_DAYS = 180  # 6 months is most common
    TERM_RANGE = (60, 540)  # Min 2 months, max 18 months

    def score(self, filing: UCCFiling, related_filings: Optional[list[UCCFiling]] = None) -> MCALead:
        """Score a single filing and produce an MCALead."""
        related = related_filings or []

        funder_score = self._score_funder(filing)
        recency_score = self._score_recency(filing)
        term_score = self._score_term_maturity(filing)
        stacking_score = self._score_stacking(filing, related)
        industry_score = self._score_industry(filing)
        vintage_score = self._score_vintage(filing)
        status_score = self._score_status(filing)

        total = (
            funder_score
            + recency_score
            + term_score
            + stacking_score
            + industry_score
            + vintage_score
            + status_score
        )

        score = LeadScore(
            total=total,
            funder_match=funder_score,
            recency=recency_score,
            term_maturity=term_score,
            stacking=stacking_score,
            industry=industry_score,
            vintage=vintage_score,
            filing_status=status_score,
        )

        tier = self._tier_from_score(total)

        # Get best funder name
        mca_funder = None
        funder_tier = 4
        for sp in filing.secured_parties:
            if sp.is_mca_funder and (sp.funder_tier or 4) < funder_tier:
                mca_funder = sp.legal_name
                funder_tier = sp.funder_tier or 4

        if not mca_funder and filing.secured_parties:
            mca_funder = filing.secured_parties[0].legal_name

        # Business info
        business_name = filing.debtor.dba_name or filing.debtor.legal_name

        # Stack count (MCA positions only)
        mca_related = [f for f in related if any(sp.is_mca_funder for sp in f.secured_parties)]
        stack_count = max(1, len(mca_related) + 1)  # +1 for this filing

        return MCALead(
            lead_id=self._make_lead_id(filing),
            business_name=business_name,
            dba_name=filing.debtor.dba_name,
            business_address=filing.debtor.address_line1,
            business_city=filing.debtor.city,
            business_state=filing.debtor.state,
            business_zip=filing.debtor.zip_code,
            mca_funder_name=mca_funder or "Unknown",
            mca_funder_tier=funder_tier,
            stack_count=stack_count,
            industry=None,  # Set by enrichment
            score=score,
            tier=tier,
            source_filing=filing,
            related_filings=related,
        )

    def _score_funder(self, filing: UCCFiling) -> int:
        """Score based on MCA funder match quality (0-25)."""
        if not filing.secured_parties:
            return 0

        best_tier = 4
        for sp in filing.secured_parties:
            if sp.is_mca_funder and sp.funder_tier:
                best_tier = min(best_tier, sp.funder_tier)

        if best_tier == 1:
            return 25  # Pure MCA funder
        elif best_tier == 2:
            return 18  # MCA + other lending
        elif best_tier == 3:
            return 10  # Adjacent alternative lender
        elif filing.collateral_type == "mca_receivables":
            return 8   # Not in DB but collateral looks MCA
        elif filing.collateral_type == "general_business_assets":
            return 4   # Could be MCA, could be traditional

        return 0

    def _score_recency(self, filing: UCCFiling) -> int:
        """Score based on how recently the filing was made (0-20)."""
        if not filing.filing_date:
            return 0

        days_ago = (datetime.now() - filing.filing_date).days

        if days_ago <= 30:
            return 20
        elif days_ago <= 60:
            return 16
        elif days_ago <= 90:
            return 12
        elif days_ago <= 120:
            return 8
        elif days_ago <= 180:
            return 4
        else:
            return 0

    def _score_term_maturity(self, filing: UCCFiling) -> int:
        """Score based on estimated % of MCA term elapsed (0-20).

        The insight: leads are HOTTEST when the current MCA is ~80%+ paid off
        because the business will need renewal/refinancing capital soon.
        """
        if not filing.filing_date:
            return 10  # Unknown — give middle score

        days_elapsed = (datetime.now() - filing.filing_date).days
        estimated_term = self.TYPICAL_MCA_TERM_DAYS
        pct_elapsed = (days_elapsed / estimated_term) * 100

        # Sweet spot: 70-95% elapsed — they're almost done & need more capital
        if 70 <= pct_elapsed <= 95:
            return 20
        elif 50 <= pct_elapsed < 70:
            return 15
        elif 30 <= pct_elapsed < 50:
            return 10
        elif pct_elapsed > 95:
            # Might have already paid off or renewed elsewhere
            return 8
        elif 10 <= pct_elapsed < 30:
            return 5
        else:
            return 2  # Just filed — won't need capital for months

    def _score_stacking(self, filing: UCCFiling, related: list[UCCFiling]) -> int:
        """Score based on multiple active MCA positions (0-15).

        Stackers (2+ MCA positions) are the most desperate for capital.
        """
        mca_count = 1  # This filing
        for rel in related:
            if any(sp.is_mca_funder for sp in rel.secured_parties):
                mca_count += 1

        if mca_count >= 5:
            return 15  # Deep stacker — very high intent
        elif mca_count == 4:
            return 13
        elif mca_count == 3:
            return 11
        elif mca_count == 2:
            return 7
        else:
            return 0

    def _score_industry(self, filing: UCCFiling) -> int:
        """Score based on industry MCA uptake (0-10).

        Currently based on debtor name keywords — full enrichment would use NAICS.
        """
        name = (filing.debtor.legal_name + " " + (filing.debtor.dba_name or "")).lower()

        for industry_kw in self.HIGH_MCA_INDUSTRIES:
            if industry_kw in name:
                return 10

        return 3  # Unknown — neutral score

    def _score_vintage(self, filing: UCCFiling) -> int:
        """Score based on business vintage (0-5).

        Currently placeholder — would use incorporation date from enrichment.
        """
        # Without enrichment, we can't determine vintage
        # Give a neutral score
        return 3

    def _score_status(self, filing: UCCFiling) -> int:
        """Score based on filing status (0-5)."""
        if filing.status == FilingStatus.ACTIVE:
            return 5
        elif filing.status == FilingStatus.CONTINUED:
            return 4
        elif filing.status == FilingStatus.AMENDED:
            return 2
        elif filing.status == FilingStatus.TERMINATED:
            return 0
        elif filing.status == FilingStatus.LAPSED:
            return 0
        else:
            return 2

    def _tier_from_score(self, score: int) -> LeadTier:
        """Map numerical score to lead tier."""
        if score >= 80:
            return LeadTier.A
        elif score >= 60:
            return LeadTier.B
        elif score >= 40:
            return LeadTier.C
        else:
            return LeadTier.D

    def _make_lead_id(self, filing: UCCFiling) -> str:
        """Generate a unique lead ID."""
        import hashlib
        raw = f"{filing.state}:{filing.filing_number}:{filing.debtor.legal_name}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]
