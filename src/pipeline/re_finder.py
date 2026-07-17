"""Real Estate UCC Lead Finder — identifies hard money and private RE lending
opportunities from scraped UCC-1 filings.

Usage:
    from pipeline.re_finder import RealEstateLeadFinder, find_re_leads

    finder = RealEstateLeadFinder()
    leads = finder.find_re_leads(filings)

    # One-liner convenience
    leads = find_re_leads(filings)

Architecture:
    Complements the MCA pipeline (pipeline/classifier.py, scorer.py) as
    a downstream pass.  After MCA classification, RealEstateLeadFinder
    scans every filing for real-estate signals:

      1. Secured party is a known hard money / private RE lender (50+ DB)
      2. Collateral description contains real property language (20+ patterns)
      3. Debtor name suggests real estate entity (REIT, property holdings, etc.)
      4. Filing recency and entity structure (LLC/LP signals)

    Each lead is scored 0-100 and classified as tier A (hot) through D (archive).
    A single converted RE lead can be worth $50K+ in brokerage fees — 10-100x MCA.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from models.filing import UCCFiling


# ═══════════════════════════════════════════════════════════════════════════
# HARD MONEY & PRIVATE REAL ESTATE LENDER DATABASE
# ═══════════════════════════════════════════════════════════════════════════
#
# Organised by tier and category:
#   Tier 1 → Hard money, fix-and-flip, and construction lenders (10-18% APR)
#   Tier 2 → Private REITs, debt funds, and bridge lenders (institutional)
#   Tier 3 → Traditional banks that also file UCC-1s on RE loans
#
# Many of these lenders file UCC-1s as a backup lien to their primary
# mortgage or deed of trust, making them detectable in the UCC record.
#
# ═══════════════════════════════════════════════════════════════════════════

HARD_MONEY_LENDERS: dict[str, dict[str, Any]] = {
    # ── Tier 1: National Hard Money / Bridge Lenders ───────────────────
    # These specialise in real-estate-secured lending at 10-18% interest,
    # $50K-$5M loan sizes.  They file UCC-1s as a backup lien to their
    # mortgage/deed of trust.
    "LENDINGHOME": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "KIAVI": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "RCN CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "ANCHOR LOANS": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "LIMA ONE CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "LIMA ONE": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "CIVIC FINANCIAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "COREVEST": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "GROUNDFLOOR": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "PATCH OF LAND": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "AVANA CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "LENDINGONE": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "FINANCE OF AMERICA": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "TEMPLE VIEW CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "ROC CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "SACHEM CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "JET CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "BEEHILL CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "B2R FINANCE": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "A10 CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "GENESIS CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "HARBOR LOANS": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "HAVEN FINANCIAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "INVESTOR LOAN SOURCE": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "LODESTAR CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "NATIONWIDE LENDING": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "SAGE CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "SHELLPOINT": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "SOUTH RIVER CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "SOUTH STREET CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "STEARNS LENDING": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "TEMPO LENDING": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "THE MONEY SOURCE": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "VISIO LENDING": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "WATERMARK LENDING": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "CAPSTONE LENDING": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "ALPINE CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "1ST ADVANTAGE": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    # ── Tier 1: Fix & Flip Lenders ──────────────────────────────────
    # Short-term capital for residential property flipping (6-24 months).
    # Extremely active borrowers — always looking for the next deal.
    "FUND THAT FLIP": {"tier": 1, "category": "fix_and_flip", "states": ["ALL"]},
    "BACKFLIP": {"tier": 1, "category": "fix_and_flip", "states": ["ALL"]},
    "EASY STREET CAPITAL": {"tier": 1, "category": "fix_and_flip", "states": ["ALL"]},
    "CONVENTUS": {"tier": 1, "category": "fix_and_flip", "states": ["ALL"]},
    "LENDINGMAX": {"tier": 1, "category": "fix_and_flip", "states": ["ALL"]},
    "MAMMOTH LENDING": {"tier": 1, "category": "fix_and_flip", "states": ["ALL"]},
    "CERBIN LENDING": {"tier": 1, "category": "fix_and_flip", "states": ["ALL"]},
    "START CAPITAL": {"tier": 1, "category": "fix_and_flip", "states": ["ALL"]},
    "CAPITAL FUNDING GROUP": {"tier": 1, "category": "fix_and_flip", "states": ["ALL"]},
    # ── Tier 1: Construction Lenders ────────────────────────────────
    # Ground-up development and major renovation financing.
    "BUILDER FINANCE": {"tier": 1, "category": "construction", "states": ["ALL"]},
    "CONSTRUCTION FINANCIAL SOLUTIONS": {"tier": 1, "category": "construction", "states": ["ALL"]},
    "BUILDERS CAPITAL": {"tier": 1, "category": "construction", "states": ["ALL"]},
    "CONSTRUCTION FINANCIAL SERVICES": {"tier": 1, "category": "construction", "states": ["ALL"]},
    "SILVER HILL FINANCIAL": {"tier": 1, "category": "construction", "states": ["ALL"]},
    "HOMEBRIDGE FINANCIAL": {"tier": 1, "category": "construction", "states": ["ALL"]},
    "EAGLE HOMES": {"tier": 1, "category": "construction", "states": ["ALL"]},
    "ADVANCED CONSTRUCTION LENDING": {"tier": 1, "category": "construction", "states": ["ALL"]},
    # ── Tier 2: Private REITs & Debt Funds ────────────────────────────
    # Large-scale real estate lenders managing billions in AUM.
    # They fund commercial, multi-family, and build-to-rent.
    "BLACKSTONE": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "STARWOOD": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "ARBOR REALTY": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "WALKER & DUNLOP": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "BERKELEY POINT": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "GREYSTONE": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "NEWREZ": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "READY CAPITAL": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "ARES MANAGEMENT": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "KKR REAL ESTATE": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "TPG REAL ESTATE": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "APOLLO": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "OAKTREE CAPITAL": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "GOLUB CAPITAL": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "BROOKFIELD": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "COLONY CAPITAL": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "FS INVESTMENT": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "GOLDMAN SACHS BANK USA": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "MORGAN STANLEY PRIVATE BANK": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "DEUTSCHE BANK": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "PEACHTREE GROUP": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "AMHERST": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "TRICON": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "FUNDRISE": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "YIELDSTREET": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    # ── Tier 2: Bridge Lenders (Multi-Family / Commercial) ─────────────
    # Gap financing between acquisition and permanent / take-out loans.
    "SANTANDER BANK": {"tier": 2, "category": "bridge_lender", "states": ["ALL"]},
    "SIGNATURE BANK": {"tier": 2, "category": "bridge_lender", "states": ["ALL"]},
    "FIRST REPUBLIC": {"tier": 2, "category": "bridge_lender", "states": ["ALL"]},
    "CITIZENS BANK": {"tier": 2, "category": "bridge_lender", "states": ["ALL"]},
    "BMO HARRIS": {"tier": 2, "category": "bridge_lender", "states": ["ALL"]},
    "KEYBANK": {"tier": 2, "category": "bridge_lender", "states": ["ALL"]},
    "M&T BANK": {"tier": 2, "category": "bridge_lender", "states": ["ALL"]},
    "BANNER BANK": {"tier": 2, "category": "bridge_lender", "states": ["ALL"]},
    "WESTERN ALLIANCE": {"tier": 2, "category": "bridge_lender", "states": ["ALL"]},
    "ZIONS BANK": {"tier": 2, "category": "bridge_lender", "states": ["ALL"]},
    # ── Tier 3: Traditional Banks with RE Lending ──────────────────────
    # Large banks that make significant real-estate loans.
    # Lower priority than hard-money / private lenders.
    "BANK OF AMERICA": {"tier": 3, "category": "traditional_bank", "states": ["ALL"]},
    "WELLS FARGO": {"tier": 3, "category": "traditional_bank", "states": ["ALL"]},
    "JPMORGAN CHASE": {"tier": 3, "category": "traditional_bank", "states": ["ALL"]},
    "TRUIST": {"tier": 3, "category": "traditional_bank", "states": ["ALL"]},
    "PNC BANK": {"tier": 3, "category": "traditional_bank", "states": ["ALL"]},
    "U.S. BANK": {"tier": 3, "category": "traditional_bank", "states": ["ALL"]},
    "CITIBANK": {"tier": 3, "category": "traditional_bank", "states": ["ALL"]},
    "REGIONS": {"tier": 3, "category": "traditional_bank", "states": ["ALL"]},
    "COMERICA": {"tier": 3, "category": "traditional_bank", "states": ["ALL"]},
    "SILICON VALLEY BANK": {"tier": 3, "category": "traditional_bank", "states": ["ALL"]},
    "MAIN STREET RENEWAL": {"tier": 3, "category": "traditional_bank", "states": ["ALL"]},
}

# ── Total lender count for diagnostics ─────────────────────────────────
TOTAL_RE_LENDERS = len(HARD_MONEY_LENDERS)

# ═══════════════════════════════════════════════════════════════════════════
# REAL ESTATE COLLATERAL DETECTION PATTERNS (30+)
# ═══════════════════════════════════════════════════════════════════════════
#
# These detect real-property language in the collateral_description field
# of a UCC-1 filing.  Multiple matches is definitive.
#
# ═══════════════════════════════════════════════════════════════════════════

_re_patterns: list[re.Pattern] = [
    re.compile(
        r"\b\d{1,5}\s+\w+(?:\s+\w+){0,3}\s+"
        r"(?:street|avenue|road|drive|lane|boulevard|blvd|place|circle|court|"
        r"way|terrace|trace|parkway|highway|route|pike|run|alley|"
        r"square|plaza|row|loop|point|view|crest|springs|ridge)"
        r"(?:\s+\w+){0,3}\s+(?:unit|suite|apt|#)?\s*\d*",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:located|situated|described|commonly\s+known|being\s+known)"
        r"(?:\s+as)?\s+(?:at\s+)?\d{1,5}\s+\w+",
        re.IGNORECASE,
    ),
    re.compile(r"real\s+(?:estate|property)\s+(?:located|situated|described|commonly)", re.IGNORECASE),
    re.compile(r"mortgage(?:-|\s)*(?:lien|note|deed|loan|property|secured|instrument)", re.IGNORECASE),
    re.compile(r"deed\s+of\s+trust", re.IGNORECASE),
    re.compile(r"fee\s+simple", re.IGNORECASE),
    re.compile(r"leasehold\s+(?:estate|interest|tenancy)", re.IGNORECASE),
    re.compile(r"together\s+with\s+(?:all\s+)?improvements", re.IGNORECASE),
    re.compile(r"block\s+\d+.*lot\s+\d+", re.IGNORECASE),
    re.compile(r"lot\s+\d+.*block\s+\d+", re.IGNORECASE),
    re.compile(r"parcel\s+(?:id|number|#|no|identifier)", re.IGNORECASE),
    re.compile(r"assessor.{0,15}(?:parcel|identification|number|id|map|pin|account)", re.IGNORECASE),
    re.compile(r"\bAPN\s*:?\s*\d[\d\-]*", re.IGNORECASE),
    re.compile(r"\bPIN\s*:?\s*\d[\d\-/]*", re.IGNORECASE),
    re.compile(r"legal\s+description", re.IGNORECASE),
    re.compile(r"recorded\s+in\s+(?:book|deed|instrument|liber|page)", re.IGNORECASE),
    re.compile(r"plat\s+(?:book|map|number|no|of)", re.IGNORECASE),
    re.compile(r"folio\s+(?:number|id|#|no|parcel)", re.IGNORECASE),
    re.compile(r"metes\s+and\s+bounds", re.IGNORECASE),
    re.compile(r"subdivision.*plat", re.IGNORECASE),
    re.compile(r"condominium\s+(?:unit|parcel|phase|regime|association)", re.IGNORECASE),
    re.compile(r"(?:township|section|range)\s+\d+.*(?:north|south|east|west)", re.IGNORECASE),
    re.compile(r"residential\s+(?:property|dwelling|home|house|development)", re.IGNORECASE),
    re.compile(r"commercial\s+(?:property|building|space|unit|development)", re.IGNORECASE),
    re.compile(r"multi[-\s]*(?:family|unit|residential|dwelling)", re.IGNORECASE),
    re.compile(r"single[-\s]*family\s+(?:residence|residential|home|dwelling)", re.IGNORECASE),
    re.compile(r"grant\s+(?:deed|bargain|sale)", re.IGNORECASE),
    re.compile(r"quit\s*claim\s*(?:deed)", re.IGNORECASE),
    re.compile(r"general\s+warranty\s+(?:deed)", re.IGNORECASE),
    re.compile(r"title\s+(?:insurance|company|search|report|policy|commitment)", re.IGNORECASE),
    re.compile(r"encroachment|easement|right\s*of\s*way", re.IGNORECASE),
    re.compile(r"\b(?:land\s+)?trust\s+(?:agreement|deed|number)", re.IGNORECASE),
    re.compile(r"\b(?:life\s+)?estate\s+(?:and|or|remainder|for\s+years)", re.IGNORECASE),
    re.compile(r"\b\d{2,4}[-\s]\d{2,4}[-\s]\d{2,4}[-\s]\d{2,4}\b", re.IGNORECASE),
    re.compile(r"\b(building|structure|improvements|dwelling)\s+(?:located|known|situated)", re.IGNORECASE),
]

# ═══════════════════════════════════════════════════════════════════════════
# REAL ESTATE DEBTOR ENTITY PATTERNS
# ═══════════════════════════════════════════════════════════════════════════
# These detect whether the debtor (borrower) name suggests a real-estate
# related entity — property holding companies, development LLCs, etc.
# ═══════════════════════════════════════════════════════════════════════════

RE_DEBTOR_PATTERNS: list[re.Pattern] = [
    # Property / real estate entities
    re.compile(r"(?i)(holdings|properties|realty|real\s+estate|investments?)\s*(?:llc|inc|lp|llp|corp)"),
    re.compile(r"(?i)\d+\s+\w+\s+(?:street|avenue|road|drive|lane|blvd|place|circle)\s"),
    # Multi-family / residential
    re.compile(r"(?i)(?:apartments?|condos?|townhomes?|duplex|triplex|fourplex|multiplex)"),
    # Fix & flip
    re.compile(r"(?i)(?:fix.*flip|flip.*fix|rehab|renovation|remodel).*(?:llc|inc)"),
    # Development / construction
    re.compile(r"(?i)(?:development|developers?)\s*(?:llc|inc|corp)"),
    re.compile(r"(?i)(?:construction|builders?)\s*(?:llc|inc|corp)"),
    # Investment funds
    re.compile(r"(?i)(?:capital\s+(?:partners|fund|group|management|investors?))\s*(?:llc|inc|lp)"),
    re.compile(r"(?i)(?:equity\s+(?:fund|partners|group|investors?))\s*(?:llc|inc|lp)"),
    re.compile(r"(?i)(?:fund\s+\d+|fund\s+(?:i|ii|iii|iv|v|vi|vii))\s*(?:llc|inc|lp)"),
    # Trusts
    re.compile(r"(?i)(?:land\s+trust|trust\s+\d+|trustee|voting\s+trust)"),
    # Property operations
    re.compile(r"(?i)(?:property\s+solutions|property\s+management|property\s+holdings|property\s+group)"),
    re.compile(r"(?i)(?:realty\s+partners|realty\s+group|realty\s+investors?|realty\s+solutions)"),
    # Flippers
    re.compile(r"(?i)(?:home\s+?flip|flipper|house\s+?flip|flip\s+?house)"),
    # Bridge / hard money keywords in name
    re.compile(r"(?i)(?:bridge\s+?capital|bridge\s+?fund|bridge\s+?lending|bridge\s+?group)"),
    re.compile(r"(?i)(?:hard\s+?money|private\s+?money|private\s+?capital)"),
]

# ═══════════════════════════════════════════════════════════════════════════
# US STATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

US_STATES_ABBR: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

# Reverse: state name -> abbreviation (lowercased)
US_STATE_NAMES: dict[str, str] = {v.lower(): k for k, v in US_STATES_ABBR.items()}

# ═══════════════════════════════════════════════════════════════════════════
# CATEGORY DISPLAY LABELS
# ═══════════════════════════════════════════════════════════════════════════

CATEGORY_LABELS: dict[str, str] = {
    "hard_money": "Hard Money",
    "fix_and_flip": "Fix & Flip",
    "construction": "Construction",
    "private_reit": "Private REIT",
    "bridge_lender": "Bridge",
    "traditional_bank": "Traditional Bank",
    "unknown": "Unknown",
}


# ═══════════════════════════════════════════════════════════════════════════
# RealEstateLeadFinder  (main class)
# ═══════════════════════════════════════════════════════════════════════════

class RealEstateLeadFinder:
    """Scans UCC filings for hard money / private RE lending opportunities.

    Complements the MCA pipeline.  After MCA classification + scoring,
    run find_re_leads() on the same filing batch to discover high-value
    real-estate-secured leads.

    Typical pipeline integration::

        from pipeline.re_finder import RealEstateLeadFinder

        re_finder = RealEstateLeadFinder()
        re_leads = re_finder.find_re_leads(accumulated_filings)

        # Each lead dict contains:
        #   {score, tier, business_name, lender_name, lender_category,
        #    location_city, location_state, collateral_excerpt, ...}
    """

    # ── Scoring weights (total = 100) ──────────────────────────────
    W_LENDER_MATCH = 25
    W_COLLATERAL_RE = 20
    W_DEBTOR_RE_ENTITY = 15
    W_FILING_RECENCY = 15
    W_LOAN_SIZE = 15
    W_ENTITY_STRUCTURE = 10

    # Tier thresholds
    TIER_A_MIN = 75   # Hot — call immediately
    TIER_B_MIN = 55   # Warm — queue this week
    TIER_C_MIN = 35   # Cold — nurture
    # Below 35 → D (Archive)

    def __init__(self) -> None:
        """Initialise the finder with the hard money lender database.

        Builds a reverse index (longest names first) so the most specific
        lender match wins over general ones.
        """
        # Index lenders by name length desc for longest-match-first
        self._lender_names_sorted: list[str] = sorted(
            HARD_MONEY_LENDERS.keys(),
            key=lambda n: (-len(n), n),
        )
        self._lenders = HARD_MONEY_LENDERS
        self._re_patterns = _re_patterns
        self._debtor_patterns = RE_DEBTOR_PATTERNS

    # ── Public API ─────────────────────────────────────────────────

    def is_real_estate_filing(self, filing: UCCFiling | dict) -> bool:
        """Quick check: does this look like a real-estate-secured filing?

        Returns True if ANY of these signals are present:
          - Secured party is a known RE lender (tier 1 or 2)
          - Collateral contains RE language
          - Debtor name suggests RE entity

        Designed to be called on every filing in a batch as a fast
        pre-filter before running the full scorer.
        """
        secured_party = self._get_secured_party(filing)
        collateral = self._get_collateral(filing)
        debtor_name = self._get_debtor_name(filing)

        # Signal 1: Known RE lender match (tier 1 or 2)
        matched = self._match_re_lender(secured_party)
        if matched is not None:
            lender_name, lender_info = matched
            if lender_info["tier"] in (1, 2):
                return True

        # Signal 2: RE collateral language
        if collateral:
            text = collateral.lower()
            re_hits = sum(1 for p in self._re_patterns if p.search(text))
            if re_hits >= 2:
                return True

        # Signal 3: Debtor name suggests RE entity
        if debtor_name:
            debtor_hits = sum(1 for p in self._debtor_patterns if p.search(debtor_name))
            if debtor_hits >= 1:
                return True

        # Edge: at least two weak signals
        weak_signals = 0
        if matched is not None:
            weak_signals += 1
        if collateral and any(p.search(collateral.lower()) for p in self._re_patterns[:5]):
            weak_signals += 1
        if debtor_name and any(p.search(debtor_name) for p in self._debtor_patterns[:3]):
            weak_signals += 1

        return weak_signals >= 2

    def classify_lender(self, secured_party_name: str) -> dict[str, Any]:
        """Classify a lender name into category, tier, and confidence.

        Args:
            secured_party_name: The lender name from the UCC filing.

        Returns:
            dict with keys:
              - category (str): hard_money, private_reit, construction,
                bridge_lender, fix_and_flip, traditional_bank, unknown
              - tier (int): 1-4 where 4 = unknown
              - confidence (float): 0.0 - 1.0
              - matched_name (str | None): Name from lender DB
              - display (str): Human-readable category label
        """
        if not secured_party_name:
            return {"category": "unknown", "tier": 4, "confidence": 0.0,
                    "matched_name": None, "display": "Unknown"}

        matched = self._match_re_lender(secured_party_name.strip())
        if matched is not None:
            lender_name, info = matched
            return {
                "category": info["category"],
                "tier": info["tier"],
                "confidence": 0.95 if self._exact_lender_match(secured_party_name.strip(), lender_name) else 0.80,
                "matched_name": lender_name,
                "display": CATEGORY_LABELS.get(info["category"], info["category"]),
            }

        # Check for RE-related keywords in the lender name
        sp_upper = secured_party_name.upper().strip()
        keyword_scores: list[tuple[str, int, float]] = [
            ("MORTGAGE", 2, 0.55),
            ("REAL ESTATE", 2, 0.60),
            ("REALTY", 2, 0.55),
            ("LENDING", 3, 0.40),
            ("PROPERTY", 2, 0.50),
            ("CONSTRUCTION LENDING", 1, 0.60),
            ("HARD MONEY", 1, 0.70),
            ("PRIVATE LENDING", 1, 0.65),
            ("BRIDGE LOAN", 1, 0.60),
            ("CAPITAL PARTNERS", 3, 0.35),
        ]

        best_tier = 4
        best_confidence = 0.0
        for kw, tier, conf in keyword_scores:
            if kw in sp_upper:
                if conf > best_confidence:
                    best_tier = tier
                    best_confidence = conf

        if best_confidence > 0:
            return {
                "category": "unknown",
                "tier": best_tier,
                "confidence": best_confidence,
                "matched_name": None,
                "display": "Unknown",
            }

        return {"category": "unknown", "tier": 4, "confidence": 0.0,
                "matched_name": None, "display": "Unknown"}

    def score_re_lead(self, filing: UCCFiling | dict) -> tuple[int, str, dict]:
        """Score a single filing for real estate lead quality (0-100).

        Args:
            filing: A UCCFiling model instance or equivalent dict.

        Returns:
            Tuple (total_score, tier, breakdown_dict).
            Tier is one of 'A', 'B', 'C', 'D'.
            Breakdown dict has per-component scores.
        """
        breakdown: dict[str, int] = {}

        # Component scores
        lender_score = self._score_lender(filing)
        breakdown["lender"] = lender_score

        collateral_score = self._score_collateral(filing)
        breakdown["collateral"] = collateral_score

        debtor_score = self._score_debtor(filing)
        breakdown["debtor"] = debtor_score

        recency_score = self._score_recency(filing)
        breakdown["recency"] = recency_score

        loan_size_score = self._score_loan_size(filing)
        breakdown["loan_size"] = loan_size_score

        entity_score = self._score_entity(filing)
        breakdown["entity"] = entity_score

        total = (
            lender_score + collateral_score + debtor_score
            + recency_score + loan_size_score + entity_score
        )
        breakdown["total"] = total

        tier = self._tier_from_score(total)

        return total, tier, breakdown

    def find_re_leads(self, filings: list[UCCFiling | dict]) -> list[dict[str, Any]]:
        """Process a batch of filings, return scored real estate leads.

        Each filing is scored and enriched with location, lender category,
        and collateral excerpts.  Results are sorted by score descending.

        Args:
            filings: List of UCCFiling objects or dicts.

        Returns:
            List of dicts, each representing a scored RE lead, sorted
            by score descending (highest first).
        """
        scored_leads: list[dict[str, Any]] = []

        for filing in filings:
            try:
                is_re = self.is_real_estate_filing(filing)
                if not is_re:
                    continue

                score, tier, breakdown = self.score_re_lead(filing)

                # Extract key fields
                business_name = self._get_debtor_name(filing)
                secured_party = self._get_secured_party(filing)
                collateral = self._get_collateral(filing) or ""
                filing_date = self._get_filing_date(filing)
                filing_number = self._get_filing_number(filing)
                filing_state = self._get_state(filing)

                # Lender info
                lender_match = self._match_re_lender(secured_party)
                if lender_match is not None:
                    lender_name_matched, lender_info = lender_match
                    lender_category = lender_info["category"]
                    lender_tier = lender_info["tier"]
                else:
                    lender_name_matched = None
                    lender_category = "unknown"
                    lender_tier = 4

                # Location (best effort)
                location_city, location_state = self._extract_location(filing)

                # Collateral excerpt
                collateral_excerpt = collateral[:200] if collateral else ""

                scored_leads.append({
                    "lead_id": self._make_lead_id(filing),
                    "score": score,
                    "tier": tier,
                    "business_name": business_name,
                    "lender_name": secured_party,
                    "lender_matched": lender_name_matched or secured_party,
                    "lender_category": lender_category,
                    "lender_tier": lender_tier,
                    "lender_display": CATEGORY_LABELS.get(lender_category, lender_category),
                    "location_city": location_city,
                    "location_state": location_state,
                    "filing_date": filing_date.isoformat() if hasattr(filing_date, "isoformat") else str(filing_date),
                    "filing_number": filing_number,
                    "filing_state": filing_state,
                    "collateral_excerpt": collateral_excerpt,
                    "score_breakdown": breakdown,
                })
            except Exception:
                # Skip malformed filings without crashing the batch
                continue

        # Sort by score descending
        scored_leads.sort(key=lambda lead: lead["score"], reverse=True)

        # Assign sequential index after sorting
        for i, lead in enumerate(scored_leads, 1):
            lead["lead_index"] = f"RE-{i}"

        return scored_leads

    # ── Lender Matching ─────────────────────────────────────────────

    def _match_re_lender(self, secured_party: str | None) -> tuple[str, dict] | None:
        """Match a secured party name against the known RE lender database.

        Uses longest-match-first strategy (so 'COREVEST AMERICAN FINANCE'
        wins over 'COREVEST' when both could match).

        Returns (matched_name, lender_info) or None.
        """
        if not secured_party:
            return None

        sp_upper = secured_party.upper().strip()

        # 1) Exact match
        if sp_upper in self._lenders:
            return (sp_upper, self._lenders[sp_upper])

        # 2) Longest-substring match (sorted by name length descending)
        for lender_name in self._lender_names_sorted:
            if lender_name in sp_upper:
                return (lender_name, self._lenders[lender_name])

        # 3) Keyword-based match for RE-related lenders not in DB
        if any(kw in sp_upper for kw in [
            "HARD MONEY", "PRIVATE LENDING", "BRIDGE FUND",
            "REAL ESTATE FUND", "REALTY CAPITAL", "MORTGAGE FUND",
            "CONSTRUCTION FUND", "PROPERTY FUND",
        ]):
            return ("_keyword_match", {"tier": 2, "category": "unknown", "states": ["ALL"]})

        return None

    @staticmethod
    def _exact_lender_match(name: str, lender_name: str) -> bool:
        """Check if a name exactly matches the lender name (case-insensitive)."""
        return name.upper().strip() == lender_name

    # ── Scoring ─────────────────────────────────────────────────────

    def _score_lender(self, filing: UCCFiling | dict) -> int:
        """Score 0-25: known RE lender match quality."""
        secured_party = self._get_secured_party(filing)
        matched = self._match_re_lender(secured_party)

        if matched is None:
            return 0

        _, info = matched
        tier = info["tier"]

        if tier == 1:
            return self.W_LENDER_MATCH  # 25 — Hard money / fix-and-flip / construction
        elif tier == 2:
            return 18  # Private REIT / bridge lender
        elif tier == 3:
            return 10  # Traditional bank
        else:
            return 0

    def _score_collateral(self, filing: UCCFiling | dict) -> int:
        """Score 0-20: real estate collateral language in description."""
        collateral = self._get_collateral(filing)
        if not collateral:
            return 0

        text = collateral.lower()
        matches = sum(1 for p in self._re_patterns if p.search(text))
        return min(self.W_COLLATERAL_RE, matches * 4)

    def _score_debtor(self, filing: UCCFiling | dict) -> int:
        """Score 0-15: debtor name suggests real estate entity."""
        debtor_name = self._get_debtor_name(filing)
        if not debtor_name:
            return 0

        matches = sum(1 for p in self._debtor_patterns if p.search(debtor_name))
        return min(self.W_DEBTOR_RE_ENTITY, matches * 5)

    def _score_recency(self, filing: UCCFiling | dict) -> int:
        """Score 0-15: how recently the filing was made."""
        filing_date = self._get_filing_date(filing)
        if not filing_date:
            return 5  # Unknown — neutral

        if hasattr(filing_date, "date"):
            now = datetime.now()
            # Handle both naive and timezone-aware datetimes
            if filing_date.tzinfo is not None:
                from datetime import timezone
                now = datetime.now(timezone.utc)
            days_ago = (now - filing_date).days
        else:
            # Dict path — string
            return 8

        if days_ago <= 30:
            return 15
        elif days_ago <= 90:
            return 12
        elif days_ago <= 180:
            return 8
        elif days_ago <= 365:
            return 4
        return 1

    def _score_loan_size(self, filing: UCCFiling | dict) -> int:
        """Score 0-15: estimate loan size from entity and collateral cues.

        Without actual loan amounts in most filings, we infer size from
        the richness of the collateral description and entity type.
        """
        collateral = self._get_collateral(filing)
        debtor_name = self._get_debtor_name(filing)

        score = 5  # Base neutral

        # Rich collateral detail suggests larger loan
        if collateral and len(collateral) > 500:
            score += 5
        elif collateral and len(collateral) > 200:
            score += 3

        # Multi-family / commercial / development entities tend to have larger loans
        if debtor_name:
            upper = debtor_name.upper()
            if any(kw in upper for kw in ["APARTMENT", "COMMERCIAL", "MULTI-FAMILY",
                                            "DEVELOPMENT", "CONSTRUCTION"]):
                score += 5
            elif any(kw in upper for kw in ["PROPERTIES", "HOLDINGS", "REALTY",
                                              "INVESTMENT", "CAPITAL"]):
                score += 3

        return min(self.W_LOAN_SIZE, score)

    def _score_entity(self, filing: UCCFiling | dict) -> int:
        """Score 0-10: entity structure signals (LLC/LP = investment vehicle)."""
        debtor_name = self._get_debtor_name(filing)
        if not debtor_name:
            return 5

        upper = debtor_name.upper()
        score = 5  # Base
        if "LLC" in upper:
            score += 3
        if "LP" in upper or "L.P." in upper:
            score += 2
        if "TRUST" in upper:
            score += 3
        if "SERIES" in upper and ("LLC" in upper or "LP" in upper):
            score += 2  # Series LLC/LP = complex entity = investor
        return min(self.W_ENTITY_STRUCTURE, score)

    @staticmethod
    def _tier_from_score(score: int) -> str:
        """Map numerical score to lead tier."""
        if score >= 75:
            return "A"
        elif score >= 55:
            return "B"
        elif score >= 35:
            return "C"
        return "D"

    # ── Location Extraction ─────────────────────────────────────────

    def _extract_location(self, filing: UCCFiling | dict) -> tuple[str | None, str | None]:
        """Best-effort extraction of property location from a filing.

        Tries (in order):
          1. Debtor city/state from the filing
          2. Collateral text address parsing
          3. Filing state as fallback

        Returns (city, state_abbreviation).
        """
        # Attempt 1: Use debtor address fields
        if hasattr(filing, "debtor"):
            d = filing.debtor
            if d.city and d.state:
                return d.city, d.state
        elif isinstance(filing, dict):
            city = filing.get("debtor_city") or filing.get("business_city")
            state = filing.get("debtor_state") or filing.get("business_state") or filing.get("filing_state")
            if city and state:
                return city, state

        # Attempt 2: Parse collateral text for address patterns
        collateral = self._get_collateral(filing)
        if collateral:
            city, state = self._parse_address_from_collateral(collateral)
            if city and state:
                return city, state

        # Attempt 3: Use filing state as a last resort
        state = self._get_state(filing)
        if state:
            return None, state

        return None, None

    @staticmethod
    def _parse_address_from_collateral(text: str) -> tuple[str | None, str | None]:
        """Try to extract a city, state from collateral text.

        Handles patterns like:
          - "Miami, FL" or "Miami, Florida"
          - "located at 123 Main St, Miami, FL 33139"
          - "property in Jacksonville, Florida"
        """
        if not text:
            return None, None

        # Pattern 1: "City, ST" (2-letter state)
        m = re.search(
            r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}),\s*("
            + "|".join(US_STATES_ABBR.keys())
            + r")\b",
            text,
        )
        if m:
            return m.group(1), m.group(2)

        # Pattern 2: "City, StateName"
        state_names = "|".join(
            re.escape(name)
            for name in sorted(US_STATE_NAMES.keys(), key=len, reverse=True)
        )
        m = re.search(
            r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}),?\s+("
            + state_names
            + r")\b",
            text,
            re.IGNORECASE,
        )
        if m:
            abbr = US_STATE_NAMES.get(m.group(2).lower())
            return m.group(1), abbr

        # Pattern 3: "located in City, ST"
        m = re.search(
            r"(?:located|situated|in)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}),\s*("
            + "|".join(US_STATES_ABBR.keys())
            + r")\b",
            text,
        )
        if m:
            return m.group(1), m.group(2)

        return None, None

    # ── ID Generation ───────────────────────────────────────────────

    @staticmethod
    def _make_lead_id(filing: UCCFiling | dict) -> str:
        """Generate a unique lead ID from filing identity fields."""
        if hasattr(filing, "filing_number") and hasattr(filing, "state") and hasattr(filing, "debtor"):
            raw = f"re:{filing.state}:{filing.filing_number}:{filing.debtor.legal_name}"
        else:
            fn = filing.get("filing_number", "")
            st = filing.get("state", "") or filing.get("filing_state", "")
            bn = filing.get("debtor_name", "") or filing.get("business_name", "")
            raw = f"re:{st}:{fn}:{bn}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    # ── Field Accessors (handle both UCCFiling and dict) ────────────

    @staticmethod
    def _get_secured_party(filing: UCCFiling | dict) -> str | None:
        """Get the best secured party name from a filing."""
        if hasattr(filing, "secured_parties"):
            if filing.secured_parties:
                return filing.secured_parties[0].legal_name
            return None
        name = filing.get("funder_name") or filing.get("secured_party_name") or ""
        return name if name else None

    @staticmethod
    def _get_collateral(filing: UCCFiling | dict) -> str | None:
        """Get the collateral description from a filing."""
        if hasattr(filing, "collateral_description"):
            return filing.collateral_description
        return filing.get("collateral_description") or filing.get("collateral_text")

    @staticmethod
    def _get_debtor_name(filing: UCCFiling | dict) -> str | None:
        """Get the debtor/business name from a filing."""
        if hasattr(filing, "debtor"):
            return filing.debtor.dba_name or filing.debtor.legal_name
        return filing.get("debtor_name") or filing.get("business_name")

    @staticmethod
    def _get_filing_date(filing: UCCFiling | dict) -> datetime | str | None:
        """Get the filing date from a filing."""
        if hasattr(filing, "filing_date"):
            return filing.filing_date
        return filing.get("filing_date")

    @staticmethod
    def _get_filing_number(filing: UCCFiling | dict) -> str:
        """Get the filing number."""
        if hasattr(filing, "filing_number"):
            return filing.filing_number
        return filing.get("filing_number", "")

    @staticmethod
    def _get_state(filing: UCCFiling | dict) -> str:
        """Get the state code from a filing."""
        if hasattr(filing, "state"):
            return filing.state
        return filing.get("state", "") or filing.get("filing_state", "")


# ═══════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def find_re_leads(filings: list[UCCFiling | dict]) -> list[dict[str, Any]]:
    """Convenience one-liner: find RE leads from a batch of filings.

    Usage::

        from pipeline.re_finder import find_re_leads

        leads = find_re_leads(my_filings)
        for lead in leads[:5]:
            print(f"{lead['lead_index']} [{lead['score']}] "
                  f"{lead['business_name']} | {lead['lender_name']}")
    """
    finder = RealEstateLeadFinder()
    return finder.find_re_leads(filings)


def format_re_lead_csv_row(lead: dict) -> dict:
    """Flatten an RE lead dict to a CSV-friendly row dict.

    Use this when exporting RE leads to CSV.
    """
    return {
        "lead_index": lead.get("lead_index", ""),
        "lead_id": lead.get("lead_id", ""),
        "score": lead.get("score", 0),
        "tier": lead.get("tier", ""),
        "business_name": lead.get("business_name", ""),
        "lender_name": lead.get("lender_name", ""),
        "lender_matched": lead.get("lender_matched", ""),
        "lender_category": lead.get("lender_category", ""),
        "lender_tier": lead.get("lender_tier", ""),
        "location_city": lead.get("location_city") or "",
        "location_state": lead.get("location_state") or "",
        "filing_date": lead.get("filing_date", ""),
        "filing_number": lead.get("filing_number", ""),
        "filing_state": lead.get("filing_state", ""),
        "collateral_excerpt": lead.get("collateral_excerpt", ""),
    }
