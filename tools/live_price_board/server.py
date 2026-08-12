#!/usr/bin/env python3
"""Live Price Board — a standalone web UI for tracking a chosen list of stocks.

Pulls quotes through Fincept Terminal's own market-data service script
(fincept-qt/scripts/yfinance_data.py — the same module MarketDataService.cpp
calls via its Python worker) instead of re-implementing quote fetching, so
this stays in sync with whatever data source/fields the desktop app uses.

Run:
    python server.py [--port 8765] [--interval 5]

Then open http://127.0.0.1:8765 in a browser. The symbol list is editable
in the page and persisted to watchlist.json next to this script.

No dependencies beyond what fincept-qt/scripts already requires (yfinance,
pandas) — the server itself is stdlib-only (http.server), so there's no new
framework to install.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Wire up Fincept Terminal's own quote service ────────────────────────────
_SCRIPTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "fincept-qt", "scripts")
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

try:
    import yfinance_data  # fincept-qt/scripts/yfinance_data.py
except ImportError as exc:  # pragma: no cover - startup guard, not a runtime path
    sys.stderr.write(
        "Could not import fincept-qt/scripts/yfinance_data.py "
        f"(looked in {_SCRIPTS_DIR}): {exc}\n"
        "Install its dependencies first: pip install -r fincept-qt/resources/requirements-numpy2.txt\n"
    )
    raise

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META", "JPM"]
WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
MAX_SYMBOLS = 50
# Same shape yfinance/Fincept tickers take: bare symbols, exchange suffixes
# (RELIANCE.NS), indices (^GSPC), futures (GC=F), crypto/forex (BTC-USD).
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^=]{1,15}$")


class Watchlist:
    """Thread-safe, disk-persisted symbol list."""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._symbols = self._load()

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            symbols = _sanitize_symbols(data.get("symbols", []))
            if symbols:
                return symbols
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return list(DEFAULT_SYMBOLS)

    def _save(self):
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"symbols": self._symbols}, f, indent=2)
        os.replace(tmp_path, self._path)

    def get(self):
        with self._lock:
            return list(self._symbols)

    def set(self, symbols):
        clean = _sanitize_symbols(symbols)
        if not clean:
            raise ValueError("watchlist must contain at least one valid symbol")
        with self._lock:
            self._symbols = clean
            self._save()
        return clean


def _sanitize_symbols(raw):
    seen = set()
    out = []
    for s in raw:
        if not isinstance(s, str):
            continue
        sym = s.strip().upper()
        if not sym or sym in seen or not _SYMBOL_RE.match(sym):
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= MAX_SYMBOLS:
            break
    return out


class QuoteCache:
    """Refetches at most once per `ttl_seconds`, shared across all pollers.

    Mirrors the 5s TTL MarketDataService uses for market:quote:* so this
    standalone board updates at the same cadence as the desktop app.
    """

    def __init__(self, ttl_seconds):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._symbols_key = None
        self._quotes = []
        self._fetched_at = 0.0

    def get(self, symbols, force=False):
        key = tuple(symbols)
        with self._lock:
            age = time.time() - self._fetched_at
            if not force and key == self._symbols_key and age < self._ttl:
                return self._quotes, self._fetched_at
        # Fetch outside the lock — yfinance does its own network I/O and
        # shouldn't block other requests from reading the still-valid cache.
        quotes = yfinance_data.get_batch_quotes(list(symbols)) if symbols else []
        by_symbol = {q.get("symbol"): q for q in quotes if isinstance(q, dict)}
        ordered = [by_symbol.get(s, {"symbol": s, "error": "no data"}) for s in symbols]
        now = time.time()
        with self._lock:
            self._symbols_key = key
            self._quotes = ordered
            self._fetched_at = now
        return ordered, now


def make_handler(watchlist, cache, refresh_interval_ms):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FinceptLivePriceBoard/1.0"

        def log_message(self, fmt, *args):  # quieter default logging
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send_html(_render_page(refresh_interval_ms))
            elif self.path.startswith("/api/quotes"):
                force = "force=1" in self.path
                symbols = watchlist.get()
                quotes, fetched_at = cache.get(symbols, force=force)
                self._send_json(
                    {
                        "symbols": symbols,
                        "quotes": quotes,
                        "fetched_at": datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat(),
                        "refresh_interval_ms": refresh_interval_ms,
                    }
                )
            elif self.path == "/api/watchlist":
                self._send_json({"symbols": watchlist.get()})
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self):
            if self.path != "/api/watchlist":
                self._send_json({"error": "not found"}, status=404)
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0 or length > 64 * 1024:
                self._send_json({"error": "invalid request body"}, status=400)
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
                symbols = payload.get("symbols", [])
                if not isinstance(symbols, list):
                    raise ValueError("symbols must be a list")
                updated = watchlist.set(symbols)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            # Force the next /api/quotes call to fetch fresh data for the
            # new list instead of serving a stale cache keyed to the old one.
            quotes, fetched_at = cache.get(updated, force=True)
            self._send_json(
                {
                    "symbols": updated,
                    "quotes": quotes,
                    "fetched_at": datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat(),
                }
            )

    return Handler


def _render_page(refresh_interval_ms):
    # Colors mirror fincept-qt's Obsidian theme (ThemeManager.cpp) so this
    # standalone board looks like it belongs to the same product.
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fincept Live Price Board</title>
<style>
  :root {
    --bg-base: #080808; --bg-surface: #0a0a0a; --bg-raised: #111111; --bg-hover: #161616;
    --border-dim: #1a1a1a; --border-bright: #333333;
    --text-primary: #e5e5e5; --text-secondary: #808080; --text-tertiary: #525252;
    --accent: #d97706; --positive: #16a34a; --negative: #dc2626;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg-base); color: var(--text-primary);
    font-family: Consolas, "Courier New", monospace; font-size: 14px;
  }
  header {
    display: flex; align-items: center; gap: 12px; padding: 10px 16px;
    background: var(--bg-raised); border-bottom: 1px solid var(--border-dim);
  }
  header h1 {
    font-size: 13px; font-weight: 700; letter-spacing: 0.5px; margin: 0;
    color: var(--accent); text-transform: uppercase;
  }
  #status { font-size: 11px; color: var(--text-tertiary); margin-left: auto; }
  #status.stale { color: var(--negative); }
  main { padding: 16px; max-width: 960px; margin: 0 auto; }
  .editbar { display: flex; gap: 8px; margin-bottom: 14px; }
  .editbar input {
    flex: 1; background: var(--bg-base); color: var(--text-primary);
    border: 1px solid var(--border-dim); padding: 6px 8px; font: inherit;
  }
  .editbar input:focus { outline: none; border-color: var(--border-bright); }
  button {
    background: rgba(217,119,6,0.1); color: var(--accent); border: 1px solid #78350f;
    padding: 6px 14px; font: inherit; font-weight: 700; cursor: pointer;
  }
  button:hover { background: var(--accent); color: #fff; }
  button.ghost { background: var(--bg-raised); color: var(--text-secondary); border-color: var(--border-dim); }
  button.ghost:hover { background: var(--bg-hover); color: var(--text-primary); }
  table { width: 100%; border-collapse: collapse; background: var(--bg-surface); }
  th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--border-dim); }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--text-tertiary); font-size: 11px; font-weight: 700; text-transform: uppercase; }
  td.symbol { font-weight: 700; }
  td.name { color: var(--text-secondary); text-align: left; font-size: 12px; }
  td.pos { color: var(--positive); }
  td.neg { color: var(--negative); }
  td.err { color: var(--text-tertiary); font-style: italic; }
  #hint { color: var(--text-tertiary); font-size: 11px; margin-top: 10px; }
</style>
</head>
<body>
<header>
  <h1>Fincept &middot; Live Price Board</h1>
  <span id="status">loading&hellip;</span>
</header>
<main>
  <div class="editbar">
    <input id="symbols-input" placeholder="AAPL, MSFT, TSLA, RELIANCE.NS, BTC-USD ..." autocomplete="off">
    <button id="update-btn">Update List</button>
    <button id="refresh-btn" class="ghost">Refresh Now</button>
  </div>
  <table id="quote-table">
    <thead>
      <tr>
        <th>Symbol</th><th class="name">Name</th><th>Price</th><th>Change</th>
        <th>Chg %</th><th>High</th><th>Low</th><th>Volume</th>
      </tr>
    </thead>
    <tbody id="quote-body"></tbody>
  </table>
  <p id="hint">Auto-refreshes every REFRESH_SECONDS s &mdash; same cadence as the Fincept Terminal desktop app's live quote widgets.</p>
</main>
<script>
const REFRESH_MS = REFRESH_INTERVAL_MS;
const statusEl = document.getElementById('status');
const bodyEl = document.getElementById('quote-body');
const inputEl = document.getElementById('symbols-input');
let lastFetchedAt = null;

function fmt(n, digits) {
  if (n === null || n === undefined || Number.isNaN(n)) return '--';
  return Number(n).toLocaleString(undefined, {minimumFractionDigits: digits, maximumFractionDigits: digits});
}
function fmtVolume(n) {
  if (n === null || n === undefined) return '--';
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (abs >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(2) + 'K';
  return String(n);
}

function render(data) {
  bodyEl.textContent = '';
  for (const q of data.quotes) {
    const tr = document.createElement('tr');
    if (q.error) {
      const symTd = document.createElement('td');
      symTd.className = 'symbol';
      symTd.textContent = q.symbol || '';
      tr.appendChild(symTd);
      const errTd = document.createElement('td');
      errTd.className = 'err';
      errTd.colSpan = 7;
      errTd.textContent = 'no data';
      tr.appendChild(errTd);
      bodyEl.appendChild(tr);
      continue;
    }
    const changeCls = (q.change ?? 0) >= 0 ? 'pos' : 'neg';
    const cells = [
      {cls: 'symbol', text: q.symbol ?? ''},
      {cls: 'name', text: q.exchange ?? ''},
      {cls: '', text: fmt(q.price, 2)},
      {cls: changeCls, text: (q.change >= 0 ? '+' : '') + fmt(q.change, 2)},
      {cls: changeCls, text: (q.change_percent >= 0 ? '+' : '') + fmt(q.change_percent, 2) + '%'},
      {cls: '', text: fmt(q.high, 2)},
      {cls: '', text: fmt(q.low, 2)},
      {cls: '', text: fmtVolume(q.volume)},
    ];
    for (const c of cells) {
      const td = document.createElement('td');
      if (c.cls) td.className = c.cls;
      td.textContent = c.text;
      tr.appendChild(td);
    }
    bodyEl.appendChild(tr);
  }
  lastFetchedAt = new Date(data.fetched_at);
  statusEl.classList.remove('stale');
  statusEl.textContent = 'updated ' + lastFetchedAt.toLocaleTimeString();
  if (!document.activeElement || document.activeElement !== inputEl) {
    inputEl.value = data.symbols.join(', ');
  }
}

async function loadQuotes(force) {
  try {
    const res = await fetch('/api/quotes' + (force ? '?force=1' : ''));
    if (!res.ok) throw new Error('http ' + res.status);
    render(await res.json());
  } catch (e) {
    statusEl.classList.add('stale');
    statusEl.textContent = 'refresh failed: ' + e.message;
  }
}

async function updateWatchlist() {
  const symbols = inputEl.value.split(',').map(s => s.trim()).filter(Boolean);
  if (!symbols.length) return;
  try {
    const res = await fetch('/api/watchlist', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbols}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('http ' + res.status));
    render(data);
  } catch (e) {
    statusEl.classList.add('stale');
    statusEl.textContent = 'update failed: ' + e.message;
  }
}

document.getElementById('update-btn').addEventListener('click', updateWatchlist);
document.getElementById('refresh-btn').addEventListener('click', () => loadQuotes(true));
inputEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') updateWatchlist(); });

loadQuotes(false);
setInterval(() => loadQuotes(false), REFRESH_MS);
</script>
</body>
</html>
""".replace("REFRESH_INTERVAL_MS", str(refresh_interval_ms)).replace(
        "REFRESH_SECONDS", str(round(refresh_interval_ms / 1000, 1))
    )


def main():
    parser = argparse.ArgumentParser(description="Standalone live price board backed by Fincept Terminal's quote service.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on (default: 8765)")
    parser.add_argument(
        "--interval", type=float, default=5.0,
        help="refresh cadence in seconds (default: 5, matches MarketDataService's market:quote:* TTL)",
    )
    parser.add_argument(
        "--symbols", default=None,
        help="comma-separated initial symbol list, only used the first time watchlist.json is created",
    )
    args = parser.parse_args()

    watchlist = Watchlist(WATCHLIST_FILE)
    if args.symbols and not os.path.exists(WATCHLIST_FILE):
        watchlist.set(args.symbols.split(","))

    cache = QuoteCache(ttl_seconds=args.interval)
    handler = make_handler(watchlist, cache, refresh_interval_ms=int(args.interval * 1000))

    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Fincept Live Price Board serving at {url}  (Ctrl+C to stop)")
    print(f"Tracking: {', '.join(watchlist.get())}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
