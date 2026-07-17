#!/usr/bin/env python3
"""Phone enrichment using free web directories — no API keys needed.

Strategies:
  1. DuckDuckGo HTML search → parse phone from snippet
  2. Google web search via scraping → parse phone numbers
  3. YellowPages scraper (free directory)
  4. WhitePages scraper (free directory)

Usage:
  python3 scripts/enrich-leads.py --tier B --limit 50
  python3 scripts/enrich-leads.py --all
"""

import asyncio
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import aiosqlite

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Phone regex patterns
PHONE_RE = re.compile(
    r'(?:\(?\d{3}\)?[\s.\-]?)?\d{3}[\s.\-]?\d{4}'
)
US_PHONE_RE = re.compile(
    r'(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}'
)
PHONE_CLEAN_RE = re.compile(r'[^\d]')


def clean_phone(raw: str) -> str | None:
    """Extract and format a US phone number."""
    nums = PHONE_CLEAN_RE.sub('', raw)
    if len(nums) == 10 and nums[0] in '23456789':
        return f"({nums[:3]}) {nums[3:6]}-{nums[6:]}"
    if len(nums) == 11 and nums[0] == '1':
        return f"({nums[1:4]}) {nums[4:7]}-{nums[7:]}"
    return None


async def duckduckgo_phone(biz_name: str, city: str, state: str) -> str | None:
    """Search DuckDuckGo HTML (no JS, no captcha) and extract phone."""
    query = quote_plus(f"{biz_name} {city} {state} phone number")
    url = f"https://html.duckduckgo.com/html/?q={query}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; UCCScraper/1.0)"
            })
            if r.status_code != 200:
                return None

            # Find snippets
            phones = set()
            for match in US_PHONE_RE.finditer(r.text):
                phone = clean_phone(match.group())
                if phone and not phone.startswith("(800)") and not phone.startswith("(888)"):
                    phones.add(phone)

            if phones:
                return list(phones)[0]

    except Exception:
        pass

    return None


async def yellowpages_phone(biz_name: str, city: str, state: str) -> str | None:
    """Search YellowPages.com for phone number."""
    query = quote_plus(f"{biz_name} {city} {state}")
    url = f"https://www.yellowpages.com/search?search_terms={query}"

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })

            if r.status_code != 200:
                return None

            # Extract phones from result cards
            # YP uses <div class="phones phone primary"> or <a href="tel:...">
            phones = set()
            for match in US_PHONE_RE.finditer(r.text):
                phone = clean_phone(match.group())
                if phone and not phone.startswith("(800)") and not phone.startswith("(888)"):
                    phones.add(phone)

            if phones:
                return list(phones)[0]

    except Exception:
        pass

    return None


async def google_web_phone(biz_name: str, city: str, state: str) -> str | None:
    """Search Google web (lightweight, no JS) and extract phone from snippets."""
    query = quote_plus(f'"{biz_name}" {city} {state} "phone"')
    url = f"https://www.google.com/search?q={query}&hl=en"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            })

            if r.status_code != 200:
                return None

            # Parse phones from search result snippets
            phones = set()
            for match in US_PHONE_RE.finditer(r.text):
                phone = clean_phone(match.group())
                if phone and not phone.startswith("(800)") and not phone.startswith("(888)"):
                    phones.add(phone)

            if phones:
                return list(phones)[0]

    except Exception:
        pass

    return None


async def enrich_one(biz_name: str, city: str, state: str) -> dict:
    """Try all strategies, return first phone found."""
    strategies = [
        ("Google", google_web_phone),
        ("DuckDuckGo", duckduckgo_phone),
        ("YellowPages", yellowpages_phone),
    ]

    for name, func in strategies:
        try:
            result = await func(biz_name, city, state)
            if result:
                return {"phone": result, "source": name}
        except Exception:
            continue

    return {"phone": None, "source": "none"}


async def enrich_leads(
    db_path: Path = Path("data/ucc_scraper.db"),
    tier: str | None = None,
    limit: int = 100,
    min_score: int = 40,
):
    """Enrich leads from the database with real phone lookups."""
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row

    where = "WHERE (phone_number IS NULL OR phone_number = '')"
    params = []
    if tier:
        where += " AND tier = ?"
        params.append(tier)
    if min_score:
        where += " AND score_total >= ?"
        params.append(min_score)

    where += " ORDER BY score_total DESC LIMIT ?"
    params.append(limit)

    cursor = await db.execute(f"SELECT * FROM leads {where}", params)
    leads = [dict(r) for r in await cursor.fetchall()]
    await db.close()

    print(f"Enriching {len(leads)} leads (tier={tier or 'all'}, min_score={min_score})")

    enriched = 0
    phones_found = 0

    for i, lead in enumerate(leads):
        biz = lead["business_name"]
        # Clean up entity name for better searching
        clean_biz = re.sub(r'\s+(LLC|INC|CORP|L\.L\.C\.|L\.P\.|CORPORATION|INC\.)\s*$', '', biz, flags=re.IGNORECASE)
        city = lead.get("business_city") or ""
        state = lead.get("business_state") or lead.get("filing_state") or ""

        if not clean_biz or len(clean_biz) < 3:
            continue

        location = f"{city}, {state}" if city else state
        print(f"[{i+1}/{len(leads)}] {clean_biz[:50]} — {location}", end=" ", flush=True)

        result = await enrich_one(clean_biz, city, state)
        phone = result.get("phone")
        source = result.get("source", "none")

        if phone:
            phones_found += 1
            print(f"→ 📞 {phone} ({source})", flush=True)

            # Update DB
            db2 = await aiosqlite.connect(str(db_path))
            await db2.execute(
                "UPDATE leads SET phone_number = ? WHERE lead_id = ?",
                (phone, lead["lead_id"]),
            )
            await db2.commit()
            await db2.close()
        else:
            print("→ no phone found", flush=True)

        enriched += 1

        # Rate limit: 1-2 sec between lookups to avoid blocks
        await asyncio.sleep(1.5 + (i % 3) * 0.7)

    print(f"\n{'=' * 60}")
    print(f"Enriched: {enriched} leads")
    print(f"Phones found: {phones_found} ({round(phones_found/max(enriched,1)*100)}%)")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Enrich leads with real phone numbers")
    p.add_argument("--db", default="data/ucc_scraper.db")
    p.add_argument("--tier", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--min-score", type=int, default=40)
    p.add_argument("--all", action="store_true")

    args = p.parse_args()
    limit = 100000 if getattr(args, "all") else args.limit

    asyncio.run(enrich_leads(
        db_path=Path(args.db),
        tier=args.tier,
        limit=limit,
        min_score=args.min_score,
    ))
