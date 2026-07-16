#!/bin/bash
# UCC-1 Scraper Daily Pipeline
# Run on VPS via cron: 0 6 * * * /opt/ucc1-scraper/scripts/vps-daily.sh
set -e

cd /opt/ucc1-scraper
source .venv/bin/activate
LOG="/var/log/ucc1-daily.log"
echo "=== $(date) ===" >> "$LOG"

# 1. Scrape OR + FL and save
python3 -c '
import asyncio, json, time, sys, os
sys.path.insert(0, "src")
from scrapers.oregon import OregonScraper
from scrapers.florida import MCA_DEBTOR_PREFIXES
from datetime import datetime, timedelta
import httpx

async def run():
    filings = []

    # Oregon SP search
    try:
        scraper = OregonScraper()
        await scraper.start()
        end = datetime.now()
        start = end - timedelta(days=180)
        start_str = start.strftime("%m/%d/%Y")
        end_str = end.strftime("%m/%d/%Y")
        funders = scraper._get_funder_first_words()
        seen = set()
        for i, word in enumerate(funders):
            results = scraper._search_secured(word, start_str, end_str)
            for r in results:
                fn = r.get("lien_number","")
                if fn in seen: continue
                seen.add(fn)
                detail = scraper._get_detail(fn)
                if detail:
                    r["debtor_name"] = detail.get("debtor_name", r["debtor_name"])
                    r["secured_party_name"] = detail.get("secured_party_name", r["debtor_name"])
                filings.append(r)
            time.sleep(0.3)
        await scraper.stop()
    except Exception as e:
        print(f"OR error: {e}")

    # Florida API
    async with httpx.AsyncClient(timeout=30) as client:
        fl_seen = set()
        for prefix in MCA_DEBTOR_PREFIXES[:40]:
            row = None
            for _ in range(10):
                params = {"text": prefix, "searchOptionType": "OrganizationDebtorName", "searchOptionSubOption": "FiledAndLapsedCompactDebtorNameList", "searchCategory": "Standard"}
                if row: params["rowNumber"] = str(row)
                try:
                    r = await client.get("https://publicsearchapi.floridaucc.com/search", params=params)
                    data = r.json()
                    payload = data.get("payload", {})
                    for d in payload.get("debtors", []):
                        ucc = d.get("uccNumber","")
                        if ucc not in fl_seen:
                            fl_seen.add(ucc)
                            filings.append({"state":"FL","filing_number":ucc,"debtor_name":d.get("name",""),"filing_date":datetime.now().strftime("%m/%d/%Y"),"debtor_city":d.get("city",""),"debtor_state":d.get("state",""),"status":d.get("status","unknown"),"secured_party_name":"","source":"florida-api"})
                    row = payload.get("nextRowNumber")
                    if not row: break
                    time.sleep(0.05)
                except: break
            time.sleep(0.1)

    os.makedirs("data", exist_ok=True)
    with open("data/vps_leads.json","w") as f: json.dump(filings, f, indent=2)
    print(f"Scraped: {len(filings)}")

asyncio.run(run())
' >> "$LOG" 2>&1

echo "Scrape done: $(python3 -c "import json; print(len(json.load(open('data/vps_leads.json'))))") filings" >> "$LOG"

# 2. Run through pipeline
rm -f data/ucc_scraper.db
./venv/bin/ucc-scrape ingest --input data/vps_leads.json >> "$LOG" 2>&1

# 3. Export CSVs
mkdir -p data/exports
./venv/bin/ucc-scrape export --tier A -o data/exports/hot_leads_$(date +%Y%m%d).csv 2>/dev/null || true
./venv/bin/ucc-scrape export --tier B -o data/exports/warm_leads_$(date +%Y%m%d).csv 2>/dev/null || true
./venv/bin/ucc-scrape export --tier all -o data/exports/all_leads_$(date +%Y%m%d).csv 2>/dev/null || true

# 4. Refresh dashboard data
python3 scripts/refresh-dashboard.py >> "$LOG" 2>&1

# 5. Summary
echo "=== $(date) DONE ===" >> "$LOG"
./venv/bin/ucc-scrape stats >> "$LOG" 2>&1
echo "" >> "$LOG"
