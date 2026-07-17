#!/bin/bash
# Feed unenriched leads to Cloudflare Worker, import phones back.
# Runs from cron after each VPS scrape.

set -e
cd /opt/ucc1-scraper

WORKER_URL="https://ucc1-enricher.erhazeariel.workers.dev"
SECRET="${WORKER_SECRET:-dev-secret}"
LIMIT="${1:-50}"

echo "=== CF Worker Enrichment ==="
echo "Worker: $WORKER_URL"
echo "Limit: $LIMIT"

# 1. Export unenriched leads as JSON
source .venv/bin/activate
python3 -c "
import aiosqlite, json, asyncio
async def main():
    db = await aiosqlite.connect('data/ucc_scraper.db')
    db.row_factory = aiosqlite.Row
    c = await db.execute('SELECT lead_id, business_name, business_city, business_state FROM leads WHERE (phone_number IS NULL OR phone_number=\"\") AND tier IN (\"B\",\"C\") ORDER BY score_total DESC LIMIT ${LIMIT}')
    rows = [dict(r) for r in await c.fetchall()]
    with open('data/cf_batch.json', 'w') as f: json.dump(rows, f)
    await db.close()
    print(f'Exported {len(rows)} leads')
asyncio.run(main())
"

# 2. POST to Cloudflare Worker
curl -s -X POST "$WORKER_URL" \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: $SECRET" \
  -d @data/cf_batch.json \
  -o data/cf_response.json

# 3. Import phones back
python3 -c "
import json, aiosqlite, asyncio
async def main():
    with open('data/cf_response.json') as f: data = json.load(f)
    results = data.get('results', [])
    found = 0
    db = await aiosqlite.connect('data/ucc_scraper.db')
    for r in results:
        phone = r.get('phone_number')
        if phone:
            await db.execute('UPDATE leads SET phone_number=? WHERE lead_id=?', (phone, r['lead_id']))
            found += 1
    await db.commit()
    await db.close()
    print(f'Imported {found} phones')
    stats = data.get('stats', {})
    print(f'Total: {stats.get(\"total\",0)}, Hit rate: {stats.get(\"hit_rate\",0)}%, Time: {stats.get(\"elapsed_ms\",0)}ms')
asyncio.run(main())
"
