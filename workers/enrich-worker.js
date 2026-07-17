/**
 * UCC Lead Enricher — Cloudflare Worker #1
 * Strategy: YellowPages + WhitePages + MerchantCircle HTTP scraping
 *
 * Deploy:
 *   npx wrangler deploy workers/enrich-worker.js --name ucc1-enricher-1
 *   npx wrangler deploy workers/enrich-worker.js --name ucc1-enricher-2
 *   (etc — works standalone, different CF edge IP per worker)
 */

const PHONE_CLEAN = /[^\d]/g;

function cleanPhone(raw) {
  if (!raw) return null;
  const digits = raw.replace(PHONE_CLEAN, '');
  if (digits.length === 10) return `(${digits.slice(0,3)}) ${digits.slice(3,6)}-${digits.slice(6)}`;
  if (digits.length === 11 && digits[0] === '1') return `(${digits.slice(1,4)}) ${digits.slice(4,7)}-${digits.slice(7)}`;
  return null;
}

async function fetchWithTimeout(url, opts = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), opts.timeout || 8000);
  try {
    const resp = await fetch(url, { ...opts, signal: controller.signal });
    return resp;
  } finally {
    clearTimeout(timeout);
  }
}

async function extractPhone(html) {
  const phones = new Set();
  const patterns = [
    /"phone":"([^"]+)"/g,
    /"phoneNumber":"([^"]+)"/g,
    /"formatted_phone_number":"([^"]+)"/g,
    /href="tel:(\+?\d+)/g,
    /"international_phone_number":"([^"]+)"/g,
    /(\d{3}[\s.-]\d{3}[\s.-]\d{4})/g,
  ];
  for (const p of patterns) {
    let m; while ((m = p.exec(html)) !== null) {
      const phone = cleanPhone(m[1]);
      if (phone && !phone.startsWith('(800)') && !phone.startsWith('(888)') && !phone.startsWith('(844)') && !phone.startsWith('(855)') && !phone.startsWith('(866)') && !phone.startsWith('(877)')) phones.add(phone);
    }
  }
  return [...phones][0] || null;
}

export default {
  async fetch(request, env) {
    const secret = env?.WORKER_SECRET || 'dev-secret';
    if (request.headers.get('X-Worker-Secret') !== secret) return new Response('Unauthorized', { status: 401 });
    if (request.method !== 'POST') return new Response('POST only', { status: 405 });

    let leads;
    try { leads = await request.json(); } catch { return new Response('Invalid JSON', { status: 400 }); }
    if (!Array.isArray(leads)) return new Response('Expected array', { status: 400 });

    const results = [];
    let found = 0;

    for (const lead of leads) {
      const biz = (lead.business_name || '').trim();
      const city = (lead.business_city || '').trim();
      const state = (lead.business_state || '').trim();
      let phone = null, source = null;

      if (!biz) {
        results.push({ ...lead, phone_number: null, source: 'skip' });
        continue;
      }

      // Strategy 1: YellowPages
      if (!phone) {
        try {
          const q = encodeURIComponent(`${biz} ${city} ${state}`);
          const resp = await fetchWithTimeout(`https://www.yellowpages.com/search?search_terms=${q}`, {
            headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' }
          });
          if (resp.ok) phone = await extractPhone(await resp.text());
          if (phone) source = 'yp';
        } catch (e) { console.error('YP error:', biz, e.message); }
      }

      // Strategy 2: WhitePages
      if (!phone) {
        try {
          const slug = biz.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/-+/g,'-').replace(/^-|-$/g,'');
          const citySlug = city.toLowerCase().replace(/[^a-z]+/g, '-');
          if (slug && citySlug && state) {
            const resp = await fetchWithTimeout(`https://www.whitepages.com/business/${slug}/${citySlug}-${state.toLowerCase()}`, {
              headers: { 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36' }
            });
            if (resp.ok) phone = await extractPhone(await resp.text());
            if (phone) source = 'wp';
          }
        } catch (e) { console.error('WP error:', biz, e.message); }
      }

      // Strategy 3: Google Places (if key configured)
      if (!phone && env?.GOOGLE_PLACES_API_KEY) {
        try {
          const q = encodeURIComponent(`${biz} ${city} ${state}`);
          const fr = await fetchWithTimeout(
            `https://maps.googleapis.com/maps/api/place/findplacefromtext/json?input=${q}&inputtype=textquery&fields=place_id&key=${env.GOOGLE_PLACES_API_KEY}`
          );
          if (fr.ok) {
            const fd = await fr.json();
            const pid = fd?.candidates?.[0]?.place_id;
            if (pid) {
              const dr = await fetchWithTimeout(
                `https://maps.googleapis.com/maps/api/place/details/json?place_id=${pid}&fields=formatted_phone_number,international_phone_number&key=${env.GOOGLE_PLACES_API_KEY}`
              );
              if (dr.ok) {
                const dd = await dr.json();
                phone = dd?.result?.formatted_phone_number || dd?.result?.international_phone_number || null;
                if (phone) source = 'google_places';
              }
            }
          }
        } catch (e) { console.error('GP error:', biz, e.message); }
      }

      // Strategy 4: MerchantCircle
      if (!phone) {
        try {
          const q = encodeURIComponent(`${biz} ${city} ${state}`);
          const resp = await fetchWithTimeout(`https://www.merchantcircle.com/search?q=${q}`, {
            headers: { 'User-Agent': 'Mozilla/5.0' }
          });
          if (resp.ok) phone = await extractPhone(await resp.text());
          if (phone) source = 'mc';
        } catch (e) { console.error('MC error:', biz, e.message); }
      }

      if (phone) found++;
      results.push({ ...lead, phone_number: phone, source });
    }

    return new Response(JSON.stringify({
      stats: { total: leads.length, found, hit_rate: Math.round(found / leads.length * 100) },
      results
    }), { headers: { 'Content-Type': 'application/json' } });
  },

  async scheduled(event, env) {
    // Cron: fetch leads from dashboard endpoint, enrich, POST back
    console.log(`CF Worker cron triggered at ${new Date().toISOString()}`);
  }
};
