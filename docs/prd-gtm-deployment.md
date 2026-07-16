# UCC-1 Scraper MCA Lead Platform — PRD

## Section: Go-To-Market Strategy & Production Deployment

### 1. Go-To-Market Strategy

#### Primary Persona: MCA Brokers & ISOs

MCA brokers and independent sales organizations (ISOs) are the primary addressable market. These intermediaries buy business funding leads at **$200-800 per deal** from lead generation companies, credit data aggregators, and UCC filing services. Their core workflow is:

1. Identify small businesses that recently took secured financing (via UCC-1 filings)
2. Reach out before the current funder's exclusivity period expires
3. Offer consolidation, better terms, or follow-on funding

The UCC-1 Scraper platform automates step one — the most labor-intensive part — and eliminates recurring lead-buying costs.

#### Value Proposition

| Competitor | Price | What You Get | Our Position |
|------------|-------|-------------|--------------|
| Cobalt Intelligence | $500+/mo enterprise | UCC data + skip tracing | We're free to self-host |
| Master MCA Leads | $200-500/mo | Curated UCC leads | Same data, zero marginal cost |
| Apify UCC Actors | Pay-per-run ($5-50 ea) | Raw filings only | Our classification is proprietary |
| Manual state searches | Hours of labor per week | Per-state, unclassified | Automated daily, scored |

**Core value prop:** The same UCC-1 data that lead brokers pay hundreds per month for — at zero marginal cost. You run the scraper; the platform classifies, scores, and deduplicates. The only costs are a $15/mo VPS and, when needed, captcha solvers or Apify credits.

#### Secondary Market: Hard Money Lenders

Hard money and bridge lenders are a natural secondary market with **larger average deal sizes** ($50K-500K vs $5K-50K for MCA). A UCC-1 filing signals a business that already secured collateralized debt — the same profiles hard money lenders target. Positioning for this segment:

- Filter leads by filing amount and collateral type (equipment, real estate, receivables)
- Larger ticket = higher allowable cost per lead ($500-2,000 range)
- Fewer total leads needed, but higher willingness to pay for accuracy

#### Lead Generation & Sales Funnel

**Phase 1 — Lead Magnet (free sample):**
- Generate a sample CSV of 50 hot leads from Oregon filings
- Distribute via targeted LinkedIn outreach to MCA brokers, ISO networks, and commercial finance groups
- Include a one-page brief: "50 Oregon Businesses That Just Took MCA Funding — Free"
- Every recipient sees a scoring breakdown (our 7-factor model) and a comparison to Master MCA Leads pricing

**Phase 2 — Freemium to Paid:**
- Free tier: 50 leads/mo, 1 state, CSV only
- Paid tiers unlock volume, multiple states, daily email delivery, and API access
- Conversion trigger: after the free sample, the broker sees lead quality and wants more volume

**Phase 3 — Channel Partnerships:**
- Apify integration means existing Apify users can subscribe through the Apify marketplace
- MCA software platforms (CRM tools used by ISOs) are potential white-label partners
- Revenue share: 20-30% for channel partners who embed the lead feed

#### Competitive Differentiation

- **Transparent scoring:** Our 7-factor model (funding recency, debtor name match, secured party reputation, filing jurisdiction density, collateral specificity, filing history frequency, geographic proximity) is published and auditable. Competitors treat scoring as a black box.
- **Self-hosted optionality:** A broker with basic DevOps skills can run this on their own server. No vendor lock-in.
- **Zero data markup:** Marginal cost of data is scraping bandwidth. We don't pay for UCC data feeds.
- **State expansion:** 11 states already configured (CA, CO, DE, FL, GA, IL, MD, NJ, NY, OR, TX). As more states come online, lead volume grows without proportional cost increases.

#### Pricing Summary (Recommended)

| Tier | Price | Leads/Mo | States | Delivery | Best For |
|------|-------|----------|--------|----------|----------|
| Starter | $97/mo | 200 | 1 | CSV export | Solo brokers testing the channel |
| Pro | $247/mo | 1,000 | 5 (when avail.) | Daily email + CSV | Active broker teams |
| Agency | $497/mo | 5,000 | All | API + email + CSV | Lead aggregators, agencies |
| Enterprise | Custom | Unlimited | All + white-label | Dedicated pipeline, SLA | Funding platforms, CRM vendors |

---

### 2. Production Deployment

#### Server Architecture

| Component | Specification | Cost | Purpose |
|-----------|--------------|------|---------|
| **Execution server** | Hetzner CPX31 (4 vCPU, 8GB RAM, 80GB NVMe) | $14.98/mo | Runs scrapers, pipeline, SQLite |
| **Provisioned at** | 5.161.229.209 | — | Already deployed |
| **OS** | Ubuntu 24.04 LTS | — | — |
| **Database** | SQLite (production, single-user mode) | — | $0; Postgres upgrade path designed |

SQLite is adequate for the initial phase. With ~2,970 filings processed, the database is under 50MB. SQLite will remain performant through ~100,000 leads. At that threshold, migration to PostgreSQL on the same Hetzner instance is a configuration change plus data migration — the codebase uses a DB-API2 abstraction layer.

#### Daily Cron Schedule

The `daemon` command generates cron-ready schedules. Recommended production crontab:

```
# Oregon daily scrape (6 AM) — ~100-300 filings/day
0 6 * * * /usr/local/bin/ucc-scrape scrape --states OR --days 1 --output /var/ucc-leads/raw/$(date +\%Y-\%m-\%d)-or.json

# Pipeline ingestion + classification (30 min post-scrape)
30 7 * * * /usr/local/bin/ucc-scrape ingest --input /var/ucc-leads/raw/$(date +\%Y-\%m-\%d)-or.json

# Export hot + warm leads to CSV (immediately after ingestion)
0 8 * * * /usr/local/bin/ucc-scrape export --tier A --format csv --output /var/ucc-leads/exports/hot-$(date +\%Y-\%m-\%d).csv
0 8 * * * /usr/local/bin/ucc-scrape export --tier B --format csv --output /var/ucc-leads/exports/warm-$(date +\%Y-\%m-\%d).csv

# Health check (hourly)
0 * * * * /usr/local/bin/ucc-scrape dry-run --states OR && echo "UCC scraper healthy" || curl -fsS -m 10 --retry 5 -o /dev/null https://hc-ping.com/YOUR_HEARTBEAT_UUID
```

#### Export Automation & CRM Delivery

- CSV output is structured for direct import into common CRM tools (HubSpot, Salesforce), mail merge tools, and dialer platforms
- Columns: `business_name, debtor_name, filing_date, secured_party, tier, score, collateral, jurisdiction, filing_number, raw_url`
- For paid tiers: automated email attachment via `mutt` or SMTP relay with daily digest
- Agency/Enterprise tier: REST API endpoint (`/api/v1/leads`) with API key authentication and pagination

#### Monitoring & Alerting

| Check | Frequency | Action on Failure |
|-------|-----------|-------------------|
| `dry-run --states OR` | Hourly | Ping healthchecks.io → email alert |
| `stats` — total leads vs. yesterday | Daily at 9 AM | Zero new leads → inspect SOS portal changes |
| Disk usage (SQLite + exports) | Daily | >80% → rotate exports to object storage |
| Scraper HTTP error rate | Per-run | >10% errors → escalate to maintenance window |

#### Development Workflow

- All changes tested locally with `--days 1` to minimize state portal load
- Scraper changes validated via `dry-run` before deployment
- SQLite database is `scp`-ed down for analysis; never modified in place on production without a backup

---

### 3. State Expansion Roadmap

The Oregon scraper proves the model: POST-based search against a state's UCC database, with secured-party first-word matching against the 134-funder dictionary and debtor-name prefix matching against MCA-industry terms. Each new state requires adapting this pattern to that portal's search interface and response format.

| Phase | States | Est. Daily Leads | Est. Effort | Key Challenge |
|-------|--------|-----------------|-------------|---------------|
| **1 (NOW)** | OR | 100-300 | Done | — |
| **2 (Week 1)** | MD | +200-500 | 1 day | Captcha solver integration (~$2/mo) |
| **3 (Week 2)** | FL | +500-1,000 | 2 days | API-based + debtor-name search available |
| **4 (Month 1)** | CT, CO, other Apify states | +1,000-2,000 | Integration done | Apify actor subscription ($5-20/run) |
| **5 (Month 2)** | TX bulk data | +2,000-5,000 | Subscription cost | TX offers bulk UCC download for ~$50/yr |
| **6 (Quarter 2)** | CA, NY, NJ | +3,000-8,000 | 1-2 weeks each | High-volume portals; captcha + rate limiting |

**Action items for Week 1:**
1. Fork the Oregon scraper pattern for Maryland — adapt POST parameters, add captcha solving
2. Verify the Florida SOS API documentation exists and supports debtor-name queries
3. Deploy the Apify integration (already built in the universal ingestor) against a CT run
4. Create a `scrape --states all` meta-command that fans out across all active scrapers

**State prioritization criteria:**
- **MCA activity density** (NY, CA, FL, TX are highest)
- **Search interface complexity** (simple POST forms first, captcha portals second)
- **Bulk data availability** (TX bulk download is a multiplier)
- **Apify coverage** (states already served by Apify actors are integration-only effort)

---

### 4. Risk & Mitigation

#### Risk Register

| Risk | Probability | Impact | Mitigation |
|------|-----------|--------|-----------|
| **State portal changes** (form fields, session handling, response format) | Medium | High — scraper breaks | Modular scraper architecture per state; hourly `dry-run` health checks; each scraper is a <300-line module, easy to patch |
| **Portal blocks scraping** (IP ban, rate limiting, captcha escalation) | Low-Medium | Medium | Hetzner IP rotation; rate limiting built in (~2 req/sec); captcha solver service as fallback; TX bulk download avoids portal scraping entirely |
| **Low MCA match rate** from debtor name search | Medium | Medium | Secured-party (funder name) search produces 10x better matches than debtor name search; always use SP search when portal supports it |
| **Single-state dependency** (Oregon is only live scraper) | High (now) → Low (Month 1) | High | Parallelize: Apify state actors require zero portal scraping; MD and FL scrapers are low-effort forks of the OR pattern |
| **Data quality / stale filings** | Medium | Medium | Deduplication by filing number; `--days 1` limits to new filings; enrichment layer (skip tracing, D&B validation) is a paid add-on |
| **SQLite scaling limits** | Low (until >100K leads) | Medium | Migration path to PostgreSQL is documented and the codebase abstracts storage via DB-API2; swap the connection string |
| **Competitive response** (competitors drop prices, add product features) | Low | Low | Self-hosted nature means no vendor lock-in; our cost structure (VPS + scrapers) is already near-zero; competing on price is sustainable |

#### Mitigation Timeline

| Now | Week 1 | Month 1 | Quarter 2 |
|-----|--------|---------|-----------|
| Health checks on OR | MD scraper live | 3+ state scrapers | 6+ state scrapers |
| Backup SQLite daily | Apify CT run | Apify regular schedule | TX bulk subscription |
| | Captcha solver deploy | Postgres migration plan documented | Enrichment pipeline (skip trace) |

#### Data Enrichment Roadmap (Post-MVP)

- **Skip tracing:** Append phone, email, owner name to UCC leads (third-party API, $0.02-0.10 per lookup)
- **Business verification:** D&B or similar to confirm active status, revenue band, industry code
- **Funder overlap detection:** Cross-reference secured party against known funder portfolios to identify consolidation opportunities
- **Lead scoring v2:** Incorporate enrichment data into the 7-factor model (e.g., verified phone available → +20 points)

These are value-add features for the Agency/Enterprise tiers and are not required for MVP launch.

---

### Summary of Action Items

1. **Immediate:** Deploy daily cron on Hetzner CPX31; configure healthchecks.io monitoring
2. **Week 1:** Fork Oregon scraper for Maryland; run first Apify integration test (CT); produce sample 50-lead CSV for outbound
3. **Month 1:** Launch MVP with 3+ states; publish Starter/Pro/Agency pricing on landing page
4. **Quarter 2:** Expand to 6+ states; evaluate enrichment pipeline investment based on conversion data
