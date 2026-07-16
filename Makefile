.PHONY: install scrape leads export stats funders clean test

# ── Installation ──────────────────────────────────────────────────

install:
	chmod +x install.sh
	./install.sh

install-dev:
	pip install -e ".[dev]"
	playwright install chromium

# ── Scraping ──────────────────────────────────────────────────────

# Scrape last 7 days from all available states
scrape:
	ucc-scrape scrape --states all --days 7

# Scrape a specific state
scrape-ny:
	ucc-scrape scrape --states NY --days 30

scrape-fl:
	ucc-scrape scrape --states FL --days 30

scrape-nj:
	ucc-scrape scrape --states NJ --days 30

scrape-ca:
	ucc-scrape scrape --states CA --days 30

# Scrape top 5 MCA states
scrape-top5:
	ucc-scrape scrape --states NY,CA,FL,TX,IL --days 7

# ── Leads ─────────────────────────────────────────────────────────

leads-hot:
	ucc-scrape leads --tier A --limit 50

leads-warm:
	ucc-scrape leads --tier B --limit 50

leads-all:
	ucc-scrape leads --tier all --limit 100

# ── Export ────────────────────────────────────────────────────────

export:
	ucc-scrape export --tier all -o leads_$(shell date +%Y%m%d).csv

export-hot:
	ucc-scrape export --tier A -o hot_leads_$(shell date +%Y%m%d).csv

export-ca:
	ucc-scrape export --tier all --state CA -o ca_leads_$(shell date +%Y%m%d).csv

export-ny:
	ucc-scrape export --tier all --state NY -o ny_leads_$(shell date +%Y%m%d).csv

# ── Stats ─────────────────────────────────────────────────────────

stats:
	ucc-scrape stats

# ── Dry Run (health check) ────────────────────────────────────────

dry-run:
	ucc-scrape dry-run --states all

dry-run-ny:
	ucc-scrape dry-run --states NY

# ── Funders ───────────────────────────────────────────────────────

funders:
	ucc-scrape funders list

funders-mca:
	ucc-scrape funders list --tier 1

# ── Daily Pipeline (one command) ──────────────────────────────────

daily:
	ucc-scrape scrape --states NY,FL,NJ,CA,TX,IL,GA --days 1
	ucc-scrape export --tier all -o data/exports/leads_$(shell date +%Y%m%d).csv
	ucc-scrape export --tier A -o data/exports/hot_leads_$(shell date +%Y%m%d).csv
	ucc-scrape stats

# ── Maintenance ───────────────────────────────────────────────────

clean:
	rm -rf data/ucc_scraper.db
	rm -rf data/exports/*
	@echo "Database cleared."

test:
	pytest tests/ -v

lint:
	ruff check src/
	mypy src/ --ignore-missing-imports
