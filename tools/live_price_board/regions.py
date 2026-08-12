"""Macro/region definitions + correlation math for the Live Price Board.

Kept separate from fincept-qt/scripts/yfinance_data.py (the production Qt
app's own service module) so this standalone tool's extra surface — region
groupings, batch history downloads for correlation — never risks the
desktop app. It uses the same `yfinance`/`pandas` dependencies fincept-qt
already requires, following the same batching patterns yfinance_data.py
uses (see get_batch_quotes / _resolve_for_history there).

"Macro signals" here are market-based proxies (indices, sovereign-yield
tickers, FX, commodities) available through yfinance — not official
statistical releases (CPI, GDP, NFP). fincept-qt/scripts also has
fred_data.py / ecb_data.py / worldbank_data.py / eurostat_data.py for that
kind of series data, but those need separate API keys (FRED, BEA) or
different call shapes; wiring those in is future work, not required for a
live "what's moving right now" board.
"""

import os
import time

import pandas as pd
import yfinance as yf

try:
    _NET_TIMEOUT = float(os.environ.get("FINCEPT_YF_TIMEOUT", "15"))
except (TypeError, ValueError):
    _NET_TIMEOUT = 15.0

# symbol -> display name. Grouped by region for the Macro tab.
# Baltics coverage on Yahoo Finance is thin/illiquid (OMX Tallinn/Riga/Vilnius
# tickers) — entries here are best-effort and may come back with no data; the
# UI shows those the same way the watchlist shows an unresolvable symbol.
MACRO_GROUPS = {
    "US": [
        ("^GSPC", "S&P 500"),
        ("^DJI", "Dow Jones Industrial Average"),
        ("^IXIC", "Nasdaq Composite"),
        ("^VIX", "CBOE Volatility Index"),
        ("^TNX", "US 10Y Treasury Yield"),
        ("DX-Y.NYB", "US Dollar Index"),
    ],
    "EU": [
        ("^STOXX50E", "Euro Stoxx 50"),
        ("^GDAXI", "Germany DAX"),
        ("^FCHI", "France CAC 40"),
        ("^FTSE", "UK FTSE 100"),
        ("EURUSD=X", "EUR/USD"),
    ],
    "Baltics": [
        ("TAL1T.TL", "Tallink Grupp (Tallinn)"),
        ("LHV1T.TL", "LHV Group (Tallinn)"),
        ("IGN1L.VS", "Ignitis Grupė (Vilnius)"),
        ("SAB1L.VS", "Šiaulių Bankas (Vilnius)"),
        ("GRD1R.RG", "Grindeks (Riga)"),
        ("EURUSD=X", "EUR/USD"),
    ],
    "Asia": [
        ("^N225", "Japan Nikkei 225"),
        ("^HSI", "Hong Kong Hang Seng"),
        ("000001.SS", "Shanghai Composite"),
        ("^NSEI", "India Nifty 50"),
        ("USDJPY=X", "USD/JPY"),
        ("USDCNY=X", "USD/CNY"),
    ],
    "Global": [
        ("GC=F", "Gold"),
        ("CL=F", "Crude Oil WTI"),
        ("BTC-USD", "Bitcoin"),
    ],
}


def flat_macro_symbols():
    """All macro symbols across every region, de-duplicated, order-preserving."""
    seen = set()
    out = []
    for entries in MACRO_GROUPS.values():
        for sym, _name in entries:
            if sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out


def macro_name(symbol):
    for entries in MACRO_GROUPS.values():
        for sym, name in entries:
            if sym == symbol:
                return name
    return symbol


def _batch_download_closes(symbols, period):
    """Download `period` of daily closes for every symbol in one HTTP round-trip.

    Returns {symbol: pandas.Series of closes indexed by date}. Symbols with
    no usable data are simply omitted — callers treat that the same as any
    other missing-data case.
    """
    import io
    import contextlib

    if not symbols:
        return {}

    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        data = yf.download(
            symbols, period=period, interval="1d", group_by="ticker",
            auto_adjust=True, progress=False, threads=True, timeout=_NET_TIMEOUT,
        )

    if data is None or data.empty:
        return {}

    single = len(symbols) == 1
    closes = {}
    for sym in symbols:
        try:
            series = data["Close"] if single else data[sym]["Close"]
            series = series.dropna()
            if not series.empty:
                closes[sym] = series
        except Exception:
            continue
    return closes


def get_correlation_matrix(stock_symbols, macro_symbols, period="6mo"):
    """Pearson correlation of daily returns: each stock vs. each macro symbol.

    Returns:
      {
        "period": str, "as_of": unix_ts,
        "stocks": [str], "macros": [str],
        "matrix": [{"symbol": str, "correlations": {macro_symbol: float|None}}],
        "top_drivers": {stock_symbol: [{"symbol", "name", "r"}, ...]}  # |r| desc, top 3
      }
    A macro or stock symbol with no resolvable history simply yields None for
    every cell touching it, rather than dropping the whole matrix — a single
    illiquid Baltic ticker shouldn't blank the rest of the board.
    """
    stock_symbols = list(dict.fromkeys(stock_symbols))
    macro_symbols = list(dict.fromkeys(macro_symbols))
    all_symbols = list(dict.fromkeys(stock_symbols + macro_symbols))

    closes = _batch_download_closes(all_symbols, period)
    if not closes:
        return {
            "period": period, "as_of": int(time.time()),
            "stocks": stock_symbols, "macros": macro_symbols,
            "matrix": [{"symbol": s, "correlations": {m: None for m in macro_symbols}} for s in stock_symbols],
            "top_drivers": {s: [] for s in stock_symbols},
            "error": "no historical data returned",
        }

    df = pd.DataFrame(closes).sort_index().ffill()
    returns = df.pct_change().dropna(how="all")
    corr = returns.corr()

    matrix = []
    top_drivers = {}
    for s in stock_symbols:
        row = {}
        drivers = []
        for m in macro_symbols:
            v = None
            if s in corr.index and m in corr.columns:
                raw = corr.loc[s, m]
                if not pd.isna(raw):
                    v = round(float(raw), 3)
            row[m] = v
            if v is not None:
                drivers.append({"symbol": m, "name": macro_name(m), "r": v})
        drivers.sort(key=lambda d: abs(d["r"]), reverse=True)
        matrix.append({"symbol": s, "correlations": row})
        top_drivers[s] = drivers[:3]

    return {
        "period": period, "as_of": int(time.time()),
        "stocks": stock_symbols, "macros": macro_symbols,
        "matrix": matrix, "top_drivers": top_drivers,
    }
