# Fincept Live Price Board

A small standalone web app for watching the live price of a chosen list of
stocks, separate from the fincept-qt desktop build. It doesn't reimplement
quote fetching — it imports and calls
[`fincept-qt/scripts/yfinance_data.py`](../../fincept-qt/scripts/yfinance_data.py)
directly, the same module `MarketDataService.cpp` uses (via `batch_quotes` /
`batch_all`) to drive the desktop app's watchlist and quote widgets.

## Run it

```bash
# from the repo root, with fincept-qt's Python deps installed
pip install -r fincept-qt/resources/requirements-numpy2.txt
python tools/live_price_board/server.py
```

Then open <http://127.0.0.1:8765>.

Options:

```bash
python tools/live_price_board/server.py --port 9000 --interval 5 --symbols AAPL,MSFT,TSLA
```

- `--port` — port to bind (default `8765`)
- `--interval` — refresh cadence in seconds (default `5`, matching the
  `market:quote:*` TTL `MarketDataService` uses in the desktop app)
- `--symbols` — comma-separated initial symbols, only applied the first time
  (i.e. when `watchlist.json` doesn't exist yet)

## What it does

- Shows a live-updating table: symbol, price, change, change %, high, low,
  volume — polling `/api/quotes` every `--interval` seconds.
- The symbol list is editable right in the page (comma-separated input +
  **Update List**) and persisted to `watchlist.json` next to `server.py`, so
  it survives a restart.
- **Refresh Now** forces an immediate re-fetch, bypassing the cache.
- Server-side quotes are cached for `--interval` seconds so multiple browser
  tabs polling at once don't multiply the number of upstream Yahoo Finance
  calls.

## Notes

- Pure standard library on the server side (`http.server`) — the only
  dependencies are the ones `fincept-qt/scripts` already requires
  (`yfinance`, `pandas`). No Flask/Django needed.
- Symbols are validated against the same shape yfinance/Fincept tickers take
  (`AAPL`, `RELIANCE.NS`, `^GSPC`, `BTC-USD`, `GC=F`, …) and capped at 50 per
  list.
- Binds to `127.0.0.1` by default — pass `--host 0.0.0.0` only if you
  specifically want it reachable from other machines on your network.
