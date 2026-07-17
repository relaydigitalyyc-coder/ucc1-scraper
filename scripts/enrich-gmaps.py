#!/usr/bin/env python3
"""Enrich warm leads with phone numbers from Google Maps.

Reads leads from the SQLite database, queries Google Maps for each business,
and extracts phone numbers using Playwright browser automation.

Usage:
  # Enrich top 20 warm (tier B) leads
  python3 scripts/enrich-gmaps.py --tier B --limit 20

  # Enrich all leads without phones
  python3 scripts/enrich-gmaps.py --all

  # Show cache stats only
  python3 scripts/enrich-gmaps.py --stats

  # Headed mode (useful for debugging captcha issues)
  python3 scripts/enrich-gmaps.py --headed --limit 5

Output:
  - Updates leads.phone_number in the database
  - Reports hit rate and cache statistics
"""

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

import aiosqlite

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipeline.gmaps_enricher import GoogleMapsEnricher, clean_phone, make_business_key

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Suppress Playwright noise
logging.getLogger("playwright").setLevel(logging.WARNING)


def clean_business_name(raw: str) -> str:
    """Remove entity suffixes for better Google Maps search matching."""
    cleaned = re.sub(r"\s+(LLC|L\.L\.C\.|INC|INC\.|CORP|CORP\.|CORPORATION|L\.P\.|LP|P\.A\.)\s*$", "", raw, flags=re.IGNORECASE)
    return cleaned.strip()


async def show_cache_stats(enricher: GoogleMapsEnricher):
    """Display cache hit statistics."""
    stats = enricher.cache_stats()
    print(f"\nCache statistics:")
    print(f"  Total entries:    {stats['total']}")
    print(f"  With phone:       {stats['with_phone']}")
    print(f"  Hit rate:         {stats['with_phone'] / max(stats['total'], 1) * 100:.1f}%")
    print()


async def enrich_leads(
    db_path: Path = Path("data/ucc_scraper.db"),
    tier: str | None = None,
    limit: int = 50,
    min_score: int = 40,
    headless: bool = True,
    show_stats_only: bool = False,
):
    """Enrich leads with phone numbers from Google Maps.

    Args:
        db_path: Path to the lead database.
        tier: Lead tier filter (A, B, C, D, or None for all).
        limit: Maximum leads to process.
        min_score: Minimum score threshold.
        headless: Run browser headless or headed.
        show_stats_only: Only show cache stats, don't scrape.
    """
    enricher = GoogleMapsEnricher(headless=headless)

    if show_stats_only:
        await show_cache_stats(enricher)
        await enricher.close()
        return

    # ── Load leads from DB ──────────────────────────────────────────
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row

    where = "WHERE (phone_number IS NULL OR phone_number = '')"
    params: list = []
    if tier:
        where += " AND tier = ?"
        params.append(tier.upper())
    if min_score:
        where += " AND score_total >= ?"
        params.append(min_score)

    where += " ORDER BY score_total DESC LIMIT ?"
    params.append(limit)

    cursor = await db.execute(f"SELECT * FROM leads {where}", params)
    leads = [dict(r) for r in await cursor.fetchall()]
    await db.close()

    if not leads:
        print("No leads found matching criteria.")
        print("Try: --tier B --limit 20  or  --all")
        await show_cache_stats(enricher)
        await enricher.close()
        return

    print(f"\nFound {len(leads)} leads to enrich")
    print(f"{'=' * 70}")

    enriched = 0
    phones_found = 0

    for i, lead in enumerate(leads):
        biz = lead["business_name"]
        search_biz = clean_business_name(biz)
        city = lead.get("business_city") or ""
        state = lead.get("business_state") or lead.get("filing_state") or ""
        lead_id = lead["lead_id"]

        if not search_biz or len(search_biz) < 3:
            continue

        location = f"{city}, {state}" if city else state
        print(f"[{i + 1}/{len(leads)}] {search_biz[:55]} — {location[:20]}", end="", flush=True)

        try:
            result = await enricher.find_phone(search_biz, city, state)
            phone = result.get("phone")
            confidence = result.get("confidence", "low")
            website = result.get("website") or ""

            if phone:
                phones_found += 1
                print(f"  PHONE: {phone} (conf={confidence})", flush=True)

                # Update DB
                db2 = await aiosqlite.connect(str(db_path))
                await db2.execute(
                    "UPDATE leads SET phone_number = ? WHERE lead_id = ?",
                    (phone, lead_id),
                )
                await db2.commit()
                await db2.close()
            else:
                print(f"  no phone", flush=True)

            enriched += 1

        except Exception as e:
            logger.error(f"Error enriching {biz}: {e}")
            print(f"  ERROR: {e}", flush=True)

        # Rate limit between businesses
        if i < len(leads) - 1:
            await asyncio.sleep(0.5)  # Additional safety delay on top of built-in

    # ── Report ──────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"ENRICHMENT COMPLETE")
    print(f"{'=' * 70}")
    print(f"  Total attempted:  {enriched}")
    print(f"  Phones found:     {phones_found}")
    hit_rate = round(phones_found / max(enriched, 1) * 100)
    print(f"  Hit rate:         {hit_rate}%")
    print()

    await show_cache_stats(enricher)
    await enricher.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Enrich leads with Google Maps phone numbers")
    p.add_argument("--db", default="data/ucc_scraper.db", help="SQLite database path")
    p.add_argument("--tier", default="B", help="Lead tier (A, B, C, D)")
    p.add_argument("--limit", type=int, default=20, help="Max leads to process")
    p.add_argument("--min-score", type=int, default=40, help="Minimum score threshold")
    p.add_argument("--all", action="store_true", help="Process all leads without phone (overrides limit)")
    p.add_argument("--headed", action="store_true", help="Run browser in headed (visible) mode")
    p.add_argument("--stats", action="store_true", help="Show cache stats only, no scraping")

    args = p.parse_args()

    limit = 99999 if getattr(args, "all") else args.limit

    asyncio.run(enrich_leads(
        db_path=Path(args.db),
        tier=None if getattr(args, "all") else args.tier,
        limit=limit,
        min_score=args.min_score,
        headless=not args.headed,
        show_stats_only=args.stats,
    ))
