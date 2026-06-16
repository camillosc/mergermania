// mergeranalyst-quote: Yahoo Finance proxy with CORS for browser fetch
// Endpoint: /quote?symbols=SPCX,SATS

addEventListener('fetch', event => {
  event.respondWith(handle(event.request));
});

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400'
  };
}

async function fetchOne(sym) {
  // Try chart endpoint first (most reliable regularMarketPrice in meta)
  const chartUrl = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?interval=1m&range=1d`;
  try {
    const r = await fetch(chartUrl, {
      headers: { 'User-Agent': 'Mozilla/5.0 (compatible; mergeranalyst-quote/1.0)' },
      cf: { cacheTtl: 20, cacheEverything: true }
    });
    if (r.ok) {
      const j = await r.json();
      const meta = j && j.chart && j.chart.result && j.chart.result[0] && j.chart.result[0].meta;
      if (meta && typeof meta.regularMarketPrice === 'number') {
        return {
          price: meta.regularMarketPrice,
          prev: (meta.chartPreviousClose != null ? meta.chartPreviousClose : meta.previousClose) || null,
          ts: meta.regularMarketTime || null,
          state: meta.marketState || null,
          source: 'chart'
        };
      }
    }
  } catch (e) { /* fall through */ }

  // Fallback: spark endpoint (legacy compatibility)
  const sparkUrl = `https://query1.finance.yahoo.com/v8/finance/spark?symbols=${encodeURIComponent(sym)}&range=1d&interval=1d`;
  try {
    const r = await fetch(sparkUrl, {
      headers: { 'User-Agent': 'Mozilla/5.0 (compatible; mergeranalyst-quote/1.0)' },
      cf: { cacheTtl: 20, cacheEverything: true }
    });
    if (r.ok) {
      const j = await r.json();
      const node = j && j[sym];
      if (node && Array.isArray(node.close) && node.close.length) {
        const px = node.close[node.close.length - 1];
        if (typeof px === 'number') {
          return {
            price: px,
            prev: (node.chartPreviousClose != null ? node.chartPreviousClose : node.previousClose) || null,
            ts: (Array.isArray(node.timestamp) && node.timestamp.length) ? node.timestamp[node.timestamp.length - 1] : null,
            state: null,
            source: 'spark'
          };
        }
      }
    }
  } catch (e) { /* nothing more */ }
  return null;
}

async function handle(req) {
  const url = new URL(req.url);

  if (req.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }
  if (url.pathname === '/' || url.pathname === '/health') {
    return new Response(JSON.stringify({ ok: true, service: 'mergeranalyst-quote', usage: '/quote?symbols=SPCX,SATS' }), {
      headers: { 'Content-Type': 'application/json', ...corsHeaders() }
    });
  }
  if (url.pathname !== '/quote') {
    return new Response('Not found', { status: 404, headers: corsHeaders() });
  }

  const raw = url.searchParams.get('symbols') || '';
  // Allow A-Z, digits, dots, hyphens, commas (e.g. BRK-B, ^GSPC); keep tight to deter abuse
  if (!/^[A-Z0-9.\-,^]+$/i.test(raw) || raw.length > 100) {
    return new Response(JSON.stringify({ error: 'bad symbols' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json', ...corsHeaders() }
    });
  }
  const symbols = [...new Set(raw.toUpperCase().split(',').filter(Boolean))];
  if (symbols.length > 12) {
    return new Response(JSON.stringify({ error: 'too many symbols' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json', ...corsHeaders() }
    });
  }

  const results = await Promise.all(symbols.map(fetchOne));
  const quotes = {};
  const errors = [];
  symbols.forEach((s, i) => {
    if (results[i]) quotes[s] = results[i];
    else errors.push(s);
  });

  const body = JSON.stringify({
    quotes,
    errors,
    fetchedAt: Date.now(),
    fetchedAtIso: new Date().toISOString()
  });

  return new Response(body, {
    headers: {
      'Content-Type': 'application/json',
      'Cache-Control': 'public, max-age=15, s-maxage=15',
      ...corsHeaders()
    }
  });
}
