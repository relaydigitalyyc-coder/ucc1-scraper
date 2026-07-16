"""SQLite storage for UCC filings and MCA leads."""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

from models.filing import UCCFiling, FilingStatus
from models.lead import MCALead, LeadTier


class Storage:
    """Async SQLite-backed persistence for scraped filings and generated leads."""

    def __init__(self, db_path: Path = Path("data/ucc_scraper.db")):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self):
        """Create tables if they don't exist."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS filings (
                    filing_number TEXT NOT NULL,
                    state TEXT NOT NULL,
                    business_name TEXT NOT NULL,
                    dba_name TEXT,
                    filing_date TEXT NOT NULL,
                    lapse_date TEXT,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    funder_name TEXT,
                    funder_tier INTEGER,
                    is_mca_filer INTEGER DEFAULT 0,
                    collateral_type TEXT,
                    collateral_text TEXT,
                    source_url TEXT,
                    raw_json TEXT,
                    scraped_at TEXT NOT NULL,
                    PRIMARY KEY (filing_number, state)
                );

                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    business_name TEXT NOT NULL,
                    dba_name TEXT,
                    business_address TEXT,
                    business_city TEXT,
                    business_state TEXT,
                    business_zip TEXT,
                    phone_number TEXT,
                    mca_funder_name TEXT NOT NULL,
                    mca_funder_tier INTEGER,
                    stack_count INTEGER DEFAULT 1,
                    score_total INTEGER NOT NULL,
                    tier TEXT NOT NULL,
                    filing_number TEXT NOT NULL,
                    filing_state TEXT NOT NULL,
                    filing_date TEXT NOT NULL,
                    collateral_type TEXT,
                    raw_json TEXT,
                    generated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_filings_date ON filings(filing_date);
                CREATE INDEX IF NOT EXISTS idx_filings_state ON filings(state);
                CREATE INDEX IF NOT EXISTS idx_filings_business ON filings(business_name);
                CREATE INDEX IF NOT EXISTS idx_filings_mca ON filings(is_mca_filer);
                CREATE INDEX IF NOT EXISTS idx_leads_tier ON leads(tier);
                CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score_total DESC);
                CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(filing_state);
                CREATE INDEX IF NOT EXISTS idx_leads_date ON leads(filing_date);
            """)
            await db.commit()

    async def save_filing(self, filing: UCCFiling):
        """Insert or update a UCC filing."""
        funder_name = None
        funder_tier = None
        is_mca = 0

        for sp in filing.secured_parties:
            if sp.is_mca_funder:
                is_mca = 1
                if sp.funder_tier and (funder_tier is None or sp.funder_tier < funder_tier):
                    funder_name = sp.legal_name
                    funder_tier = sp.funder_tier

        if not funder_name and filing.secured_parties:
            funder_name = filing.secured_parties[0].legal_name

        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.execute(
                """INSERT OR REPLACE INTO filings
                   (filing_number, state, business_name, dba_name, filing_date,
                    lapse_date, status, funder_name, funder_tier, is_mca_filer,
                    collateral_type, collateral_text, source_url, raw_json, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    filing.filing_number,
                    filing.state,
                    filing.debtor.legal_name,
                    filing.debtor.dba_name,
                    filing.filing_date.isoformat() if filing.filing_date else "",
                    filing.lapse_date.isoformat() if filing.lapse_date else None,
                    filing.status.value,
                    funder_name,
                    funder_tier,
                    is_mca,
                    filing.collateral_type,
                    filing.collateral_description[:5000] if filing.collateral_description else None,
                    filing.source_url,
                    json.dumps(filing.raw_json) if filing.raw_json else None,
                    datetime.now().isoformat(),
                ),
            )
            await db.commit()

    async def save_filings(self, filings: list[UCCFiling]):
        """Save multiple filings."""
        for f in filings:
            await self.save_filing(f)

    async def save_lead(self, lead: MCALead):
        """Save an MCA lead."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.execute(
                """INSERT OR REPLACE INTO leads
                   (lead_id, business_name, dba_name, business_address,
                    business_city, business_state, business_zip, phone_number,
                    mca_funder_name, mca_funder_tier, stack_count,
                    score_total, tier, filing_number, filing_state,
                    filing_date, collateral_type, raw_json, generated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lead.lead_id,
                    lead.business_name,
                    lead.dba_name,
                    lead.business_address,
                    lead.business_city,
                    lead.business_state,
                    lead.business_zip,
                    lead.phone_number,
                    lead.mca_funder_name,
                    lead.mca_funder_tier,
                    lead.stack_count,
                    lead.score.total,
                    lead.tier.value,
                    lead.source_filing.filing_number,
                    lead.source_filing.state,
                    lead.source_filing.filing_date.isoformat() if lead.source_filing.filing_date else "",
                    lead.source_filing.collateral_type,
                    None,  # raw_json — could serialize full lead
                    datetime.now().isoformat(),
                ),
            )
            await db.commit()

    async def save_leads(self, leads: list[MCALead]):
        """Save multiple leads."""
        for lead in leads:
            await self.save_lead(lead)

    async def get_lead_count(self) -> int:
        """Get total lead count."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM leads")
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_tier_counts(self) -> dict[str, int]:
        """Get lead counts by tier."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            cursor = await db.execute("SELECT tier, COUNT(*) FROM leads GROUP BY tier")
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}

    async def get_leads_by_tier(self, tier: str, limit: int = 100) -> list[dict]:
        """Get leads for a specific tier (string like 'A' or LeadTier enum)."""
        tier_val = tier.value if hasattr(tier, 'value') else str(tier)
        async with aiosqlite.connect(str(self.db_path)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM leads WHERE tier = ? ORDER BY score_total DESC LIMIT ?",
                (tier_val, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_recent_filing_numbers(self, days: int = 30) -> set[str]:
        """Get filing numbers scraped recently (for dedup)."""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        async with aiosqlite.connect(str(self.db_path)) as db:
            cursor = await db.execute(
                "SELECT filing_number, state FROM filings WHERE scraped_at > ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            return {f"{row[0]}|{row[1]}" for row in rows}

    async def filing_exists(self, filing_number: str, state: str) -> bool:
        """Check if a filing already exists in the DB."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            cursor = await db.execute(
                "SELECT 1 FROM filings WHERE filing_number = ? AND state = ?",
                (filing_number, state),
            )
            row = await cursor.fetchone()
            return row is not None
