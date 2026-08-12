#!/usr/bin/env python3
"""Live Price Board — a standalone market dashboard: live quotes, candlestick
+ trend charts, per-symbol drill-down, a macro-signals tab grouped by region,
and a stock/macro correlation view.

Pulls quotes and history through Fincept Terminal's own market-data service
script (fincept-qt/scripts/yfinance_data.py — the same module
MarketDataService.cpp calls via its Python worker) instead of
re-implementing fetching, so this stays in sync with whatever data
source/fields the desktop app uses. Macro region groupings and the
correlation matrix live in regions.py; true statistical-release data (GDP
growth, inflation, unemployment — via fincept-qt/scripts/worldbank_data.py)
lives in econ_stats.py. Both are kept separate from yfinance_data.py so
this tool's extra surface never touches the production Qt app's script.

Run:
    python server.py [--port 8765] [--interval 5]

Then open http://127.0.0.1:8765 in a browser. The watchlist is editable in
the page and persisted to watchlist.json next to this script.

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
from urllib.parse import urlsplit, parse_qs

# ── Wire up Fincept Terminal's own quote/history service ───────────────────
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

import regions  # local module: macro groupings + correlation math
import econ_stats  # local module: World Bank statistical-release data (not stdlib `statistics` — name avoided deliberately)

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(THIS_DIR, "static")
DEFAULT_SYMBOLS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META", "JPM"]
WATCHLIST_FILE = os.path.join(THIS_DIR, "watchlist.json")
MAX_SYMBOLS = 50
# Same shape yfinance/Fincept tickers take: bare symbols, exchange suffixes
# (RELIANCE.NS), indices (^GSPC), futures (GC=F), crypto/forex (BTC-USD).
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^=]{1,15}$")
_VALID_PERIODS = {"1mo", "3mo", "6mo", "1y", "2y", "5y"}
_VALID_INTERVALS = {"1d", "1wk", "1mo"}
_SMA_WINDOW = 20


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
    board updates at the same cadence as the desktop app.
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


class TTLCache:
    """Generic keyed cache: recomputes a key's value at most once per `ttl_seconds`."""

    def __init__(self, ttl_seconds):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._entries = {}  # key -> (value, fetched_at)

    def get(self, key, fetch_fn, force=False):
        with self._lock:
            entry = self._entries.get(key)
            if not force and entry is not None and (time.time() - entry[1]) < self._ttl:
                return entry[0], entry[1]
        value = fetch_fn()
        now = time.time()
        with self._lock:
            self._entries[key] = (value, now)
        return value, now


def _compute_sma(closes, window):
    """Simple moving average over `closes`; None for indices before the window fills."""
    out = []
    total = 0.0
    for i, c in enumerate(closes):
        total += c
        if i >= window:
            total -= closes[i - window]
        if i >= window - 1:
            out.append(round(total / window, 4))
        else:
            out.append(None)
    return out


def _trend_label(closes, sma):
    """Coarse up/down/flat read: last close vs. its own SMA, plus the SMA's own slope."""
    if not closes or not sma or sma[-1] is None:
        return "flat"
    last_close = closes[-1]
    last_sma = sma[-1]
    if last_sma == 0:
        return "flat"
    delta_pct = (last_close - last_sma) / abs(last_sma) * 100
    if delta_pct > 0.5:
        return "up"
    if delta_pct < -0.5:
        return "down"
    return "flat"


def build_history_payload(symbol, period, interval):
    points = yfinance_data.get_historical_period(symbol, period, interval)
    if isinstance(points, dict):  # {"error": ..., "symbol": ...}
        return {"symbol": symbol, "period": period, "interval": interval, "candles": [], "sma20": [], "trend": "flat",
                "error": points.get("error", "no data")}
    if not points:
        return {"symbol": symbol, "period": period, "interval": interval, "candles": [], "sma20": [], "trend": "flat",
                "error": "no data"}
    closes = [p["close"] for p in points]
    sma = _compute_sma(closes, _SMA_WINDOW)
    return {
        "symbol": symbol, "period": period, "interval": interval,
        "candles": points, "sma20": sma, "trend": _trend_label(closes, sma),
    }


def build_detail_payload(symbol):
    quotes = yfinance_data.get_batch_quotes([symbol])
    quote = quotes[0] if quotes else {"symbol": symbol, "error": "no data"}
    info = yfinance_data.get_info(symbol)
    if isinstance(info, dict) and "error" in info:
        info = None
    return {"symbol": symbol, "quote": quote, "info": info}


def _parse_period_interval(qs):
    period = (qs.get("period", ["6mo"])[0] or "6mo").lower()
    interval = (qs.get("interval", ["1d"])[0] or "1d").lower()
    if period not in _VALID_PERIODS:
        period = "6mo"
    if interval not in _VALID_INTERVALS:
        interval = "1d"
    return period, interval


def make_handler(watchlist, quote_cache, history_cache, detail_cache, macro_cache, corr_cache, stats_cache,
                  refresh_interval_ms, macro_interval_ms):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FinceptLivePriceBoard/2.0"

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

        def _send_static(self, filename, content_type):
            path = os.path.join(STATIC_DIR, filename)
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self._send_json({"error": "not found"}, status=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if content_type != "text/html; charset=utf-8":
                self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            force = qs.get("force", ["0"])[0] == "1"

            if path in ("/", "/index.html"):
                self._send_static("index.html", "text/html; charset=utf-8")
            elif path == "/app.js":
                self._send_static("app.js", "application/javascript; charset=utf-8")
            elif path == "/styles.css":
                self._send_static("styles.css", "text/css; charset=utf-8")
            elif path == "/api/config":
                self._send_json({
                    "refresh_interval_ms": refresh_interval_ms,
                    "macro_interval_ms": macro_interval_ms,
                    "macro_groups": {region: [{"symbol": s, "name": n} for s, n in entries]
                                     for region, entries in regions.MACRO_GROUPS.items()},
                })
            elif path == "/api/quotes":
                symbols = watchlist.get()
                quotes, fetched_at = quote_cache.get(symbols, force=force)
                self._send_json({
                    "symbols": symbols, "quotes": quotes,
                    "fetched_at": datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat(),
                    "refresh_interval_ms": refresh_interval_ms,
                })
            elif path == "/api/watchlist":
                self._send_json({"symbols": watchlist.get()})
            elif path == "/api/history":
                symbol = (qs.get("symbol", [""])[0] or "").strip().upper()
                if not symbol or not _SYMBOL_RE.match(symbol):
                    self._send_json({"error": "missing or invalid 'symbol'"}, status=400)
                    return
                period, interval = _parse_period_interval(qs)
                key = (symbol, period, interval)
                payload, _ = history_cache.get(key, lambda: build_history_payload(symbol, period, interval), force=force)
                self._send_json(payload)
            elif path == "/api/detail":
                symbol = (qs.get("symbol", [""])[0] or "").strip().upper()
                if not symbol or not _SYMBOL_RE.match(symbol):
                    self._send_json({"error": "missing or invalid 'symbol'"}, status=400)
                    return
                payload, _ = detail_cache.get(symbol, lambda: build_detail_payload(symbol), force=force)
                self._send_json(payload)
            elif path == "/api/macro":
                def fetch_macro():
                    flat = regions.flat_macro_symbols()
                    quotes = yfinance_data.get_batch_quotes(flat)
                    by_symbol = {q.get("symbol"): q for q in quotes if isinstance(q, dict)}
                    out_groups = {}
                    for region_name, entries in regions.MACRO_GROUPS.items():
                        rows = []
                        for sym, name in entries:
                            q = by_symbol.get(sym, {"symbol": sym, "error": "no data"})
                            rows.append({**q, "symbol": sym, "name": name})
                        out_groups[region_name] = rows
                    return out_groups

                groups, fetched_at = macro_cache.get("all", fetch_macro, force=force)
                self._send_json({
                    "groups": groups,
                    "fetched_at": datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat(),
                    "refresh_interval_ms": macro_interval_ms,
                })
            elif path == "/api/correlation":
                period = (qs.get("period", ["6mo"])[0] or "6mo").lower()
                if period not in _VALID_PERIODS:
                    period = "6mo"
                stock_symbols = watchlist.get()
                macro_symbols = regions.flat_macro_symbols()
                key = (tuple(stock_symbols), period)
                payload, fetched_at = corr_cache.get(
                    key, lambda: regions.get_correlation_matrix(stock_symbols, macro_symbols, period), force=force)
                self._send_json({**payload, "fetched_at": datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat()})
            elif path == "/api/statistics":
                payload, fetched_at = stats_cache.get("all", econ_stats.get_statistics, force=force)
                self._send_json({**payload, "fetched_at": datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat()})
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
            quotes, fetched_at = quote_cache.get(updated, force=True)
            self._send_json({
                "symbols": updated, "quotes": quotes,
                "fetched_at": datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat(),
            })

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Standalone live market dashboard backed by Fincept Terminal's quote service.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on (default: 8765)")
    parser.add_argument(
        "--interval", type=float, default=5.0,
        help="watchlist refresh cadence in seconds (default: 5, matches MarketDataService's market:quote:* TTL)",
    )
    parser.add_argument(
        "--macro-interval", type=float, default=15.0,
        help="macro tab refresh cadence in seconds (default: 15 — a ~20-symbol batch, refreshed gentler than the watchlist)",
    )
    parser.add_argument(
        "--stats-interval", type=float, default=3600.0,
        help="statistical-release (World Bank) cache lifetime in seconds (default: 3600 — these update at most "
             "daily/annually, so re-querying every page load would just hammer the API for an unchanged answer)",
    )
    parser.add_argument(
        "--symbols", default=None,
        help="comma-separated initial symbol list, only used the first time watchlist.json is created",
    )
    args = parser.parse_args()

    watchlist = Watchlist(WATCHLIST_FILE)
    if args.symbols and not os.path.exists(WATCHLIST_FILE):
        watchlist.set(args.symbols.split(","))

    quote_cache = QuoteCache(ttl_seconds=args.interval)
    history_cache = TTLCache(ttl_seconds=60.0)
    detail_cache = TTLCache(ttl_seconds=30.0)
    macro_cache = TTLCache(ttl_seconds=args.macro_interval)
    corr_cache = TTLCache(ttl_seconds=60.0)
    stats_cache = TTLCache(ttl_seconds=args.stats_interval)

    handler = make_handler(
        watchlist, quote_cache, history_cache, detail_cache, macro_cache, corr_cache, stats_cache,
        refresh_interval_ms=int(args.interval * 1000), macro_interval_ms=int(args.macro_interval * 1000),
    )

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
