#!/usr/bin/env python3
"""Import skip-traced phone numbers back into the lead database.

Flow:
  1. Export leads: ucc-scrape export --tier B -o data/tier_b.csv
  2. Upload to skip-trace service (SkipGenie, BatchSkipTracing, etc.)
  3. Download enriched CSV with phone numbers
  4. Run: python3 scripts/import-phones.py --input enriched.csv
"""

import csv
import asyncio
import aiosqlite
from pathlib import Path
from thefuzz import fuzz


async def import_phones(
    input_path: Path,
    db_path: Path = Path("data/ucc_scraper.db"),
    dry_run: bool = False,
):
    """Import phone numbers from an enriched CSV back into the DB."""

    with open(input_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("No rows found in CSV")
        return

    # Check for phone column
    phone_cols = [c for c in rows[0].keys() if "phone" in c.lower()]
    biz_cols = [c for c in rows[0].keys() if "business" in c.lower() or "company" in c.lower() or "name" in c.lower()]

    if not phone_cols:
        print("No phone column found in CSV. Available columns:")
        for c in rows[0].keys():
            print(f"  {c}")
        return

    phone_col = phone_cols[0]
    print(f"Using phone column: '{phone_col}'")
    print(f"Business name columns: {biz_cols}")

    db = await aiosqlite.connect(str(db_path))

    # Get all leads for matching
    cursor = await db.execute("SELECT lead_id, business_name FROM leads")
    all_leads = [(r[0], r[1]) for r in await cursor.fetchall()]

    matched = 0
    updated = 0

    for row in rows:
        phone = (row.get(phone_col, "") or "").strip()
        if not phone or len(re.sub(r"[^\d]", "", phone)) < 10:
            continue

        # Find matching lead by business name
        csv_name = ""
        for col in biz_cols:
            csv_name = row.get(col, "").strip()
            if csv_name:
                break

        if not csv_name:
            continue

        # Fuzzy match against DB leads
        best_score = 0
        best_id = None
        for lead_id, db_name in all_leads:
            score = fuzz.token_sort_ratio(csv_name.upper(), (db_name or "").upper())
            if score > best_score and score >= 85:
                best_score = score
                best_id = lead_id

        if best_id:
            matched += 1
            if not dry_run:
                await db.execute(
                    "UPDATE leads SET phone_number = ? WHERE lead_id = ?",
                    (phone, best_id),
                )
                updated += 1

    await db.commit()
    await db.close()

    print(f"\nMatched: {matched}/{len(rows)} rows")
    print(f"Updated: {updated} leads with phone numbers")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Import skip-traced phone numbers into lead DB")
    p.add_argument("--input", "-i", required=True, help="CSV file with phone numbers")
    p.add_argument("--db", default="data/ucc_scraper.db", help="SQLite database path")
    p.add_argument("--dry-run", action="store_true", help="Don't actually update DB")

    args = p.parse_args()
    asyncio.run(import_phones(Path(args.input), Path(args.db), args.dry_run))
