"""Real Estate UCC Lead Scorer — identifies hard-money and private RE lending.

Real estate UCC filings are the highest-ticket leads:
- Hard money lenders: 12-18% interest, 1-5 points origination, $50K-$5M loans
- Bridge lenders: Short-term financing for fix-and-flip or commercial acquisition
- Private REITs / debt funds: Institutional real estate lending
- Construction lenders: Ground-up and renovation financing

These businesses ALWAYS have active capital needs and the loan sizes
are 10-100x larger than MCA advances. A single converted lead can be worth
$50K+ in brokerage fees.

Signals we look for:
  1. Secured party is a known hard money / private RE lender
  2. Collateral description mentions real property / mortgage
  3. Debtor is a real estate entity (LLC with property-address name, etc.)
  4. Filing amount is large
"""

import re
from datetime import datetime, timedelta
from typing import Optional

from models.lead import LeadScore, LeadTier


# ── Known hard money & private real estate lenders ─────────────────────
# These are lenders that specialize in real-estate-secured lending.
# Many file UCC-1s as a backup to their mortgage/deed of trust.

HARD_MONEY_LENDERS = {
    # National hard money / bridge lenders
    "LENDINGHOME": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "KIAVI": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "LIMA ONE": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "RCN CAPITAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "ANCHOR LOANS": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "CIVIC FINANCIAL": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "GOLDMAN SACHS BANK USA": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "BLACKSTONE": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "STARWOOD": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "ARBOR REALTY": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "WALKER & DUNLOP": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "BERKELEY POINT": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "GREYSTONE": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "NEWREZ": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    # Construction lenders (file UCCs on materials + equipment)
    "BUILDERS CAPITAL": {"tier": 1, "category": "construction", "states": ["ALL"]},
    "CONSTRUCTION FINANCIAL": {"tier": 1, "category": "construction", "states": ["ALL"]},
    "COREVEST": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "FUNDRISE": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "YIELDSTREET": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "GROUNDFLOOR": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "PATCH OF LAND": {"tier": 1, "category": "hard_money", "states": ["ALL"]},
    "PEACHTREE": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "MAIN STREET RENEWAL": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "TRICON": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "PROGRESS RESIDENTIAL": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "AMHERST": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
    "INVITATION HOMES": {"tier": 2, "category": "private_reit", "states": ["ALL"]},
}

# Collateral patterns indicating real estate
RE_COLLATERAL_PATTERNS = [
    r"real\s+(estate|property)\s+(located|situated|described|commonly)",
    r"mortgage.*property",
    r"deed\s+of\s+trust",
    r"fee\s+simple",
    r"leasehold\s+(estate|interest)",
    r"together\s+with\s+(all\s+)?improvements",
    r"block\s+\d+.*lot\s+\d+",
    r"parcel\s+(id|number|#)",
    r"assessor.{0,10}parcel",
    r"legal\s+description",
    r"recorded\s+in\s+(book|deed|instrument)",
    r"subdivision.*plat",
    r"condominium.*unit",
    r"township.*range.*section",
    r"metes\s+and\s+bounds",
]

# Real estate entity patterns in debtor names
RE_DEBTOR_PATTERNS = [
    r"(?i)(holdings|properties|realty|real\s+estate|investments?)\s*(llc|inc|lp|llp|corp)",
    r"(?i)\d+\s+\w+\s+(street|avenue|road|drive|lane|blvd|place|circle)",
    r"(?i)(apartments?|condos?|townhomes?|duplex|triplex)",
    r"(?i)(fix.*flip|flip.*fix|rehab|renovation).*(llc|inc)",
    r"(?i)(development|developers?).*(llc|inc|corp)",
    r"(?i)(construction|builders?).*(llc|inc|corp)",
]


class RealEstateLeadScorer:
    """Scores UCC filings for real estate lending lead quality (0-100)."""

    # ── Scoring weights ──────────────────────────────────────────────
    LENDER_MATCH_WEIGHT = 25
    COLLATERAL_RE_WEIGHT = 20
    DEBTOR_RE_ENTITY_WEIGHT = 15
    FILING_RECENCY_WEIGHT = 15
    LOAN_SIZE_WEIGHT = 15
    ENTITY_STRUCTURE_WEIGHT = 10

    def score(self, secured_party_name: str, collateral: str,
              debtor_name: str, filing_date: Optional[datetime] = None,
              loan_amount: Optional[float] = None) -> tuple[int, str]:
        """Score a UCC filing for real estate lead quality.

        Returns (score_0_to_100, category).
        """
        lender_score = self._score_lender(secured_party_name)
        collateral_score = self._score_collateral(collateral)
        debtor_score = self._score_debtor(debtor_name)
        recency_score = self._score_recency(filing_date)
        size_score = self._score_loan_size(loan_amount)
        entity_score = self._score_entity(debtor_name)

        total = (lender_score + collateral_score + debtor_score +
                 recency_score + size_score + entity_score)

        if total >= 75:
            category = "A-Hot-RE"
        elif total >= 55:
            category = "B-Warm-RE"
        elif total >= 35:
            category = "C-Cold-RE"
        else:
            category = "D-Archive-RE"

        return total, category

    def _score_lender(self, sp_name: str) -> int:
        """Score based on whether the secured party is a known RE lender."""
        if not sp_name:
            return 0

        sp_upper = sp_name.upper()

        for lender_name, info in HARD_MONEY_LENDERS.items():
            if lender_name in sp_upper:
                if info["tier"] == 1:
                    return 25  # Known hard money lender
                elif info["tier"] == 2:
                    return 18  # Private REIT / institutional

        # Pattern-based detection
        if any(kw in sp_upper for kw in ["MORTGAGE", "LENDING", "CAPITAL PARTNERS",
                                            "REAL ESTATE", "REALTY", "PROPERTY",
                                            "CONSTRUCTION LENDING"]):
            return 10

        return 0

    def _score_collateral(self, collateral: str) -> int:
        """Score based on real estate collateral language."""
        if not collateral:
            return 0

        text = collateral.lower()
        matches = sum(1 for p in RE_COLLATERAL_PATTERNS if re.search(p, text))
        return min(20, matches * 5)

    def _score_debtor(self, debtor_name: str) -> int:
        """Score based on whether debtor looks like a real estate entity."""
        if not debtor_name:
            return 0

        matches = sum(1 for p in RE_DEBTOR_PATTERNS if re.search(p, debtor_name))
        return min(15, matches * 5)

    def _score_recency(self, filing_date: Optional[datetime]) -> int:
        """Recent filings score higher."""
        if not filing_date:
            return 5

        days_ago = (datetime.now() - filing_date).days
        if days_ago <= 30:
            return 15
        elif days_ago <= 90:
            return 12
        elif days_ago <= 180:
            return 8
        elif days_ago <= 365:
            return 4
        return 1

    def _score_loan_size(self, amount: Optional[float]) -> int:
        """Estimate based on entity and collateral clues if no amount given."""
        if amount:
            if amount >= 5_000_000:
                return 15
            elif amount >= 1_000_000:
                return 12
            elif amount >= 500_000:
                return 8
            elif amount >= 100_000:
                return 4
        return 5  # Unknown — neutral

    def _score_entity(self, debtor_name: str) -> int:
        """LLCs and LPs are more likely to be RE investment vehicles."""
        if not debtor_name:
            return 5

        upper = debtor_name.upper()
        score = 5
        if "LLC" in upper:
            score += 3
        if "LP" in upper or "L.P." in upper:
            score += 2
        return min(10, score)


# ── Quick classification helper ──────────────────────────────────────

def is_real_estate_filing(secured_party: str, collateral: str,
                          debtor_name: str) -> bool:
    """Quick check: is this UCC filing real-estate-related?"""
    scorer = RealEstateLeadScorer()
    score, _ = scorer.score(secured_party, collateral, debtor_name)
    return score >= 20
