"""Shared fixtures for UCC-1 scraper tests."""

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest

from models.filing import UCCFiling, FilingStatus, DebtorInfo, SecuredPartyInfo
from models.lead import MCALead, LeadScore, LeadTier
from pipeline.classifier import MCAClassifier
from pipeline.dedupe import Deduplicator
from pipeline.normalizer import FilingNormalizer
from pipeline.scorer import LeadScorer
from storage import Storage


@pytest.fixture
def sample_raw_fl():
    """Raw filing dict from Florida scraper."""
    return {
        "state": "FL",
        "filing_number": "2024000123456",
        "filing_date": "07/01/2024",
        "debtor_name": "MIAMI RESTAURANT GROUP LLC",
        "dba_name": "Joe's Seafood Shack",
        "debtor_address": "123 Ocean Drive",
        "debtor_city": "Miami",
        "debtor_state": "FL",
        "debtor_zip": "33139",
        "secured_party_name": "YELLOWSTONE CAPITAL LLC",
        "status": "active",
        "collateral_description": "All present and future accounts, chattel paper, "
        "deposit accounts, instruments, investment property, letter-of-credit rights, "
        "and general intangibles, including all future receivables.",
        "detail_url": "https://www.floridaucc.com/filing/123456",
    }


@pytest.fixture
def sample_raw_ny():
    """Raw filing dict from NY scraper."""
    return {
        "state": "NY",
        "filing_number": "2024071500123",
        "filing_date": "07/15/2024",
        "debtor_name": "BROOKLYN AUTO REPAIR INC",
        "debtor_city": "Brooklyn",
        "debtor_state": "NY",
        "secured_party_name": "PARAMOUNT FUNDING LLC",
        "status": "filed",
        "collateral_description": "Confession of Judgment. All assets now owned "
        "or hereafter acquired.",
        "raw_cells": {
            "debtor": "BROOKLYN AUTO REPAIR INC",
            "secured_party": "PARAMOUNT FUNDING LLC",
        },
    }


@pytest.fixture
def sample_filing(sample_raw_fl):
    """Normalized UCCFiling from FL raw data."""
    normalizer = FilingNormalizer()
    filing = normalizer.normalize(sample_raw_fl)

    # Manually set MCA funder match for testing
    for sp in filing.secured_parties:
        sp.is_mca_funder = True
        sp.funder_tier = 1
        sp.funder_db_id = "mca-001"

    filing.collateral_type = "mca_receivables"
    return filing


@pytest.fixture
def sample_filing_2(sample_raw_ny):
    """Second normalized filing from NY — same business, different state."""
    normalizer = FilingNormalizer()
    filing = normalizer.normalize(sample_raw_ny)
    for sp in filing.secured_parties:
        sp.is_mca_funder = True
        sp.funder_tier = 1
    return filing


@pytest.fixture
def sample_filing_non_mca():
    """A non-MCA filing (equipment loan from traditional bank)."""
    return UCCFiling(
        filing_number="2024000987654",
        state="CA",
        filing_date=datetime(2024, 6, 15),
        status=FilingStatus.ACTIVE,
        debtor=DebtorInfo(
            legal_name="VALLEY CONSTRUCTION INC",
            city="Fresno",
            state="CA",
            entity_type="INC",
        ),
        secured_parties=[
            SecuredPartyInfo(
                legal_name="BANK OF AMERICA NA",
                is_mca_funder=False,
            )
        ],
        collateral_description="One (1) 2024 Caterpillar 320 Hydraulic Excavator, S/N CAT00320ABC45678",
        collateral_type="equipment",
    )


@pytest.fixture
def normalizer():
    return FilingNormalizer()


@pytest.fixture
def classifier():
    return MCAClassifier()


@pytest.fixture
def scorer():
    return LeadScorer()


@pytest.fixture
def dedupe():
    return Deduplicator()


@pytest.fixture
async def storage():
    """In-memory SQLite storage for testing."""
    s = Storage(db_path=Path(tempfile.mktemp(suffix=".db")))
    await s.init()
    return s


@pytest.fixture
def funder_db_tmp(tmp_path):
    """Create a temporary funder DB for testing."""
    db_path = tmp_path / "funder_db.json"
    data = {
        "_meta": {"version": "1.0.0", "last_updated": "2026-07-16", "total_funders": 4},
        "funders": [
            {
                "id": "mca-001",
                "legal_name": "YELLOWSTONE CAPITAL LLC",
                "dbas": ["YSC"],
                "tier": 1,
                "typical_advance": "5000-500000",
                "typical_term_days": 180,
                "states_active": ["ALL"],
                "notes": "",
            },
            {
                "id": "mca-002",
                "legal_name": "PARAMOUNT FUNDING LLC",
                "dbas": ["Paramount Merchant Funding"],
                "tier": 1,
                "typical_advance": "5000-500000",
                "typical_term_days": 180,
                "states_active": ["ALL"],
                "notes": "",
            },
            {
                "id": "mca-010",
                "legal_name": "CELTIC BANK CORPORATION",
                "dbas": [],
                "tier": 2,
                "typical_advance": "50000-5000000",
                "typical_term_days": 365,
                "states_active": ["ALL"],
                "notes": "",
            },
            {
                "id": "mca-050",
                "legal_name": "BALBOA CAPITAL CORPORATION",
                "dbas": [],
                "tier": 3,
                "typical_advance": "25000-1500000",
                "typical_term_days": 365,
                "states_active": ["ALL"],
                "notes": "",
            },
        ],
    }
    with open(db_path, "w") as f:
        json.dump(data, f)
    return db_path
