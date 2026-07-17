"""UCC-1 Scraper CLI — scrape state UCC filings and generate MCA leads.

Usage:
    ucc-scrape scrape --states NY,FL,NJ --days 7
    ucc-scrape scrape --states all --days 1
    ucc-scrape leads --tier A
    ucc-scrape export --format csv --output leads.csv
    ucc-scrape stats
    ucc-scrape funders list
"""

import asyncio
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

import click

from pipeline.classifier import MCAClassifier
from pipeline.dedupe import Deduplicator
from pipeline.normalizer import FilingNormalizer
from pipeline.re_finder import RealEstateLeadFinder, format_re_lead_csv_row
from pipeline.scorer import LeadScorer
from scrapers.registry import get_scraper, list_available_states
from storage import Storage


@click.group()
@click.option("--db", default="data/ucc_scraper.db", help="SQLite database path")
@click.pass_context
def cli(ctx, db):
    """UCC-1 Scraper: Find MCA leads from state UCC filings."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = Path(db)


@cli.command()
@click.option("--states", default="all", help="Comma-separated state codes or 'all'")
@click.option("--days", default=7, help="Number of days back to scrape")
@click.option("--headless/--no-headless", default=True, help="Run browser headless")
@click.option("--proxy", default=None, help="Proxy server URL")
@click.pass_context
def scrape(ctx, states, days, headless, proxy):
    """Scrape UCC filings from state portals."""
    db_path = ctx.obj["db_path"]

    if states == "all":
        state_list = list_available_states()
    else:
        state_list = [s.strip().upper() for s in states.split(",")]

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    click.echo(f"Scraping {len(state_list)} state(s) from {start_date.date()} to {end_date.date()}")
    click.echo(f"States: {', '.join(state_list)}")

    asyncio.run(_scrape_states(state_list, start_date, end_date, db_path, headless, proxy))


async def _scrape_states(
    state_list: list[str],
    start_date: datetime,
    end_date: datetime,
    db_path: Path,
    headless: bool,
    proxy: str | None,
):
    """Run scrapers for all requested states."""
    storage = Storage(db_path)
    await storage.init()
    normalizer = FilingNormalizer()
    classifier = MCAClassifier()
    scorer = LeadScorer()
    dedupe = Deduplicator()

    total_filings = 0
    total_leads = 0

    for state_code in state_list:
        click.echo(f"\n{'='*50}")
        click.echo(f"  Scraping {state_code}...")
        click.echo(f"{'='*50}")

        scraper = get_scraper(state_code, headless=headless, proxy=proxy)
        if not scraper:
            click.echo(f"  ⚠ No scraper available for {state_code} — skipping")
            continue

        try:
            await scraper.start()
            state_filings = 0

            async for raw in scraper.search_by_date_range(start_date, end_date):
                # Normalize
                filing = normalizer.normalize(raw)

                # Skip if already in DB
                if await storage.filing_exists(filing.filing_number, filing.state):
                    continue

                # Classify
                filing = classifier.classify(filing)

                # Only save if MCA-related (or we save everything for analysis)
                await storage.save_filing(filing)
                dedupe.add_filing(filing)

                state_filings += 1
                total_filings += 1

                if state_filings % 50 == 0:
                    click.echo(f"  ... {state_filings} filings scraped from {state_code}")

            click.echo(f"  ✓ {state_filings} new filings from {state_code}")

        except Exception as e:
            click.echo(f"  ✗ Error scraping {state_code}: {e}", err=True)
        finally:
            await scraper.stop()

    # ── Generate leads from all filings ─────────────────────────────
    click.echo(f"\n{'='*50}")
    click.echo(f"  Scoring leads...")
    click.echo(f"{'='*50}")

    all_businesses = dedupe.get_all_businesses()
    for business_key, filings in all_businesses.items():
        for filing in filings:
            if not any(sp.is_mca_funder for sp in filing.secured_parties):
                if filing.collateral_type not in ("mca_receivables", "general_business_assets"):
                    continue  # Skip non-MCA filings

            related = dedupe.get_related(filing)
            lead = scorer.score(filing, related)
            await storage.save_lead(lead)
            total_leads += 1

    click.echo(f"\n{'='*50}")
    click.echo(f"  SCRAPE COMPLETE")
    click.echo(f"{'='*50}")
    click.echo(f"  Total new filings: {total_filings}")
    click.echo(f"  Total leads generated: {total_leads}")

    # Show tier breakdown
    tier_counts = await storage.get_tier_counts()
    click.echo(f"  Tier A (Hot):  {tier_counts.get('A', 0)}")
    click.echo(f"  Tier B (Warm): {tier_counts.get('B', 0)}")
    click.echo(f"  Tier C (Cold): {tier_counts.get('C', 0)}")
    click.echo(f"  Tier D (Arch): {tier_counts.get('D', 0)}")


@cli.command()
@click.option("--tier", default="A", help="Lead tier filter (A, B, C, D, or 'all')")
@click.option("--limit", default=20, help="Number of leads to show")
@click.option("--state", default=None, help="Filter by state")
@click.pass_context
def leads(ctx, tier, limit, state):
    """Show scored leads."""
    db_path = ctx.obj["db_path"]
    storage = Storage(db_path)
    asyncio.run(_show_leads(storage, tier, limit, state))


async def _show_leads(storage: Storage, tier: str, limit: int, state: str | None):
    """Display leads from the database."""
    await storage.init()

    if tier == "all":
        tiers = ["A", "B", "C", "D"]
    else:
        tiers = [tier.upper()]

    for t in tiers:
        leads = await storage.get_leads_by_tier(t, limit)
        if not leads:
            continue

        click.echo(f"\n── Tier {t} Leads ──")
        for lead in leads[:limit]:
            click.echo(
                f"  [{lead['score_total']:3d}] {lead['business_name'][:40]:40s} | "
                f"{lead['mca_funder_name'][:25]:25s} | "
                f"{lead['business_city'] or 'N/A':15s}, {lead['business_state'] or lead['filing_state']}"
            )


@cli.command()
@click.option("--format", "fmt", default="csv", help="Export format (csv)")
@click.option("--output", "-o", default="leads.csv", help="Output file path")
@click.option("--tier", default="all", help="Filter by tier")
@click.option("--state", default=None, help="Filter by state")
@click.pass_context
def export(ctx, fmt, output, tier, state):
    """Export leads to CSV."""
    db_path = ctx.obj["db_path"]
    storage = Storage(db_path)
    asyncio.run(_export_leads(storage, fmt, output, tier, state))


async def _export_leads(storage: Storage, fmt: str, output: str, tier: str, state: str | None):
    """Export leads from database to file."""
    await storage.init()

    tiers = ["A", "B", "C", "D"] if tier == "all" else [tier.upper()]
    all_leads = []
    for t in tiers:
        leads = await storage.get_leads_by_tier(t, limit=10000)
        for lead in leads:
            if state and lead.get("filing_state") != state.upper():
                continue
            all_leads.append(lead)

    if fmt == "csv":
        if not all_leads:
            click.echo("No leads to export.")
            return

        fieldnames = all_leads[0].keys()
        with open(output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_leads)

        click.echo(f"✓ Exported {len(all_leads)} leads to {output}")

    else:
        click.echo(f"Unknown format: {fmt}", err=True)


# ── Real Estate Lead Commands ────────────────────────────────────────────


@cli.command()
@click.option("--tier", default="A", help="Lead tier filter (A, B, C, D, or 'all')")
@click.option("--limit", default=50, help="Number of RE leads to show")
@click.option("--category", default=None, help="Filter by lender category (hard_money, private_reit, construction, etc.)")
@click.option("--lenders", is_flag=True, default=False, help="List the known RE lender database and exit")
@click.option("--export", "export_path", default=None, help="Export RE leads to CSV file path")
@click.pass_context
def re_leads(ctx, tier, limit, category, lenders, export_path):
    """Show real estate UCC leads — hard money, bridge, fix-and-flip, construction.

    Scans all filings in the database for real-estate-secured UCC-1 filings.
    Scores each lead 0-100 and classifies by tier (A=hot through D=archive).

    Examples:
        ucc-scrape re-leads                       # Tier A leads (hot)
        ucc-scrape re-leads --tier all            # All tiers
        ucc-scrape re-leads --category hard_money # Hard money lenders only
        ucc-scrape re-leads --lenders             # Show lender database
        ucc-scrape re-leads --export re_leads.csv # Export all to CSV
    """
    db_path = ctx.obj["db_path"]

    if lenders:
        from pipeline.re_finder import HARD_MONEY_LENDERS, TOTAL_RE_LENDERS, CATEGORY_LABELS
        click.echo(f"\nReal Estate Lender Database ({TOTAL_RE_LENDERS} lenders)")
        click.echo("=" * 70)

        for cat in ["hard_money", "fix_and_flip", "construction", "private_reit", "bridge_lender", "traditional_bank"]:
            cat_lenders = {k: v for k, v in HARD_MONEY_LENDERS.items() if v["category"] == cat}
            if cat_lenders:
                click.echo(f"\n  {CATEGORY_LABELS[cat]} ({len(cat_lenders)}):")
                for name in sorted(cat_lenders.keys()):
                    tier_label = {1: "HM", 2: "REIT", 3: "BK"}.get(cat_lenders[name]["tier"], "?")
                    click.echo(f"    [{tier_label}] {name}")
        return

    storage = Storage(db_path)
    asyncio.run(_show_re_leads(storage, tier, limit, category, export_path))


async def _show_re_leads(storage: Storage, tier: str, limit: int, category: str | None, export_path: str | None):
    """Display real estate leads from the database or generate fresh."""
    await storage.init()

    finder = RealEstateLeadFinder()

    # Try loading from DB first, then fall back to fresh scan
    stored_leads = []
    if tier == "all":
        for t in ["A", "B", "C", "D"]:
            stored_leads.extend(await storage.get_re_leads_by_tier(t, limit))
    else:
        stored_leads = await storage.get_re_leads_by_tier(tier.upper(), limit)

    if stored_leads:
        leads = stored_leads
    else:
        # No stored leads — do a fresh scan of all filings in DB
        from ingestor import UCCIngestor
        ingestor = UCCIngestor(storage.db_path)

        # Load all filings from DB
        async with aiosqlite.connect(str(storage.db_path)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM filings ORDER BY filing_date DESC LIMIT 5000"
            )
            rows = await cursor.fetchall()

        raw_filings = [dict(row) for row in rows]
        leads = finder.find_re_leads(raw_filings)

        if leads:
            await storage.save_re_leads(leads)

    # Apply category filter
    if category:
        leads = [l for l in leads if l.get("lender_category") == category]

    if not leads:
        click.echo("No RE leads found.")
        return

    # Export to CSV
    if export_path:
        fieldnames = list(format_re_lead_csv_row(leads[0]).keys())
        with open(export_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for lead in leads:
                writer.writerow(format_re_lead_csv_row(lead))
        click.echo(f"Exported {len(leads)} RE leads to {export_path}")
        return

    # Display
    click.echo(f"\n── Real Estate Leads (Tier {tier}) ──")
    click.echo(f"{'=' * 70}")

    for lead in leads[:limit]:
        score = lead["score_total"]
        biz = lead["business_name"][:40]
        lender = lead.get("lender_matched", lead.get("lender_name", ""))[:30]
        category_display = lead.get("lender_display", lead.get("lender_category", "RE"))
        city = lead.get("location_city") or ""
        state = lead.get("location_state") or lead.get("filing_state", "")
        location = f"{city}, {state}" if city else state
        lead_index = lead.get("lead_index", "RE-?")

        click.echo(
            f"  {lead_index} [{score:3d}] {biz:40s} | "
            f"{lender:30s} | {location:20s} | {category_display}"
        )

    click.echo(f"\n  Total: {len(leads)} leads shown")


@cli.command()
@click.option("--limit", default=20000, help="Max filings to scan")
@click.pass_context
def refresh_re_leads(ctx, limit):
    """Re-scan all filings in the DB and regenerate RE lead scores."""
    db_path = ctx.obj["db_path"]
    asyncio.run(_refresh_re_leads(db_path, limit))


async def _refresh_re_leads(db_path: Path, limit: int):
    """Re-scan all filings, generate fresh RE leads, and store them."""
    import aiosqlite

    storage = Storage(db_path)
    await storage.init()

    finder = RealEstateLeadFinder()

    # Load all filings from DB
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM filings ORDER BY filing_date DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()

    raw_filings = [dict(row) for row in rows]
    click.echo(f"Scanning {len(raw_filings)} filings for RE leads...")

    leads = finder.find_re_leads(raw_filings)
    click.echo(f"Found {len(leads)} RE leads.")

    # Save to DB
    await storage.save_re_leads(leads)

    # Show tier breakdown
    tier_counts = await storage.get_re_tier_counts()
    click.echo(f"\n  RE Tier A (Hot):     {tier_counts.get('A', 0)}")
    click.echo(f"  RE Tier B (Warm):    {tier_counts.get('B', 0)}")
    click.echo(f"  RE Tier C (Cold):    {tier_counts.get('C', 0)}")
    click.echo(f"  RE Tier D (Archive): {tier_counts.get('D', 0)}")


@cli.command()
@click.option("--states", default="all", help="Comma-separated state codes or 'all'")
@click.pass_context
def dry_run(ctx, states):
    """Check scraper connectivity without scraping (health check)."""
    if states == "all":
        state_list = list_available_states()
    else:
        state_list = [s.strip().upper() for s in states.split(",")]

    click.echo(f"Health-checking {len(state_list)} state(s)...\n")
    asyncio.run(_dry_run_states(state_list, ctx.obj["db_path"]))


async def _dry_run_states(state_list: list[str], db_path: Path):
    """Run health checks on each state scraper."""
    from scrapers.registry import get_scraper

    results = {}
    for state_code in state_list:
        scraper = get_scraper(state_code, headless=True, proxy=None)
        if not scraper:
            click.echo(f"  {state_code}: ⚠ No scraper available")
            continue

        try:
            await scraper.start()
            health = await scraper.health_check()
            status = "✓ OK" if health["ok"] else "✗ FAIL"
            click.echo(f"  {state_code}: {status} (HTTP {health['status_code']} → {health['url']})")
            if health.get("error"):
                click.echo(f"         Error: {health['error']}")
            results[state_code] = health
        except Exception as e:
            click.echo(f"  {state_code}: ✗ FAIL ({e})")
        finally:
            await scraper.stop()


@cli.command()
@click.pass_context
def stats(ctx):
    """Show database statistics."""
    db_path = ctx.obj["db_path"]
    storage = Storage(db_path)
    asyncio.run(_show_stats(storage))


async def _show_stats(storage: Storage):
    """Display scraper statistics."""
    await storage.init()

    lead_count = await storage.get_lead_count()
    tier_counts = await storage.get_tier_counts()
    re_lead_count = await storage.get_re_lead_count()
    re_tier_counts = await storage.get_re_tier_counts()

    click.echo(f"\nUCC-1 Scraper Statistics")
    click.echo(f"{'='*40}")
    click.echo(f"  Total leads:       {lead_count}")
    click.echo(f"  Tier A (Hot):      {tier_counts.get('A', 0)}")
    click.echo(f"  Tier B (Warm):     {tier_counts.get('B', 0)}")
    click.echo(f"  Tier C (Cold):     {tier_counts.get('C', 0)}")
    click.echo(f"  Tier D (Archive):  {tier_counts.get('D', 0)}")
    click.echo(f"  ─────────────────────────")
    click.echo(f"  RE leads:          {re_lead_count}")
    click.echo(f"  RE Tier A (Hot):   {re_tier_counts.get('A', 0)}")
    click.echo(f"  RE Tier B (Warm):  {re_tier_counts.get('B', 0)}")
    click.echo(f"  RE Tier C (Cold):  {re_tier_counts.get('C', 0)}")
    click.echo(f"  RE Tier D (Arch):  {re_tier_counts.get('D', 0)}")
    click.echo(f"\n  Available states: {', '.join(list_available_states())}")


@cli.group()
def funders():
    """Manage MCA funder database."""
    pass


@funders.command("list")
@click.option("--tier", default=None, type=int, help="Filter by tier (1, 2, 3)")
def funders_list(tier):
    """List known MCA funders."""
    classifier = MCAClassifier()

    click.echo(f"\nMCA Funder Database ({classifier.funder_count} funders)")
    click.echo(f"{'='*60}")

    for funder in classifier._funders:
        if tier and funder["tier"] != tier:
            continue
        tier_label = {1: "Pure MCA", 2: "MCA+Other", 3: "Adjacent"}.get(funder["tier"], "?")
        click.echo(f"  [{tier_label:12s}] {funder['legal_name'][:45]:45s}")
        for dba in funder.get("dbas", []):
            click.echo(f"                      aka: {dba}")


@funders.command("add")
@click.option("--name", required=True, help="Legal entity name")
@click.option("--tier", required=True, type=int, help="Tier (1=Pure MCA, 2=MCA+Other, 3=Adjacent)")
@click.option("--dba", multiple=True, help="DBA names (repeatable)")
@click.option("--notes", default="", help="Notes about this funder")
def funders_add(name, tier, dba, notes):
    """Add a new MCA funder to the database."""
    import json

    db_path = Path(__file__).parent / "funders" / "funder_db.json"
    with open(db_path) as f:
        data = json.load(f)

    new_id = f"mca-{len(data['funders']) + 1:03d}"
    funder = {
        "id": new_id,
        "legal_name": name.upper(),
        "dbas": list(dba),
        "tier": tier,
        "typical_advance": "5000-250000",
        "typical_term_days": 180,
        "states_active": ["ALL"],
        "notes": notes,
    }

    data["funders"].append(funder)
    data["_meta"]["total_funders"] = len(data["funders"])
    data["_meta"]["last_updated"] = datetime.now().strftime("%Y-%m-%d")

    with open(db_path, "w") as f:
        json.dump(data, f, indent=2)

    click.echo(f"✓ Added {name} (Tier {tier}, ID: {new_id})")
    click.echo(f"  Total funders: {len(data['funders'])}")


@cli.command()
@click.option("--states", default="all", help="Comma-separated state codes")
@click.option("--days", default=7, help="Days back to scrape")
@click.option("--schedule", default=None, help="Cron schedule for recurring runs (e.g., '0 6 * * *')")
@click.pass_context
def daemon(ctx, states, days, schedule):
    """Run as a scheduled daemon (scrape daily, export leads)."""
    if schedule:
        click.echo(f"Would schedule: {schedule} — cron integration via systemd/cron")
        click.echo("Set up with: crontab -e")
        click.echo(f"0 6 * * * cd '{Path.cwd()}' && ucc-scrape scrape --states {states} --days {days}")
        click.echo(f"30 6 * * * cd '{Path.cwd()}' && ucc-scrape export --tier all -o leads_$(date +%Y%m%d).csv")
    else:
        click.echo("Run manually: ucc-scrape scrape --states NY,FL,NJ --days 7")


# ── Register ingest command ──────────────────────────────────────────────
from ingestor import register_ingest_command
register_ingest_command(cli)


if __name__ == "__main__":
    cli()
