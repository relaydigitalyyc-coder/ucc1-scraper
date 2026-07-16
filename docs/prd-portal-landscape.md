# PRD: UCC-1 Scraper MCA Lead Platform -- Section 3: State Portal Landscape & Technical Architecture

**Status:** Draft v2.0 (updated 2026-07-16)
**Based on:** Live probing of 10+ state UCC portals
**Previous:** PRD.md (v1.0, 2026-07-15) -- this section supersedes Sections 6 and 8 of the original PRD

---

## 3. State Portal Landscape & Technical Architecture

### 3.1 State Portal Landscape

Over two days (2026-07-15/16), we probed 12 state UCC search portals to understand their technical architecture, search capabilities, and anti-bot posture. The results fundamentally reshape our go-to-market strategy.

#### 3.1.1 Portal Comparison Matrix

| State | URL | Tech Stack | Date Range Search? | Secured Party Search? | Results Include SP? | Bot Protection | Verdict |
|-------|-----|------------|-------------------|----------------------|-------------------|----------------|---------|
| **OR** | secure.sos.state.or.us/ucc/ | Java/Struts, POST-based | YES (beginningDate/endingDate) | YES (radio button) | YES (detail page only) | None | **VIABLE NOW** |
| **FL** | floridaucc.com | React/MUI SPA + JSON API | NO | NO | YES (results table) | None | **PARTIAL** (API exists, debtor-only) |
| **MD** | egov.maryland.gov/SDAT/UCCFiling/ | Tyler Technologies ASP.NET | NO | YES (radio button) | YES | Cloudflare Turnstile | **BLOCKED** (solvable with captcha service) |
| **NY** | apps.dos.ny.gov/ucc-search/ | Oracle PL/SQL (mod_plsql) | Unknown | Unknown | Unknown | TCP-level (datacenter IP blocking) | **BLOCKED** (needs residential proxy) |
| **CO** | sos.state.co.us/ucc/ | JavaServer Faces (JSF) | Unknown | Unknown | Unknown | HTTP 403 on all endpoints | **BLOCKED** (needs investigation) |
| **MI** | (moved to ISC RegSys 06/2025) | Cloudflare-protected | Unknown | Unknown | Unknown | Cloudflare bot protection | **BLOCKED** (post-migration) |
| **MA** | -- | Imperva/Incapsula WAF | Unknown | Unknown | Unknown | WAF (403, timeout loops) | **BLOCKED** |
| **TX** | direct.sos.state.tx.us/ | Classic ASP, login-gated | Unknown | Unknown | Unknown | Account-based (paid $1/search) | **BLOCKED** (paywall) |
| **NJ** | njportal.com/UCC/ | ASP.NET Web Forms | YES (FromDate/ToDate) | NO (debtor-only free tier) | YES | None | **UNSUITABLE** (no SP in free tier) |
| **DE** | icis.corp.delaware.gov/ | ASP.NET (entity search only) | N/A | N/A | N/A | None | **UNSUITABLE** (no public UCC data) |
| **IL** | apps.ilsos.gov/uccsearch/ | SPA (anti-bot) | Unknown | Unknown | Unknown | Cloudflare-like | **UNTESTED** (needs proxy) |
| **CA** | bizfileonline.sos.ca.gov/ | Modern SPA | Unknown | Unknown | Unknown | Unknown | **UNTESTED** |

#### 3.1.2 Viability Map

```
VIABLE NOW (can produce leads today):
  OR  -- Oregon (fully working, ~2,971 filings from 196 searches over 1 year)
  FL  -- Florida (partial -- has JSON API but debtor-only search)

BLOCKED (portal is reachable but obstructed):
  MD  -- Maryland (has ideal features! blocked only by Turnstile captcha -- ~$2/mo to solve)
  NY  -- New York (#1 MCA market, blocked by datacenter IP rejection -- needs residential proxy)
  CO  -- Colorado (403 on all endpoints)
  MI  -- Michigan (post-migration, unknown new system)
  MA  -- Massachusetts (Imperva WAF)

UNSUITABLE (cannot get secured party data):
  NJ  -- New Jersey (debtor-only free search, no secured party without paid subscription)
  TX  -- Texas (SOSDirect requires paid login, $1/search)
  DE  -- Delaware (no public UCC search at all -- "Authorized Searcher" certification required)

UNTESTED (code written, portal behavior unknown):
  CA  -- California (BizFile portal, needs live probing)
  IL  -- Illinois (anti-bot protection, needs residential proxy testing)
  GA  -- Georgia (GSCCCA portal, needs live probing)
```

#### 3.1.3 Key Insight: State Portals Are Debtor Self-Service Tools

The most important finding from our probe: **state UCC portals are built for debtors to look up their own filings, not for lead generation.** This has specific consequences:

1. **Secured party search is rare.** Only Oregon and Maryland confirmed they have a "Search by Secured Party" radio button. Most portals default to debtor name search -- they assume the filer (the debtor) is the user.

2. **Date range search is rare.** Only Oregon confirmed date-range search with beginningDate/endingDate parameters. Most portals expect you to search by a specific name or filing number.

3. **Result limits are tight.** Most portals cap results at 50-500 filings. Oregon returns 500 max per search. No portal we found exposes pagination beyond the first page of results in a meaningful way.

4. **Detail pages are inconsistent.** Some states show all parties (debtor + secured) on the search results page (FL). Others show only the searched party on results and require a detail lookup for the other side (OR). This has major implications for scraper design.

### 3.2 Scraper Architecture

#### 3.2.1 BaseStateScraper -- Dual-Protocol Design

All state scrapers inherit from `BaseStateScraper` (src/scrapers/base.py), which provides:

- **Playwright async browser lifecycle** (start/stop/new_page) for JS-heavy portals (FL, NJ, CA, TX, MD)
- **HTTP POST capability** (via httpx) for server-rendered portals that require no JavaScript (OR)
- **Rate limiting** via configurable `requests_per_second`, `max_retries`, and `retry_delay`
- **Tenacity-based retry** with exponential backoff for connection errors and server 5xx responses
- **Stealth initialization** via `page.add_init_script()` that hides `webdriver` and `plugins` detection vectors
- **Abstract interface** with three methods: `search_by_date_range()`, `get_filing_detail()`, `check_status()`

```python
class BaseStateScraper(ABC):
    state: str
    state_name: str
    base_url: str
    requests_per_second: float = 1.0
    max_retries: int = 3
    retry_delay: float = 5.0

    async def search_by_date_range(start_date, end_date) -> AsyncIterator[dict]: ...
    async def get_filing_detail(filing_number) -> dict: ...
    async def check_status(filing_number) -> str: ...
    async def health_check() -> dict: ...
```

Scrapers self-register via the `@register_scraper("XX")` decorator (src/scrapers/registry.py). The registry provides `get_scraper("OR")` and `list_available_states()` for dynamic discovery.

#### 3.2.2 Oregon Reference Implementation

The Oregon scraper (src/scrapers/oregon.py) is our reference implementation because it demonstrates every key pattern:

**Step 1 -- CSRF Token Acquisition:**
```python
# GET the search home page, extract CSRF token from hidden input
r = self._client.get(SEARCH_URL)  # https://secure.sos.state.or.us/ucc/searchHome.action
soup = BeautifulSoup(r.text, "html.parser")
self._csrf = soup.find("input", {"name": "CSRFToken"})["value"]
```

**Step 2 -- POST Search with Parameters:**
```python
# Same endpoint for debtor and secured party search; radio button controls behavior
data = {
    "nonStandardEntityType": "Organization",
    "nonStandardSearchOrgName": name,          # "begins with" matching
    "assocNameType": "Search by Secured Party", # or "Search by Debtor"
    "beginningDate": "01/01/2026",              # MM/DD/YYYY
    "endingDate": "06/30/2026",
    "CSRFToken": self._csrf,
}
r = self._client.post(NS_SEARCH_URL, data)     # /ucc/nsSearch.action
```

**Step 3 -- Parse Results Table:**
- Parses `table#securedTable` with columns: Name, Address, Lien Number, Type, Filed, Terminate, Lapse Date
- Clickable lien numbers via `generateFileNumberSearchResult(id)` JavaScript function

**Step 4 -- Detail Lookup:**
```python
r = self._client.post(DETAIL_URL, {
    "inputLienNumberStr": lien_number,
    "CSRFToken": self._csrf,
})
# Parse CSS classes: dName (debtor name), spName (secured party name)
```

**Step 5 -- Dual Search Strategy:**
```python
# Strategy A: Search by secured party (funder first word) -- yields known-MCA filings
for word in funder_first_words:
    results = self._search_secured(word, start_date, end_date)
    # Detail gives us the real debtor name

# Strategy B: Search by debtor (MCA-industry keywords) -- discovers new funders
for prefix in MCA_DEBTOR_PREFIXES:  # "RESTAURANT", "TRUCKING", etc.
    results = self._search_debtor(prefix, start_date, end_date)
    # Detail gives us the secured party name
```

**Rate Limiting:** Oregon resets connections at ~2 req/sec. The scraper uses 1 req/sec with exponential backoff (2^attempt seconds) and CSRF refresh on retry.

**Yield:** ~2,971 unique filings from 196 combined searches across a 1-year window.

#### 3.2.3 API-First Approach Where Possible

Florida revealed an alternative pattern: the public SPA at floridaucc.com exposes a backing JSON API at `publicsearchapi.floridaucc.com/search`. While this API only supports debtor name search (no secured party, no date range), the JSON response model is far easier to consume than jQuery-based page parsing.

**Principle:** For any state with a SPA-based portal, first attempt to find and reverse-engineer the backing JSON API using browser DevTools. API scraping is faster, more reliable, and less brittle than DOM scraping or Playwright automation.

#### 3.2.4 Retry + Rate Limiting Strategy

```
UNIFIED STRATEGY (applied per-state via configurable parameters):
┌─────────────────────────────────────────────────────────┐
│  1. requests_per_second: float                          │
│     → Throttle: sleep(1 / rps) between requests         │
│     → Oregon: 1.0, Florida: 0.5, Texas: 0.33           │
│                                                         │
│  2. max_retries: int = 3                                │
│     → Exponential backoff: 2^attempt seconds            │
│     → On connect reset: refresh CSRF + retry            │
│                                                         │
│  3. Server 5xx handler                                  │
│     → Retry up to max_retries with 3^attempt delay      │
│                                                         │
│  4. CSRF token refresh on retry                         │
│     → GET searchHome → extract new token → retry POST   │
│                                                         │
│  5. Health check before full scrape                     │
│     → Returns {ok, status_code, url, error}             │
│     → Skips state if portal is down/blocked             │
└─────────────────────────────────────────────────────────┘
```

#### 3.2.5 The "Funder First Word" Search Strategy

The core algorithmic insight of this project: **most state portals support "begins with" name matching but not substring search.** If we search for "YELLOWSTONE", only entities whose name starts with "YELLOWSTONE" match.

However, searching for the full funder name "YELLOWSTONE CAPITAL INC" is too narrow. **Searching by the funder's first word only** captures all filings where the secured party name starts with that word -- including variants like "YELLOWSTONE FUNDING LLC", "YELLOWSTONE CAPITAL PARTNERS", etc.

Implementation (OregonScraper._get_funder_first_words):
- Load all Tier 1 and Tier 2 funders from the MCA funder database
- Extract the first word of each funder's legal name
- Filter to words >= 3 characters
- Deduplicate and sort
- Search each word as a secured party name

This generates ~50-100 search terms (one per funder) vs. the thousands of individual funder name variants, dramatically reducing the search surface while maintaining high recall.

#### 3.2.6 CSRF Token Management Pattern

Many state portals (Oregon, potentially others) use hidden CSRF tokens that must be acquired, included in POST data, and refreshed when they expire:

```
1. GET /ucc/searchHome.action
2. Parse: <input name="CSRFToken" value="abc123" />
3. POST /ucc/nsSearch.action { ..., "CSRFToken": "abc123" }
4. Response includes new token in HTML
5. Extract new token for next request
6. On any HTTP error: re-GET searchHome to acquire fresh token
```

This pattern is handled in OregonScraper._refresh_csrf() and the post-retry logic.

### 3.3 Lessons Learned

#### 3.3.1 What We Thought Would Work vs. What Actually Worked

| Expectation | Reality | Impact |
|-------------|---------|--------|
| NY would be the first working scraper (#1 MCA market) | NY blocks datacenter IPs entirely | OR is now the reference, not NY |
| Most states would have date-range search | Only OR confirmed working date range | Must design around name-based search |
| Secured party search would be common | Only OR + MD confirmed SP search | Funder-first-word strategy is critical |
| Modern SPAs would be easier to scrape | ASP.NET/Java portals (OR, MD) are simpler than React SPAs (FL) | Playwright-over-HTTP tradeoff favors old tech |
| High-volume states would be accessible | Top 5 MCA states (NY, CA, FL, TX, IL) are all blocked or limited | Must rely on secondary states initially |

#### 3.3.2 Why Searching by MCA Funder Name as Secured Party Beats Debtor Industry Search

Two search strategies exist, and we now know the tradeoffs:

**Strategy A: Search by Secured Party (funder name)**
- Directly finds UCC filings where known MCA funders are lenders
- Results are HIGH precision (near 100% MCA relevance)
- But only finds filings from funders we already know about
- Cannot discover new funders

**Strategy B: Search by Debtor (MCA-industry keywords like "RESTAURANT")**
- Discovers MCA filings from ANY funder (including unknown ones)
- Results are LOW precision (majority are equipment loans, real estate, etc.)
- Requires detail lookup to identify the secured party for each filing
- High search volume (50+ industry keywords per state)

**Our approach: Use both, with A as primary and B as discovery.**

The yield differential is significant: Strategy A produces MCA filings at a rate ~10x higher per search than Strategy B, because every matched filing has a known MCA funder. Strategy B's value is expanding the funder database by discovering unknown funders.

#### 3.3.3 The Real Estate Gold Mine

During our probe, a separate line of analysis revealed that real estate UCC filings are potentially more valuable than MCA filings:

| Dimension | MCA Filing | Real Estate Filing |
|-----------|-----------|-------------------|
| Loan size | $5K-$500K | $50K-$5M+ |
| Broker commission (3%) | $150-$15K | $1.5K-$150K+ |
| Number of lenders | 50+ MCA funders | 500+ hard money/private lenders |
| Refinance frequency | Every 3-18 months | Every 6-24 months |
| Hit rate per SP search | High (MCA is distinctive) | Moderate (RE language overlaps general lending) |
| Collateral signal | "future receivables" | "real property", "mortgage" |

The real estate scorer (`src/pipeline/real_estate_scorer.py`) uses 25/20/15/15/15/10 weighting for lender match, collateral analysis, debtor entity type, recency, loan size, and entity structure. It can detect:
- Known hard money lenders (Kiavi, LendingHome, Lima One, etc.)
- Real estate collateral language ("deed of trust", "block X lot Y", "metes and bounds")
- Real estate entity patterns ("Properties LLC", "Holdings LP")

**Recommendation:** The platform should detect and flag real estate UCC filings as a separate lead type, not just filter them out as non-MCA noise. A single converted RE lead can be worth 10-100 MCA leads in commission.

#### 3.3.4 The "Begins With" Matching Problem

Most state portals only support prefix ("begins with") matching -- not substring or fuzzy search. This means:

- Searching for "ADVANCE" returns "ADVANCE FUNDING LLC" but NOT "CAPITAL ADVANCE LLC"
- Searching for "YELLOW" returns "YELLOWSTONE CAPITAL" but NOT "LEGACY YELLOWSTONE"
- Manually searching for every possible prefix variant is impractical (there are 300K+ possible name starts)

**Mitigations:**
1. **First-word search** (described above) -- captures most direct matches
2. **Search by known DBA names** (the funder DB includes multiple name variants per funder)
3. **Broad debtor search** (Strategy B) discovers funders we haven't indexed yet
4. **Cross-reference** with the MCA classifier's fuzzy matching engine (`thefuzz` with 85% token-sort threshold)

This is a fundamental constraint of the domain. No amount of technical sophistication can make a portal return results for a search it doesn't support. The principle is to maximize coverage within each portal's matching rules.

---

*End of Section 3. For the full PRD including executive summary, target users, lead scoring, pricing, and next steps, see PRD.md.*
