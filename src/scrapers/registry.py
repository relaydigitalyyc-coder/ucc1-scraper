"""Scraper registry — maps state codes to scraper classes."""

from typing import Optional

from scrapers.base import BaseStateScraper

# Registry of available state scrapers
_SCRAPER_REGISTRY: dict[str, type[BaseStateScraper]] = {}


def register_scraper(state_code: str):
    """Decorator to register a state scraper class."""

    def wrapper(cls: type[BaseStateScraper]):
        _SCRAPER_REGISTRY[state_code.upper()] = cls
        return cls

    return wrapper


def get_scraper(state_code: str, headless: bool = True, proxy: Optional[str] = None) -> Optional[BaseStateScraper]:
    """Get a scraper instance for a given state code."""
    cls = _SCRAPER_REGISTRY.get(state_code.upper())
    if cls is None:
        return None
    return cls(headless=headless, proxy=proxy)


def list_available_states() -> list[str]:
    """List all state codes that have scrapers implemented."""
    return sorted(_SCRAPER_REGISTRY.keys())


# ── Import scrapers to trigger registration ────────────────────────
# New state scrapers are discovered when their module is imported here.

from .florida import FloridaScraper  # noqa: F401
from .new_jersey import NewJerseyScraper  # noqa: F401
from .georgia import GeorgiaScraper  # noqa: F401
from .illinois import IllinoisScraper  # noqa: F401
from .new_york import NewYorkScraper  # noqa: F401
from .california import CaliforniaScraper  # noqa: F401
from .texas import TexasScraper  # noqa: F401
from .colorado import ColoradoScraper  # noqa: F401
from .delaware import DelawareScraper  # noqa: F401
from .maryland import MarylandScraper  # noqa: F401
from .oregon import OregonScraper  # noqa: F401
