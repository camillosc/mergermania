#!/usr/bin/env python3
"""MergerAnalyst model portfolio: daily mark-to-market, closings/breaks, candidate screen, trade booking.

Illustrative model portfolio only (not investment advice). State lives in portfolio.json in the repo:
positions, cash, trade log and NAV history (inception NAV 100). Prices come from the site's quote worker
(Yahoo-backed); deal terms and statuses come from DEALS in index.html.

Usage (GITHUB_TOKEN in env; pushes portfolio.json merge-safe unless --dry):
  python3 portfolio_update.py                      # daily: MTM, book closings/breaks/term changes, record NAV, print candidates
  python3 portfolio_update.py --add TICKER --reason "..." [--weight W]   # buy at the latest close; default size = 1.75% NAV break-loss budget / downside, 3-10%
  python3 portfolio_update.py --trim TICKER --to 0.03 --reason "..."         # partial sale to a target weight (hedge covered pro rata)
  python3 portfolio_update.py --exit TICKER --reason "..."                   # discretionary exit at the latest close
  python3 portfolio_update.py --rank               # print quality scores for holdings and candidates (no trades)
  add --dry to print without pushing.
"""
import os, sys, json, re, base64, time, argparse, subprocess, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta, date

REPO = 'https://api.github.com/repos/Camillosc/mergermania'
QUOTE = 'https://mergeranalyst-quote.camillosc2.workers.dev/quote?symbols='
TARGET_WEIGHT, MAX_WEIGHT, MIN_CASH = 0.07, 0.10, 0.05   # legacy equal weight, hard cap, cash buffer kept after any new purchase
# Downside-based sizing: size by potential loss, not expected gain
BREAK_LOSS_BUDGET, MIN_WEIGHT, TRIM_TRIGGER = 0.0175, 0.03, 0.03   # break loss per deal ~1.75% of NAV; floor 3%; trim if a position's break loss > 3% of NAV
DEFAULT_DOWNSIDE = 0.25   # used when the undisturbed price is unavailable or the deal leaked before announcement (computed downside < 10%)
EU_CLOSE_UTC, US_CLOSE_UTC = (16, 35), (21, 0)   # conservative (winter) closes; used to decide whether today's print is final
EU_SFX = {'L', 'IR', 'DE', 'F', 'PA', 'AS', 'BR', 'MI', 'MC', 'LS', 'HE', 'VI', 'ST', 'OL', 'CO', 'SW'}
SFX_CUR = {'L': 'GBp', 'IR': 'EUR', 'DE': 'EUR', 'F': 'EUR', 'PA': 'EUR', 'AS': 'EUR', 'BR': 'EUR', 'MI': 'EUR', 'MC': 'EUR', 'LS': 'EUR',
           'HE': 'EUR', 'VI': 'EUR', 'ST': 'SEK', 'OL': 'NOK', 'CO': 'DKK', 'SW': 'CHF', 'TO': 'CAD'}

H = {'Authorization': f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
     'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}

def api(url, data=None, method='GET', accept=None):
    h = dict(H, **({'Accept': accept} if accept else {}))
    for i in range(4):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h), timeout=60).read()
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503) and i < 3: time.sleep(3 * (i + 1)); continue
            raise

def get_file(path):
    meta = json.loads(api(f'{REPO}/contents/{path}?ref=main'))
    return meta['sha'], api(f"{REPO}/git/blobs/{meta['sha']}", accept='application/vnd.github.raw').decode('utf-8')

def load_deals():
    _, html = get_file('index.html')
    i = html.index('const DEALS = [') + len('const DEALS = ')
    return json.loads(html[i:html.index('\n];', i) + 2])

def quotes(symbols):
    out, syms = {}, sorted({s for s in symbols if s})
    for k in range(0, len(syms), 12):              # worker: max 12 symbols/request; urllib gets 403, so use curl
        r = subprocess.run(['curl', '-s', '--max-time', '30', QUOTE + ','.join(syms[k:k + 12])], capture_output=True, text=True)
        out.update(json.loads(r.stdout or '{}').get('quotes', {}))
    return out

def is_eu(sym):
    return '.' in sym and sym.rsplit('.', 1)[1] in EU_SFX

def last_close(sym, q, now):
    """Price of the last completed session: today's print only once that market has closed."""
    if not q or q.get('price') is None: return None, None
    ts = datetime.fromtimestamp(q['ts'], timezone.utc)
    hh, mm = EU_CLOSE_UTC if is_eu(sym) else US_CLOSE_UTC
    if ts.date() == now.date() and (now.hour, now.minute) < (hh, mm):
        return q.get('prev') or q['price'], (ts.date() - timedelta(days=1))
    return q['price'], ts.date()

def session_date(now):
    """Date the NAV is stamped with: last weekday whose US session has closed."""
    d = now.date() if (now.hour, now.minute) >= US_CLOSE_UTC else now.date() - timedelta(days=1)
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d.isoformat()

def norm(v): return re.sub(r'\s+', ' ', str(v or '')).strip().lower()

def find_row(deals, pos):
    rows = [d for d in deals if norm(d.get('targetTicker')) == norm(pos['dealKey'])]
    if len(rows) > 1:
        rows = [d for d in rows if norm(pos['acquirer'])[:12] in norm(d.get('acquirer'))] or rows
    return rows[0] if rows else None

def deal_value(terms, acq_px):
    if terms.get('acq') and acq_px is None: return None
    v = terms.get('cash', 0) + terms.get('ratio', 0) * (acq_px or 0)
    if terms.get('cap') is not None: v = min(v, terms['cap'])
    if terms.get('floor') is not None: v = max(v, terms['floor'])
    return v

# --- region screen (mirrors geoRegion() in index.html) ---
REGION_OF = {**{k: 'us' for k in ('usa', 'us', 'united states')}, 'canada': 'ca', 'uk': 'uk', 'united kingdom': 'uk',
             **{k: 'eu' for k in ('eu', 'europe', 'france', 'germany', 'italy', 'spain', 'ireland', 'switzerland', 'netherlands',
                                  'sweden', 'denmark', 'finland', 'austria', 'belgium', 'norway', 'portugal', 'poland', 'bulgaria',
                                  'greece', 'luxembourg')}}
DOMICILE = {'bvi', 'bermuda', 'cayman', 'cayman islands', 'luxembourg', 'global'}
def region(d):
    toks = [t.strip() for t in re.split(r'\s*(?:/|,|&|\band\b)\s*', str(d.get('geo') or '').lower()) if t.strip()]
    fb = None
    for t in toks:
        r = REGION_OF.get(t)
        if not r and t in ('brazil', 'peru', 'mexico', 'colombia', 'chile', 'argentina', 'australia', 'japan', 'china', 'india',
                           'israel', 'uae', 'egypt', 'taiwan', 'singapore', 'new zealand', 'indonesia', 'south korea', 'asia-pacific'):
            return 'other'
        if not r: continue
        if t in DOMICILE: fb = fb or r; continue
        return r
    return fb or ('us' if not toks else 'other')

# --- quality score (0-100): closing probability 40, annualized spread 25, reverse-fee protection 20, deal risk 10, target fee 5 ---
def fee_pcts(d):
    f = d.get('fees') or {}
    rv, tg = (f.get('reverse') or {}).get('pct'), (f.get('target') or {}).get('pct')
    return rv, tg, f.get('regime')

def unaffected_price(sym, announced):
    """Last close before the announcement date (Yahoo chart API), as the break-price estimate."""
    try:
        a = datetime.fromisoformat(str(announced)[:10]).replace(tzinfo=timezone.utc)
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={int((a - timedelta(days=12)).timestamp())}&period2={int(a.timestamp())}&interval=1d'
        r = subprocess.run(['curl', '-s', '--max-time', '20', '-A', 'Mozilla/5.0', url], capture_output=True, text=True).stdout
        res = json.loads(r)['chart']['result'][0]
        pts = [(datetime.fromtimestamp(t, timezone.utc).date().isoformat(), x) for t, x in zip(res['timestamp'], res['indicators']['quote'][0]['close']) if x]
        pts = [p for p in pts if p[0] < str(announced)[:10]]
        return pts[-1] if pts else None
    except Exception:
        return None

def downside(sym, announced, cur, currency=None):
    """(breakPrice, downside fraction, basis). Downside = (current - undisturbed) / current, floored to the default when unreliable."""
    u = unaffected_price(sym, announced) if cur else None
    if u:
        bp = u[1] * (100 if currency == 'GBp' and u[1] < cur / 20 else 1)
        dn = (cur - bp) / cur
        if dn >= 0.10: return round(bp, 4), round(dn, 4), f'undisturbed close {u[0]}'
    return round(cur * (1 - DEFAULT_DOWNSIDE), 4) if cur else None, DEFAULT_DOWNSIDE, 'default 25% (undisturbed price unavailable or pre-leaked)'

def size_for(dn):
    return round(min(MAX_WEIGHT, max(MIN_WEIGHT, BREAK_LOSS_BUDGET / max(dn, 0.01))), 4)

def quality_score(d, ann=None):
    ann = d.get('annualizedReturn') if ann is None else ann
    rv, tg, regime = fee_pcts(d)
    prob = min(max(d.get('closingProbability') or 0, 0), 100) / 100 * 40
    ret = min(max(ann or 0, 0), 15) / 15 * 25
    rev = min(rv or 0, 6) / 6 * 20                  # a reverse/regulatory fee of 6%+ of equity value earns full credit
    risk = {'low': 10, 'medium': 5}.get(norm(d.get('dealRisk')), 0 if norm(d.get('dealRisk')) == 'high' else 5)
    tgt = 2.5 if regime == 'uk-code-none-permitted' else min(tg or 0, 4) / 4 * 5   # UK Code bars target fees: neutral credit
    return round(min(100, prob + ret + rev + risk + tgt), 1)

def candidates(deals, held, today, excluded=None):
    """Quantitative pre-screen; the qualitative quality screen (see rules) is applied by the reviewer before --add."""
    out = []
    for d in deals:
        if d.get('status') != 'pending' or region(d) not in ('us', 'eu', 'uk'): continue
        if norm(d.get('targetTicker')) in held or d.get('spread') is None or d.get('noAnnualize'): continue
        if str(d.get('spreadNote', '')).startswith('Not calculable'): continue
        if (d.get('closingProbability') or 0) < 80 or norm(d.get('dealRisk')) == 'high': continue
        days = (date.fromisoformat(d['expectedClose']) - today).days if re.match(r'\d{4}-\d\d-\d\d$', str(d.get('expectedClose'))) else None
        ann = d.get('annualizedReturn') or 0
        if ann >= 5 or (ann >= 3 and days is not None and days <= 45):
            out.append({'ticker': d['targetTicker'], 'target': d['target'], 'acquirer': d.get('acquirer'), 'spread': d['spread'],
                        'annualized': ann, 'expectedClose': d.get('expectedClose'), 'prob': d.get('closingProbability'),
                        'risk': d.get('dealRisk'), 'otherBidders': d.get('otherBidders'), 'score': quality_score(d, ann),
                        'reverseFeePct': fee_pcts(d)[0], 'targetFeePct': fee_pcts(d)[1], 'feeRegime': fee_pcts(d)[2],
                        **({'previouslyExcluded': (excluded or {})[norm(d['targetTicker'])]} if norm(d['targetTicker']) in (excluded or {}) else {})})
    return sorted(out, key=lambda c: -c['score'])

def refresh_fees(p, row):
    rv, tg, regime = fee_pcts(row)
    p['fees'] = {'reversePct': rv, 'targetPct': tg, 'regime': regime}
    p['qualityScore'] = quality_score(row)

def break_loss(p, nav):
    """Loss in NAV fraction if the deal breaks and the target falls to its break price (hedge P&L ignored)."""
    if p.get('breakPrice') is None or not nav: return None
    return round(p['shares'] * max(0.0, (p.get('lastPrice') or p['entryPrice']) - p['breakPrice']) / nav, 5)

def trim_position(pf, p, d, px, apx, new_weight, nav, reason):
    """Partial sale down to new_weight of NAV at the session close; the hedge is covered pro rata."""
    keep = new_weight * nav / px
    qty = p['shares'] - keep
    if qty <= 1e-9: return
    pf['cash'] += qty * px
    log(pf, d, 'TRIM', p, px, qty, reason)
    if p.get('hedge'):
        hq = p['hedge']['shares'] * qty / p['shares']
        pf['cash'] -= hq * apx
        log(pf, d, 'COVER', p, apx, hq, 'Hedge reduced pro rata with the trim')
        p['hedge']['shares'] -= hq
    p['realizedPnl'] = round(p.get('realizedPnl', 0) + qty * (px - p['entryPrice']), 6)
    p['shares'] = keep; p['targetWeight'] = new_weight

# --- portfolio mechanics ---
def mark(p, qs, now):
    px, _ = last_close(p['quoteSymbol'], qs.get(p['quoteSymbol']), now)
    acq = p['terms'].get('acq')
    apx = last_close(acq, qs.get(acq), now)[0] if acq else None
    return px, apx

def nav_of(pf, qs, now):
    longs = shorts = 0.0
    for p in pf['positions']:
        if p['status'] != 'open': continue
        px, apx = mark(p, qs, now)
        p['lastPrice'] = px if px is not None else p.get('lastPrice', p['entryPrice'])
        if p.get('hedge'):
            p['hedge']['lastPrice'] = apx if apx is not None else p['hedge'].get('lastPrice', p['hedge']['entryPrice'])
            shorts += p['hedge']['shares'] * p['hedge']['lastPrice']
        p['dealValueNow'] = deal_value(p['terms'], p.get('hedge', {}).get('lastPrice')) if p['terms'].get('acq') else p['entryDealValue']
        longs += p['shares'] * p['lastPrice']
    return pf['cash'] + longs - shorts, longs, shorts

def log(pf, d, action, p, price, qty, reason):
    pf['trades'].append({'date': d, 'action': action, 'ticker': p['ticker'], 'symbol': p['quoteSymbol'] if action in ('BUY', 'SELL', 'CLOSE', 'BREAK') else p['terms'].get('acq'),
                         'price': round(price, 6), 'qty': round(qty, 6), 'notional': round(price * qty, 4), 'reason': reason})

def close_position(pf, p, d, px_long, apx, action, reason):
    pf['cash'] += p['shares'] * px_long
    log(pf, d, action, p, px_long, p['shares'], reason)
    if p.get('hedge'):
        pf['cash'] -= p['hedge']['shares'] * apx
        log(pf, d, 'COVER', p, apx, p['hedge']['shares'], 'Hedge closed with the position' + (' (acquirer shares received deliver against the short)' if action == 'CLOSE' else ''))
    p.update(status='closed' if action == 'CLOSE' else 'broken' if action == 'BREAK' else 'exited', exitDate=d, exitPrice=px_long)
    p['realizedPnl'] = round(p['shares'] * (px_long - p['entryPrice']) - (p['hedge']['shares'] * (apx - p['hedge']['entryPrice']) if p.get('hedge') else 0), 6)

def open_position(pf, row, qs, now, d, weight, reason, rationale=None, force=False):
    """weight=None sizes by downside: BREAK_LOSS_BUDGET / downside, clamped to [MIN_WEIGHT, MAX_WEIGHT]."""
    sym = row.get('quoteSymbol') or row['targetTicker']
    lt = row.get('liveTerms') or {}
    terms = {'cash': lt.get('cash', row['dealPrice']) if lt else row['dealPrice'], 'ratio': lt.get('ratio', 0), 'acq': lt.get('acq')}
    for k in ('cap', 'floor'):
        if lt.get(k) is not None: terms[k] = lt[k]
    if lt.get('collar'): raise SystemExit(f'{sym}: collar terms need a manual hedge; not supported by --add')
    px = last_close(sym, qs.get(sym), now)[0]
    apx = last_close(terms['acq'], qs.get(terms['acq']), now)[0] if terms['acq'] else None
    if px is None or (terms['acq'] and apx is None): raise SystemExit(f'no price for {sym} / {terms["acq"]}')
    cur_code = row.get('priceCurrency') or SFX_CUR.get(sym.rsplit('.', 1)[1] if '.' in sym else '', 'USD')
    bp, dn, basis = downside(sym, row.get('announced'), px, cur_code)
    if weight is None: weight = size_for(dn)
    nav = nav_of(pf, qs, now)[0]
    notional = min(weight, MAX_WEIGHT) * nav
    if pf['cash'] - notional < MIN_CASH * nav - 1e-9 and not force:
        raise SystemExit(f'cash {pf["cash"]:.2f} would fall below the {MIN_CASH:.0%} buffer after a {notional:.2f} purchase')
    p = {'ticker': row['targetTicker'], 'dealKey': row['targetTicker'], 'quoteSymbol': sym, 'currency': row.get('priceCurrency') or SFX_CUR.get(sym.rsplit('.', 1)[1] if '.' in sym else '', 'USD'),
         'target': row['target'], 'acquirer': row.get('acquirer', ''), 'terms': terms, 'entryDate': d, 'entryPrice': px,
         'entryDealValue': deal_value(terms, apx), 'shares': notional / px, 'targetWeight': weight, 'expectedClose': row.get('expectedClose'),
         'rationale': rationale or reason, 'status': 'open', 'breakPrice': bp, 'downside': dn, 'breakBasis': basis}
    refresh_fees(p, row)
    pf['cash'] -= notional
    pf['positions'].append(p)
    log(pf, d, 'BUY', p, px, p['shares'], reason)
    if terms['acq'] and terms['ratio']:
        hs = p['shares'] * terms['ratio']
        p['hedge'] = {'symbol': terms['acq'], 'shares': hs, 'entryPrice': apx}
        pf['cash'] += hs * apx
        log(pf, d, 'SHORT', p, apx, hs, f"Hedge: short {terms['ratio']} {terms['acq']} per target share")
    return p

def daily(pf, deals, now):
    d = session_date(now)
    syms = {p['quoteSymbol'] for p in pf['positions'] if p['status'] == 'open'} | {p['terms'].get('acq') for p in pf['positions'] if p['status'] == 'open'}
    qs = quotes(syms)
    events = []
    for p in pf['positions']:
        if p['status'] != 'open': continue
        row = find_row(deals, p)
        px, apx = mark(p, qs, now)
        st = norm(row.get('status')) if row else ''
        if st in ('approved', 'closed', 'completed'):           # DEALS uses "approved" for completed deals
            v = deal_value(p['terms'], apx) if p['terms'].get('acq') else p['entryDealValue']
            close_position(pf, p, d, v, apx, 'CLOSE', f"Deal closed (DEALS status '{row['status']}'): booked at deal value {v:.4f}")
            events.append(f"CLOSE {p['ticker']} @ {v:.4f}")
        elif st in ('terminated', 'broken', 'withdrawn', 'failed', 'lapsed'):
            close_position(pf, p, d, px, apx, 'BREAK', f"Deal {row['status']}: sold at market {px}; loss vs entry {p['entryPrice']} booked")
            events.append(f"BREAK {p['ticker']} @ {px}")
        elif row:
            lt = row.get('liveTerms') or {}
            new_cash = lt.get('cash', row.get('dealPrice')) if lt else row.get('dealPrice')
            if new_cash is not None and abs(new_cash - p['terms']['cash']) > 1e-9 and not p['terms'].get('acq'):
                pf['trades'].append({'date': d, 'action': 'TERMS', 'ticker': p['ticker'], 'symbol': p['quoteSymbol'], 'price': new_cash, 'qty': 0,
                                     'notional': 0, 'reason': f"Deal terms changed in DEALS: {p['terms']['cash']} -> {new_cash}"})
                p['terms']['cash'] = p['entryDealValue'] = new_cash
                events.append(f"TERMS {p['ticker']} -> {new_cash}")
            p['flag'] = row.get('status') if st != 'pending' else None
            refresh_fees(p, row)
            if row.get('expectedClose'): p['expectedClose'] = row['expectedClose']
    nav, longs, shorts = nav_of(pf, qs, now)
    for p in pf['positions']:
        if p['status'] == 'open':
            p['breakLoss'] = break_loss(p, nav)
            if p['breakLoss'] is not None and p['breakLoss'] > TRIM_TRIGGER: events.append(f"TRIM-WATCH {p['ticker']} break loss {p['breakLoss']:.1%} of NAV")
    pt = {'date': d, 'nav': round(nav, 4), 'cash': round(pf['cash'], 4), 'long': round(longs, 4), 'short': round(shorts, 4),
          'open': sum(p['status'] == 'open' for p in pf['positions'])}
    pf['navHistory'] = [x for x in pf['navHistory'] if x['date'] != d] + [pt]
    pf['navHistory'].sort(key=lambda x: x['date'])
    pf['asOf'] = now.strftime('%Y-%m-%d %H:%M UTC')
    excl = {norm(x['ticker']): x['reason'] for x in pf.get('excludedAtInception', [])}
    return events, candidates(deals, {norm(p['dealKey']) for p in pf['positions'] if p['status'] == 'open'}, date.fromisoformat(d), excl)

def push(pf, msg):
    for attempt in range(1, 6):
        try:
            sha, cur = get_file('portfolio.json')
            body = json.dumps(pf, indent=1, ensure_ascii=False) + '\n'
            if body == cur: print('NOOP'); return
            payload = json.dumps({'message': msg, 'content': base64.b64encode(body.encode()).decode(), 'sha': sha, 'branch': 'main'}).encode()
            print(json.loads(api(f'{REPO}/contents/portfolio.json', data=payload, method='PUT'))['commit']['sha']); return
        except urllib.error.HTTPError as e:
            sys.stderr.write(f'PUSH retry {attempt}: HTTP {e.code}\n')
            if e.code not in (409, 422): raise
            time.sleep(3 * attempt)
    raise SystemExit('push failed')

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--add'); ap.add_argument('--exit'); ap.add_argument('--reason', default='')
    ap.add_argument('--weight', type=float, default=None); ap.add_argument('--dry', action='store_true')
    ap.add_argument('--rationale', default=None); ap.add_argument('--trim'); ap.add_argument('--to', type=float); ap.add_argument('--rank', action='store_true')
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    pf = json.loads(get_file('portfolio.json')[1])
    deals = load_deals()
    if a.rank:
        print('HOLDINGS (score, reverse fee %, target fee %, regime):')
        for p in pf['positions']:
            if p['status'] != 'open': continue
            row = find_row(deals, p)
            if row: print(f"  {p['ticker']:8} {quality_score(row):5}  rev {fee_pcts(row)[0]}  tgt {fee_pcts(row)[1]}  {fee_pcts(row)[2]}  ann {row.get('annualizedReturn')}")
        excl = {norm(x['ticker']): x['reason'] for x in pf.get('excludedAtInception', [])}
        print('CANDIDATES:')
        for c in candidates(deals, {norm(p['dealKey']) for p in pf['positions'] if p['status'] == 'open'}, now.date(), excl)[:20]:
            print('  ', json.dumps(c, ensure_ascii=False))
        sys.exit(0)
    if a.trim:
        if not a.reason or a.to is None: raise SystemExit('--trim needs --to and --reason')
        d = session_date(now)
        p = next(p for p in pf['positions'] if p['status'] == 'open' and norm(p['ticker']) == norm(a.trim))
        qs = quotes({q['quoteSymbol'] for q in pf['positions'] if q['status'] == 'open'} | {q['terms'].get('acq') for q in pf['positions'] if q['status'] == 'open'})
        nav = nav_of(pf, qs, now)[0]; px, apx = mark(p, qs, now)
        trim_position(pf, p, d, px, apx, a.to, nav, a.reason)
        nav_of(pf, qs, now); msg = f"Model portfolio {d}: TRIM {p['ticker']}"
    elif a.add or a.exit:
        if not a.reason: raise SystemExit('--reason is required')
        d = session_date(now)
        if a.add:
            row = next((r for r in deals if norm(r.get('targetTicker')) == norm(a.add) and r.get('status') == 'pending'), None)
            if not row: raise SystemExit(f'{a.add}: no pending DEALS row')
            sym, acq = row.get('quoteSymbol') or row['targetTicker'], (row.get('liveTerms') or {}).get('acq')
            qs = quotes({sym, acq} | {p['quoteSymbol'] for p in pf['positions'] if p['status'] == 'open'} | {p['terms'].get('acq') for p in pf['positions'] if p['status'] == 'open'})
            p = open_position(pf, row, qs, now, d, a.weight, a.reason, rationale=a.rationale)
            msg = f"Model portfolio {d}: BUY {p['ticker']}"
        else:
            p = next(p for p in pf['positions'] if p['status'] == 'open' and norm(p['ticker']) == norm(a.exit))
            qs = quotes({p['quoteSymbol'], p['terms'].get('acq')})
            px, apx = mark(p, qs, now)
            close_position(pf, p, d, px, apx, 'SELL', a.reason)
            msg = f"Model portfolio {d}: SELL {p['ticker']}"
        nav_of(pf, quotes({p['quoteSymbol'] for p in pf['positions'] if p['status'] == 'open'} | {p['terms'].get('acq') for p in pf['positions'] if p['status'] == 'open'}), now)
    else:
        events, cands = daily(pf, deals, now)
        last = pf['navHistory'][-1]
        print(f"NAV {last['date']}: {last['nav']:.2f} (cash {last['cash']:.2f}, open {last['open']}); events: {events or 'none'}")
        print('CANDIDATES (quant pre-screen; apply the quality screen before --add; previouslyExcluded = failed it at inception, add only if that reason is resolved):')
        for c in cands[:15]: print('  ', json.dumps(c, ensure_ascii=False))
        msg = f"Model portfolio NAV {last['date']}: {last['nav']:.2f}" + (f" ({'; '.join(events)})" if events else '')
    if a.dry: print(json.dumps(pf['navHistory'][-1:]), '(dry run, not pushed)')
    else: push(pf, msg + '\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')
