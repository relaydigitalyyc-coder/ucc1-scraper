# Multi-State UCC-1 Scraper Platform — Architecture & Expansion PRD

**Status:** Draft v3.0 (2026-07-17)
**Based on:** Live probing of 12+ state portals (2026-07-15/16), public-record-data-scrapper methodology, production operation of Oregon + Florida scrapers
**Build Status:** 10,262 leads in DB, 639 with phones, 260 tests passing

---

## 1. Multi-State Architecture

### 1.1 Access Method Tiers (in preference order)

Each state ranked by best available access method:

| Tier | Method | Example | Speed | Cost | Fragility |
|------|--------|---------|-------|------|-----------|
| 1 | **Public API** | FL publicsearchapi, CT Socrata | Fast | Free | Low |
| 2 | **Direct POST** | OR Java/Struts, NJ ASP.NET | Medium | Free | Medium |
| 3 | **Bulk Download** | TX bulk CSV, CSC/Wolters Kluwer | Fast | Paid | Low |
| 4 | **Scraper (browser)** | MD ASP.NET + Turnstile, CA BizFile | Slow | Free | High |
| 5 | **Scraper (datacenter proxy)** | NY Oracle, CO JSF | Slow | Proxy cost | High |
| 6 | **Third-party vendor** | Apify actors, Cobalt Intelligence | Fast | Per-record | Low |

### 1.2 Current State Matrix

| State | Tier | Method | Secured Party? | Date Range? | Daily Volume | Status |
|-------|------|--------|---------------|-------------|-------------|--------|
| **OR** | ✅ 2 | POST | YES | YES | ~3,000 | **LIVE** |
| **FL** | ✅ 1 | REST API | NO | NO | ~20,000 | **LIVE** (debtor only) |
| **MD** | ⏳ 4 | ASP.NET + Turnstile | YES | NO | ~2,000-5,000 | **READY** (needs CapSolver key) |
| **NY** | ⏳ 5 | Oracle + F5 TSPD | Unknown | Unknown | ~1,000 | **SODA FALLBACK** (debtor data only) |
| **CT** | ⏳ 1 | Socrata Open Data | YES | YES | ~500 | **APIFY PROVEN** (waiting for free tier reset) |
| **CO** | ⏳ 1 | Socrata Open Data | YES | YES | ~500 | **APIFY PROVEN** |
| **TX** | 📋 3 | SOSDirect bulk | YES | YES | ~5,000+ | **NEEDS SUBSCRIPTION** |
| **NJ** | ❌ | ASP.NET | NO | YES | — | **UNSUITABLE** (no SP) |
| **DE** | ❌ | Entity search | NO | — | — | **UNSUITABLE** (no UCC data) |
| **CA** | 📋 4 | BizFile SPA | Unknown | Unknown | — | **UNEXPLORED** |
| **CO (direct)** | ❌ 5 | JSF portal | — | — | — | **HTTP 403** |

### 1.3 Hybrid Architecture

The `StateCollectorFactory` pattern from public-record-data-scrapper models each state as a tiered collector:

```
For each state:
  Try Tier 1 (API) → Fallback to Tier 2 (POST) → Fallback to Tier 4 (Browser)
  → Fallback to Tier 5 (Proxy) → Fallback to Tier 6 (Vendor)
```

Implemented in our `BaseStateScraper` + per-state scraper classes + `registry.py`:

```
/scrapers/base.py        — Abstract base (Playwright + httpx dual support)
/scrapers/oregon.py      — Tier 2 (direct POST, CSRF, detail pages)
/scrapers/florida.py     — Tier 1 (public REST API, no auth)
/scrapers/maryland.py    — Tier 4 (Playwright + CapSolver Turnstile bypass)
/scrapers/new_york.py    — Tier 5 (SODA API fallback)
```

---

## 2. Scoring & Classification Engine

### 2.1 MCA Lead Scoring (7 factors, 0-100)

| Factor | Weight | Logic |
|--------|--------|-------|
| Funder Match | 25 pts | Tier 1 = 25, Tier 2 = 18, Tier 3 = 10, MCA collateral = 8 |
| Recency | 20 pts | ≤30d = 20, ≤60d = 16, ≤90d = 12, ≤120d = 8, ≤180d = 4 |
| Term Maturity | 20 pts | 70-95% elapsed = 20 (renewal sweet spot) |
| Stacking | 15 pts | 5+ MCA positions = 15, 4 = 13, 3 = 11, 2 = 7 |
| Industry | 10 pts | Restaurant/trucking/construction/medical/auto = 10 |
| Vintage | 5 pts | Placeholder — needs incorporation date enrichment |
| Status | 5 pts | Active = 5, Amended = 2, Terminated = 0 |

**Tiers:** A (80-100) Hot, B (60-79) Warm, C (40-59) Cold, D (<40) Archive

**Production results (OR + FL, 10,262 leads):**
- 1,188 MCA-classified (11.6% match rate)
- 34 Tier B (Warm), 1,460 Tier C
- Top funders: CFG MERCHANT SOLUTIONS, CREDIBLY, FUNDING METRICS, NEWTEK

### 2.2 MCA Funder Database (165 entities)

| Tier | Count | Type |
|------|-------|------|
| 1 (Pure MCA) | 106 | Yellowstone, Forward Financing, CFG, Credibly, Kapitus, OnDeck, etc. |
| 2 (MCA+Other) | 33 | Celtic Bank, WebBank, Cross River, Ready Capital |
| 3 (Adjacent) | 26 | Equipment finance, factoring, revenue-based financing |

### 2.3 Collateral NLP (17 MCA patterns + 35 RE patterns)

MCA indicators: future receivables, COJ, daily ACH, lock box, split funding, purchase of future accounts
RE indicators: real property, deed of trust, block/lot, parcel ID, metes and bounds, subdivision plat

### 2.4 Real Estate Lead Scoring (101 lenders, 35 collateral patterns)

| Category | Lenders | Expected Value per Lead |
|----------|---------|------------------------|
| Hard Money / Bridge | 38 | $50K-$5M loans, 12-18% APR |
| Fix & Flip | 9 | Short-term, 6-24 months |
| Construction | 8 | Ground-up development |
| Private REITs | 35 | Institutional, multi-family |
| Traditional Bank | 11 | Conventional RE loans |

---

## 3. Enrichment Pipeline

### 3.1 Phone Number Enrichment (639 of 10,262 leads enriched)

| Method | Hit Rate | Cost | Status |
|--------|----------|------|--------|
| Google Maps Playwright (local IP) | 87% | Free | 419 phones — rate-limited |
| DeepSeek API | 73% | ~$0.50/100 leads | 216 phones — rate-limited |
| Cloudflare Workers (10 edge) | TBD | Free | Deployed, need Google Places key |
| Free tier (SEC EDGAR, OSHA, USPTO) | TBD | Free | Built, untested |
| Skip-trace CSV upload | ~85% | $0.01-0.03/lead | Ready for manual use |

### 3.2 Enrichment Architecture

```
LeadEnricher (LeadEnricher class, 1,131 lines)
├── LLMEnricher (DeepSeek/OpenAI/Claude) — 73% hit, ~$0.005/lead
├── GooglePlacesEnricher — 90% hit, $200/mo free credit
├── WebSearchEnricher — DuckDuckGo fallback
├── FreeEnricher — SEC EDGAR, OSHA, USPTO, Census
└── GmapsEnricher — Playwright-based, 87% hit
```

---

## 4. Deployment Architecture

### 4.1 Current Infrastructure

| Component | Host | Spec | Cost |
|-----------|------|------|------|
| VPS | Hetzner CPX31 | 4 vCPU, 8GB RAM, 150GB SSD | $14.98/mo |
| Dashboard | Cloudflare Pages | Static HTML, edge CDN | Free |
| Enrichment Workers | Cloudflare Workers (x10) | 100K requests/day free | Free |
| Phone Enrichment | API-based (GMaps/DeepSeek) | Usage-based | ~$1-5/mo |
| Source Code | GitHub | Private repo | Free |

### 4.2 Daily Pipeline

```
6:03 AM UTC — VPS cron: vps-daily.sh
  ├── Scrape Oregon (SP search, ~2,849 filings)
  ├── Scrape Florida (API, ~7,413 filings)
  ├── Ingest → Classify → Score → Dedupe
  ├── Refresh dashboard JSON
  └── Export CSV (tier A, B, all)

Manual / as needed:
  └── enrich-leads.py (DeepSeek or GMaps)
      └── Deploy dashboard with new phones
```

### 4.3 Costs

| Item | Monthly |
|------|---------|
| VPS (Hetzner) | $14.98 |
| DeepSeek API | ~$2-10 (pay-as-you-go) |
| CapSolver (for MD) | ~$2 |
| Google Places API (optional) | $0 ($200/mo free credit) |
| Cloudflare Pages | $0 |
| **Total** | **~$17-27/mo** |

---

## 5. Expansion Roadmap

### Phase 1 (Now — Week 1) — Core States

| State | Action | Est. Leads | Priority |
|-------|--------|------------|----------|
| OR | ✅ Live — daily cron running | 3,000/day | — |
| FL | ✅ Live — API scraper running | 20,000/day | — |
| MD | 🚀 Deploy — set CapSolver key, turn on | 3,000/day | **HIGH** |
| CT/CO | 🚀 Apify on free tier reset (monthly) | 500/day | **MEDIUM** |

### Phase 2 (Weeks 2-4) — Scale

| State | Action | Est. Leads | Priority |
|-------|--------|------------|----------|
| TX | Subscribe to SOSDirect bulk ($) | 5,000/day | **HIGH** |
| NY | Residential proxy for F5 bypass | 3,000/day | **HIGH** |
| CA | Live probe BizFile portal | 5,000/day | **MEDIUM** |
| Apify | Integrate all 3 UCC actors with token | 1,000/day | **MEDIUM** |

### Phase 3 (Month 2+) — Full Coverage

| State | Action | Est. Leads | Priority |
|-------|--------|------------|----------|
| All 50 | SSC/Wolters Kluwer bulk subscription | 50,000+/day | **LOW** (expensive) |
| Auto-enrich | Wire DeepSeek into daily pipeline | — | **HIGH** |

### Volume Projection

```
Phase 1: OR + FL + MD  = ~26,000/day
Phase 2: + TX + NY + CA = ~39,000/day  
Phase 3: Full bulk       = ~60,000+/day
```

At 11% MCA match rate: Phase 1 = **~2,860 callable MCA leads/day**

---

## 6. CLI Quick Reference

```bash
# Scrape
ucc-scrape scrape --states OR,FL,MD --days 7

# Enrich
export DEEPSEEK_API_KEY="..."
ucc-scrape enrich --method deepseek --limit 100

# View & Export
ucc-scrape leads --tier B
ucc-scrape export --tier all -o leads.csv

# Dashboard
python3 scripts/refresh-dashboard.py
npx wrangler pages deploy dashboard --project-name ucc1-leads --commit-dirty=true

# Stats
ucc-scrape stats
```

---

*PRD v3.0 — synthesized from live portal probing, 260 passing tests, and 10,262-lead production database.*
