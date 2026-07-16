"""Tests for MCA Classifier — funder matching and collateral text analysis."""

import pytest


class TestFunderMatching:
    def test_exact_match_tier_1(self, classifier, sample_filing):
        classifier.classify(sample_filing)
        assert sample_filing.secured_parties[0].is_mca_funder is True
        assert sample_filing.secured_parties[0].funder_tier == 1
        assert sample_filing.secured_parties[0].funder_db_id == "mca-001"

    def test_fuzzy_match_close_name(self, classifier):
        """Fuzzy match should catch minor variations."""
        result = classifier._match_funder("YELLOWSTONE CAPITAL LLC.")
        assert result is not None
        assert result["tier"] == 1

    def test_fuzzy_match_different_case(self, classifier):
        result = classifier._match_funder("yellowstone capital llc")
        assert result is not None
        assert result["tier"] == 1

    def test_substring_match(self, classifier):
        """Fund name appears inside a longer secured party name."""
        result = classifier._match_funder("YELLOWSTONE CAPITAL LLC AS AGENT")
        assert result is not None
        assert result["tier"] == 1

    def test_match_dba_name(self, funder_db_tmp):
        """Should match against known DBA/alias."""
        from pipeline.classifier import MCAClassifier
        c = MCAClassifier(funder_db_path=funder_db_tmp)
        result = c._match_funder("YSC")
        assert result is not None
        assert result["tier"] == 1

    def test_no_match_unknown_funder(self, classifier):
        result = classifier._match_funder("COMPLETELY UNKNOWN LENDER XYZ")
        assert result is None

    def test_no_match_empty_name(self, classifier):
        assert classifier._match_funder("") is None
        assert classifier._match_funder(None) is None

    def test_match_tier_2_bank(self, classifier):
        result = classifier._match_funder("Celtic Bank Corporation")
        assert result is not None
        assert result["tier"] == 2

    def test_match_tier_3_adjacent(self, classifier):
        result = classifier._match_funder("BALBOA CAPITAL CORPORATION")
        assert result is not None
        assert result["tier"] == 3


class TestCollateralClassification:
    def test_classifies_mca_receivables(self, classifier):
        text = "All present and future accounts and receivables including future credit card receivables"
        result = classifier._classify_collateral(text)
        assert result == "mca_receivables"

    def test_classifies_confession_of_judgment(self, classifier):
        text = "Confession of Judgment entered in Supreme Court, New York County. All business assets."
        result = classifier._classify_collateral(text)
        assert result == "mca_receivables"

    def test_classifies_coj_abbreviation(self, classifier):
        text = "COJ filed. All assets of debtor including future receivables."
        result = classifier._classify_collateral(text)
        assert result == "mca_receivables"

    def test_classifies_daily_ach_language(self, classifier):
        text = "Daily ACH authorization for repayment. All business assets."
        result = classifier._classify_collateral(text)
        assert result == "mca_receivables"

    def test_classifies_equipment(self, classifier):
        text = "One (1) 2024 Caterpillar 320 Excavator S/N CAT00320ABC. All attachments and accessories."
        result = classifier._classify_collateral(text)
        assert result == "equipment"

    def test_classifies_real_estate(self, classifier):
        text = "Real property located at 123 Main Street, Block 456, Lot 789, together with all improvements."
        result = classifier._classify_collateral(text)
        assert result == "real_estate"

    def test_classifies_inventory(self, classifier):
        text = "All inventory including finished goods, work in progress, and raw materials."
        result = classifier._classify_collateral(text)
        assert result == "inventory"

    def test_classifies_vehicle(self, classifier):
        text = "2023 Freightliner Cascadia VIN 1FUJGLDR5DSBS1234 including all attached equipment."
        result = classifier._classify_collateral(text)
        assert result == "vehicle"

    def test_classifies_general_business_assets(self, classifier):
        text = "All assets of the debtor, all personal property, all equipment and fixtures."
        result = classifier._classify_collateral(text)
        assert result == "general_business_assets"

    def test_classifies_unknown_when_empty(self, classifier):
        assert classifier._classify_collateral("") == "unknown"
        assert classifier._classify_collateral(None) == "unknown"


class TestMCAClassificationFlow:
    def test_full_classification_mca_filing(self, classifier, sample_filing):
        result = classifier.classify(sample_filing)
        assert result.secured_parties[0].is_mca_funder is True
        assert classifier.is_mca_filing(result) is True

    def test_full_classification_non_mca_filing(self, classifier, sample_filing_non_mca):
        result = classifier.classify(sample_filing_non_mca)
        assert result.secured_parties[0].is_mca_funder is False
        assert result.collateral_type == "equipment"
        assert classifier.is_mca_filing(result) is False

    def test_collateral_only_classification(self, classifier):
        """Even without funder name match, MCA collateral language should flag it."""
        from models.filing import UCCFiling, FilingStatus, DebtorInfo, SecuredPartyInfo
        from datetime import datetime

        filing = UCCFiling(
            filing_number="999",
            state="NY",
            filing_date=datetime.now(),
            status=FilingStatus.ACTIVE,
            debtor=DebtorInfo(legal_name="TEST CORP"),
            secured_parties=[SecuredPartyInfo(legal_name="UNKNOWN CAPITAL LLC")],
            collateral_description="Purchase of future receivables. Lock box arrangement for daily ACH.",
        )
        result = classifier.classify(filing)
        assert result.collateral_type == "mca_receivables"
        assert classifier.is_mca_filing(result) is True

    def test_funder_count_property(self, classifier):
        assert classifier.funder_count > 0
        assert isinstance(classifier.funder_count, int)
