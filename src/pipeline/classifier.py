"""MCA Classification — identifies which UCC filings are from MCA funders.

Uses two strategies:
1. Known Funder DB match — exact/fuzzy match against our MCA funder database
2. Collateral Text Analysis — NLP patterns that indicate MCA vs traditional lending
"""

import json
import re
from pathlib import Path
from typing import Optional

from thefuzz import fuzz  # Fuzzy string matching

from models.filing import UCCFiling, FilingStatus


def _keyword_collateral_type(text_lower: str) -> str:
    """Classify collateral by keyword analysis (fallback when regex doesn't match)."""
    if not text_lower:
        return "unknown"

    # Equipment indicators
    if re.search(r"(excavator|bulldozer|crane|forklift|tractor|trailer|"
                 r"serial\s*(no|number|\#|num)|VIN|vehicle\s+identification)",
                 text_lower):
        if "real" not in text_lower or "estate" not in text_lower:
            return "equipment"

    # Vehicle indicators
    if re.search(r"(motor\s+vehicle|automobile|truck\s+(tractor|trailer)|"
                 r"vin\s*[:#]?\s*[a-z0-9]{11,})", text_lower):
        return "vehicle"

    # Inventory
    if re.search(r"(inventory|stock\s+in\s+trade|finished\s+goods|"
                 r"raw\s+materials|work\s+in\s+progress)", text_lower):
        return "inventory"

    # Real estate
    if re.search(r"(real\s+(estate|property)|mortgage|deed\s+of\s+trust|"
                 r"located\s+at\s+\d+|block\s+\d+|lot\s+\d+)", text_lower):
        return "real_estate"

    return "traditional"


class MCAClassifier:
    """Classifies UCC filings as MCA-related or not."""

    # Distinctive MCA collateral language patterns
    MCA_COLLATERAL_PATTERNS = [
        r"future\s+(accounts|receivables|revenue)",
        r"purchase\s+of\s+future\s+(receivables|accounts|revenue)",
        r"future\s+credit\s+card\s+(receivables|receipts|sales)",
        r"confession\s+of\s+judgment",
        r"COJ",
        r"all\s+assets\s+now\s+owned\s+or\s+hereafter\s+acquired",
        r"all\s+(present|current)\s+and\s+future\s+(accounts|assets|receivables)",
        r"merchant\s+cash\s+advance",
        r"MCA\s+(agreement|contract|financing)",
        r"revenue\s+based\s+(financing|advance|loan)",
        r"ACH\s+(authorization|agreement).*daily",
        r"daily\s+(ACH|debit|remittance|payment)",
        r"ACH\s+(authorization|agreement)",
        r"(Daily|daily)\s+ACH",
        r"ACH.*(repayment|daily|debit)",
        r"lock\s*box.*receivables",
        r"split\s*funding.*receivables",
    ]

    # Patterns that indicate NOT MCA (traditional lending)
    NON_MCA_PATTERNS = [
        r"real\s+(estate|property)\s+(located|situated|described)",
        r"mortgage.*property",
        r"motor\s+vehicle.*VIN",
        r"vehicle\s+identification\s+number",
        r"manufactured\s+home",
        r"fixture\s+filing",
        r"timber.*(land|property)",
        r"farm\s+(products|equipment|land)",
        r"oil.*gas.*mineral",
        r"specific\s+equipment.*serial\s+number",
    ]

    def __init__(self, funder_db_path: Optional[Path] = None):
        self.funder_db_path = funder_db_path or Path(__file__).parent.parent / "funders" / "funder_db.json"
        self._funders: list[dict] = []
        self._funder_names: set[str] = set()
        self._load_funders()

    def _load_funders(self):
        """Load the MCA funder database."""
        if self.funder_db_path.exists():
            with open(self.funder_db_path) as f:
                data = json.load(f)
            self._funders = data.get("funders", [])
            # Index all known funder names (legal names + DBAs) for fast lookup
            for funder in self._funders:
                self._funder_names.add(funder["legal_name"].upper().strip())
                for dba in funder.get("dbas", []):
                    self._funder_names.add(dba.upper().strip())

    def classify(self, filing: UCCFiling) -> UCCFiling:
        """Classify a filing — mutates the filing in place by setting secured party MCA flags."""
        for sp in filing.secured_parties:
            match = self._match_funder(sp.legal_name)
            if match:
                sp.is_mca_funder = True
                sp.funder_db_id = match.get("id")
                sp.funder_tier = match.get("tier", 3)

        # If no funder matched by name, try collateral text analysis
        if filing.collateral_description and not any(sp.is_mca_funder for sp in filing.secured_parties):
            filing.collateral_type = self._classify_collateral(filing.collateral_description)

        return filing

    def _match_funder(self, name: str) -> Optional[dict]:
        """Try to match a secured party name against the MCA funder DB."""
        if not name:
            return None

        name_upper = name.upper().strip()

        # 1. Exact match
        if name_upper in self._funder_names:
            return self._find_funder_by_name(name_upper)

        # 2. Fuzzy match against all known funder names
        best_score = 0
        best_funder = None
        for funder_name in self._funder_names:
            score = fuzz.token_sort_ratio(name_upper, funder_name)
            if score > best_score and score >= 85:  # 85% threshold
                best_score = score
                best_funder = funder_name

        if best_funder:
            return self._find_funder_by_name(best_funder)

        # 3. Substring match — the funder name appears inside the secured party name
        for funder_name in self._funder_names:
            if len(funder_name) > 5 and funder_name in name_upper:
                return self._find_funder_by_name(funder_name)

        return None

    def _find_funder_by_name(self, name_upper: str) -> Optional[dict]:
        """Find a funder in the DB by its legal name."""
        for funder in self._funders:
            if funder["legal_name"].upper().strip() == name_upper:
                return funder
            for dba in funder.get("dbas", []):
                if dba.upper().strip() == name_upper:
                    return funder
        return None

    def _classify_collateral(self, text: str) -> str:
        """Classify collateral description as MCA or other type."""
        if not text:
            return "unknown"

        text_lower = text.lower()

        # Check for MCA indicators
        mca_score = sum(1 for p in self.MCA_COLLATERAL_PATTERNS if re.search(p, text_lower, re.IGNORECASE))
        non_mca_score = sum(1 for p in self.NON_MCA_PATTERNS if re.search(p, text_lower, re.IGNORECASE))

        if mca_score > 0 and non_mca_score == 0:
            return "mca_receivables"
        elif non_mca_score > 0 and mca_score == 0:
            return _keyword_collateral_type(text_lower)
        elif mca_score > 0 and non_mca_score > 0:
            # Mixed — lean toward MCA if patterns are stronger
            return "mca_receivables" if mca_score >= non_mca_score else "traditional"

        # No regex match — fall through to keyword classification
        kw_result = _keyword_collateral_type(text_lower)
        if kw_result != "traditional":
            return kw_result

        # Check for general blanket lien language (common in MCA but not unique)
        if re.search(r"all\s+(assets|personal\s+property)", text_lower):
            return "general_business_assets"

        return "unknown"

    def is_mca_filing(self, filing: UCCFiling) -> bool:
        """Quick check: is this filing MCA-related?"""
        # Check funder match
        if any(sp.is_mca_funder for sp in filing.secured_parties):
            return True
        # Check collateral type
        if filing.collateral_type in ("mca_receivables",):
            return True
        return False

    @property
    def funder_count(self) -> int:
        return len(self._funders)
