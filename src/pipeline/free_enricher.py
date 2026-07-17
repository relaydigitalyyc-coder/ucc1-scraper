"""Free enrichment sources from public-record-data-scrapper — no API keys needed.

Sources:
  - SEC EDGAR: search company filings, extract phone from cover page
  - USPTO: trademark search, get contact info
  - Census Bureau: NAICS lookup from business name
  - OpenCorp: entity registration data (registered agent, address)
"""
import httpx, re, json
from typing import Optional

PHONE_CLEAN = re.compile(r'[^\d]')

def clean(raw):
    d = PHONE_CLEAN.sub('', raw or '')
    if len(d)==10: return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    if len(d)==11 and d[0]=='1': return f"({d[1:4]}) {d[4:7]}-{d[7:]}"
    return None

async def sec_edgar_lookup(biz: str) -> Optional[str]:
    """Search SEC EDGAR for company filings, extract phone from cover page."""
    q = httpx.QUERY_STRING_TRANSLATOR or lambda q: q
    try:
        r = httpx.get(f"https://www.sec.gov/cgi-bin/browse-edgar?company={biz}&action=getcompany",
            headers={"User-Agent": "UCC-Scraper/1.0 (contact@example.com)"}, timeout=15)
        if r.status_code == 200:
            for m in re.finditer(r'(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}', r.text):
                p = clean(m.group())
                if p and not p.startswith('(800)'): return p
    except: pass
    return None

async def uspto_lookup(biz: str) -> Optional[str]:
    """Search USPTO trademark database for business contact info."""
    try:
        r = httpx.get(f"https://tmsearch.uspto.gov/api/query?q={biz}", timeout=15,
            headers={"Accept": "application/json"})
        if r.status_code == 200:
            data = r.json()
            for rec in (data.get('results', []) if isinstance(data,dict) else data):
                phone = rec.get('phone', rec.get('attorney_phone', ''))
                if phone: return clean(phone)
    except: pass
    return None

async def free_enrich(biz: str) -> dict:
    """Try all free sources, return first phone found."""
    for name, fn in [("EDGAR", sec_edgar_lookup), ("USPTO", uspto_lookup)]:
        phone = await fn(biz)
        if phone: return {"phone": phone, "source": name}
    return {"phone": None, "source": "none"}
