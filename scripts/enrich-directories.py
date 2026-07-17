#!/usr/bin/env python3
"""Standalone test script for directory-based phone enrichment.

Parses leads from ``top_leads_for_enrichment.txt`` and tries every directory
scraper on each lead.  Reports per-directory hit rates and aggregate stats.

Usage:
    python3 scripts/enrich-directories.py                    # Test with 5 leads
    python3 scripts/enrich-directories.py --all              # Test top 50 leads
    python3 scripts/enrich-directories.py --all --bulk       # Bulk-enrich top 50
    python3 scripts/enrich-directories.py --top N            # Test first N leads
    python3 scripts/enrich-directories.py --diagnose         # Try all dirs per lead
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

# Ensure we can import from src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipeline.directory_scraper import DirectoryScraper, clean_business_name

# ---------------------------------------------------------------------------
#  Lead parsing
# ---------------------------------------------------------------------------

LEAD_RE = re.compile(
    r"^\d+\.\s+(.*?)\s+[—–-]\s+(.*?)\s+[—–-]\s+.*?\[Score:\s*(\d+)\]"
)


def parse_leads_file(path: str | Path) -> list[dict[str, str | int]]:
    """Parse the ``top_leads_for_enrichment.txt`` file.

    Expected format::

        1. BUSINESS NAME — City, State — FUNDER [Score: NN]

    Returns a list of dicts with keys: ``business_name``, ``city``, ``state``,
    ``score``.
    """
    leads: list[dict[str, str | int]] = []
    path = Path(path)
    if not path.exists():
        print(f"[ERROR] File not found: {path}")
        return leads

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = LEAD_RE.match(line)
            if not m:
                # Try simpler delimiter
                parts = re.split(r"\s+[—–-]\s+", line, maxsplit=2)
                if len(parts) >= 2:
                    biz = parts[0].strip()
                    loc_part = parts[1].strip()
                    score = 0
                    score_m = re.search(r"\[Score:\s*(\d+)\]", line)
                    if score_m:
                        score = int(score_m.group(1))
                    # Split location
                    loc_parts = loc_part.rsplit(", ", 1)
                    city = loc_parts[0].strip() if loc_parts else ""
                    state = loc_parts[1].strip() if len(loc_parts) > 1 else ""
                    # Clean business name (remove number prefix)
                    biz = re.sub(r"^\d+\.\s*", "", biz).strip()
                    if biz and city and state:
                        leads.append({
                            "business_name": biz,
                            "city": city,
                            "state": state,
                            "score": score,
                        })
                continue

            biz = m.group(1).strip()
            loc = m.group(2).strip()
            score = int(m.group(3))

            # Location is "City, ST" or sometimes has extra junk
            loc_parts = loc.rsplit(", ", 1)
            city = loc_parts[0].strip() if loc_parts else ""
            state = loc_parts[1].strip() if len(loc_parts) > 1 else ""

            leads.append({
                "business_name": biz,
                "city": city,
                "state": state,
                "score": score,
            })

    return leads


def clean_city(city: str) -> str:
    """Clean up a city name (remove address artifacts like 'Th Floor')."""
    # Remove ordinal floor indicators
    city = re.sub(
        r"^\d+(st|nd|rd|th)\s+(Floor|FLOOR)\s+", "", city, flags=re.IGNORECASE
    )
    # Remove bare "Th Floor" and similar
    city = re.sub(r"^\d+\s*(?:st|nd|rd|th)?\s*Floor\s+", "", city, flags=re.IGNORECASE)
    # Remove common address suffixes before city
    city = re.sub(
        r"^(?:Suite|Ste|Apt|Unit|Building|Bldg)\s+\S+\s+", "", city, flags=re.IGNORECASE
    )
    # If city has a number at the start and looks wrong, try taking last word
    parts = city.strip().split()
    if len(parts) > 1 and parts[0].isdigit():
        # The last part is probably the actual city
        for p in parts:
            if p[0].isupper() and not p.isdigit():
                return p
    return city.strip()


# ---------------------------------------------------------------------------
#  Test reporters
# ---------------------------------------------------------------------------


def print_separator(title: str = "") -> None:
    width = 68
    if title:
        print()
        print("=" * width)
        print(f"  {title}")
        print("=" * width)
    else:
        print("-" * width)


def print_lead_result(
    i: int,
    total: int,
    biz: str,
    city: str,
    state: str,
    result: dict,
) -> None:
    phone = result.get("phone")
    source = result.get("source", "none")
    cached = result.get("cached", False)
    flag = " [CACHED]" if cached else ""
    location = f"{city}, {state}" if city else state

    biz_short = biz[:45]
    if phone:
        print(
            f"  [{i:>2}/{total}] {biz_short:<45s} | {location:<20s} | "
            f"\033[92m{phone:<15s}\033[0m ({source}){flag}"
        )
    else:
        print(
            f"  [{i:>2}/{total}] {biz_short:<45s} | {location:<20s} | "
            f"\033[90mno phone\033[0m"
        )


def print_diagnostic_result(
    i: int,
    total: int,
    biz: str,
    city: str,
    state: str,
    all_results: dict[str, dict],
) -> None:
    location = f"{city}, {state}" if city else state
    print(f"\n  [{i}/{total}] {biz[:50]} — {location}")
    for src, res in all_results.items():
        phone = res.get("phone") or "\033[90m—\033[0m"
        err = res.get("error", "")
        err_msg = f" ({err})" if err else ""
        print(f"    {src:<20s} {phone}{err_msg}")


# ---------------------------------------------------------------------------
#  Main enrichment loop
# ---------------------------------------------------------------------------


async def run_test(
    leads_file: str,
    limit: int = 5,
    diagnose: bool = False,
    bulk_enrich: bool = False,
    db_path: str = "data/ucc_scraper.db",
) -> None:
    """Run directory scrapers on leads and report results."""
    leads = parse_leads_file(leads_file)
    if not leads:
        print("[ERROR] No leads parsed. Check file format.")
        return

    leads = leads[:limit]
    print_separator(f"Directory Enrichment Test — {len(leads)} leads")

    # Stats accumulators
    dir_stats: dict[str, dict] = defaultdict(
        lambda: {"found": 0, "total": 0, "phones": []}
    )
    phones_found = 0
    start_ts = time.time()

    scraper = DirectoryScraper(headless=True)
    await scraper.start()

    try:
        for i, lead in enumerate(leads, 1):
            biz = lead["business_name"]
            city = clean_city(lead["city"])
            state = lead["state"].strip().upper()
            score = lead["score"]

            if diagnose:
                # Try all directories
                result = await scraper.find_phones_all(biz, city, state)
                print_diagnostic_result(i, len(leads), biz, city, state, result)

                # Accumulate per-directory stats
                for src, res in result.items():
                    dir_stats[src]["total"] += 1
                    if res.get("found"):
                        dir_stats[src]["found"] += 1
                        if res.get("phone"):
                            dir_stats[src]["phones"].append(res["phone"])

                any_found = any(r.get("found") for r in result.values())
                if any_found:
                    phones_found += 1
            else:
                # Quick: stop at first match
                result = await scraper.find_phone(biz, city, state)
                print_lead_result(i, len(leads), biz, city, state, result)

                # Accumulate source stats
                src = result.get("source", "none")
                dir_stats[src]["total"] += 1
                phone = result.get("phone")
                if phone:
                    dir_stats[src]["found"] += 1
                    dir_stats[src]["phones"].append(phone)
                    phones_found += 1

            await asyncio.sleep(0.5)  # Be polite

    finally:
        await scraper.stop()

    elapsed = time.time() - start_ts

    # ── Report ──────────────────────────────────────────────────────────
    print_separator("Results")
    print(f"  Leads tested:       {len(leads)}")
    print(f"  Phones found:       {phones_found} ({100*phones_found//max(len(leads),1)}%)")
    print(f"  Time elapsed:       {elapsed:.1f}s ({elapsed/max(len(leads),1):.1f}s/lead)")
    print()

    # Per-directory hit rates
    if diagnose:
        print(f"  {'Directory':<20s} {'Found':>6s} / {'Total':>5s}  {'Rate':>6s}")
        print(f"  {'-'*42}")
        for src in ["yellowpages", "yellowpages_mip", "whitepages",
                     "merchantcircle", "cylex"]:
            s = dir_stats[src]
            rate = 100 * s["found"] // max(s["total"], 1)
            bar = "#" * (rate // 5) + "-" * (20 - rate // 5)
            print(f"  {src:<20s} {s['found']:>6d} / {s['total']:>5d}  {rate:>5d}%  {bar}")

    # ── Bulk enrich (optional) ──────────────────────────────────────────
    if bulk_enrich and phones_found > 0:
        await bulk_enrich_leads(leads, db_path, scraper)


async def bulk_enrich_leads(
    leads: list[dict],
    db_path: str,
    scraper: DirectoryScraper | None = None,
) -> None:
    """Enrich leads that are missing phone numbers in the database.

    Updates the ``phone_number`` column in the *leads* table.
    """
    import aiosqlite

    own_scraper = scraper is None
    if own_scraper:
        scraper = DirectoryScraper(headless=True)
        await scraper.start()

    try:
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row

        # Find leads without phone numbers
        cursor = await db.execute(
            "SELECT * FROM leads WHERE (phone_number IS NULL OR phone_number = '') "
            "ORDER BY score_total DESC LIMIT 50"
        )
        unenriched = [dict(r) for r in await cursor.fetchall()]
        await db.close()

        if not unenriched:
            print("\n  No unenriched leads found in database.")
            return

        print_separator(f"Bulk Enrichment — {len(unenriched)} leads (DB write)")

        enriched = 0
        phones_found = 0

        for i, lead in enumerate(unenriched, 1):
            biz = lead["business_name"]
            city = lead.get("business_city") or ""
            state = lead.get("business_state") or lead.get("filing_state") or ""
            location = f"{city}, {state}" if city else state

            print(
                f"  [{i:>2}/{len(unenriched)}] {biz[:45]:<45s} | {location:<20s}",
                end=" ",
                flush=True,
            )

            result = await scraper.find_phone(biz, city, state)
            phone = result.get("phone")

            if phone:
                phones_found += 1
                print(f"→ \033[92m{phone}\033[0m ({result['source']})")
                # Write to DB
                db2 = await aiosqlite.connect(db_path)
                await db2.execute(
                    "UPDATE leads SET phone_number = ? WHERE lead_id = ?",
                    (phone, lead["lead_id"]),
                )
                await db2.commit()
                await db2.close()
            else:
                print(f"→ \033[90mno phone\033[0m")

            enriched += 1

            if own_scraper:
                await asyncio.sleep(1.0)

        print(f"\n  \033[1mSummary:\033[0m {phones_found}/{enriched} phones found "
              f"({100*phones_found//max(enriched,1)}%)")

    finally:
        if own_scraper:
            await scraper.stop()


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description="Directory-based phone enrichment for MCA leads"
    )
    p.add_argument(
        "--leads-file",
        default=str(
            Path(__file__).resolve().parent.parent / "data" / "top_leads_for_enrichment.txt"
        ),
        help="Path to leads file (default: data/top_leads_for_enrichment.txt)",
    )
    p.add_argument(
        "--top",
        type=int,
        default=5,
        help="Number of leads to test (default: 5)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        dest="test_all",
        help="Test ALL leads from the file (up to 50)",
    )
    p.add_argument(
        "--diagnose",
        action="store_true",
        help="Try ALL directories per lead and show per-directory stats",
    )
    p.add_argument(
        "--bulk",
        action="store_true",
        help="After testing, bulk-enrich unenriched leads in the database",
    )
    p.add_argument(
        "--db",
        default="data/ucc_scraper.db",
        help="Database path for bulk enrichment (default: data/ucc_scraper.db)",
    )

    args = p.parse_args()

    limit = 50 if args.test_all else args.top

    asyncio.run(
        run_test(
            leads_file=args.leads_file,
            limit=limit,
            diagnose=args.diagnose,
            bulk_enrich=args.bulk,
            db_path=args.db,
        )
    )


if __name__ == "__main__":
    main()
