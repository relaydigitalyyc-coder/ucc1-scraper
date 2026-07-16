# PRD: Lead Scoring Engine & Real Estate Strategy

**Status:** Built, tested, and proven on Oregon production data
**Date:** 2026-07-16
**Section of:** UCC-1 Scraper MCA Lead Platform PRD

---

## 1. MCA Lead Scoring Architecture

### 1.1 The 7-Factor Composite Score

Every UCC filing that enters the pipeline is scored from 0-100 on seven weighted factors. Scores are computed using only data available at the time of scraping — no enrichment required. The scoring engine (`src/pipeline/scorer.py`) implements this as a deterministic, rules-based system:

| Factor | Max Points | Scoring Logic | Signal Captured |
|--------|-----------|---------------|-----------------|
| **Funder Match** | 25 | Tier 1 (Pure MCA) = 25, Tier 2 (MCA + other) = 18, Tier 3 (Adjacent) = 10, MCA collateral detected but no funder match = 8, General business assets = 4 | Is the secured party a known MCA lender? |
| **Recency** | 20 | 0-30 days = 20, 31-60 = 16, 61-90 = 12, 91-120 = 8, 121-180 = 4, 180+ = 0 | How fresh is this lead? |
| **Term Maturity** | 20 | 70-95% of estimated term elapsed (renewal sweet spot) = 20, 50-70% = 15, 30-50% = 10, >95% = 8, <30% = 2-5 | Is the business nearing payoff and ready to renew? |
| **Stacking** | 15 | 5+ active MCA positions = 15, 4 = 13, 3 = 11, 2 = 7, 1 = 0 | Is the business stacking MCA debt (desperation signal)? |
| **Industry** | 10 | Keyword match in debtor name (restaurant, trucking, retail, construction, medical, auto, hotel, salon, etc.) = 10, no match = 3 | Does this industry have high MCA uptake? |
| **Vintage** | 5 | Currently a placeholder — always returns 3 (neutral). Requires incorporation date enrichment to score properly. | How long has the business been operating? |
| **Filing Status** | 5 | Active = 5, Continued = 4, Amended = 2, Terminated/Lapsed = 0, Unknown = 2 | Is the filing still in effect? |

The total is clamped to 0-100 and mapped to one of four lead tiers.

### 1.2 Lead Tiers

The tier system translates raw scores into broker-facing action labels:

| Tier | Score Range | Label | Action | Oregon Distribution |
|------|-------------|-------|--------|-------------------|
| **A** | 80-100 | Hot | Call immediately — nearing end of MCA term, proven repeat user | 4 leads |
| **B** | 60-79 | Warm | Queue for this week — active MCA, decent timing signal | 231 leads |
| **C** | 40-59 | Cold | Nurture campaign — recently filed, won't need capital for months | 949 leads |
| **D** | <40 | Archive | Low priority — traditional lender, terminated, or too old | 1,787 leads |

Tier A (Hot) leads are the highest-value output: businesses with active MCA positions from pure-MCA funders, within the renewal sweet spot, and showing stacking behavior. A broker calling these leads has a time advantage over every competitor who relies on purchased lead lists.

### 1.3 The MCA Funder Database (Proprietary Moat)

The funder database (`src/funders/funder_db.json`) is the single largest source of competitive advantage. It contains 165 known MCA funders catalogued across three tiers:

- **106 Tier 1 (Pure MCA):** Yellowstone Capital, Forward Financing, Pearl Capital, Kapitus, Credibly, CFG Merchant Solutions, Rapid Advance, Snap Advances, Square Capital, PayPal Working Capital, Stripe Capital, Shopify Capital, Amazon Capital, and 94 others
- **33 Tier 2 (MCA + Other Lending):** Celtic Bank, WebBank, Cross River Bank, Customers Bank, Axos Bank, Live Oak Banking Company, and others that do MCA alongside SBA, consumer, or commercial lending
- **26 Tier 3 (Adjacent):** Equipment finance companies (GreatAmerica, Balboa Capital), revenue-based financing platforms (Pipe, Flyer Capital, Novel Capital), and alternative lenders

Each funder entry includes: legal name, all known DBAs, typical advance size range, typical term length, and active states. This allows term maturity estimation — a filing from a funder with a 180-day typical term lets us compute payoff proximity from the filing date.

**Funder Matching Strategy** (implemented in `src/pipeline/classifier.py`):
1. **Exact match** — quick lookup against indexed names + DBAs
2. **Fuzzy match** — TheFuzz token_sort_ratio at 85% threshold (catches "YELLOWSTONE CAPITAL LLC AS AGENT" vs "YELLOWSTONE CAPITAL LLC")
3. **Substring match** — funder name appears inside a longer secured party name (minimum 5 chars)

### 1.4 Collateral NLP: 17 MCA Patterns + Keyword Fallback

When no funder name match exists, the classifier falls back to collateral description analysis. The system maintains 17 regex patterns that detect distinctive MCA language:

- **Future receivables language:** "future accounts", "future receivables", "purchase of future receivables", "future credit card receivables"
- **Confession of Judgment:** "confession of judgment", "COJ" (unique to MCA/NY filings)
- **Daily ACH patterns:** "daily ACH", "ACH authorization", "daily debit", "lock box receivables", "split funding receivables"
- **Blanket liens:** "all assets now owned or hereafter acquired", "all present and future accounts"
- **Revenue-based financing:** "revenue based financing", "merchant cash advance", "MCA agreement"

Ten anti-patterns prevent false positives: real estate descriptions, vehicle VIN numbers, specific equipment serial numbers, fixture filings, and agricultural/farm collateral are excluded.

After pattern matching, a keyword fallback classifies collateral into equipment, vehicle, inventory, or real estate using industry-standard terms (excavator, bulldozer, VIN, deed of trust, etc.). This catches equipment loans, vehicle loans, and real estate filings that the MCA patterns correctly exclude.

**False Positive Guard:** The system explicitly checks for mixed patterns. If both MCA and non-MCA patterns appear, the classifier prefers MCA only if MCA pattern count >= non-MCA count. This prevents blanket liens on real estate from being misclassified.

### 1.5 Production Results: Oregon (2,971 Filings)

The first production run against Oregon UCC filings demonstrates the system works at scale:

| Metric | Value |
|--------|-------|
| Total UCC filings scraped (Oregon) | 2,971 |
| MCA-classified (via funder DB + collateral NLP) | ~1,190 (40% match rate) |
| Tier A (Hot) leads delivered | 4 |
| Tier B (Warm) leads delivered | 231 |
| Tier C (Cold) leads delivered | 949 |
| Top Tier 1 MCA funders found | CFG Merchant Solutions, Epic Advance, Maverick Funding, VitalCap Fund, CapChase |
| Top Tier 2 funders found | Celtic Bank, Cross River Bank, WebBank (multiple DBAs), Axos Bank, Live Oak Banking Company |

The 40% MCA match rate from a broad secured-party search confirms that MCA funders are heavy UCC filers. Even at 4 Hot leads per state, expanding to 10+ states yields 40+ calls-ready-now leads per scrape cycle. The 231 Warm leads are actionable this week across any state.

---

## 2. Stacking Detection Algorithm

### 2.1 Cross-State Deduplication

The stacking detector (`src/pipeline/dedupe.py`) identifies businesses with multiple active MCA positions — the strongest signal of urgent capital need. It operates through three layers:

**Layer 1: Business Key Generation.** Every filing gets a normalized business key: `normalized_name|city|state`. Normalization strips entity suffixes (LLC, INC, CORP, LP), removes punctuation, collapses whitespace, and uppercases. This ensures "Joe's Restaurant LLC" and "JOES RESTAURANT" in the same city/state resolve to the same business.

**Layer 2: Fuzzy Name Matching Across Filings.** When comparing candidate filings within the same business key, the system uses fuzzy name similarity at 90%+ threshold. Location match (same city + state = +2 points) and lender match (same secured party = +2 points) boost confidence. A combined score of 4+ signals a high-confidence duplicate. The 85% threshold mirrors the funder matching threshold, ensuring consistency.

**Layer 3: MCA Position Counting.** For each business, the system counts active filings where the secured party is flagged as an MCA funder. The scoring engine then maps raw count to points:

| Active MCA Positions | Stacking Score | Interpretation |
|---------------------|----------------|----------------|
| 1 | 0 (none) | Single MCA — normal renewal cycle |
| 2 | 7 | Mild stacker — may need consolidation |
| 3 | 11 | Moderate stacker — high urgency |
| 4 | 13 | Heavy stacker — likely desperate |
| 5+ | 15 (max) | Deep stacker — critical need, high risk |

Stackers are the most responsive leads for consolidation offers. A business with 3+ active MCA positions is rotating advances to stay afloat — they will take a call and they will listen to a better option.

**Important caveat:** Stacking count is limited by the number of states scraped. A business with 4 MCA positions across NY, CA, and FL will only show 1 if only Oregon is scraped. Full stacking detection requires multi-state coverage, but even single-state stacking is a strong signal.

---

## 3. Real Estate Lead Strategy

### 3.1 Why RE UCCs Are 10x More Valuable Than MCA

Hard money and private real estate lenders file UCC-1s as a backup to their mortgage/deed of trust. These filings are a hidden gold mine for commercial real estate brokers and bridge lenders:

| Dimension | MCA Leads | Real Estate UCC Leads |
|-----------|-----------|----------------------|
| Typical loan size | $5K-$500K | $50K-$5M+ |
| Typical brokerage fee | $1K-$15K | $5K-$150K+ |
| Interest rates | 1.1-1.5 factor (implied 30%+ APR) | 12-18% hard money |
| Lead conversion value | $200-$800 per funded deal | $50K+ per funded deal |
| Capital need frequency | 4-6 month cycles | 6-24 month cycles |
| Borrower sophistication | Low (main street) | Moderate (real estate investors) |

A single converted real estate lead can equal the lifetime value of 50+ MCA leads. The risk profile is also better — RE leads are backed by hard assets, and the borrowers are generally more creditworthy and responsive to outreach.

### 3.2 Hard Money Lender Database

The real estate scorer (`src/pipeline/real_estate_scorer.py`) catalogues 25+ known alternative real estate lenders, organized by tier:

**Tier 1 — Dedicated Hard Money / Bridge Lenders (25 points):**
LendingHome, Kiavi, Lima One Capital, RCN Capital, Anchor Loans, Civic Financial, CoreVest, Groundfloor, Patch of Land, Builders Capital, Construction Financial

**Tier 2 — Private REITs / Institutional Real Estate Lenders (18 points):**
Goldman Sachs Bank USA (real estate arm), Blackstone, Starwood, Arbor Realty, Walker & Dunlop, Berkeley Point, Greystone, NewRez, Fundrise, Yieldstreet, Peachtree, Main Street Renewal, Tricon, Progress Residential, Amherst, Invitation Homes

**Fallback pattern detection (10 points):** Any secured party containing MORTGAGE, LENDING, CAPITAL PARTNERS, REAL ESTATE, REALTY, PROPERTY, or CONSTRUCTION LENDING is scored as mortgage-adjacent even if not in the known database.

### 3.3 RE Collateral Detection (15 patterns)

The system uses 15 regex patterns to detect real estate collateral language:

- Real property descriptions: "real estate/property located/situated at", "legal description"
- Deed language: "deed of trust", "mortgage property", "fee simple", "leasehold estate/interest"
- Parcel identifiers: "block [number] lot [number]", "parcel id/number/#", "assessor parcel", "APN"
- Plat/subdivision: "subdivision plat", "condominium unit"
- Township/range: "township range section" (common in western states)
- Boundary descriptions: "metes and bounds"
- Improvements: "together with all improvements"
- Recording references: "recorded in book/deed/instrument"

Each pattern matched adds 5 points (capped at 20). A filing hitting 3+ RE collateral patterns scores maximum collateral points.

### 3.4 RE Debtor Entity Detection (6 patterns)

Real estate investors and developers name their LLCs distinctively. The system detects these entity patterns (5 points each, capped at 15):

1. **Holdings/property entities:** "holdings|properties|realty|real estate|investments [LLC/INC/LP]"
2. **Property-address names:** "[number] [street/avenue/road/drive]" in the business name
3. **Multi-family keywords:** "apartments|condos|townhomes|duplex|triplex"
4. **Fix-and-flip entities:** "fix flip|flip fix|rehab|renovation [LLC/INC]"
5. **Development entities:** "development|developers [LLC/INC/CORP]"
6. **Construction entities:** "construction|builders [LLC/INC/CORP]"

Entity structure itself adds up to 10 points: LLCs get +3, LPs get +2, and any entity gets a baseline of 5 — reflecting that real estate investments are almost always structured through pass-through entities.

### 3.5 RE Scoring Weights (0-100)

| Factor | Max Points | Scoring |
|--------|-----------|---------|
| **Lender Match** | 25 | Known hard money = 25, Private REIT = 18, Mortgage-adjacent keyword = 10 |
| **Collateral RE Patterns** | 20 | 5 points per RE pattern matched (max 4 patterns) |
| **Debtor RE Entity** | 15 | 5 points per RE entity pattern (max 3 patterns) |
| **Recency** | 15 | 0-30 days = 15, 31-90 = 12, 91-180 = 8, 181-365 = 4, 365+ = 1 |
| **Loan Size** | 15 | >= $5M = 15, >= $1M = 12, >= $500K = 8, >= $100K = 4, unknown = 5 |
| **Entity Structure** | 10 | LLC = 8 (5 base + 3), LP = 7 (5 base + 2), Other = 5 base |

Tier thresholds: A (75+, Hot-RE), B (55-74, Warm-RE), C (35-54, Cold-RE), D (<35, Archive-RE).

### 3.6 Combined MCA + RE Pipeline

Every UCC filing that enters the pipeline is scored for **both** MCA and real estate potential simultaneously. A single filing from a hard money lender against a property-address LLC scores high on the RE side, while a filing from CFG Merchant Solutions against a restaurant scores high on the MCA side. The two scoring paths are independent and additive — a filing can be a Tier B MCA lead and a Tier A RE lead.

This dual-path architecture means a broker using the platform sees every filing scored for both use cases, effectively doubling the value of each scrape.

---

## 4. Enrichment Opportunities (Future)

The following enrichment layers would materially improve scoring accuracy and lead actionability. None are implemented yet — they represent the highest-ROI development priorities post-MVP.

### 4.1 Skip Tracing for Phone Numbers

The single most requested feature from brokers. A business phone number transforms a CSV row into a dialable lead. No skip trace integration is currently wired. Target providers for evaluation: ZoomInfo, Apollo, Clearbit, and dedicated skip-trace APIs (TloNet, LexisNexis). Phone number acquisition would add phone_number and contact_name fields to the `MCALead` model, which already supports them.

### 4.2 Property Value Estimation from Address

For RE leads specifically, address-based property value estimation would dramatically improve loan size scoring. A hard money lead against a $2M property is worth 10x a $200K property, and current loan size scoring defaults to neutral (5 points) for unknowns. Integration with county assessor databases, Zillow/ATTOM APIs, or public MLS data would enable accurate value estimation.

### 4.3 Incorporation Date Lookup (Vintage Scoring)

The vintage score (5 points) is currently a placeholder returning neutral 3 because incorporation date is not available from UCC filings alone. Adding secretary of state business entity lookups — searching for the debtor's legal name to find formation date — would unlock this factor. A business with 5+ years of incorporation is a materially better credit risk than a 6-month-old LLC. Multiple states offer free SOS business search APIs.

### 4.4 Owner/Principal Name Extraction

Beyond phone numbers, identifying business owners by name is the second most requested enrichment. UCC filings sometimes include individual names in the debtor field (especially for sole proprietorships and single-member LLCs), but most business-entity filings do not. Cross-referencing the business name against secretary of state filings (registered agent, officers) or business databases (Dun & Bradstreet, Data Axle) would yield owner names — directly enabling personalized outreach.

### 4.5 Funding Priority Map

| Enrichment | Impact on Scoring | Impact on Actionability | Implementation Effort | Priority |
|-----------|-------------------|------------------------|----------------------|----------|
| Skip trace (phone) | Low | Critical (dialable lead) | Medium (API integration) | P0 |
| Incorporation date | Medium (unlocks vintage) | Low | Low (SOS API lookup) | P1 |
| Property value | High (RE loan size) | Medium | High (multiple data sources) | P1 |
| Owner name extraction | Low | High (personalized outreach) | Medium | P2 |
| Industry/NAICS enrichment | Medium (precise industry scoring) | Medium | Medium | P2 |

---

*This section covers built and production-tested functionality. The MCA scoring engine, stacking detection, and real estate lead strategy are fully implemented and verified against Oregon UCC data.*
