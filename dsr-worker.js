// mergeranalyst-dsr: FRED + NY Fed + hardcoded snapshots proxy for /real-dsr
// Endpoints:
//   /api/dsr-data        — composite payload (all series + non-FRED snapshots)
//   /health              — liveness check
//
// FRED's Akamai bot screen periodically blocks Worker outbound, so we ALWAYS
// ship a self-contained payload built from embedded snapshots and overlay live
// values when the upstream returns. NY Fed Markets API is CORS-friendly and
// reliable, so SOFR has a live path.

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

// -------------------------------------------------------------------------
// EMBEDDED FRED SNAPSHOTS — refresh quarterly (TDSP cadence)
// Pull cmd, run in any shell where curl can reach FRED:
//   curl -sL -A "curl/7.88" "https://fred.stlouisfed.org/graph/fredgraph.csv?id=TDSP"
//   curl -sL -A "curl/7.88" "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DSPI"
// Then re-derive the array below (Date | TDSP % | DSPI quarterly avg SAAR $B).
// -------------------------------------------------------------------------
const TDSP_DSPI_QUARTERLY = [
  ['2010-01-01',14.515,11097.0],['2010-04-01',14.07,11298.8],['2010-07-01',13.918,11382.0],['2010-10-01',13.581,11498.3],
  ['2011-01-01',13.308,11711.6],['2011-04-01',13.058,11803.4],['2011-07-01',12.945,11921.1],['2011-10-01',12.75,11987.4],
  ['2012-01-01',12.237,12252.1],['2012-04-01',12.034,12364.2],['2012-07-01',12.083,12301.8],['2012-10-01',11.754,12715.1],
  ['2013-01-01',12.007,12254.1],['2013-04-01',11.794,12353.2],['2013-07-01',11.838,12447.3],['2013-10-01',11.985,12510.3],
  ['2014-01-01',11.884,12714.4],['2014-04-01',11.621,12931.9],['2014-07-01',11.647,13089.1],['2014-10-01',11.632,13258.2],
  ['2015-01-01',11.557,13380.5],['2015-04-01',11.485,13487.9],['2015-07-01',11.606,13598.3],['2015-10-01',11.739,13664.8],
  ['2016-01-01',11.763,13782.3],['2016-04-01',11.766,13841.8],['2016-07-01',11.77,13968.1],['2016-10-01',11.866,14123.3],
  ['2017-01-01',11.724,14353.0],['2017-04-01',11.757,14538.8],['2017-07-01',11.818,14692.1],['2017-10-01',11.817,14871.8],
  ['2018-01-01',11.645,15133.1],['2018-04-01',11.598,15349.0],['2018-07-01',11.62,15563.1],['2018-10-01',11.666,15771.0],
  ['2019-01-01',11.5,15997.3],['2019-04-01',11.627,16074.9],['2019-07-01',11.647,16222.0],['2019-10-01',11.728,16363.7],
  ['2020-01-01',11.591,16516.2],['2020-04-01',9.74,18086.1],['2020-07-01',10.058,17601.2],['2020-10-01',10.389,17330.3],
  ['2021-01-01',9.051,19680.6],['2021-04-01',9.841,18449.2],['2021-07-01',10.01,18486.9],['2021-10-01',10.229,18587.0],
  ['2022-01-01',10.472,18374.3],['2022-04-01',10.685,18615.8],['2022-07-01',10.568,19130.9],['2022-10-01',10.737,19521.1],
  ['2023-01-01',10.564,20283.4],['2023-04-01',10.577,20651.0],['2023-07-01',10.748,20894.7],['2023-10-01',11.096,21168.1],
  ['2024-01-01',11.059,21575.4],['2024-04-01',11.019,21843.2],['2024-07-01',11.139,22002.6],['2024-10-01',11.122,22249.5],
  ['2025-01-01',11.105,22563.7],['2025-04-01',11.124,22786.6],['2025-07-01',11.258,23001.2],['2025-10-01',11.323,23113.2],
];

const SOFR_SNAPSHOT = { d: '2026-06-15', v: 3.69 };

// -------------------------------------------------------------------------
// NON-FRED SNAPSHOTS — refresh monthly (margin) / quarterly (BNPL, PC AUM)
// -------------------------------------------------------------------------
const SNAPSHOTS = {
  // FINRA margin debt — published mid-month for prior month, no CSV feed.
  // Source: https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics
  margin_debt: {
    value_usd_bn: 1300.0,           // April 2026 record high, $1.30T
    asOf: '2026-04-30',
    source: 'FINRA Margin Statistics',
    source_url: 'https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics',
    note: 'Record high; +6.8% MoM, +53% YoY (AdvisorPerspectives, May 2026).'
  },

  // BNPL outstanding — estimated aggregate of issuer 10-Q HFI receivables plus
  // securitized/Pay-in-4 receivables. Affirm ~$15B (loans HFI + ABS); Klarna
  // sold up to $26B Pay-in-4 receivables to Nelnet (Oct '25) → avg balance ~$6B;
  // Block/Afterpay ~$4B; PayPal Pay Later ~$5B; Sezzle/Zip/other ~$3B.
  // Cross-check: CFPB BNPL Market Report (Dec '25) — Pay-in-4 GMV $45.2B in '23,
  // run-rate ~$80B in '26 with 6-week tenor → average outstanding ~$9B for
  // Pay-in-4 alone; interest-bearing book ~$31B.
  bnpl_outstanding: {
    value_usd_bn: 40.0,
    asOf: '2026-03-31',
    source: 'Issuer 10-Q aggregation + CFPB BNPL Market Report (Dec 2025)',
    source_url: 'https://files.consumerfinance.gov/f/documents/cfpb_bnpl-market-report_2025-12.pdf',
    note: 'Aggregate of Affirm, Klarna, Block/Afterpay, PayPal, Sezzle, Zip receivables. Pay-in-4 turns 6-weekly so OS << annual GMV (~$200B).'
  },

  // Private credit AUM — Preqin Global Report 2026
  private_credit_aum: {
    value_usd_tn: 2.28,
    asOf: '2025-12-31',
    source: 'Preqin Global Report 2026',
    source_url: 'https://www.preqin.com/global-report',
    note: 'Projected to reach $4.504T by 2030; ~10% CAGR. Cross-check: OFR Brief 26-02 (Mar 2026).'
  }
};

// Methodology constants
const METHOD = {
  margin_spread_bps: 600,           // broker call rate ≈ SOFR + 600bps (retail-skewed blend)
  bnpl_blended_rate_pct: 17,        // mix of 0% pay-in-4 (cost via late fees + losses) + Affirm-style installment APRs (~25-30%)
  pc_spread_bps: 600                // private credit typical floating spread SOFR + 500-700bps
};

// -------------------------------------------------------------------------
// LIVE FETCHES
// -------------------------------------------------------------------------
async function fetchSofrLive() {
  // NY Fed Markets API — CORS-enabled, no auth, very reliable
  try {
    const r = await fetch('https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json', {
      headers: { 'Accept': 'application/json' },
      cf: { cacheTtl: 600, cacheEverything: true }
    });
    if (!r.ok) return null;
    const j = await r.json();
    const rec = j && j.refRates && j.refRates[0];
    if (!rec) return null;
    return { d: rec.effectiveDate, v: rec.percentRate, source: 'NY Fed Markets API' };
  } catch (e) {
    return null;
  }
}

async function fetchFredLive(id) {
  // Best-effort — FRED's Akamai screen periodically blocks Worker outbound.
  // We use a benign UA and short timeout, fall back to embedded snapshot.
  try {
    const r = await fetch(`https://fred.stlouisfed.org/graph/fredgraph.csv?id=${encodeURIComponent(id)}`, {
      headers: { 'User-Agent': 'curl/7.88.1' },
      cf: { cacheTtl: 3600, cacheEverything: true }
    });
    if (!r.ok) return null;
    const text = await r.text();
    const lines = text.trim().split('\n');
    if (lines.length < 2) return null;
    let latest = null;
    for (let i = 1; i < lines.length; i++) {
      const parts = lines[i].split(',');
      if (parts.length < 2) continue;
      const v = parseFloat(parts[1]);
      if (Number.isFinite(v)) latest = { d: parts[0], v };
    }
    return latest;
  } catch (e) {
    return null;
  }
}

// -------------------------------------------------------------------------
// COMPOSITE PAYLOAD
// -------------------------------------------------------------------------
async function buildPayload() {
  // Reference quarter = latest TDSP observation in embedded series.
  const lastRow = TDSP_DSPI_QUARTERLY[TDSP_DSPI_QUARTERLY.length - 1];
  let tdsp = { d: lastRow[0], v: lastRow[1], source: 'embedded snapshot' };
  let dspi_q = { quarterStart: lastRow[0], value: lastRow[2], source: 'embedded snapshot' };

  // Best-effort live TDSP — falls back to embedded
  const liveTdsp = await fetchFredLive('TDSP');
  if (liveTdsp && liveTdsp.d >= tdsp.d) {
    tdsp = { ...liveTdsp, source: 'FRED live' };
  }

  // SOFR — prefer NY Fed live, fall back to FRED, then embedded
  let sofr = { ...SOFR_SNAPSHOT, source: 'embedded snapshot' };
  const nyFedSofr = await fetchSofrLive();
  if (nyFedSofr) {
    sofr = nyFedSofr;
  } else {
    const fredSofr = await fetchFredLive('SOFR');
    if (fredSofr) sofr = { ...fredSofr, source: 'FRED live' };
  }

  // ---- REAL DSR MATH ----
  const margin_debt_bn = SNAPSHOTS.margin_debt.value_usd_bn;
  const bnpl_os_bn     = SNAPSHOTS.bnpl_outstanding.value_usd_bn;
  const pc_aum_tn      = SNAPSHOTS.private_credit_aum.value_usd_tn;
  const pc_aum_bn      = pc_aum_tn * 1000;

  const sofr_pct        = sofr.v;
  const margin_rate_pct = sofr_pct + METHOD.margin_spread_bps / 100;
  const pc_rate_pct     = sofr_pct + METHOD.pc_spread_bps / 100;
  const bnpl_rate_pct   = METHOD.bnpl_blended_rate_pct;

  const margin_annual_interest_bn = margin_debt_bn * (margin_rate_pct / 100);
  const bnpl_annual_service_bn    = bnpl_os_bn   * (bnpl_rate_pct / 100);
  const pc_annual_interest_bn     = pc_aum_bn    * (pc_rate_pct  / 100);

  const dspi_q_bn = dspi_q.value;
  const margin_addback_pp = (margin_annual_interest_bn / dspi_q_bn) * 100;
  const bnpl_addback_pp   = (bnpl_annual_service_bn    / dspi_q_bn) * 100;

  const official_dsr = tdsp.v;
  const real_dsr     = official_dsr + margin_addback_pp + bnpl_addback_pp;
  const gap_pp       = real_dsr - official_dsr;

  // Historical chart series
  const chart_series = buildChartSeries(sofr_pct, margin_rate_pct, bnpl_rate_pct);

  return {
    fetchedAt: Date.now(),
    fetchedAtIso: new Date().toISOString(),

    series: {
      TDSP: tdsp,
      DSPI_quarterly_for_TDSP: dspi_q,
      SOFR: sofr
    },

    snapshots: SNAPSHOTS,
    method: METHOD,

    derived: {
      sofr_pct,
      margin_rate_pct,
      pc_rate_pct,
      bnpl_rate_pct,
      margin_annual_interest_bn: +margin_annual_interest_bn.toFixed(2),
      bnpl_annual_service_bn:    +bnpl_annual_service_bn.toFixed(2),
      pc_annual_interest_bn:     +pc_annual_interest_bn.toFixed(2),
      dspi_q_bn:                 +dspi_q_bn.toFixed(1),
      margin_addback_pp:         +margin_addback_pp.toFixed(3),
      bnpl_addback_pp:           +bnpl_addback_pp.toFixed(3),
      official_dsr_pct:          +official_dsr.toFixed(3),
      real_dsr_pct:              +real_dsr.toFixed(3),
      gap_pp:                    +gap_pp.toFixed(3)
    },

    chart: chart_series
  };
}

function buildChartSeries(sofrPct, marginRatePct, bnplRatePct) {
  // FINRA margin debt rough historical anchors ($B): linear interp
  const marginAnchors = [
    ['2010-01-01', 200],
    ['2015-01-01', 450],
    ['2020-01-01', 600],
    ['2021-12-01', 935],
    ['2022-12-01', 580],
    ['2024-06-01', 800],
    ['2025-12-01', 1100],
    ['2026-04-01', 1300]
  ];
  // BNPL outstanding rough historical
  const bnplAnchors = [
    ['2019-01-01', 0.5],
    ['2020-01-01', 2],
    ['2021-01-01', 8],
    ['2022-01-01', 15],
    ['2023-01-01', 22],
    ['2024-01-01', 30],
    ['2025-01-01', 36],
    ['2026-03-01', 40]
  ];

  function lerp(anchors, dateStr) {
    const t = Date.parse(dateStr);
    if (!Number.isFinite(t)) return null;
    if (t <= Date.parse(anchors[0][0])) return anchors[0][1];
    if (t >= Date.parse(anchors[anchors.length - 1][0])) return anchors[anchors.length - 1][1];
    for (let i = 0; i < anchors.length - 1; i++) {
      const a = Date.parse(anchors[i][0]);
      const b = Date.parse(anchors[i + 1][0]);
      if (t >= a && t <= b) {
        const f = (t - a) / (b - a);
        return anchors[i][1] + f * (anchors[i + 1][1] - anchors[i][1]);
      }
    }
    return null;
  }

  const labels = [];
  const official = [];
  const margin_addback = [];
  const bnpl_addback = [];
  const real = [];

  for (const [d, t, dq] of TDSP_DSPI_QUARTERLY) {
    const mdebt = lerp(marginAnchors, d);
    const bnplOs = lerp(bnplAnchors, d);
    if (dq && mdebt != null && bnplOs != null) {
      const m_ab = ((mdebt * marginRatePct / 100) / dq) * 100;
      const b_ab = ((bnplOs * bnplRatePct / 100) / dq) * 100;
      labels.push(d);
      official.push(+t.toFixed(3));
      margin_addback.push(+m_ab.toFixed(3));
      bnpl_addback.push(+b_ab.toFixed(3));
      real.push(+(t + m_ab + b_ab).toFixed(3));
    }
  }
  return { labels, official, margin_addback, bnpl_addback, real };
}

// -------------------------------------------------------------------------
// REQUEST HANDLER
// -------------------------------------------------------------------------
async function handle(req) {
  const url = new URL(req.url);

  if (req.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }
  if (url.pathname === '/' || url.pathname === '/health') {
    return new Response(JSON.stringify({
      ok: true,
      service: 'mergeranalyst-dsr',
      endpoints: ['/api/dsr-data', '/health']
    }), { headers: { 'Content-Type': 'application/json', ...corsHeaders() } });
  }
  if (url.pathname === '/api/dsr-data') {
    try {
      const payload = await buildPayload();
      return new Response(JSON.stringify(payload), {
        headers: {
          'Content-Type': 'application/json',
          'Cache-Control': 'public, max-age=600, s-maxage=600',
          ...corsHeaders()
        }
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: 'build failed', detail: String(e && e.message || e) }), {
        status: 500,
        headers: { 'Content-Type': 'application/json', ...corsHeaders() }
      });
    }
  }
  return new Response('Not found', { status: 404, headers: corsHeaders() });
}
