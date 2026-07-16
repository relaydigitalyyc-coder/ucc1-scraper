#!/usr/bin/env bash
# UCC-1 Scraper Installation Script
# Installs Python dependencies and Playwright browsers.

set -euo pipefail

echo "=== UCC-1 Scraper Installer ==="
echo ""

# Check Python version
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ required but not found."
    exit 1
fi

PY_VERSION=$($PYTHON --version 2>&1 | grep -oP '\d+\.\d+')
echo "Python: $PY_VERSION"

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv .venv
fi

source .venv/bin/activate

# Install dependencies
echo "Installing Python dependencies..."
pip install --upgrade pip
pip install -e ".[dev]"

# Install Playwright browsers
echo "Installing Playwright Chromium..."
playwright install chromium

# Verify installation
echo ""
echo "Verifying installation..."
python -c "
from src.scrapers.registry import list_available_states
states = list_available_states()
print(f'Available state scrapers ({len(states)}): {', '.join(states)}')

from src.pipeline.classifier import MCAClassifier
c = MCAClassifier()
print(f'MCA funders loaded: {c.funder_count}')
"

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Quick start:"
echo "  source .venv/bin/activate"
echo "  ucc-scrape scrape --states NY,FL --days 30"
echo "  ucc-scrape leads --tier A"
echo "  ucc-scrape export --tier all -o leads.csv"
echo ""
echo "Daily cron for automated scraping:"
echo "  0 6 * * * cd '$(pwd)' && $(pwd)/.venv/bin/ucc-scrape scrape --states all --days 1"
