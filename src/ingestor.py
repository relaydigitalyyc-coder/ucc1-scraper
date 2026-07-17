"""Universal UCC filing ingestor — accepts filings from any source and
routes them through the classification → scoring → dedup → storage pipeline.

Sources:
  1. JSON file — array of filing dicts
  2. CSV file — with column mapping
  3. Apify UCC scraper output
  4. State-specific raw formats (auto-detected)

Usage:
  ucc-scrape ingest --input filings.json
  ucc-scrape ingest --input filings.csv --map debtor_name:debtorName
  ucc-scrape ingest --apify-run  # Run Apify UCC scraper, ingest results
"""

import asyncio
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

import click

from models.filing import UCCFiling, FilingStatus, DebtorInfo, SecuredPartyInfo
from models.lead import MCALead
from pipeline.classifier import MCAClassifier
from pipeline.dedupe import Deduplicator
from pipeline.enricher import LeadEnricher
from pipeline.normalizer import FilingNormalizer
from pipeline.scorer import LeadScorer
from storage import Storage


class UCCIngestor:
    """Ingest raw UCC filings from any format into the lead pipeline."""

    # ── Field name aliases for auto-mapping ──────────────────────────
    FIELD_MAP = {
        # Filing identity
        "filing_number": [
            "filing_number", "filingNumber", "uccNumber", "ucc_number",
            "document_number", "documentNumber", "docNumber", "doc_number",
            "filingId", "filing_id", "id",
        ],
        "state": ["state", "jurisdiction", "filingState"],
        "source_url": ["source_url", "sourceUrl", "detail_url", "detailUrl", "url"],

        # Dates
        "filing_date": [
            "filing_date", "filingDate", "file_date", "fileDate",
            "date_filed", "dateFiled", "filed", "recorded_date", "recordedDate",
        ],
        "lapse_date": ["lapse_date", "lapseDate", "expiration_date", "expirationDate"],

        # Status
        "status": ["status", "filingStatus", "filing_status"],

        # Debtor
        "debtor_name": [
            "debtor_name", "debtorName", "name", "debtor",
            "business_name", "businessName", "organization_name", "organizationName",
            "company_name", "companyName",
        ],
        "dba_name": ["dba_name", "dbaName", "dba", "trade_name", "tradeName", "aka"],
        "debtor_address": [
            "debtor_address", "debtorAddress", "address", "debtorAddressLine1",
            "address_line1", "addressLine1",
        ],
        "debtor_city": ["debtor_city", "debtorCity", "city"],
        "debtor_state": ["debtor_state", "debtorState", "debtorStateCode"],
        "debtor_zip": ["debtor_zip", "debtorZip", "zip", "zipCode", "zip_code", "postalCode"],

        # Secured Party
        "secured_party_name": [
            "secured_party_name", "securedPartyName", "secured_party",
            "securedParty", "lender_name", "lenderName", "lender",
            "funder_name", "funderName", "creditor_name", "creditorName",
        ],
        "secured_party_address": [
            "secured_party_address", "securedPartyAddress",
            "lender_address", "lenderAddress",
        ],
        "secured_party_city": ["secured_party_city", "securedPartyCity", "lender_city", "lenderCity"],
        "secured_party_state": ["secured_party_state", "securedPartyState", "lender_state", "lenderState"],

        # Collateral
        "collateral_description": [
            "collateral_description", "collateralDescription",
            "collateral", "collateral_text", "collateralText",
            "description", "cover", "collateralSummary",
        ],
    }

    # ── Source-specific mappers ──────────────────────────────────────

    @staticmethod
    def from_apify_result(item: dict) -> dict:
        """Map Apify UCC scraper output to our standard format."""
        return {
            "state": (item.get("state") or item.get("jurisdiction") or "").upper(),
            "filing_number": item.get("filingNumber") or item.get("uccNumber") or item.get("id", ""),
            "filing_date": item.get("filingDate") or item.get("fileDate") or item.get("dateFiled", ""),
            "debtor_name": item.get("debtorName") or item.get("debtor") or "",
            "dba_name": item.get("dba") or item.get("tradeName"),
            "debtor_address": item.get("debtorAddress") or item.get("address"),
            "debtor_city": item.get("debtorCity") or item.get("city"),
            "debtor_state": item.get("debtorState"),
            "debtor_zip": item.get("debtorZip") or item.get("zipCode"),
            "secured_party_name": item.get("securedParty") or item.get("securedPartyName") or item.get("lender", ""),
            "secured_party_address": item.get("securedPartyAddress"),
            "secured_party_city": item.get("securedPartyCity"),
            "secured_party_state": item.get("securedPartyState"),
            "collateral_description": item.get("collateral") or item.get("collateralDescription"),
            "status": item.get("status") or "unknown",
            "source_url": item.get("url") or item.get("detailUrl"),
        }

    @staticmethod
    def auto_map(item: dict) -> dict:
        """Auto-map raw dict fields using FIELD_MAP aliases."""
        mapped: dict = {}
        item_lower = {k.lower(): v for k, v in item.items()}

        for target, aliases in UCCIngestor.FIELD_MAP.items():
            for alias in aliases:
                # Try exact key
                if alias in item:
                    mapped[target] = item[alias]
                    break
                # Try case-insensitive
                if alias.lower() in item_lower:
                    mapped[target] = item_lower[alias.lower()]
                    break

        # Ensure required fields
        mapped.setdefault("filing_number", "")
        mapped.setdefault("state", "")
        mapped.setdefault("filing_date", "")
        mapped.setdefault("debtor_name", "")
        mapped.setdefault("secured_party_name", "")
        mapped.setdefault("status", "unknown")

        return mapped

    # ── Main processing pipeline ─────────────────────────────────────

    def __init__(self, db_path: Path | None = None, enrich: bool = False):
        self.db_path = db_path or Path("data/ucc_scraper.db")
        self.enrich = enrich
        self.normalizer = FilingNormalizer()
        self.classifier = MCAClassifier()
        self.scorer = LeadScorer()
        self.dedupe = Deduplicator()
        self.enricher: LeadEnricher | None = None

    async def process(
        self,
        filings: AsyncIterator[dict] | list[dict],
        source_label: str = "import",
    ) -> dict:
        """Process raw filings through the full pipeline.

        Returns stats: {total, mca, leads, tier_a, tier_b, tier_c, tier_d, errors}
        """
        storage = Storage(self.db_path)
        await storage.init()

        stats = {
            "source": source_label,
            "total": 0,
            "mca_filings": 0,
            "leads": 0,
            "tier_a": 0,
            "tier_b": 0,
            "tier_c": 0,
            "tier_d": 0,
            "errors": 0,
            "duplicates": 0,
        }

        items = filings if isinstance(filings, list) else [f async for f in filings]

        for raw in items:
            stats["total"] += 1
            try:
                # Map to standard format
                mapped = self.auto_map(raw)

                # Normalize
                filing = self.normalizer.normalize(mapped)

                # Skip if already in DB
                if await storage.filing_exists(filing.filing_number, filing.state):
                    stats["duplicates"] += 1
                    continue

                # Classify (MCA funder match)
                filing = self.classifier.classify(filing)

                # Check if MCA-related
                is_mca = self.classifier.is_mca_filing(filing)
                if is_mca:
                    stats["mca_filings"] += 1

                # Save filing
                await storage.save_filing(filing)
                self.dedupe.add_filing(filing)

                # Generate lead
                related = self.dedupe.get_related(filing)
                lead = self.scorer.score(filing, related)

                # Enrich with contact info if --enrich flag is set
                if self.enrich:
                    lead = await self._enrich_lead(lead)

                await storage.save_lead(lead)
                stats["leads"] += 1

                tier = lead.tier.value.lower()
                stats[f"tier_{tier}"] = stats.get(f"tier_{tier}", 0) + 1

            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 3:
                    click.echo(f"  ⚠ Error processing filing: {e}", err=True)

        return stats

    # ── Source loaders ───────────────────────────────────────────────

    async def load_json(self, path: Path) -> list[dict]:
        """Load filings from a JSON file (array of objects or {filings: [...]})."""
        with open(path) as f:
            data = json.load(f)

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Common patterns: {filings: [...]}, {data: [...]}, {results: [...]}
            for key in ("filings", "data", "results", "payload", "items", "records"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            # Single object → wrap in list
            return [data]
        return []

    async def load_csv(self, path: Path, column_map: Optional[dict[str, str]] = None) -> list[dict]:
        """Load filings from CSV with optional column mapping."""
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        if column_map:
            rows = [
                {column_map.get(k, k): v for k, v in row.items()}
                for row in rows
            ]

        return rows

    async def load_apify_dataset(self, dataset_id: str) -> list[dict]:
        """Load filings from an Apify dataset by ID."""
        import httpx

        url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
        params = {"format": "json", "limit": 10000}

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            items = r.json()

        # Map Apify format to our standard
        return [self.from_apify_result(item) for item in items]

    async def load_apify_run(self, actor_id: str, input_data: dict) -> list[dict]:
        """Run an Apify actor and load results.

        Actor ID examples:
          - 'inexhaustible_glass~ucc-filings-scraper' (CT, CO, OR UCC)
        """
        import httpx

        token = input_data.pop("_apify_token", None)
        if not token:
            token = input_data.pop("apifyToken", None)

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(timeout=300, headers=headers) as client:
            # Start run
            run_url = f"https://api.apify.com/v2/acts/{actor_id}/runs"
            r = await client.post(run_url, json=input_data)
            r.raise_for_status()
            run_data = r.json()
            run_id = run_data.get("data", {}).get("id", "")

            if not run_id:
                raise RuntimeError(f"Failed to start Apify run: {r.text}")

            click.echo(f"  Apify run started: {run_id}")

            # Wait for completion (poll)
            status_url = f"https://api.apify.com/v2/acts/{actor_id}/runs/{run_id}"
            while True:
                await asyncio.sleep(10)
                sr = await client.get(status_url)
                sr.raise_for_status()
                status_data = sr.json()
                status = status_data.get("data", {}).get("status", "")
                click.echo(f"  Status: {status}")
                if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                    break

            if status != "SUCCEEDED":
                raise RuntimeError(f"Apify run failed with status: {status}")

            # Get dataset ID
            default_dataset = status_data.get("data", {}).get("defaultDatasetId", "")
            if default_dataset:
                return await self.load_apify_dataset(default_dataset)

            return []

    async def _ensure_enricher(self) -> LeadEnricher:
        """Lazy-init the enricher (with API keys from env)."""
        if self.enricher is None:
            self.enricher = LeadEnricher()
            await asyncio.to_thread(self.enricher._cache.open)
        return self.enricher

    async def _enrich_lead(self, lead: MCALead) -> MCALead:
        """Enrich a single lead with phone/website/industry data."""
        enricher = await self._ensure_enricher()
        try:
            enriched = await enricher.enrich(lead)
            if enriched.phone_number:
                click.echo(f"    Phone: {enriched.phone_number} ({enriched.business_name})")
            return enriched
        except Exception as e:
            click.echo(f"  ⚠ Enrichment failed for {lead.business_name}: {e}", err=True)
            import traceback; traceback.print_exc()
            return lead

    # ── Report ───────────────────────────────────────────────────────

    @staticmethod
    def print_stats(stats: dict) -> None:
        """Print ingestion statistics."""
        click.echo(f"\n{'='*50}")
        click.echo(f"  INGESTION COMPLETE — {stats['source']}")
        click.echo(f"{'='*50}")
        click.echo(f"  Total filings processed: {stats['total']}")
        click.echo(f"  MCA filings identified:  {stats['mca_filings']}")
        click.echo(f"  Duplicates skipped:      {stats['duplicates']}")
        click.echo(f"  Errors:                  {stats['errors']}")
        click.echo(f"  Leads generated:         {stats['leads']}")
        click.echo(f"  ── Tier breakdown ──")
        click.echo(f"  Tier A (Hot):            {stats.get('tier_a', 0)}")
        click.echo(f"  Tier B (Warm):           {stats.get('tier_b', 0)}")
        click.echo(f"  Tier C (Cold):           {stats.get('tier_c', 0)}")
        click.echo(f"  Tier D (Archive):        {stats.get('tier_d', 0)}")


# ── CLI integration ────────────────────────────────────────────────────────


async def _ingest_command(
    db_path: Path,
    input_file: Optional[str] = None,
    input_format: str = "auto",
    column_map: Optional[str] = None,
    apify_actor: Optional[str] = None,
    apify_input: Optional[str] = None,
    apify_token: Optional[str] = None,
    apify_dataset: Optional[str] = None,
    enrich: bool = False,
):
    """Run the ingestion pipeline."""
    ingestor = UCCIngestor(db_path, enrich=enrich)

    if apify_dataset:
        click.echo(f"Loading Apify dataset: {apify_dataset}")
        filings = await ingestor.load_apify_dataset(apify_dataset)
        stats = await ingestor.process(filings, source_label=f"apify-dataset-{apify_dataset}")

    elif apify_actor:
        actor_input = {}
        if apify_input:
            actor_input = json.loads(apify_input)
        if apify_token:
            actor_input["_apify_token"] = apify_token

        click.echo(f"Running Apify actor: {apify_actor}")
        filings = await ingestor.load_apify_run(apify_actor, actor_input)
        stats = await ingestor.process(filings, source_label=f"apify-{apify_actor}")

    elif input_file:
        path = Path(input_file)
        if not path.exists():
            click.echo(f"File not found: {input_file}", err=True)
            return

        fmt = input_format
        if fmt == "auto":
            fmt = path.suffix.lower().lstrip(".")

        if fmt == "json":
            click.echo(f"Loading JSON: {input_file}")
            filings = await ingestor.load_json(path)
        elif fmt == "csv":
            click.echo(f"Loading CSV: {input_file}")
            col_map = None
            if column_map:
                col_map = dict(p.split(":") for p in column_map.split(","))
            filings = await ingestor.load_csv(path, col_map)
        else:
            click.echo(f"Unknown format: {fmt}", err=True)
            return

        stats = await ingestor.process(filings, source_label=input_file)

    else:
        click.echo("No input source specified. Use --input, --apify-actor, or --apify-dataset.", err=True)
        return

    UCCIngestor.print_stats(stats)


# Register the CLI command (called from cli.py)
def register_ingest_command(cli):
    @cli.command()
    @click.option("--input", "-i", "input_file", help="Input file (JSON or CSV)")
    @click.option("--format", "-f", "input_format", default="auto", help="Input format: json, csv, auto")
    @click.option("--map", "column_map", default=None, help="CSV column mapping: our_name:csv_col,...")
    @click.option("--apify-actor", default=None, help="Apify actor ID to run")
    @click.option("--apify-input", default=None, help="Apify actor input JSON")
    @click.option("--apify-token", default=None, help="Apify API token")
    @click.option("--apify-dataset", default=None, help="Apify dataset ID to load directly")
    @click.option("--enrich/--no-enrich", default=False, help="Skip-trace phone numbers and website via Google Places / LLM")
    @click.pass_context
    def ingest(ctx, input_file, input_format, column_map, apify_actor, apify_input, apify_token, apify_dataset, enrich):
        """Ingest UCC filings from file or API, run through classification pipeline."""
        db_path = ctx.obj["db_path"]
        asyncio.run(_ingest_command(
            db_path,
            input_file=input_file,
            input_format=input_format,
            column_map=column_map,
            apify_actor=apify_actor,
            apify_input=apify_input,
            apify_token=apify_token,
            apify_dataset=apify_dataset,
            enrich=enrich,
        ))

    return cli
