#!/usr/bin/env python3
"""Mass Google Maps enrichment — runs on VPS, feeds dashboard.

Usage:
  python3 scripts/mass-enrich-gmaps.py --limit 200    # Enrich 200 leads
  python3 scripts/mass-enrich-gmaps.py --tier B       # Warm leads only
  python3 scripts/mass-enrich-gmaps.py --all           # All unenriched

Runs headless on VPS with browser rotation every 30 lookups.
Pushes results to dashboard/data/leads.json for deployment.
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import aiosqlite

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from pipeline.gmaps_enricher import GoogleMapsEnricher


async def mass_enrich(
    db_path: Path = Path("data/ucc_scraper.db"),
    tier: str | None = None,
    limit: int = 200,
    rotate_every: int = 25,
):
    """Enrich leads at scale on a VPS with browser rotation."""

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row

    where = "WHERE (phone_number IS NULL OR phone_number = '')"
    params = []
    if tier:
        where += " AND tier = ?"
        params.append(tier)

    where += " ORDER BY score_total DESC LIMIT ?"
    params.append(limit)

    cursor = await db.execute(
        f"SELECT * FROM leads {where}", params
    )
    leads = [dict(r) for r in await cursor.fetchall()]
    await db.close()

    print(f"Mass enriching {len(leads)} leads")
    print(f"  Rotating browser every {rotate_every} lookups")
    print(f"  Started: {datetime.now().isoformat()}")
    print()

    enriched = 0
    phones = 0
    websites = 0
    batch = 0

    enricher = GoogleMapsEnricher(headless=True)

    for i, lead in enumerate(leads):
        biz = lead["business_name"]
        city = lead.get("business_city", "") or ""
        state = lead.get("business_state", "") or ""

        try:
            result = await enricher.find_phone(biz, city, state)
        except Exception as e:
            print(f"[{i+1}/{len(leads)}] {biz[:50]} — ⚠ {str(e)[:60]}")
            continue

        phone = result.get("phone")
        website = result.get("website")
        enriched += 1

        status = ""
        if phone:
            phones += 1
            status = f"📞 {phone}"
        if website:
            websites += 1
            status += f" | 🌐 {website[:50]}" if status else f"🌐 {website[:50]}"
        if not status:
            status = "— no contact"

        eta = (len(leads) - i - 1) * 5  # ~5s per lookup with delays
        eta_str = f"{eta//60}m{eta%60}s" if eta > 0 else "done"
        print(f"[{i+1}/{len(leads)}] {biz[:50]} — {status} (ETA: {eta_str})")

        # Update DB immediately
        if phone:
            db2 = await aiosqlite.connect(str(db_path))
            await db2.execute(
                "UPDATE leads SET phone_number = ? WHERE lead_id = ?",
                (phone, lead["lead_id"]),
            )
            await db2.commit()
            await db2.close()

        # Periodic progress
        if (i + 1) % 10 == 0:
            print(f"  ── {i+1}/{len(leads)} done: {phones} phones, {websites} websites ({round(phones/max(enriched,1)*100)}% hit) ──")

        # Browser rotation
        if (i + 1) % rotate_every == 0 and i + 1 < len(leads):
            print(f"  ↻ Rotating browser ({i+1} lookups)...")
            await enricher.close()
            await asyncio.sleep(3)
            enricher = GoogleMapsEnricher(headless=True)

    await enricher.close()

    # ── Publish to dashboard ──
    print(f"\n{'='*60}")
    print(f"DONE: {enriched} enriched, {phones} phones ({round(phones/max(enriched,1)*100)}%), {websites} websites")

    # Refresh dashboard data
    print("Refreshing dashboard...")
    os.system(f"{sys.executable} scripts/refresh-dashboard.py")

    print(f"\nReady to deploy. Run:")
    print(f"  npx wrangler pages deploy dashboard --project-name ucc1-leads --commit-dirty=true")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/ucc_scraper.db")
    p.add_argument("--tier", default=None)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--all", action="store_true")
    p.add_argument("--rotate-every", type=int, default=25)
    args = p.parse_args()

    limit = 100000 if getattr(args, "all") else args.limit
    asyncio.run(mass_enrich(
        Path(args.db), args.tier, limit, args.rotate_every
    ))
