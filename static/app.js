/* ============================================================
   Stock Reversal Scanner – Frontend Logic (Full Market + Options)
   ============================================================ */

let scanData = [];
let currentFilter = "all";
let scanMode = "watchlist";
let pollTimer = null;
let hideTimeout = null;
let userWatchlist = [];

// ── Format helpers ─────────────────────────────────────────

function fmtVolume(v) {
  if (v >= 1_000_000_000) return (v / 1_000_000_000).toFixed(1) + "B";
  if (v >= 1_000_000)     return (v / 1_000_000).toFixed(1) + "M";
  if (v >= 1_000)         return (v / 1_000).toFixed(0) + "K";
  return v.toString();
}

function rsiClass(rsi) {
  if (rsi <= 30) return "low";
  if (rsi >= 70) return "high";
  return "mid";
}

function fmtEta(seconds) {
  if (seconds <= 0) return "";
  if (seconds < 60) return `~${seconds}s left`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `~${m}m ${s}s left`;
}

function updateModeDesc() {
  const desc = document.getElementById("modeDesc");
  const subtitle = document.getElementById("headerSubtitle");
  if (desc) desc.textContent = "Scans your watchlist tickers using all criteria: Reversals, 3σ/2σ Bands, RSI Divergence & Options Exhaustion";
  if (subtitle) subtitle.textContent = "All Criteria × Your Custom Watchlist";
}

async function loadLastWatchlistScan() {
  const scanBtn = document.getElementById("scanBtn");
  if (scanBtn) scanBtn.querySelector(".scan-btn__text").textContent = "📋  Scan Watchlist";
  try {
    showSkeleton();
    const res = await fetch("/api/scan/watchlist/results");
    if (res.ok) {
      const data = await res.json();
      if (data.ok && data.results) {
        displayResults(data);
        return;
      }
    }
  } catch (e) {
    console.error("No saved Watchlist scan available yet");
  }

  document.getElementById("results").innerHTML = `
    <div class="empty-state">
      <div class="empty-state__icon">📋</div>
      <div class="empty-state__title">Ready to scan</div>
      <div class="empty-state__text">Tap scan to analyze your watchlist tickers<br>using all criteria (Reversals, Sigma Bands, RSI & Options)</div>
    </div>
  `;
  hideAuxUI();
}

function hideAuxUI() {
  document.getElementById("statsBar").classList.add("hidden");
  document.getElementById("timestamp").classList.add("hidden");
  document.getElementById("filters").classList.add("hidden");
  document.getElementById("scanBadge").classList.add("hidden");
}

// ── Skeleton loader ────────────────────────────────────────

function showSkeleton() {
  const el = document.getElementById("results");
  el.innerHTML = `
    <div class="skeleton">
      ${[1,2,3,4].map((_, i) => `
        <div class="skeleton-card" style="animation-delay:${i * 0.15}s">
          <div class="skeleton-line skeleton-line--short"></div>
          <div class="skeleton-line skeleton-line--long"></div>
          <div class="skeleton-line skeleton-line--med skeleton-line--last"></div>
        </div>
      `).join("")}
    </div>
  `;
}

// ── Build a single card ────────────────────────────────────

function buildCard(item, index) {
  const parseSignals = (str) => {
    if (!str || str === "—") return [];
    // Remove outer brackets, then split by pipe
    const inner = str.replace(/^\[/, '').replace(/\]$/, '');
    return inner.split(" | ").map(s => s.trim()).filter(s => s);
  };

  const parsePatterns = (str) => {
    if (!str || str === "—") return [];
    return str.split(" | ").map(s => s.trim()).filter(s => s);
  };

  const bullish = parseSignals(item["Bullish Signals"]);
  const bearish = parseSignals(item["Bearish Signals"]);
  const patternsList = parsePatterns(item.Patterns);

  const rsi = item.RSI;
  const rsiCls = rsiClass(rsi);

  const maxVol = Math.max(...scanData.map(d => d.Volume), 1);
  const volPct = Math.round((item.Volume / maxVol) * 100);

  const grade = item.Grade || "B";
  const score = item.Score || 0;
  const gradeCls = grade === "A+" ? "grade--aplus" : grade === "A" ? "grade--a" : "grade--b";

  const makePill = (s, type) => {
    if (s.startsWith("News:") && item["News Details"]) {
      const encoded = encodeURIComponent(JSON.stringify(item["News Details"]));
      return `<span class="pill pill--${type} pill--clickable" onclick="openNewsModal('${encoded}')" style="cursor: pointer; text-decoration: underline;">${s}</span>`;
    }
    return `<span class="pill pill--${type}">${s}</span>`;
  };

  const bullPills = bullish.map(s => makePill(s, "bull")).join("");
  const bearPills = bearish.map(s => makePill(s, "bear")).join("");

  const bullIcon = "🟢";
  const bearIcon = "🔴";

  // Build Technical Grid
  const rvolVal = item.RVOL !== undefined ? `${item.RVOL.toFixed(1)}x` : "—";
  const rvolClass = item.RVOL >= 1.5 ? "tech-chip__value--green" : "";
  const adrVal = item.ADR !== undefined ? `${item.ADR.toFixed(1)}%` : "—";
  
  const bbVal = item.BB_Pct !== undefined ? `${Math.round(item.BB_Pct)}%` : "—";
  const bbClass = (item.BB_Pct <= 10 || item.BB_Pct >= 90) ? (item.BB_Pct <= 10 ? "tech-chip__value--green" : "tech-chip__value--red") : "";

  const ema20DistVal = item.EMA20_Dist !== undefined ? `${item.EMA20_Dist > 0 ? '+' : ''}${item.EMA20_Dist.toFixed(1)}%` : "—";
  const ema20Class = item.EMA20_Dist > 0 ? "tech-chip__value--green" : "tech-chip__value--red";

  const sma200DistVal = item.SMA200_Dist !== undefined ? `${item.SMA200_Dist > 0 ? '+' : ''}${item.SMA200_Dist.toFixed(1)}%` : "—";
  const sma200Class = item.SMA200_Dist > 0 ? "tech-chip__value--green" : "tech-chip__value--red";

  const squeezeVal = item.Squeeze ? `<span class="tech-chip__value--squeeze-on">ON 🔥</span>` : "OFF";
  const squeezeCls = item.Squeeze ? "tech-chip--squeeze-on" : "";

  const techGridHtml = `
    <div class="card__tech-grid">
      <div class="tech-chip">
        <span class="tech-chip__label">RVOL</span>
        <span class="tech-chip__value ${rvolClass}">${rvolVal}</span>
      </div>
      <div class="tech-chip">
        <span class="tech-chip__label">ADR</span>
        <span class="tech-chip__value">${adrVal}</span>
      </div>
      <div class="tech-chip ${squeezeCls}">
        <span class="tech-chip__label">Squeeze</span>
        <span class="tech-chip__value">${squeezeVal}</span>
      </div>
      <div class="tech-chip">
        <span class="tech-chip__label">BB %B</span>
        <span class="tech-chip__value ${bbClass}">${bbVal}</span>
      </div>
      <div class="tech-chip">
        <span class="tech-chip__label">20 EMA</span>
        <span class="tech-chip__value ${ema20Class}">${ema20DistVal}</span>
      </div>
      <div class="tech-chip">
        <span class="tech-chip__label">200 SMA</span>
        <span class="tech-chip__value ${sma200Class}">${sma200DistVal}</span>
      </div>
    </div>
  `;

  const patternBadges = patternsList.length ? `
    <div class="card__patterns">
      ${patternsList.map(pat => `<span class="pattern-badge">📐 ${pat}</span>`).join("")}
    </div>
  ` : "";

  return `
    <div class="card" style="animation-delay: ${Math.min(index * 0.04, 1.2)}s">
      <div class="card__top">
        <div class="card__ticker-wrap">
          <div class="card__ticker card__ticker--clickable" onclick="openChartModal('${item.Ticker}')" title="Click to view chart">${item.Ticker}</div>
          <button class="btn-chart" onclick="openChartModal('${item.Ticker}')" title="Open Interactive Chart">📈 Chart</button>
          <div class="grade-badge ${gradeCls}">${grade} <span class="grade-badge__score">(${score}pts)</span></div>
        </div>
        <div class="card__price">$${item["Last Price"].toFixed(2)}</div>
      </div>
      <div class="card__meta">
        <div class="card__meta-item">
          📊 <span>${fmtVolume(item.Volume)}</span>
          <div class="vol-bar"><div class="vol-bar__fill" style="width:${volPct}%"></div></div>
        </div>
        <div class="card__meta-item">
          RSI <span>${rsi !== null ? rsi.toFixed(1) : "N/A"}</span>
          <div class="rsi-gauge">
            <div class="rsi-gauge__fill rsi-gauge__fill--${rsiCls}" style="width:${rsi ?? 50}%"></div>
          </div>
        </div>
      </div>
      ${techGridHtml}
      ${patternBadges}
      <div class="card__signals">
        ${bullish.length ? `<div class="signal-row"><span class="signal-row__icon">${bullIcon}</span>${bullPills}</div>` : ""}
        ${bearish.length ? `<div class="signal-row"><span class="signal-row__icon">${bearIcon}</span>${bearPills}</div>` : ""}
      </div>
      ${item["Stop Loss"] ? `
        <div class="card__trade-levels">
          <div class="trade-level">
            <span class="trade-level__label">Entry</span>
            <span class="trade-level__value trade-level__value--entry">$${item["Entry"]?.toFixed(2)}</span>
          </div>
          <div class="trade-level">
            <span class="trade-level__label">Stop Loss</span>
            <span class="trade-level__value trade-level__value--sl">$${item["Stop Loss"]?.toFixed(2)}</span>
          </div>
          <div class="trade-level">
            <span class="trade-level__label">Target</span>
            <span class="trade-level__value trade-level__value--target">$${item["Profit Target"]?.toFixed(2)}</span>
          </div>
        </div>
      ` : ""}
      ${item["Option Play"] && typeof item["Option Play"] === "object" ? `
        <div class="card__option">
          <div class="option-tag">SUGGESTED OPTION PLAY</div>
          <div class="option-card-layout">
            <div class="option-card-header">
              <span class="option-card-type option-card-type--${(item["Option Play"].type || 'CALL').toLowerCase()}">${item["Option Play"].type || 'CALL'}</span>
              <span class="option-card-strike">$${item["Option Play"].strike || '—'} Strike</span>
              <span class="option-card-exp">${item["Option Play"].exp || '—'} (${item["Option Play"].dte || '0'}d DTE)</span>
            </div>
            <div class="option-card-body">
              <div class="option-card-metric">
                <span class="option-metric-label">Premium:</span>
                <span class="option-metric-value">${item["Option Play"].mid !== undefined ? '@$' + item["Option Play"].mid.toFixed(2) : '—'}</span>
              </div>
              <div class="option-card-metric">
                <span class="option-metric-label">IV:</span>
                <span class="option-metric-value option-metric-value--iv">${item["Option Play"].iv !== undefined ? item["Option Play"].iv + '%' : '—'}</span>
              </div>
              <div class="option-card-symbol">${item["Option Play"].symbol || ''}</div>
            </div>
          </div>
        </div>
      ` : (item["Suggested Option"] && item["Suggested Option"] !== "—" ? `
        <div class="card__option">
          <div class="option-tag">TRADE IDEA</div>
          <div class="option-val">${item["Suggested Option"]}</div>
        </div>
      ` : "")}
    </div>
  `;
}

// ── Build an options card ───────────────────────────────────

function buildOptionsCard(item, index) {
  const isBullish = item.Direction === "Bullish";
  const dirIcon = isBullish ? "🟢" : "🔴";
  const dirClass = isBullish ? "opts-dir--bull" : "opts-dir--bear";
  const typeClass = isBullish ? "opts-type--call" : "opts-type--put";

  const makeOptPill = (s) => {
    const type = isBullish ? 'bull' : 'bear';
    if (s.startsWith("News:") && item["News Details"]) {
      const encoded = encodeURIComponent(JSON.stringify(item["News Details"]));
      return `<span class="pill pill--${type} pill--clickable" onclick="openNewsModal('${encoded}')" style="cursor: pointer; text-decoration: underline;">${s}</span>`;
    }
    return `<span class="pill pill--${type}">${s}</span>`;
  };

  const parsePatterns = (str) => {
    if (!str || str === "—") return [];
    return str.split(" | ").map(s => s.trim()).filter(s => s);
  };

  const catalystPills = (item["Catalyst Tags"] || "").split(" | ").filter(s => s).map(makeOptPill).join("");
  const patternsList = parsePatterns(item.Patterns);

  const flowBadge = item["Unusual Flow"] ? `
    <div class="opts-flow-badge">
      <span class="opts-flow-badge__icon">🔥</span>
      <span>Unusual Flow${item["Flow Detail"] ? " · " + item["Flow Detail"] : ""}</span>
    </div>
  ` : "";

  const ivRankVal = item["IV Rank Value"];
  const ivRankDisplay = item["IV Rank"] || "Building...";
  let ivRankClass = "opts-iv--building";
  if (ivRankVal >= 0 && ivRankVal <= 15) ivRankClass = "opts-iv--low";
  else if (ivRankVal > 15 && ivRankVal <= 30) ivRankClass = "opts-iv--med";
  else if (ivRankVal > 30) ivRankClass = "opts-iv--high";

  const catScore = item["Catalyst Score"] || 0;
  let catGrade = "B";
  if (catScore >= 6) catGrade = "A+";
  else if (catScore >= 4) catGrade = "A";
  const gradeCls = catGrade === "A+" ? "grade--aplus" : catGrade === "A" ? "grade--a" : "grade--b";

  // Build Technical Grid
  const rvolVal = item.RVOL !== undefined ? `${item.RVOL.toFixed(1)}x` : "—";
  const rvolClass = item.RVOL >= 1.5 ? "tech-chip__value--green" : "";
  const adrVal = item.ADR !== undefined ? `${item.ADR.toFixed(1)}%` : "—";
  
  const bbVal = item.BB_Pct !== undefined ? `${Math.round(item.BB_Pct)}%` : "—";
  const bbClass = (item.BB_Pct <= 10 || item.BB_Pct >= 90) ? (item.BB_Pct <= 10 ? "tech-chip__value--green" : "tech-chip__value--red") : "";

  const ema20DistVal = item.EMA20_Dist !== undefined ? `${item.EMA20_Dist > 0 ? '+' : ''}${item.EMA20_Dist.toFixed(1)}%` : "—";
  const ema20Class = item.EMA20_Dist > 0 ? "tech-chip__value--green" : "tech-chip__value--red";

  const sma200DistVal = item.SMA200_Dist !== undefined ? `${item.SMA200_Dist > 0 ? '+' : ''}${item.SMA200_Dist.toFixed(1)}%` : "—";
  const sma200Class = item.SMA200_Dist > 0 ? "tech-chip__value--green" : "tech-chip__value--red";

  const squeezeVal = item.Squeeze ? `<span class="tech-chip__value--squeeze-on">ON 🔥</span>` : "OFF";
  const squeezeCls = item.Squeeze ? "tech-chip--squeeze-on" : "";

  const techGridHtml = `
    <div class="card__tech-grid" style="margin-top: 14px;">
      <div class="tech-chip">
        <span class="tech-chip__label">RVOL</span>
        <span class="tech-chip__value ${rvolClass}">${rvolVal}</span>
      </div>
      <div class="tech-chip">
        <span class="tech-chip__label">ADR</span>
        <span class="tech-chip__value">${adrVal}</span>
      </div>
      <div class="tech-chip ${squeezeCls}">
        <span class="tech-chip__label">Squeeze</span>
        <span class="tech-chip__value">${squeezeVal}</span>
      </div>
      <div class="tech-chip">
        <span class="tech-chip__label">BB %B</span>
        <span class="tech-chip__value ${bbClass}">${bbVal}</span>
      </div>
      <div class="tech-chip">
        <span class="tech-chip__label">20 EMA</span>
        <span class="tech-chip__value ${ema20Class}">${ema20DistVal}</span>
      </div>
      <div class="tech-chip">
        <span class="tech-chip__label">200 SMA</span>
        <span class="tech-chip__value ${sma200Class}">${sma200DistVal}</span>
      </div>
    </div>
  `;

  const patternBadges = patternsList.length ? `
    <div class="card__patterns" style="margin-top: 10px;">
      ${patternsList.map(pat => `<span class="pattern-badge">📐 ${pat}</span>`).join("")}
    </div>
  ` : "";

  return `
    <div class="card opts-card" style="animation-delay: ${Math.min(index * 0.04, 1.2)}s">
      <div class="card__top">
        <div class="card__ticker-wrap">
          <div class="card__ticker card__ticker--clickable" onclick="openChartModal('${item.Ticker}')" title="Click to view chart">${item.Ticker}</div>
          <button class="btn-chart" onclick="openChartModal('${item.Ticker}')" title="Open Interactive Chart">📈 Chart</button>
          <div class="opts-dir-badge ${dirClass}">${dirIcon} ${item.Direction}</div>
          <div class="grade-badge ${gradeCls}">${catGrade} <span class="grade-badge__score">(${catScore}pts)</span></div>
        </div>
        <div class="card__price">$${item["Last Price"].toFixed(2)}</div>
      </div>

      <div class="opts-contract">
        <div class="opts-contract__tag">CONTRACT</div>
        <div class="opts-contract__details">
          <span class="opts-contract__type ${typeClass}">${item.Type}</span>
          <span class="opts-contract__strike">$${item.Strike}</span>
          <span class="opts-contract__exp">${item.Exp}</span>
        </div>
        <div class="opts-contract__price">@$${item.Mid.toFixed(2)}</div>
      </div>

      <div class="opts-metrics">
        <div class="opts-metric">
          <div class="opts-metric__value">${item.DTE}d</div>
          <div class="opts-metric__label">DTE</div>
        </div>
        <div class="opts-metric">
          <div class="opts-metric__value">${item["Est Delta"].toFixed(2)}Δ</div>
          <div class="opts-metric__label">Delta</div>
        </div>
        <div class="opts-metric">
          <div class="opts-metric__value ${ivRankClass}">${ivRankDisplay}</div>
          <div class="opts-metric__label">IV Rank</div>
        </div>
        <div class="opts-metric">
          <div class="opts-metric__value">${item.IV}%</div>
          <div class="opts-metric__label">IV</div>
        </div>
      </div>

      <div class="opts-liquidity">
        <div class="opts-liq-item">
          <span class="opts-liq-label">Vol</span>
          <span class="opts-liq-value">${fmtVolume(item.Volume)}</span>
        </div>
        <div class="opts-liq-item">
          <span class="opts-liq-label">OI</span>
          <span class="opts-liq-value">${fmtVolume(item.OI)}</span>
        </div>
        <div class="opts-liq-item">
          <span class="opts-liq-label">Spread</span>
          <span class="opts-liq-value">${item.Spread}</span>
        </div>
        <div class="opts-liq-item">
          <span class="opts-liq-label">Bid/Ask</span>
          <span class="opts-liq-value">$${item.Bid.toFixed(2)}/$${item.Ask.toFixed(2)}</span>
        </div>
      </div>

      ${flowBadge}
      ${techGridHtml}
      ${patternBadges}

      <div class="card__signals">
        <div class="signal-row">
          <span class="signal-row__icon">⚡</span>
          ${catalystPills}
        </div>
      </div>
      ${item["Stop Loss"] ? `
        <div class="card__trade-levels">
          <div class="trade-level">
            <span class="trade-level__label">Entry</span>
            <span class="trade-level__value trade-level__value--entry">$${item["Entry"]?.toFixed(2)}</span>
          </div>
          <div class="trade-level">
            <span class="trade-level__label">Stop Loss</span>
            <span class="trade-level__value trade-level__value--sl">$${item["Stop Loss"]?.toFixed(2)}</span>
          </div>
          <div class="trade-level">
            <span class="trade-level__label">Target</span>
            <span class="trade-level__value trade-level__value--target">$${item["Profit Target"]?.toFixed(2)}</span>
          </div>
        </div>
      ` : ""}
    </div>
  `;
}

// ── Build a tight spreads option card ─────────────────────

function buildOptionsSpreadsCard(item, index) {
  const isCall = item.Type === "CALL";
  const typeClass = isCall ? "opts-type--call" : "opts-type--put";
  const spreadPct = item["Spread (%)"] !== undefined ? item["Spread (%)"] : 0;
  const spreadDollar = item["Spread ($)"] !== undefined ? item["Spread ($)"] : 0;

  return `
    <div class="card card--option" style="animation-delay: ${index * 0.05}s">
      <div class="card__header">
        <div class="card__symbol-group">
          <span class="card__symbol card__symbol--clickable" onclick="openChartModal('${item.Ticker}')">${item.Ticker}</span>
          <span class="opts-type ${typeClass}">${item.Type}</span>
          <span class="pill pill--bull">Spread: ${spreadPct}% ($${spreadDollar.toFixed(2)})</span>
        </div>
        <div class="card__score-badge grade--aplus">
          ${item.DTE}d DTE
        </div>
      </div>

      <div class="opts-card-details" style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 12px; background: rgba(15, 23, 42, 0.6); padding: 12px; border-radius: 8px; border: 1px solid rgba(255, 255, 255, 0.05);">
        <div class="opts-detail-row">
          <span class="opts-detail-label" style="font-size: 0.75rem; color: #94a3b8; display: block;">Strike / Exp</span>
          <span class="opts-detail-val" style="color: #f8fafc; font-weight: 600;">$${item.Strike} (${item.Expiration})</span>
        </div>
        <div class="opts-detail-row">
          <span class="opts-detail-label" style="font-size: 0.75rem; color: #94a3b8; display: block;">Bid / Ask</span>
          <span class="opts-detail-val" style="color: #60a5fa; font-weight: 600;">$${item.Bid.toFixed(2)} / $${item.Ask.toFixed(2)}</span>
        </div>
        <div class="opts-detail-row">
          <span class="opts-detail-label" style="font-size: 0.75rem; color: #94a3b8; display: block;">Mid Price</span>
          <span class="opts-detail-val" style="color: #4ade80; font-weight: 700;">$${item["Mid Price"].toFixed(2)}</span>
        </div>
        <div class="opts-detail-row">
          <span class="opts-detail-label" style="font-size: 0.75rem; color: #94a3b8; display: block;">Volume / OI</span>
          <span class="opts-detail-val" style="color: #e2e8f0;">${(item.Volume || 0).toLocaleString()} / ${(item.OI || 0).toLocaleString()}</span>
        </div>
      </div>

      <div class="card__option" style="margin-top: 10px; background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); padding: 8px 12px; border-radius: 6px;">
        <div class="option-tag" style="color: #34d399; font-size: 0.7rem; font-weight: 700; letter-spacing: 0.5px;">🎯 TIGHT SPREAD PLAY</div>
        <div class="option-val" style="color: #f1f5f9; font-size: 0.9rem; font-weight: 600; margin-top: 2px;">${item["Suggested Option"]}</div>
      </div>
    </div>
  `;
}


// ── Render results ─────────────────────────────────────────

function renderResults() {
  const el = document.getElementById("results");
  const isOptionsSpreads = (scanData[0] && scanData[0]["Spread (%)"] !== undefined);
  const isOptions = (scanData[0] && scanData[0].Direction !== undefined);

  let filtered = scanData;
  if (isOptionsSpreads) {
    if (currentFilter === "bullish") {
      filtered = scanData.filter(d => d.Type === "CALL");
    } else if (currentFilter === "bearish") {
      filtered = scanData.filter(d => d.Type === "PUT");
    }
  } else if (isOptions) {
    if (currentFilter === "bullish") {
      filtered = scanData.filter(d => d.Direction === "Bullish");
    } else if (currentFilter === "bearish") {
      filtered = scanData.filter(d => d.Direction === "Bearish");
    }
  } else {
    if (currentFilter === "bullish") {
      filtered = scanData.filter(d =>
        d["Bullish Signals"] && d["Bullish Signals"] !== "—");
    } else if (currentFilter === "bearish") {
      filtered = scanData.filter(d =>
        d["Bearish Signals"] && d["Bearish Signals"] !== "—");
    } else if (currentFilter === "both") {
      filtered = scanData.filter(d =>
        (d["Bullish Signals"] && d["Bullish Signals"] !== "—") &&
        (d["Bearish Signals"] && d["Bearish Signals"] !== "—"));
    }
  }

  if (filtered.length === 0) {
    el.innerHTML = `
      <div class="empty-state">
        <div class="empty-state__icon">🏜️</div>
        <div class="empty-state__title">No matches</div>
        <div class="empty-state__text">Try a different filter or scan again later</div>
      </div>
    `;
    return;
  }

  let cardBuilder = buildCard;
  if (isOptionsSpreads) cardBuilder = buildOptionsSpreadsCard;
  else if (isOptions) cardBuilder = buildOptionsCard;

  el.innerHTML = `<div class="cards">${filtered.map(cardBuilder).join("")}</div>`;
}

// ── Stats bar ──────────────────────────────────────────────

function updateStats() {
  const isOptionsSpreads = (scanData[0] && scanData[0]["Spread (%)"] !== undefined);
  const isOptions = (scanData[0] && scanData[0].Direction !== undefined);

  if (isOptionsSpreads) {
    const callsCount = scanData.filter(d => d.Type === "CALL").length;
    const putsCount = scanData.filter(d => d.Type === "PUT").length;
    document.getElementById("statTotal").textContent = scanData.length;
    document.getElementById("statBull").textContent = callsCount;
    document.getElementById("statBear").textContent = putsCount;
    document.querySelectorAll(".stat__label")[1].textContent = "Calls";
    document.querySelectorAll(".stat__label")[2].textContent = "Puts";
  } else if (isOptions) {
    const bullCount = scanData.filter(d => d.Direction === "Bullish").length;
    const bearCount = scanData.filter(d => d.Direction === "Bearish").length;
    document.getElementById("statTotal").textContent = scanData.length;
    document.getElementById("statBull").textContent = bullCount;
    document.getElementById("statBear").textContent = bearCount;
    document.querySelectorAll(".stat__label")[1].textContent = "Calls";
    document.querySelectorAll(".stat__label")[2].textContent = "Puts";
  } else {
    const bullCount = scanData.filter(d =>
      d["Bullish Signals"] && d["Bullish Signals"] !== "—").length;
    const bearCount = scanData.filter(d =>
      d["Bearish Signals"] && d["Bearish Signals"] !== "—").length;
    document.getElementById("statTotal").textContent = scanData.length;
    document.getElementById("statBull").textContent = bullCount;
    document.getElementById("statBear").textContent = bearCount;
    document.querySelectorAll(".stat__label")[1].textContent = "Bullish";
    document.querySelectorAll(".stat__label")[2].textContent = "Bearish";
  }
}

// ── Display results (shared logic) ─────────────────────────

function displayResults(data) {
  const isOptionsSpreads = (data.mode === "options_spreads" || (data.results && data.results[0] && data.results[0]["Spread (%)"] !== undefined));
  const isOptions = (data.results && data.results[0] && data.results[0].Direction !== undefined);

  if (isOptionsSpreads) {
    scanData = (data.results || []).sort((a, b) => (a["Spread (%)"] || 0) - (b["Spread (%)"] || 0));
  } else if (isOptions) {
    scanData = (data.results || []).sort((a, b) => (b["Catalyst Score"] || 0) - (a["Catalyst Score"] || 0));
  } else {
    scanData = (data.results || []).sort((a, b) => (b.Score || 0) - (a.Score || 0));
  }

  document.getElementById("statsBar").classList.remove("hidden");
  document.getElementById("timestamp").classList.remove("hidden");
  document.getElementById("filters").classList.remove("hidden");
  document.getElementById("tsValue").textContent = data.timestamp;

  const badge = document.getElementById("scanBadge");
  if (isOptionsSpreads) {
    badge.textContent = `Watchlist Option Plays`;
    badge.classList.remove("hidden");
  } else if (data.tickers_scanned) {
    badge.textContent = `Scanned ${data.tickers_scanned.toLocaleString()} tickers`;
    badge.classList.remove("hidden");
  } else if (data.mode === "watchlist") {
    badge.textContent = `Watchlist option plays (all criteria)`;
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }

  updateStats();

  currentFilter = "all";
  document.querySelectorAll(".filter-btn").forEach(b =>
    b.classList.remove("filter-btn--active"));
  document.querySelector('[data-filter="all"]').classList.add("filter-btn--active");

  let bullLabel, bearLabel;
  if (isOptionsSpreads || isOptions) {
    bullLabel = "🟢 Calls";
    bearLabel = "🔴 Puts";
  } else {
    bullLabel = "🟢 Bullish";
    bearLabel = "🔴 Bearish";
  }
  document.querySelector('[data-filter="bullish"]').textContent = bullLabel;
  document.querySelector('[data-filter="bearish"]').textContent = bearLabel;

  const bothBtn = document.querySelector('[data-filter="both"]');
  if (isOptionsSpreads || isOptions) {
    bothBtn.classList.add("hidden");
  } else {
    bothBtn.classList.remove("hidden");
  }

  if (scanData.length === 0) {
    let emptyTitle, emptyText;
    if (isOptions) {
      emptyTitle = "No setups found";
      emptyText = "No options meeting all criteria right now.";
    } else {
      emptyTitle = "All clear";
      emptyText = "No reversal setups found right now.";
    }
    
    document.getElementById("results").innerHTML = `
      <div class="empty-state">
        <div class="empty-state__icon">✅</div>
        <div class="empty-state__title">${emptyTitle}</div>
        <div class="empty-state__text">${emptyText}<br>Check back after the next session.</div>
      </div>
    `;
  } else {
    renderResults();
  }
}

// ── Filter handler ─────────────────────────────────────────

function setFilter(filter, btnEl) {
  currentFilter = filter;
  document.querySelectorAll(".filter-btn").forEach(b =>
    b.classList.remove("filter-btn--active"));
  btnEl.classList.add("filter-btn--active");
  renderResults();
}

// ── Progress polling (both scan modes) ─────────────────────

function startProgressPolling(scanType = "watchlist") {
  if (hideTimeout) {
    clearTimeout(hideTimeout);
    hideTimeout = null;
  }
  const wrap = document.getElementById("progressWrap");
  wrap.classList.remove("hidden");
  document.getElementById("progressPct").textContent = "1%";
  document.getElementById("progressFill").style.width = "1%";

  const btn = document.getElementById(scanType === "options_spreads" ? "scanSpreadsBtn" : "scanBtn");
  btn.classList.add("scan-btn--loading");
  btn.disabled = true;

  if (pollTimer) {
    clearInterval(pollTimer);
  }

  let maxSeenPct = 0;

  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/scan/progress?t=${Date.now()}`);
      const p = await res.json();

      if (p.status === "done" || p.status === "error" || p.status === "idle") {
        if (p.status === "done" || p.status === "idle") {
          document.getElementById("progressPct").textContent = "100%";
          document.getElementById("progressFill").style.width = "100%";
        }
        stopProgressPolling();

        if (p.status === "done" || p.status === "idle") {
          const resultsUrl = scanType === "options_spreads" ? "/api/scan/options/tight_spreads/results" : "/api/scan/watchlist/results";
          const resData = await fetch(`${resultsUrl}?t=${Date.now()}`);
          const data = await resData.json();
          if (data.ok && data.results && data.results.length > 0) {
            displayResults(data);
          }
        } else if (p.status === "error") {
          document.getElementById("results").innerHTML = `
            <div class="empty-state">
              <div class="empty-state__icon">⚠️</div>
              <div class="empty-state__title">Scan error</div>
              <div class="empty-state__text">${p.phase_label}</div>
            </div>
          `;
        }

        document.getElementById("scanBtn").classList.remove("scan-btn--loading");
        document.getElementById("scanBtn").disabled = false;
        if (document.getElementById("scanSpreadsBtn")) {
          document.getElementById("scanSpreadsBtn").classList.remove("scan-btn--loading");
          document.getElementById("scanSpreadsBtn").disabled = false;
        }
        return;
      }

      if (typeof p.pct === "number" && p.pct > maxSeenPct) {
        maxSeenPct = p.pct;
      }
      const displayPct = Math.max(maxSeenPct, p.pct || 0);

      document.getElementById("progressPhase").textContent = p.phase_label || "Working...";
      document.getElementById("progressPct").textContent = `${displayPct}%`;
      document.getElementById("progressFill").style.width = `${displayPct}%`;
      document.getElementById("progressDetail").textContent =
        p.found > 0 ? `${p.found} matches found` : "";
      document.getElementById("progressEta").textContent = fmtEta(p.eta_seconds);

      const fill = document.getElementById("progressFill");
      if (p.phase === "downloading") {
        fill.style.background = "linear-gradient(90deg, #6366f1, #818cf8)";
      } else if (p.phase === "analyzing") {
        fill.style.background = "linear-gradient(90deg, #22c55e, #4ade80)";
      }
    } catch (e) {
      console.error("Progress poll error:", e);
    }
  }, 1500);
}

function stopProgressPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (hideTimeout) {
    clearTimeout(hideTimeout);
    hideTimeout = null;
  }
  hideTimeout = setTimeout(() => {
    document.getElementById("progressWrap").classList.add("hidden");
    hideTimeout = null;
  }, 800);
}

// ── Main scan ──────────────────────────────────────────────

async function runScan(scanType = "watchlist") {
  const btn = document.getElementById(scanType === "options_spreads" ? "scanSpreadsBtn" : "scanBtn");
  btn.classList.add("scan-btn--loading");
  btn.disabled = true;

  document.getElementById("emptyState")?.classList.add("hidden");
  document.getElementById("results").innerHTML = "";

  document.getElementById("statsBar")?.classList.add("hidden");
  document.getElementById("timestamp")?.classList.add("hidden");
  document.getElementById("scanBadge")?.classList.add("hidden");
  document.getElementById("filters")?.classList.add("hidden");

  const extHours = document.getElementById("extHoursToggle")?.checked || false;

  const endpoint = scanType === "options_spreads" ? "/api/scan/options/tight_spreads" : "/api/scan/watchlist";
  const payload = scanType === "options_spreads" ? { use_watchlist: true } : { extended_hours: extHours };

  const maxRetries = 3;
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      if (attempt > 1) {
        document.getElementById("results").innerHTML = `
          <div class="empty-state">
            <div class="empty-state__icon">⏳</div>
            <div class="empty-state__title">Waking up server...</div>
            <div class="empty-state__text">Free servers sleep when idle. Retrying (${attempt}/${maxRetries})...</div>
          </div>
        `;
        await new Promise(r => setTimeout(r, 3000));
      }

      const res = await fetch(endpoint, { 
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!data.ok) {
        if (res.status === 409 || (data.error && data.error.includes("already running"))) {
          console.log("Scan already running on server — attaching to active scan progress...");
          startProgressPolling(scanType);
          return;
        }
        throw new Error(data.error || "Failed to start scan");
      }
      startProgressPolling(scanType);
      return;
    } catch (err) {
      if (err.message && err.message.includes("already running")) {
        startProgressPolling(scanType);
        return;
      }
      if (attempt === maxRetries) {
        let errorHtml = `
          <div class="empty-state">
            <div class="empty-state__icon">⚠️</div>
            <div class="empty-state__title">Scan error</div>
            <div class="empty-state__text">${err.message}</div>
        `;
        
        if (err.message.includes("already running") || err.message.includes("409")) {
          errorHtml += `
            <button class="reset-btn" onclick="resetServerScanState(this)" style="margin-top: 15px; padding: 10px 20px; background: linear-gradient(135deg, #ef4444, #f87171); border: none; border-radius: 6px; color: white; font-weight: 600; cursor: pointer; transition: all 0.2s; box-shadow: 0 4px 12px rgba(239, 68, 68, 0.3);">
              🔓 Reset Server Scan State
            </button>
          `;
        } else {
          errorHtml += `<small style="display:block;margin-top:10px;opacity:0.7;">The server may be starting up — try again in 30 seconds.</small>`;
        }
        
        errorHtml += `</div>`;
        document.getElementById("results").innerHTML = errorHtml;
        btn.classList.remove("scan-btn--loading");
        btn.disabled = false;
      }
    }
  }
}

// ── Self-Healing Scan State Reset ───────────────────────────

async function resetServerScanState(btnEl) {
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = "Resetting...";
    btnEl.style.opacity = "0.7";
  }
  try {
    const res = await fetch("/api/scan/reset", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      document.getElementById("results").innerHTML = `
        <div class="empty-state">
          <div class="empty-state__icon">🔓</div>
          <div class="empty-state__title">Scan Reset</div>
          <div class="empty-state__text">The server has been reset to idle. You can start a new scan now!</div>
        </div>
      `;
    } else {
      alert("Failed to reset: " + (data.error || "Unknown error"));
      if (btnEl) {
        btnEl.disabled = false;
        btnEl.textContent = "🔓 Reset Server Scan State";
        btnEl.style.opacity = "1";
      }
    }
  } catch (e) {
    alert("Network error resetting scan state: " + e.message);
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = "🔓 Reset Server Scan State";
      btnEl.style.opacity = "1";
    }
  }
}

// ── Init ───────────────────────────────────────────────────────

async function checkActiveScan() {
  try {
    const res = await fetch(`/api/scan/progress?t=${Date.now()}`);
    if (!res.ok) return false;
    const p = await res.json();
    if (p.status === "running") {
      if (p.mode === scanMode) {
        startProgressPolling();
        return true;
      }
      return false;
    }
  } catch (e) {
    console.error("Error checking active scan:", e);
  }
  return false;
}

checkActiveScan().then(running => {
  if (!running) {
    loadLastWatchlistScan();
  }
});

// Fetch user watchlist from server on app load
fetchWatchlist();

// ── Watchlist Manager Functions ───────────────────────────────────

async function fetchWatchlist() {
  try {
    const res = await fetch("/api/watchlist");
    if (res.ok) {
      const data = await res.json();
      if (data.ok && Array.isArray(data.watchlist)) {
        userWatchlist = data.watchlist;
        localStorage.setItem("userWatchlist", JSON.stringify(userWatchlist));
      }
    }
  } catch (e) {
    console.error("Error fetching watchlist:", e);
    const saved = localStorage.getItem("userWatchlist");
    if (saved) {
      try { userWatchlist = JSON.parse(saved); } catch (_) {}
    }
  }
}

function renderWatchlistUI() {
  const container = document.getElementById("watchlistContainer");
  const countEl = document.getElementById("watchlistCount");
  if (!container) return;

  if (countEl) {
    countEl.textContent = `${userWatchlist.length} TICKER${userWatchlist.length === 1 ? '' : 'S'}`;
  }

  if (userWatchlist.length === 0) {
    container.innerHTML = `<div class="modal__empty">No tickers in watchlist. Add one above!</div>`;
    return;
  }

  container.innerHTML = userWatchlist.map(sym => `
    <div class="ticker-chip">
      <span>${sym}</span>
      <button class="ticker-chip__remove" onclick="deleteTicker('${sym}')">&times;</button>
    </div>
  `).join("");
}

async function openWatchlistModal() {
  await fetchWatchlist();
  renderWatchlistUI();
  const msgEl = document.getElementById("modalMsg");
  if (msgEl) msgEl.classList.add("hidden");
  document.getElementById("watchlistModal")?.classList.remove("hidden");
}

function closeWatchlistModal() {
  document.getElementById("watchlistModal")?.classList.add("hidden");
}

async function addTickerFromInput() {
  const input = document.getElementById("newTickerInput");
  if (!input) return;
  const raw = input.value.trim().toUpperCase();
  if (!raw) return;

  const msgEl = document.getElementById("modalMsg");
  input.value = "";

  try {
    const res = await fetch("/api/watchlist/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker: raw })
    });
    const data = await res.json();
    if (data.ok) {
      userWatchlist = data.watchlist;
      localStorage.setItem("userWatchlist", JSON.stringify(userWatchlist));
      renderWatchlistUI();
      if (msgEl) {
        msgEl.textContent = `Added ${raw} to watchlist`;
        msgEl.className = "modal__msg modal__msg--success";
        msgEl.classList.remove("hidden");
        setTimeout(() => msgEl.classList.add("hidden"), 3000);
      }
    } else {
      if (msgEl) {
        msgEl.textContent = data.error || "Failed to add ticker";
        msgEl.className = "modal__msg modal__msg--error";
        msgEl.classList.remove("hidden");
      }
    }
  } catch (e) {
    if (msgEl) {
      msgEl.textContent = "Network error adding ticker";
      msgEl.className = "modal__msg modal__msg--error";
      msgEl.classList.remove("hidden");
    }
  }
}

function handleNewTickerKey(e) {
  if (e.key === "Enter") {
    addTickerFromInput();
  }
}

async function deleteTicker(sym) {
  const msgEl = document.getElementById("modalMsg");
  try {
    const res = await fetch("/api/watchlist/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker: sym })
    });
    const data = await res.json();
    if (data.ok) {
      userWatchlist = data.watchlist;
      localStorage.setItem("userWatchlist", JSON.stringify(userWatchlist));
      renderWatchlistUI();
    } else if (msgEl) {
      msgEl.textContent = data.error || "Failed to remove ticker";
      msgEl.className = "modal__msg modal__msg--error";
      msgEl.classList.remove("hidden");
    }
  } catch (e) {
    if (msgEl) {
      msgEl.textContent = "Network error removing ticker";
      msgEl.className = "modal__msg modal__msg--error";
      msgEl.classList.remove("hidden");
    }
  }
}

async function importFromWebull() {
  const btn = document.getElementById("importWebullBtn");
  const msgEl = document.getElementById("modalMsg");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Importing from Webull...";
  }
  try {
    const res = await fetch("/api/watchlist/import-webull", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      userWatchlist = data.watchlist;
      localStorage.setItem("userWatchlist", JSON.stringify(userWatchlist));
      renderWatchlistUI();
      if (msgEl) {
        msgEl.textContent = `Imported ${data.added_count} new ticker(s) from Webull! (${data.total_imported} total)`;
        msgEl.className = "modal__msg modal__msg--success";
        msgEl.classList.remove("hidden");
      }
    } else if (msgEl) {
      msgEl.textContent = data.error || "Import failed";
      msgEl.className = "modal__msg modal__msg--error";
      msgEl.classList.remove("hidden");
    }
  } catch (e) {
    if (msgEl) {
      msgEl.textContent = "Network error importing Webull watchlists";
      msgEl.className = "modal__msg modal__msg--error";
      msgEl.classList.remove("hidden");
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "📥 Import from Webull";
    }
  }
}

function openNewsModal(newsJsonEncoded) {
  try {
    const news = JSON.parse(decodeURIComponent(newsJsonEncoded));
    document.getElementById("newsModalTitle").textContent = news.title || "No Title Available";
    document.getElementById("newsModalPublisher").textContent = news.publisher || "Unknown";
    document.getElementById("newsModalTime").textContent = news.publish_time || "Unknown";
    
    const linkEl = document.getElementById("newsModalLink");
    if (news.url) {
      linkEl.href = news.url;
      linkEl.style.display = "block";
    } else {
      linkEl.style.display = "none";
    }
    
    document.getElementById("newsModal").classList.remove("hidden");
  } catch (e) {
    console.error("Error opening news modal:", e);
  }
}

function closeNewsModal() {
  document.getElementById("newsModal").classList.add("hidden");
}

// ── Interactive Chart Modal ────────────────────────────────────

function openChartModal(ticker) {
  const modal = document.getElementById("chartModal");
  const tickerEl = document.getElementById("chartModalTicker");
  const extLink = document.getElementById("chartExternalLink");
  
  if (tickerEl) tickerEl.textContent = ticker;
  if (extLink) extLink.href = `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(ticker)}`;
  
  if (modal) modal.classList.remove("hidden");
  document.body.style.overflow = "hidden";

  const container = document.getElementById("tradingview_chart");
  if (container) container.innerHTML = "";

  if (typeof TradingView !== "undefined") {
    try {
      new TradingView.widget({
        "autosize": true,
        "symbol": ticker,
        "interval": "15",
        "timezone": "Etc/UTC",
        "theme": "dark",
        "style": "1",
        "locale": "en",
        "toolbar_bg": "#0a0e17",
        "enable_publishing": false,
        "allow_symbol_change": true,
        "container_id": "tradingview_chart",
        "studies": [
          "STD;Bollinger_Bands",
          "STD;RSI"
        ]
      });
    } catch (err) {
      console.warn("TradingView widget init error, using iframe fallback:", err);
      container.innerHTML = `<iframe src="https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(ticker)}&interval=15&symboledit=1&saveimage=1&toolbarbg=0a0e17&theme=dark&style=1&timezone=Exchange&studies=%5B%22STD%3BBollinger_Bands%22%2C%22STD%3BRSI%22%5D" style="width:100%;height:100%;border:0;" allowfullscreen></iframe>`;
    }
  } else {
    container.innerHTML = `<iframe src="https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(ticker)}&interval=15&symboledit=1&saveimage=1&toolbarbg=0a0e17&theme=dark&style=1&timezone=Exchange&studies=%5B%22STD%3BBollinger_Bands%22%2C%22STD%3BRSI%22%5D" style="width:100%;height:100%;border:0;" allowfullscreen></iframe>`;
  }
}

function closeChartModal() {
  const modal = document.getElementById("chartModal");
  if (modal) modal.classList.add("hidden");
  document.body.style.overflow = "";
  const container = document.getElementById("tradingview_chart");
  if (container) container.innerHTML = "";
}

function handleChartModalOverlayClick(event) {
  if (event.target.id === "chartModal") {
    closeChartModal();
  }
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeChartModal();
    closeNewsModal();
  }
});

