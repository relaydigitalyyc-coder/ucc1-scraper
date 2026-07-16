"""Lead generation models — MCA-enriched filings turned into actionable leads."""

from datetime import datetime
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field, computed_field

from models.filing import UCCFiling


class LeadTier(StrEnum):
    A = "A"  # Hot: 80-100 — call immediately
    B = "B"  # Warm: 60-79 — queue this week
    C = "C"  # Cold: 40-59 — nurture
    D = "D"  # Archive: <40 — low priority


class LeadScore(BaseModel):
    """Breakdown of the composite lead score (0-100)."""
    total: int = Field(ge=0, le=100)

    funder_match: int = Field(default=0, ge=0, le=25, description="/25 — known MCA funder match quality")
    recency: int = Field(default=0, ge=0, le=20, description="/20 — how recently filed")
    term_maturity: int = Field(default=0, ge=0, le=20, description="/20 — estimated % of term complete")
    stacking: int = Field(default=0, ge=0, le=15, description="/15 — multiple active MCA positions")
    industry: int = Field(default=0, ge=0, le=10, description="/10 — industry MCA uptake")
    vintage: int = Field(default=0, ge=0, le=5, description="/5 — time in business")
    filing_status: int = Field(default=0, ge=0, le=5, description="/5 — active vs terminated")


class MCALead(BaseModel):
    """An enriched UCC filing turned into an MCA sales lead."""

    # Core identity
    lead_id: str = Field(description="Unique lead ID (hash of normalized business + filing)")
    business_name: str = Field(description="Best available business name")
    dba_name: Optional[str] = None
    business_address: Optional[str] = None
    business_city: Optional[str] = None
    business_state: Optional[str] = None
    business_zip: Optional[str] = None

    # Contact (from enrichment)
    phone_number: Optional[str] = Field(default=None, description="Business phone from skip trace")
    contact_name: Optional[str] = Field(default=None, description="Owner/principal name from enrichment")
    email: Optional[str] = None
    website: Optional[str] = None

    # MCA position
    mca_funder_name: str = Field(description="The MCA funder on the UCC")
    mca_funder_tier: int = Field(description="1=Pure MCA, 2=MCA+Other, 3=Adjacent")
    estimated_advance_amount: Optional[float] = Field(default=None, description="Estimated advance size in USD")
    estimated_term_days: Optional[int] = Field(default=None, description="Estimated MCA term in days")
    estimated_remaining_days: Optional[int] = Field(default=None, description="Days until estimated payoff")
    stack_count: int = Field(default=1, description="Number of active MCA positions found")
    total_mca_exposure: Optional[float] = Field(default=None, description="Sum of all estimated MCA positions")

    # Classification
    industry: Optional[str] = Field(default=None, description="Business industry / NAICS category")
    years_in_business: Optional[float] = None

    # Lead scoring
    score: LeadScore
    tier: LeadTier

    # Source data
    source_filing: UCCFiling = Field(description="Original UCC filing this lead derives from")
    related_filings: list[UCCFiling] = Field(
        default_factory=list,
        description="Other UCC filings for same business (for stacking context)"
    )

    # Metadata
    generated_at: datetime = Field(default_factory=datetime.now)
    notes: list[str] = Field(default_factory=list)

    def to_csv_row(self) -> dict:
        """Flatten lead to a CSV-friendly dictionary."""
        return {
            "lead_id": self.lead_id,
            "business_name": self.business_name,
            "dba_name": self.dba_name or "",
            "business_address": self.business_address or "",
            "business_city": self.business_city or "",
            "business_state": self.business_state or "",
            "business_zip": self.business_zip or "",
            "phone_number": self.phone_number or "",
            "contact_name": self.contact_name or "",
            "email": self.email or "",
            "website": self.website or "",
            "mca_funder_name": self.mca_funder_name,
            "mca_funder_tier": self.mca_funder_tier,
            "estimated_advance_amount": self.estimated_advance_amount or "",
            "estimated_term_days": self.estimated_term_days or "",
            "stack_count": self.stack_count,
            "industry": self.industry or "",
            "years_in_business": self.years_in_business or "",
            "score_total": self.score.total,
            "tier": self.tier.value,
            "filing_date": self.source_filing.filing_date.isoformat(),
            "filing_state": self.source_filing.state,
            "filing_number": self.source_filing.filing_number,
            "filing_status": self.source_filing.status.value,
            "collateral_type": self.source_filing.collateral_type or "",
            "generated_at": self.generated_at.isoformat(),
        }
