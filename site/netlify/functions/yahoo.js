// Netlify serverless function: routes all Yahoo Finance calls.
//   /api/yahoo?symbol=X&range=1y&interval=1d   -> chart data
//   /api/yahoo/search?q=tata                    -> symbol search (any NSE/BSE/global)
//   /api/yahoo/quote?symbol=X                   -> live fundamentals (P/E, EPS, ROE, ...)
//
// Netlify edge IPs aren't aggressively rate-limited by Yahoo, so we hit
// query[12].finance.yahoo.com directly and only fall back to public proxies on failure.

const SYMBOL_RE = /^[A-Z0-9.\-^=]{1,25}$/i;
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

// Module-level crumb cache (lives across function invocations in warm containers)
let _crumbCache = { crumb: null, cookie: null, ts: 0 };
const CRUMB_TTL_MS = 25 * 60 * 1000;

async function tryFetch(url, headers, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: ctrl.signal, headers });
    if (!r.ok) throw new Error('http ' + r.status);
    return r;
  } finally {
    clearTimeout(timer);
  }
}

async function getCrumb() {
  if (_crumbCache.crumb && Date.now() - _crumbCache.ts < CRUMB_TTL_MS) {
    return _crumbCache;
  }
  // Step 1: fetch cookie from fc.yahoo.com
  const cookieRes = await fetch('https://fc.yahoo.com', { headers: { 'User-Agent': UA } });
  const setCookie = cookieRes.headers.get('set-cookie') || '';
  // Parse out the A1/A3/B cookies
  const cookie = setCookie.split(',').map(s => s.split(';')[0].trim()).join('; ');

  // Step 2: get crumb using that cookie
  const crumbRes = await fetch('https://query2.finance.yahoo.com/v1/test/getcrumb', {
    headers: { 'User-Agent': UA, Cookie: cookie }
  });
  if (!crumbRes.ok) throw new Error('crumb fetch failed: ' + crumbRes.status);
  const crumb = (await crumbRes.text()).trim();
  if (!crumb || crumb.length > 30 || crumb.startsWith('{')) throw new Error('bad crumb');
  _crumbCache = { crumb, cookie, ts: Date.now() };
  return _crumbCache;
}

function jsonResponse(statusCode, obj, extraHeaders) {
  return {
    statusCode,
    headers: {
      'Access-Control-Allow-Origin': '*',
      'Content-Type': 'application/json',
      'Cache-Control': 'public, max-age=60',
      ...(extraHeaders || {})
    },
    body: typeof obj === 'string' ? obj : JSON.stringify(obj)
  };
}

async function handleChart(symbol, range, interval) {
  if (!SYMBOL_RE.test(symbol)) return jsonResponse(400, { error: 'invalid symbol' });
  const direct = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?interval=${interval}&range=${range}&includePrePost=false`;
  const allorigins = 'https://api.allorigins.win/raw?url=' + encodeURIComponent(direct);
  for (const u of [direct, allorigins, direct]) {
    try {
      const r = await tryFetch(u, { 'User-Agent': UA, Accept: 'application/json' }, 10000);
      const body = await r.text();
      if (body.indexOf('"chart"') !== -1) return jsonResponse(200, body);
    } catch (e) {}
  }
  return jsonResponse(502, { error: 'chart fetch failed' });
}

async function handleSearch(q) {
  const target = `https://query2.finance.yahoo.com/v1/finance/search?q=${encodeURIComponent(q)}&lang=en-US&region=IN&quotesCount=15&newsCount=0`;
  const allorigins = 'https://api.allorigins.win/raw?url=' + encodeURIComponent(target);
  for (const u of [target, allorigins]) {
    try {
      const r = await tryFetch(u, { 'User-Agent': UA, Accept: 'application/json' }, 10000);
      const body = await r.text();
      if (body.indexOf('"quotes"') !== -1) {
        return jsonResponse(200, body, { 'Cache-Control': 'public, max-age=600' });
      }
    } catch (e) {}
  }
  return jsonResponse(502, { error: 'search failed' });
}

async function handleQuote(symbol) {
  if (!SYMBOL_RE.test(symbol)) return jsonResponse(400, { error: 'invalid symbol' });
  const modules = 'summaryDetail,defaultKeyStatistics,financialData,assetProfile,price,earnings,incomeStatementHistory,balanceSheetHistory';
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const { crumb, cookie } = await getCrumb();
      const url = `https://query2.finance.yahoo.com/v10/finance/quoteSummary/${encodeURIComponent(symbol)}?modules=${modules}&crumb=${encodeURIComponent(crumb)}`;
      const r = await tryFetch(url, { 'User-Agent': UA, Cookie: cookie, Accept: 'application/json' }, 12000);
      const body = await r.text();
      if (body.indexOf('"quoteSummary"') !== -1) {
        return jsonResponse(200, body, { 'Cache-Control': 'public, max-age=120' });
      }
      _crumbCache = { crumb: null, cookie: null, ts: 0 };
    } catch (e) {
      _crumbCache = { crumb: null, cookie: null, ts: 0 };
    }
  }
  return jsonResponse(502, { error: 'quote fetch failed' });
}

exports.handler = async (event) => {
  if (event.httpMethod === 'OPTIONS') {
    return {
      statusCode: 204,
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type'
      },
      body: ''
    };
  }

  const path = (event.path || '').toLowerCase();
  const qs = event.queryStringParameters || {};

  if (path.endsWith('/search')) {
    const q = (qs.q || '').trim();
    if (!q) return jsonResponse(400, { error: 'missing q' });
    return await handleSearch(q);
  }
  if (path.endsWith('/quote')) {
    return await handleQuote((qs.symbol || '').trim());
  }
  // Default = chart
  return await handleChart(
    (qs.symbol || '').trim(),
    qs.range || '1y',
    qs.interval || '1d'
  );
};
