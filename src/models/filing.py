"""Core data models for UCC filings."""

from datetime import datetime
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field


class FilingStatus(StrEnum):
    ACTIVE = "active"
    AMENDED = "amended"
    CONTINUED = "continued"
    TERMINATED = "terminated"
    LAPSED = "lapsed"
    UNKNOWN = "unknown"


class DebtorInfo(BaseModel):
    """Normalized debtor (borrower) information."""
    legal_name: str = Field(description="Legal business name as it appears on filing")
    dba_name: Optional[str] = Field(default=None, description="DBA or trade name if different")
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    entity_type: Optional[str] = Field(default=None, description="LLC, Corp, LP, etc.")
    raw_text: Optional[str] = Field(default=None, description="Raw debtor text from filing")


class SecuredPartyInfo(BaseModel):
    """Normalized secured party (lender) information."""
    legal_name: str = Field(description="Legal name of the secured party")
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    is_mca_funder: bool = Field(default=False, description="Matched to known MCA funder DB")
    funder_db_id: Optional[str] = Field(default=None, description="ID in MCA funder database")
    funder_tier: Optional[int] = Field(default=None, description="1=Pure MCA, 2=MCA+Other, 3=Adjacent, 4=Traditional")
    raw_text: Optional[str] = Field(default=None, description="Raw secured party text from filing")


class UCCFiling(BaseModel):
    """A single UCC-1 financing statement from any state."""

    # Identity
    filing_number: str = Field(description="State-assigned filing/document number")
    state: str = Field(description="Two-letter state code", min_length=2, max_length=2)
    source_url: Optional[str] = Field(default=None, description="URL where filing was found")

    # Dates
    filing_date: Optional[datetime] = Field(default_factory=datetime.now, description="Date the UCC-1 was filed")
    lapse_date: Optional[datetime] = Field(default=None, description="When the filing lapses (typically filing_date + 5 years)")
    termination_date: Optional[datetime] = Field(default=None)

    # Status
    status: FilingStatus = Field(default=FilingStatus.UNKNOWN)

    # Parties
    debtor: DebtorInfo = Field(description="The business that received financing")
    secured_parties: list[SecuredPartyInfo] = Field(
        default_factory=list,
        description="Lenders/funders listed as secured parties"
    )

    # Collateral
    collateral_description: Optional[str] = Field(
        default=None,
        description="Full text of collateral description from the filing"
    )
    collateral_type: Optional[str] = Field(
        default=None,
        description="Classified type: mca_receivables, equipment, inventory, general_business, real_estate, etc."
    )

    # Amendments / History
    amendments: list[dict] = Field(default_factory=list, description="Amendment history")
    original_filing_number: Optional[str] = Field(default=None, description="If this is an amendment, the original filing number")

    # Metadata
    scraped_at: datetime = Field(default_factory=datetime.now)
    raw_json: Optional[dict] = Field(default=None, description="Raw data from the source (for debugging)")

# Enable WAL mode for concurrent enrichment writers
_ENABLE_WAL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;
"""
