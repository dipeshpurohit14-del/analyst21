# Your Analyst — Live Yahoo Finance + Feature Expansion

## Status (2026-01)

### Original problem
Live data was not loading on https://youranalyst.netlify.app/ — fundamentally because the deployed JS had zero fetch calls (everything hardcoded) and the supposed CORS proxy (`corsproxy.io`) had moved to a paid plan.

### Current implementation (`/app/dist/`)
**Files:**
- `index.html` — patched static site with all features below
- `netlify/functions/yahoo.js` — Netlify serverless function (proxy to query1.finance.yahoo.com)
- `netlify.toml` — wires `/api/yahoo` → the function

**Backend mirror (preview env):**
- `/app/backend/server.py` has the same `/api/yahoo` proxy endpoint (so the in-cluster preview URL works end-to-end). Uses allorigins.win first (since the pod IP is often Yahoo-rate-limited), then direct Yahoo, with retries.

## Features delivered

### Bug fix (initial)
- Real Yahoo Finance integration (chart endpoint)
- LIVE badge shows real status (FETCHING / LIVE · HH:MM / OFFLINE · cached)
- Real 1-year price chart replaces random walk

### Feature expansion (this iteration)
1. **16 additional NSE stocks** — full set of 24 in `SEARCH_LIST` now supported (price, volume, 52W, chart, market cap all live). Fundamentals (P/E, ROE, 5Y financials) shown as "—" for these since Yahoo's chart endpoint doesn't return them.
2. **Auto-refresh** — re-fetches every 60s while user is on a stock page; pauses when tab hidden; reads "last updated" timestamp on the LIVE badge.
3. **Chart range toggles** — 1M / 3M / 6M / 1Y / 5Y buttons; each triggers a fresh Yahoo fetch with the appropriate interval (5y uses weekly bars).
4. **Live market cap** — computed as `livePrice × shares` so it stays correct after stock splits.
5. **Share button** — uses Web Share API on mobile, falls back to clipboard copy with "✓ Copied" feedback. Deep-link format `?s=SYM` or `#SYM`.
6. **Currency formatting** — all amounts now in Crore (with Indian commas e.g. `₹3,80,866 Cr`). No more T/B units.
7. **Deep link auto-load** — `analyst.html#TITAN` opens that stock directly; `hashchange` listener handles back/forward.

## Verified
Preview URL: https://ticker-debug-1.preview.emergentagent.com/analyst.html
End-to-end browser test confirmed:
- TITAN deep-link → ₹4,290 (+16.11%), Market Cap ₹3,80,866 Cr, 52W ₹3,303–₹4,605
- Share button visible & works
- 3M range tab activates and chart re-renders as "3-Month Price Chart · Live"

## Known limitations
- **Fundamentals for 16 new stocks** still show "—" (P/E, ROE, 5Y revenue). Yahoo's `quoteSummary` module provides these but requires a crumb/cookie auth flow — deferred.
- **Preview environment occasionally 502s** for some symbols due to our pod IP being rate-limited by Yahoo + transient allorigins.win flakiness. On Netlify production this won't happen (edge IP pool is huge).

## Deploy
Three files in `/app/dist/`:
- `index.html`
- `netlify.toml`
- `netlify/functions/yahoo.js`

## Backlog
- P1: Pull true `quoteSummary` fundamentals (P/E, ROE, EPS, 5Y financials) for the 16 new stocks
- P2: Persist last-viewed stock in localStorage for instant restore
- P2: Add Comparison page support for any-vs-any (currently only the 8 stocks with full fundamentals can be radar-compared)
