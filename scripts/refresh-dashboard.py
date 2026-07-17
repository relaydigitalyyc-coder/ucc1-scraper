#!/usr/bin/env python3
"""Export latest leads from SQLite to dashboard JSON.

Run after each scrape to keep dashboard current.
Usage: python3 scripts/refresh-dashboard.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import aiosqlite


async def main():
    db_path = Path("data/ucc_scraper.db")
    if not db_path.exists():
        print("No database found. Run a scrape first.", file=sys.stderr)
        sys.exit(1)

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row

    # Get all leads — phone-populated first, then by score
    cursor = await db.execute(
        "SELECT * FROM leads ORDER BY CASE WHEN phone_number IS NOT NULL AND phone_number != '' THEN 0 ELSE 1 END, score_total DESC LIMIT 1000"
    )
    rows = await cursor.fetchall()
    leads = [dict(r) for r in rows]

    # Get summary stats
    cursor2 = await db.execute(
        "SELECT tier, COUNT(*) as cnt FROM leads GROUP BY tier"
    )
    tier_rows = await cursor2.fetchall()
    tiers = {r["tier"]: r["cnt"] for r in tier_rows}

    cursor3 = await db.execute("SELECT COUNT(*) as cnt FROM leads")
    total = (await cursor3.fetchone())["cnt"]

    cursor4 = await db.execute(
        "SELECT COUNT(*) as cnt FROM leads WHERE mca_funder_tier IN (1, 2)"
    )
    mca_count = (await cursor4.fetchone())["cnt"]

    cursor5 = await db.execute(
        "SELECT COUNT(*) as cnt FROM leads WHERE phone_number IS NOT NULL AND phone_number != ''"
    )
    phones = (await cursor5.fetchone())["cnt"]

    await db.close()

    # Build dashboard payload
    dashboard = {
        "updated": __import__("datetime").datetime.now().isoformat(),
        "stats": {
            "total": total,
            "hot": tiers.get("A", 0),
            "warm": tiers.get("B", 0),
            "cold": tiers.get("C", 0),
            "archive": tiers.get("D", 0),
            "mca_match_rate": round(mca_count / total * 100, 1) if total else 0,
            "with_phones": phones,
        },
        "leads": leads,
    }

    # Write to dashboard data dir
    out_dir = Path("dashboard/data")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "leads.json"

    with open(out_path, "w") as f:
        json.dump(dashboard, f, indent=2, default=str)

    print(f"✓ Dashboard refreshed: {total} leads → {out_path}")
    print(f"  Hot: {tiers.get('A', 0)}, Warm: {tiers.get('B', 0)}, "
          f"Cold: {tiers.get('C', 0)}, MCA rate: {dashboard['stats']['mca_match_rate']}%")


if __name__ == "__main__":
    asyncio.run(main())
