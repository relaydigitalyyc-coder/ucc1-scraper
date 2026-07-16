# PRD: UCC-1 Scraper — Best MCA Potential Leads

**Status:** Draft v1.0
**Date:** 2026-07-15
**Author:** Ariel (via Claude Opus 4.8)

---

## 1. Executive Summary

### 1.1 What Is This?

A specialized web scraping and lead intelligence platform that extracts UCC-1 financing filings from public state records, identifies businesses that have existing Merchant Cash Advance (MCA) financing, and ranks them by their likelihood to accept a new MCA offer.

### 1.2 Why It Matters

MCA funders spend $200–$800 per acquired lead through brokers and aggregators. The most valuable leads are businesses that have **already proven** they'll use MCA financing — they have active UCC-1 filings from known MCA lenders. The UCC-1 public record is the single strongest signal of MCA intent. No existing tool combines:
- Multi-state UCC scraping at scale
- MCA-funder identification (which secured parties are MCA vs traditional lenders)
- Lead scoring based on timing, stacking behavior, and industry
- Actionable lead exports for broker CRMs

### 1.3 Core Value Proposition

> **Find businesses who are about to need more capital — before your competitors call them.**

---

## 2. Problem Statement

### 2.1 Current Pain

| Pain Point | Impact |
|---|---|
| UCC data exists in 50+ separate state databases, each with different UIs, search APIs, and formats | Manual lookups take hours per lead, impossible to scale |
| Most UCC filings are not MCA-related (equipment loans, real estate, etc.) | Signal-to-noise ratio is terrible without filtering |
| MCA brokers buy stale lead lists from aggregators | Leads are cold by the time they're sold — 3–10 other brokers already called |
| No automated way to detect "stackers" (businesses with 2+ active MCA positions) | Missing the highest-intent, highest-urgency leads |
| Existing scrapers (Apify actors) are generic — they scrape filings but don't score MCA relevance | Manual filtering and qualification still required |

### 2.2 The Opportunity

MCA funders file UCC-1 financing statements against a business's future receivables. These are **public record** in every state. A business with a UCC-1 from an MCA funder has:
- **Proven intent**: They've used MCA before
- **Active need**: Their current advance is being repaid daily
- **Predictable timing**: You can estimate when their current advance will be paid off based on filing date + typical advance term
- **Verified existence**: The business is real, has receivables, and passed underwriting

---

## 3. Target Users

### 3.1 Primary Personas

| Persona | Needs | Use Case |
|---|---|---|
| **MCA Broker/ISO** | Daily fresh leads, phone numbers, business details | Cold calling businesses nearing end of current MCA term |
| **MCA Funder/Direct Lender** | High-intent leads, stacking detection, risk filtering | Direct mail + outbound to qualified prospects |
| **Lead Aggregator/Reseller** | Bulk exports, API access, white-label | Reselling enriched UCC leads to multiple brokers |

### 3.2 User Stories (MVP)

- As a broker, I want to see **every business in my state** that took an MCA in the last 90 days, so I can call them before they refinance elsewhere.
- As a broker, I want to know **which businesses have 2+ active MCA positions** (stackers), because they're the most desperate for consolidation capital.
- As a funder, I want to **filter by industry** (restaurants, trucking, retail) so I only buy leads in my lending verticals.
- As a funder, I want a **daily email** with new UCC filings from MCA funders in my target states.
- As a power user, I want to **export to my CRM** (GoHighLevel, Salesforce, HubSpot) with one click.

---

## 4. Product Overview

### 4.1 System Diagram (Conceptual)

```
┌─────────────────────────────────────────────────────────────┐
│                    UCC-1 Scraper Platform                     │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐  │
│  │  State   │   │  State   │   │  State   │   │  State   │  │
│  │ Scraper  │   │ Scraper  │   │ Scraper  │   │ Scraper  │  │
│  │   (NY)   │   │   (CA)   │   │   (FL)   │   │  (...N)  │  │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘  │
│       │               │               │               │       │
│       └───────────────┴───────┬───────┴───────────────┘       │
│                               │                                │
│                    ┌──────────▼──────────┐                    │
│                    │   UCC Ingestion      │                    │
│                    │   & Normalization    │                    │
│                    └──────────┬──────────┘                    │
│                               │                                │
│                    ┌──────────▼──────────┐                    │
│                    │  MCA Funder Match    │                    │
│                    │  (Known Funder DB)   │                    │
│                    └──────────┬──────────┘                    │
│                               │                                │
│                    ┌──────────▼──────────┐                    │
│                    │   Lead Scoring       │                    │
│                    │   Engine             │                    │
│                    └──────────┬──────────┘                    │
│                               │                                │
│                    ┌──────────▼──────────┐                    │
│                    │   API / Export /     │                    │
│                    │   Notification Layer │                    │
│                    └─────────────────────┘                    │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 Key Data Points per Lead

| Field | Source | Priority |
|---|---|---|
| Business Legal Name | UCC Debtor field | Critical |
| DBA / Trade Name | UCC Debtor field | High |
| Business Address | UCC Debtor address | Critical |
| Phone Number | Enrichment (Skip Trace) | High |
| Filing Date | UCC filing metadata | Critical |
| Filing Number | UCC filing metadata | Medium |
| Secured Party (Lender) Name | UCC Secured Party field | Critical |
| Secured Party Type | MCA Funder DB match | Critical |
| Collateral Description | UCC Collateral field | High |
| Filing Status (Active/Terminated) | UCC status | Critical |
| Jurisdiction / State | Source system | Critical |
| Legal Structure (LLC, Corp, etc.) | UCC debtor info | Medium |
| Industry / NAICS | Enrichment | High |
| Estimated Revenue | Enrichment | Medium |
| Number of Active MCA Positions | Cross-reference DB | Critical |
| Estimated Advance Amount | Collateral/receivables analysis | Medium |
| Days Since Filing | Computed | Critical |
| Stacking Score | Lead scoring engine | Critical |

---

## 5. MCA Funder Identification

### 5.1 The MCA Funder Database

This is the **proprietary moat** of the product. A maintained database of known MCA funders with:

- Legal entity names and all known DBAs
- States they operate in
- Typical advance sizes
- Typical term lengths
- Industries they lend to
- Known UCC filing patterns (how they describe collateral)

### 5.2 Funder Categories

| Category | Examples | Signal Strength |
|---|---|---|
| **Tier 1: Pure MCA** | Yellowstone Capital, forward financing, Pearl Capital, Kapitus, OnDeck, Credibly, Expansion Capital, Everest Business Funding | Very High |
| **Tier 2: MCA + Other** | Celtic Bank (also SBA), WebBank (also consumer), Cross River Bank | High |
| **Tier 3: Adjacent Alternative** | Equipment finance companies, factoring companies, revenue-based financing | Medium |
| **Tier 4: Traditional** | Local banks, credit unions, traditional term lenders | Low (filter out) |

### 5.3 Collateral Text Analysis

MCA UCC-1s have distinctive collateral language patterns. Example signals:

```
"all accounts, chattel paper, deposit accounts, instruments, 
investment property, letter-of-credit rights, and general 
intangibles, including all future accounts and receivables"
```

Key patterns that flag MCA vs traditional lending:
- "future receivables" or "future accounts"
- "purchase of future receivables" (not a loan — it's a purchase agreement)
- No specific equipment or real property listed
- "all assets" blanket liens (common in MCA)
- Confession of judgment language (COJ — unique to MCA/NY)

---

## 6. Technical Architecture

### 6.1 Stack Recommendation

| Layer | Technology | Rationale |
|---|---|---|
| **Scrapers** | Python (Playwright + Scrapy) | Best ecosystem for web scraping, stealth plugins |
| **Queue/Orchestration** | Redis + Celery / BullMQ | Job scheduling, rate limiting, retry logic |
| **Database** | PostgreSQL + TimescaleDB | Relational data + time-series for filing history |
| **Search** | Elasticsearch | Full-text search across business names, addresses |
| **API** | FastAPI (Python) or Hono (TypeScript) | RESTful API for frontend and integrations |
| **Frontend** | Next.js + shadcn/ui | Dashboard, search, export UI |
| **Infrastructure** | Docker + Kubernetes | Scalable scraping fleet |
| **Proxy** | Residential proxy pool (Bright Data, Oxylabs) | Avoid IP blocks on state websites |
| **LLM/ML** | OpenAI/Claude API for collateral text classification | NLP on collateral descriptions to classify MCA vs non-MCA |

### 6.2 Scraper Architecture

Each state gets its own scraper module implementing a common interface:

```python
class StateUCCScraper(ABC):
    state: str
    base_url: str
    search_endpoint: str
    
    @abstractmethod
    async def search_by_date_range(self, start: date, end: date) -> List[UCCFiling]:
        ...
    
    @abstractmethod
    async def get_filing_detail(self, filing_number: str) -> UCCFilingDetail:
        ...
    
    @abstractmethod
    async def check_status(self, filing_number: str) -> FilingStatus:
        ...
```

### 6.3 State System Categories

| Category | States | Approach |
|---|---|---|
| **Modern API** | DE, NV, CO | Direct REST/JSON API calls |
| **Legacy Web Portal** | NY, CA, FL, TX, IL | Playwright browser automation |
| **Third-Party Aggregator** | Smaller states | CSC, Wolters Kluwer, or Secretary of State bulk data |
| **PDF/Image-Based** | Some rural states | OCR + structured extraction |
| **Paywalled** | Some states | Subscription API integration |

### 6.4 Data Pipeline

```
Scrape → Normalize → Deduplicate → Classify (MCA?) → Enrich → Score → Store → Export
  │          │            │              │             │        │        │        │
  │          │            │              │             │        │        │        │
  Raw      Clean     Cross-ref     MCA Funder     Skip     Lead     Lead    CSV/API/
  HTML     struct    business      DB match +     Trace    Score    DB      CRM
           fields    across        collateral     (phone,                    push
                     states        NLP            email,
                                                  industry)
```

---

## 7. Lead Scoring Engine

### 7.1 The "Best MCA Potential" Score

Composite score (0–100) built from weighted factors:

| Factor | Weight | Rationale |
|---|---|---|
| **MCA Funder Match** | 25% | Is the secured party a known MCA funder? Tier 1 = 100, Tier 2 = 70, Tier 3 = 40 |
| **Recency** | 20% | Days since filing. 0–30d = 100, 31–60d = 80, 61–90d = 60, 91–120d = 40, 120d+ = 20 |
| **Term Maturity** | 20% | Estimated remaining term. Nearing payoff (80%+ complete) = 100 — they need renewal capital |
| **Stacking** | 15% | Multiple active MCA positions. 2 = 50, 3 = 75, 4+ = 100 (desperate for consolidation) |
| **Industry** | 10% | High-MCA-uptake industries (restaurants, trucking, retail, construction, medical) = 100 |
| **Business Vintage** | 5% | Time in business. <1yr = 0 (risky), 1–2yr = 50, 2–5yr = 80, 5yr+ = 100 |
| **Filing Status** | 5% | Active = 100, Amended = 50, Terminated = 0 (filter) |

### 7.2 Lead Tiers

| Tier | Score Range | Action |
|---|---|---|
| **Tier A — Hot** | 80–100 | Immediate call. Nearing end of term, proven MCA user, likely shopping for next round. |
| **Tier B — Warm** | 60–79 | Queue for this week. Active MCA position, decent timing. |
| **Tier C — Cold** | 40–59 | Nurture campaign. Recently filed, won't need capital for months. |
| **Tier D — Archive** | <40 | Low priority. Traditional lender, terminated filing, or too old. |

### 7.3 Stacking Detection Algorithm

```
For each business (normalized name + address):
  1. Find all active UCC-1 filings where debtor matches
  2. Filter to MCA funders only (Tiers 1-3)
  3. Count active positions with filing dates within typical MCA term window (3-18 months)
  4. If count ≥ 2 → flag as "stacker"
  5. Stacking Severity = count × (1 / avg_days_remaining)
```

---

## 8. Supported Jurisdictions (Phased)

### Phase 1 — High-Volume MCA States (MVP)
| State | Why | Est. Daily New Filings |
|---|---|---|
| **New York** | #1 MCA market, COJ state, highest filing volume | 200–400 |
| **California** | Largest business population | 300–500 |
| **Florida** | High MCA penetration | 150–300 |
| **Texas** | Large business population | 150–300 |
| **Illinois** | Major MCA market | 100–200 |

### Phase 2 — Secondary MCA Markets
NJ, GA, PA, OH, NC, MA, AZ, CO, NV, WA

### Phase 3 — Full Coverage
All 50 states + DC

---

## 9. CRM & Export Integrations

### MVP Integrations
- **CSV/Excel Export** — Universal, works with everything
- **Webhook Push** — Real-time push to any endpoint
- **API (REST)** — Programmatic access for power users

### Post-MVP
- GoHighLevel (most popular MCA broker CRM)
- Salesforce
- HubSpot
- Pipedrive
- Zapier (connects to everything else)

---

## 10. Compliance & Legal Considerations

### 10.1 Public Records
UCC-1 filings are **public records** — scraping them is legal. However:
- Respect `robots.txt` and rate limits
- Use official state APIs where available
- Do not bypass paywalls illegally

### 10.2 TCPA Compliance
- Scraped phone numbers are business contact information (B2B exemption is broader)
- Users are responsible for DNC list scrubbing
- Include disclaimer in product

### 10.3 Data Storage
- Store only public record data + derived analytics
- No PII beyond business contact info
- Implement data retention policy (e.g., auto-archive terminated filings after 2 years)

---

## 11. Competitive Landscape

| Competitor | Strengths | Weaknesses |
|---|---|---|
| **Apify UCC Actors** | Cheap, already built | Generic — no MCA classification, no lead scoring, no enrichment |
| **Master MCA Leads** | MCA-specific, good filters | Curated/aggregated — not raw scraping, higher cost per lead |
| **LeadX** | UCC data organization for equipment finance | Equipment finance focus, not MCA-optimized |
| **Cobalt Intelligence** | Real-time UCC monitoring across states | Enterprise pricing, API-only, not MCA-specific |
| **FICOSO** | UCC search + flood determination | Traditional banking focus, not MCA |
| **Manual State Searches** | Free | Hours per lead, no scale |

### Our Differentiation
- **MCA-Native**: Built specifically for MCA lead generation, not generic UCC search
- **Smart Scoring**: Proprietary lead scoring based on MCA-specific signals
- **Enriched Output**: Business phone, industry, revenue estimates — ready to call
- **Affordable**: Priced for individual brokers, not enterprise-only

---

## 12. MVP Scope

### 12.1 MVP Must-Haves (Phase 1 — 6-8 weeks)

| Feature | Priority | Effort |
|---|---|---|
| NY, CA, FL scraper modules | P0 | 2 weeks |
| UCC data normalization pipeline | P0 | 1 week |
| MCA Funder DB (initial 200+ funders) | P0 | 1 week |
| Collateral text NLP classifier (MCA vs non-MCA) | P0 | 1 week |
| Deduplication across states | P1 | 3 days |
| Basic lead scoring (v1 algorithm) | P0 | 1 week |
| Web dashboard (search, filter, view leads) | P1 | 1 week |
| CSV export | P0 | 2 days |
| Daily scrape job (cron automation) | P0 | 3 days |
| Basic auth (login/signup) | P1 | 3 days |

### 12.2 Post-MVP (Phase 2 — 4-6 weeks)

| Feature | Priority |
|---|---|
| Additional 10 states | P1 |
| Skip tracing enrichment (phone numbers) | P0 |
| Industry classification enrichment | P1 |
| Email notification: daily new leads digest | P1 |
| Stacking detection algorithm | P1 |
| REST API | P1 |
| GoHighLevel integration | P2 |
| Lead status tracking (contacted, converted, dead) | P2 |

### 12.3 Future (Phase 3+)

| Feature | Priority |
|---|---|
| All 50 states | P2 |
| Real-time filing alerts (same-day detection) | P2 |
| Predictive lead scoring (ML model trained on conversion data) | P3 |
| Bank statement lead cross-referencing | P3 |
| White-label reseller portal | P3 |
| Mobile app for brokers in the field | P3 |

---

## 13. Success Metrics

### 13.1 Product KPIs
| Metric | Target (Month 3) | Target (Month 12) |
|---|---|---|
| Daily new leads scraped | 500+ | 5,000+ |
| MCA classification accuracy | >90% | >95% |
| Lead-to-call time (filing date → broker sees it) | <48 hours | <12 hours |
| Unique businesses in DB | 50,000+ | 500,000+ |
| Active MCA funders identified | 200+ | 500+ |

### 13.2 Business KPIs
| Metric | Target (Month 3) | Target (Month 12) |
|---|---|---|
| Active users | 20 | 200 |
| Monthly churn | <10% | <5% |
| Leads exported per user/month | 500 | 2,000 |

---

## 14. Pricing Model (Recommended)

| Tier | Price | Includes |
|---|---|---|
| **Starter** | $97/mo | 1 state, 200 lead exports/mo, CSV only |
| **Pro** | $247/mo | 5 states, 1,000 lead exports/mo, API access, email alerts |
| **Agency** | $497/mo | All states, 5,000 lead exports/mo, CRM integrations, priority support |
| **Enterprise** | Custom | White-label, dedicated proxy pool, SLA, on-premise option |

---

## 15. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| State websites change UI/block scrapers | Data pipeline stops | Modular scrapers, monitoring alerts, proxy rotation, Playwright stealth |
| Legal challenge to scraping public records | Business viability | Use official APIs where available, consult legal counsel, ToS compliance |
| Competitor enters MCA-specific UCC scraping | Market share | Move fast, build data moat (funder DB, lead scoring model), establish brand |
| MCA industry regulation changes | Market shrinks | Expand to adjacent verticals (equipment finance, SBA, factoring) |
| Data quality issues (wrong phone, stale info) | User trust | Skip trace enrichment, user feedback loop (flag bad leads), freshness indicators |

---

## 16. Open Questions

1. **Skip trace provider**: Which data enrichment vendor for business phone numbers? (ZoomInfo, Apollo, Clearbit, direct skip trace APIs?)
2. **Hosting**: Self-hosted on bare metal (cheaper proxy management) or cloud (AWS/GCP)?
3. **Initial funder DB**: Build manually (200 funders = ~40 hours research) or license from existing data provider?
4. **Free tier?** Offer 50 free leads/month to drive adoption, or paid-only?
5. **Open source core?** Open-source the scrapers, sell the platform/scoring/enrichment? (Freemium developer model)
6. **Multi-language?** UCC filings in Puerto Rico are in Spanish — handle?
7. **Historical backfill?** Scrape past 2 years of filings or start from today forward?

---

## 17. Recommendation & Next Steps

### Immediate Actions
1. **Build MCA Funder Database**: Research and catalog 200+ known MCA funders, their legal entities, DBAs, and UCC filing patterns. This is the foundation everything else depends on.
2. **Pilot NY State Scraper**: New York is the #1 MCA market. Build the NY scraper first as proof of concept — validate the scraping approach, MCA classification accuracy, and lead quality before expanding.
3. **Collateral Text Classifier**: Train/test an LLM-based classifier on 500+ sample UCC collateral descriptions to distinguish MCA from traditional lending with >90% accuracy.
4. **Talk to 10 MCA Brokers**: Validate willingness to pay, feature priorities, and pricing before building full platform.

### Build Decision
The architecture lends itself to **incremental delivery**: scraper → pipeline → scoring → UI. Each layer adds value independently. Recommend building the NY-only scraper + classification pipeline first, delivering CSV exports manually to 3–5 pilot users, then expanding to platform.

---

*End of PRD. For questions, clarifications, or to proceed to technical specification, see next steps above.*
