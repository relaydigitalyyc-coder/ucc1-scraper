"""Normalize raw state-specific filing dicts into standard UCCFiling objects."""

import re
from datetime import datetime
from typing import Optional

from models.filing import (
    UCCFiling,
    FilingStatus,
    DebtorInfo,
    SecuredPartyInfo,
)


class FilingNormalizer:
    """Takes a raw filing dict from any state scraper and produces a clean UCCFiling."""

    # Known date formats used by state portals
    DATE_FORMATS = [
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%Y-%m-%d",
        "%m/%d/%y",
        "%d-%b-%Y",
        "%B %d, %Y",
        "%d %B %Y",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
    ]

    @classmethod
    def normalize(cls, raw: dict) -> UCCFiling:
        """Convert a raw filing dict to a UCCFiling object."""
        filing_date = cls._parse_date(raw.get("filing_date", ""))
        status = cls._parse_status(raw.get("status", "unknown"))
        debtor = cls._parse_debtor(raw)
        secured_parties = cls._parse_secured_parties(raw)

        # Try to get collateral description from detail if available
        collateral = raw.get("collateral_description") or raw.get("collateral")

        return UCCFiling(
            filing_number=str(raw.get("filing_number", "")),
            state=str(raw.get("state", "")).upper(),
            source_url=raw.get("detail_url") or raw.get("source_url"),
            filing_date=filing_date,
            lapse_date=cls._parse_date(raw.get("lapse_date", "")),
            termination_date=cls._parse_date(raw.get("termination_date", "")),
            status=status,
            debtor=debtor,
            secured_parties=secured_parties,
            collateral_description=collateral,
            original_filing_number=raw.get("original_filing_number"),
            raw_json=raw,
        )

    @classmethod
    def _parse_date(cls, value: str) -> Optional[datetime]:
        """Try to parse a date string using known formats."""
        if not value or not str(value).strip():
            return datetime.now()

        value = str(value).strip()

        for fmt in cls.DATE_FORMATS:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue

        # Try to extract date with regex
        match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", value)
        if match:
            m, d, y = match.groups()
            y = int(y)
            if y < 100:
                y += 2000 if y < 50 else 1900
            try:
                return datetime(y, int(m), int(d))
            except ValueError:
                pass

        return datetime.now()

    @classmethod
    def _parse_status(cls, value: str) -> FilingStatus:
        """Parse filing status from text."""
        v = str(value).lower().strip()

        if "terminat" in v:
            return FilingStatus.TERMINATED
        elif "lapsed" in v or "expir" in v:
            return FilingStatus.LAPSED
        elif "amend" in v or "continu" in v:
            return FilingStatus.AMENDED
        elif "continu" in v:
            return FilingStatus.CONTINUED
        elif "active" in v or "filed" in v or "current" in v:
            return FilingStatus.ACTIVE

        return FilingStatus.UNKNOWN

    @classmethod
    def _parse_debtor(cls, raw: dict) -> DebtorInfo:
        """Parse debtor information from raw dict."""
        name = (
            raw.get("debtor_name")
            or raw.get("debtorName")
            or raw.get("debtor")
            or raw.get("organization_name")
            or ""
        )

        dba = raw.get("dba_name") or raw.get("dba") or raw.get("trade_name")

        entity_type = None
        name_upper = name.upper()
        for suffix in ["LLC", "L.L.C.", "INC", "INC.", "CORP", "CORPORATION", "LP", "L.P.", "LLP"]:
            if suffix in name_upper:
                entity_type = suffix.strip(".")
                break

        return DebtorInfo(
            legal_name=name.strip(),
            dba_name=dba.strip() if dba else None,
            address_line1=raw.get("debtor_address") or raw.get("address"),
            city=raw.get("debtor_city") or raw.get("city"),
            state=raw.get("debtor_state") or raw.get("state"),
            zip_code=raw.get("debtor_zip") or raw.get("zip"),
            entity_type=entity_type,
            raw_text=raw.get("debtor_raw") or raw.get("raw_cells", {}).get("debtor"),
        )

    @classmethod
    def _parse_secured_parties(cls, raw: dict) -> list[SecuredPartyInfo]:
        """Parse secured party info — may be a single party or list."""
        sp_name = (
            raw.get("secured_party_name")
            or raw.get("securedPartyName")
            or raw.get("secured_party")
            or raw.get("lender_name")
            or ""
        )

        if not sp_name:
            # Check for multiple secured parties
            sp_list = raw.get("secured_parties", [])
            if isinstance(sp_list, list) and sp_list:
                parties = []
                for sp in sp_list:
                    if isinstance(sp, dict):
                        parties.append(SecuredPartyInfo(
                            legal_name=sp.get("name", ""),
                            address_line1=sp.get("address"),
                            city=sp.get("city"),
                            state=sp.get("state"),
                            raw_text=str(sp),
                        ))
                    elif isinstance(sp, str):
                        parties.append(SecuredPartyInfo(legal_name=sp))
                return parties

            return []

        return [
            SecuredPartyInfo(
                legal_name=sp_name.strip(),
                address_line1=raw.get("secured_party_address") or raw.get("lender_address"),
                city=raw.get("secured_party_city"),
                state=raw.get("secured_party_state"),
                raw_text=raw.get("secured_party_raw"),
            )
        ]
