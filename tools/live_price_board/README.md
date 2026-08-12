# Fincept Live Market Dashboard

A standalone local web app — three tabs (Watchlist, Macro, Correlation) for
tracking live prices, candlestick/trend charts, per-symbol drill-down, and
macro-vs-stock correlation — separate from the fincept-qt desktop build.

It doesn't reimplement quote/history fetching: it imports and calls
[`fincept-qt/scripts/yfinance_data.py`](../../fincept-qt/scripts/yfinance_data.py)
directly, the same module `MarketDataService.cpp` uses (via `batch_quotes` /
`batch_all` / `historical_period` / `info`) to drive the desktop app's
watchlist and quote widgets. Region groupings and the correlation matrix
live in `regions.py`, kept separate so this tool's extra surface never
touches the production Qt app's script.

## Run it

```bash
# from the repo root, with fincept-qt's Python deps installed
pip install -r fincept-qt/resources/requirements-numpy2.txt
python tools/live_price_board/server.py
```

Then open <http://127.0.0.1:8765>.

Options:

```bash
python tools/live_price_board/server.py --port 9000 --interval 5 --macro-interval 15 --symbols AAPL,MSFT,TSLA
```

- `--port` — port to bind (default `8765`)
- `--interval` — Watchlist tab refresh cadence in seconds (default `5`,
  matching the `market:quote:*` TTL `MarketDataService` uses in the desktop
  app)
- `--macro-interval` — Macro tab refresh cadence in seconds (default `15` —
  it's a ~20-symbol batch, refreshed gentler than the watchlist)
- `--symbols` — comma-separated initial watchlist, only applied the first
  time (i.e. when `watchlist.json` doesn't exist yet)

## Tabs

**Watchlist** — live table (price, change, %, high, low, volume) for an
editable symbol list, persisted to `watchlist.json`. **Refresh Now** forces
an immediate re-fetch, bypassing the cache.

**Macro** — market-based macro proxies (equity indices, bond-yield tickers,
FX, commodities) grouped by region: US, EU, Baltics, Asia, Global. These are
*market* signals (what indices/yields/FX are actually doing right now), not
official statistical releases (CPI/GDP/NFP) — `fincept-qt/scripts` also has
`fred_data.py` / `ecb_data.py` / `worldbank_data.py` / `eurostat_data.py` for
that, but those need separate API keys and a different call shape, so
they're not wired in here. Baltic coverage (OMX Tallinn/Riga/Vilnius) is
best-effort — thin/illiquid tickers may come back with no data, same as any
unresolvable symbol elsewhere in the app.

**Correlation** — Pearson correlation of daily returns between every
watchlist stock and every macro signal above, over a selectable lookback
(1mo–2y). Rendered as a heatmap plus a "top 3 drivers" card per stock. Reads
as "when this macro factor has moved, this stock has historically moved
…" — a statistical tendency from history, not a forecast. Computed on
demand (tab open / period change / **Recompute**), not auto-polled — a
~30-symbol batch history download isn't something to run every 5 seconds.

## Click any row for detail

Clicking a symbol (in Watchlist or Macro) opens a panel with:
- Full quote stats (price, change, open/high/low, prev close, volume,
  exchange)
- A candlestick chart with a 20-period SMA trend line, period switcher
  (1M/3M/6M/1Y/2Y), drawn on `<canvas>` — no charting library, just JS
- Fundamentals (sector, industry, market cap, P/E, forward P/E, dividend
  yield, beta, 52W high/low, avg volume, country, currency) when available —
  omitted for symbols with no fundamentals (indices, FX, futures)

## Notes

- Pure standard library on the server side (`http.server`) and vanilla JS
  on the client (candlestick chart included) — the only dependencies are
  the ones `fincept-qt/scripts` already requires (`yfinance`, `pandas`). No
  Flask/Django, no charting library, no build step.
- Symbols are validated against the same shape yfinance/Fincept tickers take
  (`AAPL`, `RELIANCE.NS`, `^GSPC`, `BTC-USD`, `GC=F`, …) and the watchlist is
  capped at 50 symbols.
- Binds to `127.0.0.1` by default — pass `--host 0.0.0.0` only if you
  specifically want it reachable from other machines on your network.
- Each tab caches server-side (Watchlist/Macro: their refresh interval;
  history/detail: 30–60s; correlation: 60s) so multiple open browser tabs,
  or flipping between tabs, don't multiply upstream Yahoo Finance calls.
