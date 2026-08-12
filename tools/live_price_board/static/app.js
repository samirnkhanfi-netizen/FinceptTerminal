"use strict";

// ── Shared state ────────────────────────────────────────────────────────
let refreshMs = 5000;
let macroMs = 15000;
let activeTab = "watchlist";
let macroLoadedOnce = false;
let corrLoadedOnce = false;
let statsLoadedOnce = false;
let macroNameBySymbol = {};
let currentDetailSymbol = null;
let currentDetailPeriod = "6mo";

// ── Formatting helpers ──────────────────────────────────────────────────
function fmt(n, digits) {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  return Number(n).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function fmtCompact(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  const abs = Math.abs(n);
  if (abs >= 1e12) return (n / 1e12).toFixed(2) + "T";
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(2) + "K";
  return String(n);
}
function pctClass(v) {
  return (v ?? 0) >= 0 ? "pos" : "neg";
}
function el(tag, opts) {
  const node = document.createElement(tag);
  if (opts) {
    if (opts.cls) node.className = opts.cls;
    if (opts.text !== undefined) node.textContent = opts.text;
    if (opts.title) node.title = opts.title;
    if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  }
  return node;
}

// ── Tabs ─────────────────────────────────────────────────────────────────
function setupTabs() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.tab;
      if (tab === activeTab) return;
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + tab));
      activeTab = tab;
      if (tab === "macro" && !macroLoadedOnce) loadMacro(false);
      if (tab === "macro" && !statsLoadedOnce) loadStatistics(false);
      if (tab === "correlation" && !corrLoadedOnce) loadCorrelation(false);
    });
  });
}

// ── Watchlist tab ────────────────────────────────────────────────────────
const statusEl = () => document.getElementById("status");
const bodyEl = () => document.getElementById("quote-body");
const inputEl = () => document.getElementById("symbols-input");

function quoteRow(q, extraCols) {
  const tr = el("tr");
  tr.dataset.symbol = q.symbol || "";
  if (q.error) {
    tr.appendChild(el("td", { cls: "symbol", text: q.symbol || "" }));
    const errTd = el("td", { cls: "err", text: "no data" });
    errTd.colSpan = extraCols;
    tr.appendChild(errTd);
    return tr;
  }
  return tr;
}

function renderWatchlist(data) {
  const body = bodyEl();
  body.textContent = "";
  for (const q of data.quotes) {
    if (q.error) {
      body.appendChild(quoteRow(q, 7));
      continue;
    }
    const tr = el("tr");
    tr.dataset.symbol = q.symbol || "";
    const changeCls = pctClass(q.change);
    tr.appendChild(el("td", { cls: "symbol", text: q.symbol ?? "" }));
    tr.appendChild(el("td", { cls: "col-name", text: q.exchange ?? "" }));
    tr.appendChild(el("td", { text: fmt(q.price, 2) }));
    tr.appendChild(el("td", { cls: changeCls, text: (q.change >= 0 ? "+" : "") + fmt(q.change, 2) }));
    tr.appendChild(el("td", { cls: changeCls, text: (q.change_percent >= 0 ? "+" : "") + fmt(q.change_percent, 2) + "%" }));
    tr.appendChild(el("td", { text: fmt(q.high, 2) }));
    tr.appendChild(el("td", { text: fmt(q.low, 2) }));
    tr.appendChild(el("td", { text: fmtCompact(q.volume) }));
    body.appendChild(tr);
  }
  const fetchedAt = new Date(data.fetched_at);
  statusEl().classList.remove("stale");
  statusEl().textContent = "updated " + fetchedAt.toLocaleTimeString();
  if (document.activeElement !== inputEl()) {
    inputEl().value = data.symbols.join(", ");
  }
}

async function loadQuotes(force) {
  try {
    const res = await fetch("/api/quotes" + (force ? "?force=1" : ""));
    if (!res.ok) throw new Error("http " + res.status);
    renderWatchlist(await res.json());
  } catch (e) {
    statusEl().classList.add("stale");
    statusEl().textContent = "refresh failed: " + e.message;
  }
}

async function updateWatchlist() {
  const symbols = inputEl().value.split(",").map((s) => s.trim()).filter(Boolean);
  if (!symbols.length) return;
  try {
    const res = await fetch("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "http " + res.status);
    renderWatchlist(data);
    corrLoadedOnce = false; // watchlist changed — correlation matrix is now stale
  } catch (e) {
    statusEl().classList.add("stale");
    statusEl().textContent = "update failed: " + e.message;
  }
}

// ── Macro tab ────────────────────────────────────────────────────────────
async function loadMacro(force) {
  try {
    const res = await fetch("/api/macro" + (force ? "?force=1" : ""));
    if (!res.ok) throw new Error("http " + res.status);
    const data = await res.json();
    renderMacro(data);
    macroLoadedOnce = true;
  } catch (e) {
    console.error("macro refresh failed", e);
  }
}

function renderMacro(data) {
  const container = document.getElementById("macro-groups");
  container.textContent = "";
  for (const [region, rows] of Object.entries(data.groups)) {
    const wrap = el("div", { cls: "macro-group" });
    wrap.appendChild(el("h2", { text: region }));
    const table = el("table", { cls: "data-table clickable" });
    const thead = el("thead");
    const headRow = el("tr");
    ["Signal", "Name", "Price", "Change", "Chg %"].forEach((h, i) =>
      headRow.appendChild(el("th", { cls: i === 1 ? "col-name" : "", text: h }))
    );
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const q of rows) {
      const tr = el("tr");
      tr.dataset.symbol = q.symbol || "";
      if (q.error) {
        tr.appendChild(el("td", { cls: "symbol", text: q.symbol || "" }));
        const errTd = el("td", { cls: "err", text: "no data" });
        errTd.colSpan = 4;
        tr.appendChild(errTd);
        tbody.appendChild(tr);
        continue;
      }
      const changeCls = pctClass(q.change);
      tr.appendChild(el("td", { cls: "symbol", text: q.symbol ?? "" }));
      tr.appendChild(el("td", { cls: "col-name", text: q.name ?? "" }));
      tr.appendChild(el("td", { text: fmt(q.price, 2) }));
      tr.appendChild(el("td", { cls: changeCls, text: (q.change >= 0 ? "+" : "") + fmt(q.change, 2) }));
      tr.appendChild(el("td", { cls: changeCls, text: (q.change_percent >= 0 ? "+" : "") + fmt(q.change_percent, 2) + "%" }));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    container.appendChild(wrap);
  }
}

// ── Statistical releases (World Bank) ────────────────────────────────────
async function loadStatistics(force) {
  const statusEl2 = document.getElementById("stats-status");
  statusEl2.textContent = "loading…";
  try {
    const res = await fetch("/api/statistics" + (force ? "?force=1" : ""));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "http " + res.status);
    renderStatistics(data);
    statsLoadedOnce = true;
    const asOf = new Date(data.fetched_at).toLocaleTimeString();
    statusEl2.textContent = data.error ? `${data.source} — fetched ${asOf} (partial: ${data.error})` : `${data.source} — fetched ${asOf}`;
  } catch (e) {
    statusEl2.textContent = "failed: " + e.message;
  }
}

function renderStatistics(data) {
  const container = document.getElementById("stats-groups");
  container.textContent = "";
  for (const [region, rows] of Object.entries(data.groups)) {
    const wrap = el("div", { cls: "stats-group" });
    wrap.appendChild(el("h3", { text: region }));
    const table = el("table", { cls: "data-table" });
    const thead = el("thead");
    const headRow = el("tr");
    headRow.appendChild(el("th", { text: "Country" }));
    for (const ind of data.indicator_labels) headRow.appendChild(el("th", { text: ind.label }));
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const row of rows) {
      const tr = el("tr");
      tr.appendChild(el("td", { cls: "symbol", text: row.country_name }));
      for (const ind of row.indicators) {
        const td = el("td");
        if (ind.value === null || ind.value === undefined) {
          td.className = "stat-null";
          td.textContent = "—";
        } else {
          td.appendChild(el("span", { text: fmt(ind.value, 1) + (ind.unit || "") }));
          if (ind.date) td.appendChild(el("span", { cls: "stat-date", text: "as of " + ind.date }));
        }
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    container.appendChild(wrap);
  }
}

// ── Correlation tab ──────────────────────────────────────────────────────
function corrColor(v) {
  if (v === null || v === undefined) return null;
  const t = Math.min(1, Math.abs(v));
  // Sage / terracotta, matching --positive/--negative in styles.css.
  const rgb = v >= 0 ? "142,168,149" : "192,128,111";
  return `rgba(${rgb},${(0.12 + 0.45 * t).toFixed(2)})`;
}
function corrStrength(v) {
  const a = Math.abs(v);
  if (a >= 0.7) return "strong";
  if (a >= 0.4) return "moderate";
  if (a >= 0.2) return "weak";
  return "negligible";
}

async function loadCorrelation(force) {
  const statusSpan = document.getElementById("corr-status");
  statusSpan.textContent = "loading…";
  const period = document.getElementById("corr-period").value;
  try {
    const res = await fetch(`/api/correlation?period=${encodeURIComponent(period)}${force ? "&force=1" : ""}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "http " + res.status);
    renderCorrelation(data);
    corrLoadedOnce = true;
    statusSpan.textContent = "as of " + new Date(data.fetched_at).toLocaleTimeString();
  } catch (e) {
    statusSpan.textContent = "failed: " + e.message;
  }
}

function renderCorrelation(data) {
  const wrap = document.getElementById("corr-table-wrap");
  wrap.textContent = "";
  if (!data.stocks.length) {
    wrap.appendChild(el("p", { cls: "hint", text: "Watchlist is empty — nothing to correlate." }));
    return;
  }
  const table = el("table", { cls: "heatmap" });
  const thead = el("thead");
  const headRow = el("tr");
  headRow.appendChild(el("th", { text: "" }));
  for (const m of data.macros) headRow.appendChild(el("th", { text: m, title: macroNameBySymbol[m] || m }));
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = el("tbody");
  for (const row of data.matrix) {
    const tr = el("tr");
    tr.appendChild(el("td", { cls: "row-label", text: row.symbol }));
    for (const m of data.macros) {
      const v = row.correlations[m];
      const td = el("td", { cls: v === null ? "corr-empty" : "corr-cell", text: v === null ? "—" : v.toFixed(2) });
      if (v !== null) {
        td.style.background = corrColor(v);
        td.title = `${row.symbol} vs ${macroNameBySymbol[m] || m}: r=${v.toFixed(2)} (${corrStrength(v)} ${v >= 0 ? "positive" : "negative"})`;
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);

  const driversEl = document.getElementById("corr-drivers");
  driversEl.textContent = "";
  for (const stock of data.stocks) {
    const drivers = (data.top_drivers && data.top_drivers[stock]) || [];
    const card = el("div", { cls: "driver-card" });
    card.appendChild(el("h3", { text: stock }));
    const ul = el("ul");
    if (!drivers.length) {
      ul.appendChild(el("li", { text: "no data" }));
    } else {
      for (const d of drivers) {
        const li = el("li");
        li.appendChild(el("span", { text: `${d.name} (${d.symbol})` }));
        li.appendChild(el("span", { cls: "r-val " + pctClass(d.r), text: (d.r >= 0 ? "+" : "") + d.r.toFixed(2) }));
        ul.appendChild(li);
      }
    }
    card.appendChild(ul);
    driversEl.appendChild(card);
  }
}

// ── Candlestick + trend chart ────────────────────────────────────────────
function drawChart(canvas, candles, sma) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, w, h);
  if (!candles || !candles.length) {
    ctx.fillStyle = "#8a8272";
    ctx.font = "13px Karla, sans-serif";
    ctx.fillText("No historical data available", 16, h / 2);
    return;
  }

  const pad = { left: 54, right: 12, top: 12, bottom: 22 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;
  const highs = candles.map((c) => c.high).filter((v) => v !== null && v !== undefined);
  const lows = candles.map((c) => c.low).filter((v) => v !== null && v !== undefined);
  const smaVals = (sma || []).filter((v) => v !== null && v !== undefined);
  let maxV = Math.max(...highs, ...(smaVals.length ? smaVals : [-Infinity]));
  let minV = Math.min(...lows, ...(smaVals.length ? smaVals : [Infinity]));
  if (!isFinite(maxV) || !isFinite(minV)) return;
  const range = maxV - minV || Math.max(1, maxV * 0.02);
  maxV += range * 0.06;
  minV -= range * 0.06;

  const n = candles.length;
  const slot = plotW / n;
  const candleW = Math.max(1, Math.min(9, slot * 0.62));
  const yFor = (v) => pad.top + plotH - ((v - minV) / (maxV - minV)) * plotH;
  const xFor = (i) => pad.left + slot * i + slot / 2;

  // gridlines + y-axis labels
  ctx.strokeStyle = "#f0ebdf";
  ctx.fillStyle = "#8a8272";
  ctx.font = "10px Karla, sans-serif";
  const ticks = 5;
  for (let t = 0; t <= ticks; t++) {
    const v = minV + ((maxV - minV) * t) / ticks;
    const y = yFor(v);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(w - pad.right, y);
    ctx.stroke();
    ctx.fillText(v.toFixed(2), 2, y + 3);
  }

  // candles
  for (let i = 0; i < n; i++) {
    const c = candles[i];
    if (c.open === null || c.close === null || c.high === null || c.low === null) continue;
    const x = xFor(i);
    const up = c.close >= c.open;
    ctx.strokeStyle = ctx.fillStyle = up ? "#8ea895" : "#c0806f";
    ctx.beginPath();
    ctx.moveTo(x, yFor(c.high));
    ctx.lineTo(x, yFor(c.low));
    ctx.stroke();
    const yOpen = yFor(c.open), yClose = yFor(c.close);
    const top = Math.min(yOpen, yClose);
    const bodyH = Math.max(1, Math.abs(yClose - yOpen));
    ctx.fillRect(x - candleW / 2, top, candleW, bodyH);
  }

  // SMA trend line
  ctx.strokeStyle = "#a8825e";
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  let started = false;
  for (let i = 0; i < n; i++) {
    const v = sma ? sma[i] : null;
    if (v === null || v === undefined) continue;
    const x = xFor(i), y = yFor(v);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
  ctx.lineWidth = 1;

  // x-axis date labels
  ctx.fillStyle = "#8a8272";
  ctx.textAlign = "left";
  ctx.fillText(new Date(candles[0].timestamp * 1000).toLocaleDateString(), pad.left, h - 4);
  ctx.textAlign = "right";
  ctx.fillText(new Date(candles[n - 1].timestamp * 1000).toLocaleDateString(), w - pad.right, h - 4);
  ctx.textAlign = "left";
}

// ── Detail overlay ────────────────────────────────────────────────────────
function statCell(label, value, cls) {
  const cell = el("div", { cls: "stat-cell" });
  cell.appendChild(el("span", { cls: "label", text: label }));
  cell.appendChild(el("span", { cls: "value" + (cls ? " " + cls : ""), text: value }));
  return cell;
}

function renderDetailQuote(quote) {
  const grid = document.getElementById("detail-quote-grid");
  grid.textContent = "";
  if (!quote || quote.error) {
    grid.appendChild(el("p", { cls: "hint", text: "No live quote available for this symbol." }));
    return;
  }
  const changeCls = pctClass(quote.change);
  grid.appendChild(statCell("Price", fmt(quote.price, 2)));
  grid.appendChild(statCell("Change", (quote.change >= 0 ? "+" : "") + fmt(quote.change, 2), changeCls));
  grid.appendChild(statCell("Chg %", (quote.change_percent >= 0 ? "+" : "") + fmt(quote.change_percent, 2) + "%", changeCls));
  grid.appendChild(statCell("Open", fmt(quote.open, 2)));
  grid.appendChild(statCell("High", fmt(quote.high, 2)));
  grid.appendChild(statCell("Low", fmt(quote.low, 2)));
  grid.appendChild(statCell("Prev Close", fmt(quote.previous_close, 2)));
  grid.appendChild(statCell("Volume", fmtCompact(quote.volume)));
  if (quote.exchange) grid.appendChild(statCell("Exchange", quote.exchange));
}

function renderDetailInfo(info) {
  const grid = document.getElementById("detail-info-grid");
  grid.textContent = "";
  if (!info) return; // macro symbols / ETFs often have no fundamentals — just omit the section
  const rows = [
    ["Sector", info.sector], ["Industry", info.industry],
    ["Market Cap", info.market_cap ? fmtCompact(info.market_cap) : null],
    ["P/E", info.pe_ratio ? fmt(info.pe_ratio, 2) : null],
    ["Forward P/E", info.forward_pe ? fmt(info.forward_pe, 2) : null],
    ["Dividend Yield", info.dividend_yield ? fmt(info.dividend_yield * 100, 2) + "%" : null],
    ["Beta", info.beta ? fmt(info.beta, 2) : null],
    ["52W High", info.fifty_two_week_high ? fmt(info.fifty_two_week_high, 2) : null],
    ["52W Low", info.fifty_two_week_low ? fmt(info.fifty_two_week_low, 2) : null],
    ["Avg Volume", info.average_volume ? fmtCompact(info.average_volume) : null],
    ["Country", info.country], ["Currency", info.currency],
  ];
  for (const [label, value] of rows) {
    if (value === null || value === undefined || value === "N/A" || value === "") continue;
    grid.appendChild(statCell(label, String(value)));
  }
}

async function loadDetailInfo(symbol) {
  try {
    const res = await fetch(`/api/detail?symbol=${encodeURIComponent(symbol)}`);
    const data = await res.json();
    document.getElementById("detail-name").textContent = data.info && data.info.company_name ? data.info.company_name : "";
    renderDetailQuote(data.quote);
    renderDetailInfo(data.info);
  } catch (e) {
    console.error("detail fetch failed", e);
  }
}

async function loadDetailChart(symbol, period) {
  currentDetailPeriod = period;
  document.querySelectorAll("#detail-period-picker button").forEach((b) => b.classList.toggle("active", b.dataset.period === period));
  try {
    const res = await fetch(`/api/history?symbol=${encodeURIComponent(symbol)}&period=${encodeURIComponent(period)}&interval=1d`);
    const data = await res.json();
    drawChart(document.getElementById("detail-chart"), data.candles, data.sma20);
    const badge = document.getElementById("detail-trend");
    badge.className = "trend-badge " + (data.trend || "flat");
    badge.textContent = data.error ? "no data" : (data.trend || "flat") + " trend";
  } catch (e) {
    console.error("history fetch failed", e);
  }
}

function openDetail(symbol) {
  if (!symbol) return;
  currentDetailSymbol = symbol;
  document.getElementById("detail-symbol").textContent = symbol;
  document.getElementById("detail-name").textContent = "";
  document.getElementById("detail-quote-grid").textContent = "";
  document.getElementById("detail-info-grid").textContent = "";
  document.getElementById("detail-overlay").classList.remove("hidden");
  loadDetailInfo(symbol);
  loadDetailChart(symbol, "6mo");
}

function closeDetail() {
  document.getElementById("detail-overlay").classList.add("hidden");
  currentDetailSymbol = null;
}

function setupDetailOverlay() {
  document.getElementById("detail-close").addEventListener("click", closeDetail);
  document.getElementById("detail-overlay").addEventListener("click", (e) => {
    if (e.target.id === "detail-overlay") closeDetail();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDetail();
  });
  document.querySelectorAll("#detail-period-picker button").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (currentDetailSymbol) loadDetailChart(currentDetailSymbol, btn.dataset.period);
    });
  });
}

function setupRowClicks() {
  // Event delegation: both the watchlist table and every macro-group table
  // (rebuilt on each refresh) route row clicks through here.
  document.addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-symbol]");
    if (!tr || !tr.dataset.symbol) return;
    // Ignore clicks that originated inside the edit bar's own controls.
    if (e.target.closest("input, select, button")) return;
    openDetail(tr.dataset.symbol);
  });
}

// ── Boot ─────────────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    const data = await res.json();
    refreshMs = data.refresh_interval_ms || refreshMs;
    macroMs = data.macro_interval_ms || macroMs;
    document.getElementById("wl-interval-s").textContent = String(Math.round(refreshMs / 1000));
    macroNameBySymbol = {};
    for (const entries of Object.values(data.macro_groups || {})) {
      for (const { symbol, name } of entries) macroNameBySymbol[symbol] = name;
    }
  } catch (e) {
    console.error("config fetch failed", e);
  }
}

async function boot() {
  setupTabs();
  setupDetailOverlay();
  setupRowClicks();
  document.getElementById("update-btn").addEventListener("click", updateWatchlist);
  document.getElementById("refresh-btn").addEventListener("click", () => loadQuotes(true));
  inputEl().addEventListener("keydown", (e) => {
    if (e.key === "Enter") updateWatchlist();
  });
  document.getElementById("corr-recompute").addEventListener("click", () => loadCorrelation(true));
  document.getElementById("corr-period").addEventListener("change", () => loadCorrelation(true));
  document.getElementById("stats-refresh").addEventListener("click", () => loadStatistics(true));

  await loadConfig();
  loadQuotes(false);
  setInterval(() => loadQuotes(false), refreshMs);
  setInterval(() => {
    if (activeTab === "macro") loadMacro(false);
  }, macroMs);
}

boot();
