// Netlify serverless function: proxies Yahoo Finance chart API.
// Solves browser CORS restriction with a resilient multi-strategy fetch chain.
//
// Usage from frontend:
//   /.netlify/functions/yahoo?symbol=RELIANCE.NS&range=1y&interval=1d

const SYMBOL_RE = /^[A-Z0-9.\-^=]{1,20}$/i;
const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

async function tryFetch(url, headers, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: ctrl.signal, headers });
    if (!r.ok) throw new Error('http ' + r.status);
    const txt = await r.text();
    if (txt.indexOf('"chart"') === -1) throw new Error('no chart in response');
    return txt;
  } finally {
    clearTimeout(timer);
  }
}

exports.handler = async (event) => {
  const cors = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Cache-Control': 'public, max-age=60'
  };

  if (event.httpMethod === 'OPTIONS') {
    return { statusCode: 204, headers: cors, body: '' };
  }

  const qs = event.queryStringParameters || {};
  const symbol = (qs.symbol || '').trim();
  const range = qs.range || '1y';
  const interval = qs.interval || '1d';

  if (!SYMBOL_RE.test(symbol)) {
    return {
      statusCode: 400,
      headers: { ...cors, 'Content-Type': 'application/json' },
      body: JSON.stringify({ error: 'Invalid or missing symbol' })
    };
  }

  const direct =
    `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}` +
    `?interval=${encodeURIComponent(interval)}&range=${encodeURIComponent(range)}&includePrePost=false`;
  const allorigins =
    'https://api.allorigins.win/raw?url=' + encodeURIComponent(direct);

  // Netlify edge IPs are not rate-limited by Yahoo, so direct goes first.
  // Allorigins fallback covers the rare 5xx from Yahoo.
  const strategies = [
    { url: direct, headers: { 'User-Agent': UA, 'Accept': 'application/json,text/plain,*/*' }, timeout: 9000 },
    { url: allorigins, headers: { 'Accept': 'application/json' }, timeout: 16000 },
    { url: direct, headers: { 'User-Agent': UA, 'Accept': 'application/json,text/plain,*/*' }, timeout: 9000 },
    { url: allorigins, headers: { 'Accept': 'application/json' }, timeout: 16000 }
  ];

  let lastErr;
  for (const s of strategies) {
    try {
      const body = await tryFetch(s.url, s.headers, s.timeout);
      return {
        statusCode: 200,
        headers: { ...cors, 'Content-Type': 'application/json' },
        body
      };
    } catch (e) {
      lastErr = e;
    }
  }

  return {
    statusCode: 502,
    headers: { ...cors, 'Content-Type': 'application/json' },
    body: JSON.stringify({ error: 'All fetch strategies failed', detail: String(lastErr && lastErr.message || lastErr) })
  };
};
