"""Deduplication — merges duplicate business records across states and filings."""

from collections import defaultdict

from thefuzz import fuzz

from models.filing import UCCFiling


class Deduplicator:
    """Detects and merges duplicate business records.

    The same business may have UCC filings in multiple states, or
    multiple filings in the same state. We need to group them.
    """

    # Threshold for fuzzy name matching
    NAME_SIMILARITY_THRESHOLD = 85

    def __init__(self):
        self._business_index: dict[str, list[UCCFiling]] = defaultdict(list)

    def add_filing(self, filing: UCCFiling):
        """Add a filing to the index, grouping by business."""
        key = self._make_key(filing)
        self._business_index[key].append(filing)

    def add_filings(self, filings: list[UCCFiling]):
        """Add multiple filings."""
        for f in filings:
            self.add_filing(f)

    def get_related(self, filing: UCCFiling) -> list[UCCFiling]:
        """Get all other filings for the same business."""
        key = self._make_key(filing)
        all_filings = self._business_index.get(key, [])
        return [f for f in all_filings if f.filing_number != filing.filing_number]

    def get_all_businesses(self) -> dict[str, list[UCCFiling]]:
        """Get all businesses grouped by key."""
        return dict(self._business_index)

    def find_duplicates(self, filing: UCCFiling) -> list[UCCFiling]:
        """Find potential duplicate filings (same business, possibly same MCA round)."""
        related = self.get_related(filing)

        duplicates = []
        for rel in related:
            # Same debtor name similarity
            name_sim = fuzz.token_sort_ratio(
                filing.debtor.legal_name.upper(),
                rel.debtor.legal_name.upper(),
            )

            # Same city/state is strong signal
            same_location = (
                filing.debtor.city == rel.debtor.city
                and filing.debtor.state == rel.debtor.state
            )

            # Same secured party is a strong signal
            same_lender = any(
                sp1.legal_name.upper() == sp2.legal_name.upper()
                for sp1 in filing.secured_parties
                for sp2 in rel.secured_parties
            )

            # Scoring for duplicate detection
            score = 0
            if name_sim >= 90:
                score += 3
            elif name_sim >= 80:
                score += 1

            if same_location:
                score += 2

            if same_lender:
                score += 2

            if score >= 4:  # High confidence duplicate
                duplicates.append(rel)

        return duplicates

    @staticmethod
    def _make_key(filing: UCCFiling) -> str:
        """Create an index key for a business.

        Uses normalized name + city + state for uniqueness.
        """
        name = filing.debtor.legal_name.upper().strip()
        # Remove common suffixes to improve matching
        for suffix in [", LLC", " LLC", ", L.L.C.", " L.L.C.", ", INC", " INC",
                       ", INC.", " INC.", ", CORP", " CORP", ", CORPORATION",
                       " CORPORATION", ", LP", " LP", ", L.P.", " L.P.",
                       ", THE", " THE "]:
            name = name.replace(suffix, "")

        # Remove punctuation and extra whitespace
        import re
        name = re.sub(r"[^\w\s]", "", name)
        name = " ".join(name.split())

        city = (filing.debtor.city or "").upper().strip()
        state = (filing.debtor.state or filing.state or "").upper().strip()

        return f"{name}|{city}|{state}"

    def get_stacker_count(self, filing: UCCFiling) -> int:
        """Count how many active MCA positions a business has."""
        related = self.get_related(filing)
        active_mca = [f for f in related if f.status.value in ("active", "unknown")]

        # Count unique MCA funders
        mca_funders = set()
        for f in active_mca + [filing]:
            for sp in f.secured_parties:
                if sp.is_mca_funder or sp.funder_tier:
                    mca_funders.add(sp.legal_name.upper())

        return len(mca_funders)
