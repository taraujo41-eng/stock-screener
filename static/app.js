/* ============================================================
   Stock Reversal Scanner – Frontend Logic (Full Market + Options)
   ============================================================ */

let scanData = [];
let currentFilter = "all";
let scanMode = "3sigma";
let pollTimer = null;
let hideTimeout = null;

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
  if (scanMode === "3sigma") {
    desc.textContent = "Scans S&P 500, NASDAQ 100, and ETFs for 15m regular hour Close piercing Daily 3-Sigma Bollinger Bands";
    if (subtitle) subtitle.textContent = "15m Close × Daily 3-Sigma Bollinger Bands";
  } else if (scanMode === "2sigma") {
    desc.textContent = "Scans S&P 500, NASDAQ 100, and ETFs for 15m regular hour Close piercing Daily 2-Sigma Bollinger Bands";
    if (subtitle) subtitle.textContent = "15m Close × Daily 2-Sigma Bollinger Bands";
  } else if (scanMode === "52w") {
    desc.textContent = "Scans S&P 500, NASDAQ 100, and ETFs for daily RSI divergence at 52-week Highs and Lows";
    if (subtitle) subtitle.textContent = "Daily 52-Week High/Low × RSI Divergence";
  }
}

async function loadLast3SigmaScan() {
  const scanBtn = document.getElementById("scanBtn");
  scanBtn.querySelector(".scan-btn__text").textContent = "🔔  Scan 3-Sigma Bot";
  try {
    showSkeleton();
    const res = await fetch("/api/scan/3sigma/results");
    if (res.ok) {
      const data = await res.json();
      if (data.ok && data.results) {
        displayResults(data);
        updateModeDesc();
        return;
      }
    }
  } catch (e) {
    console.error("No saved 3-sigma scan available yet");
  }

  document.getElementById("results").innerHTML = `
    <div class="empty-state">
      <div class="empty-state__icon">🔔</div>
      <div class="empty-state__title">Ready to scan</div>
      <div class="empty-state__text">Click above to scan S&P 500, NASDAQ 100, and ETFs for 15m Close crossing Daily 3-Sigma Bollinger Bands</div>
    </div>
  `;
  hideAuxUI();
  updateModeDesc();
}

async function loadLast2SigmaScan() {
  const scanBtn = document.getElementById("scanBtn");
  scanBtn.querySelector(".scan-btn__text").textContent = "⚡  Scan 2-Sigma Bot";
  try {
    showSkeleton();
    const res = await fetch("/api/scan/2sigma/results");
    if (res.ok) {
      const data = await res.json();
      if (data.ok && data.results) {
        displayResults(data);
        updateModeDesc();
        return;
      }
    }
  } catch (e) {
    console.error("No saved 2-sigma scan available yet");
  }

  document.getElementById("results").innerHTML = `
    <div class="empty-state">
      <div class="empty-state__icon">⚡</div>
      <div class="empty-state__title">Ready to scan</div>
      <div class="empty-state__text">Click above to scan S&P 500, NASDAQ 100, and ETFs for 15m Close crossing Daily 2-Sigma Bollinger Bands</div>
    </div>
  `;
  hideAuxUI();
  updateModeDesc();
}

async function loadLast52wScan() {
  const scanBtn = document.getElementById("scanBtn");
  scanBtn.querySelector(".scan-btn__text").textContent = "📈  Scan 52-Week Reversal";
  try {
    showSkeleton();
    const res = await fetch("/api/scan/52w/results");
    if (res.ok) {
      const data = await res.json();
      if (data.ok && data.results) {
        displayResults(data);
        updateModeDesc();
        return;
      }
    }
  } catch (e) {
    console.error("No saved 52w scan available yet");
  }

  document.getElementById("results").innerHTML = `
    <div class="empty-state">
      <div class="empty-state__icon">📈</div>
      <div class="empty-state__title">Ready to scan</div>
      <div class="empty-state__text">Click above to scan for stocks at 52-week High/Low showing daily RSI divergence</div>
    </div>
  `;
  hideAuxUI();
  updateModeDesc();
}

async function switchTab(mode) {
  if (scanMode === mode) return;
  scanMode = mode;
  
  document.querySelectorAll(".mode-tab").forEach(tab => {
    tab.classList.remove("mode-tab--active");
  });
  
  // Clear any existing polling timer if we change tabs
  stopProgressPolling();
  
  // Reset scan button loading states and set text
  const btn = document.getElementById("scanBtn");
  btn.classList.remove("scan-btn--loading");
  btn.disabled = false;
  const btnText = btn.querySelector(".scan-btn__text");
  
  if (mode === "3sigma") {
    if (btnText) btnText.textContent = "🔔  Scan 3-Sigma Bot";
    document.getElementById("tab3Sigma").classList.add("mode-tab--active");
    document.getElementById("extHoursWrap")?.classList.remove("hidden");
  } else if (mode === "2sigma") {
    if (btnText) btnText.textContent = "⚡  Scan 2-Sigma Bot";
    document.getElementById("tab2Sigma").classList.add("mode-tab--active");
    document.getElementById("extHoursWrap")?.classList.remove("hidden");
  } else if (mode === "52w") {
    if (btnText) btnText.textContent = "📈  Scan 52-Week Reversal";
    document.getElementById("tab52w").classList.add("mode-tab--active");
    document.getElementById("extHoursWrap")?.classList.add("hidden");
  }
  updateModeDesc();

  // Check if a scan is already running on the server
  const running = await checkActiveScan();
  if (!running) {
    // If no scan is running, load historical results for the tab
    if (mode === "3sigma") {
      await loadLast3SigmaScan();
    } else if (mode === "2sigma") {
      await loadLast2SigmaScan();
    } else if (mode === "52w") {
      await loadLast52wScan();
    }
  }
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
          <div class="card__ticker">${item.Ticker}</div>
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
      ${item["Suggested Option"] && item["Suggested Option"] !== "—" ? `
        <div class="card__option">
          <div class="option-tag">TRADE IDEA</div>
          <div class="option-val">${item["Suggested Option"]}</div>
        </div>
      ` : ""}
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
          <div class="card__ticker">${item.Ticker}</div>
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

// ── Render results ─────────────────────────────────────────

function renderResults() {
  const el = document.getElementById("results");
  const isOptions = scanMode === "options" || (scanData[0] && scanData[0].Direction);

  let filtered = scanData;
  if (isOptions) {
    // Options mode filters by direction
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

  const cardBuilder = isOptions ? buildOptionsCard : buildCard;
  el.innerHTML = `<div class="cards">${filtered.map(cardBuilder).join("")}</div>`;
}

// ── Stats bar ──────────────────────────────────────────────

function updateStats() {
  const isOptions = scanMode === "options" || (scanData[0] && scanData[0].Direction);

  if (isOptions) {
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
  const isOptions = scanMode === "options" || (data.mode && data.mode.startsWith("options"));

  if (isOptions) {
    scanData = (data.results || []).sort((a, b) => (b["Catalyst Score"] || 0) - (a["Catalyst Score"] || 0));
  } else {
    scanData = (data.results || []).sort((a, b) => (b.Score || 0) - (a.Score || 0));
  }

  document.getElementById("statsBar").classList.remove("hidden");
  document.getElementById("timestamp").classList.remove("hidden");
  document.getElementById("filters").classList.remove("hidden");
  document.getElementById("tsValue").textContent = data.timestamp;

  const badge = document.getElementById("scanBadge");
  if (data.tickers_scanned) {
    badge.textContent = `Scanned ${data.tickers_scanned.toLocaleString()} tickers`;
    badge.classList.remove("hidden");
  } else if (data.mode === "3sigma") {
    badge.textContent = `3-Sigma Bot scan`;
    badge.classList.remove("hidden");
  } else if (data.mode === "2sigma") {
    badge.textContent = `2-Sigma Bot scan`;
    badge.classList.remove("hidden");
  } else if (data.mode === "52w") {
    badge.textContent = `52-Week Reversal scan`;
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }

  updateStats();

  currentFilter = "all";
  document.querySelectorAll(".filter-btn").forEach(b =>
    b.classList.remove("filter-btn--active"));
  document.querySelector('[data-filter="all"]').classList.add("filter-btn--active");

  // Update filter labels
  let bullLabel, bearLabel;
  if (isOptions) {
    bullLabel = "🟢 Calls";
    bearLabel = "🔴 Puts";
  } else {
    bullLabel = "🟢 Bullish";
    bearLabel = "🔴 Bearish";
  }
  document.querySelector('[data-filter="bullish"]').textContent = bullLabel;
  document.querySelector('[data-filter="bearish"]').textContent = bearLabel;

  // Hide "both" filter for options mode (not applicable)
  const bothBtn = document.querySelector('[data-filter="both"]');
  if (isOptions) {
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

function startProgressPolling() {
  if (hideTimeout) {
    clearTimeout(hideTimeout);
    hideTimeout = null;
  }
  const wrap = document.getElementById("progressWrap");
  wrap.classList.remove("hidden");

  // Disable buttons and set loading state for the scanner button
  const btn = document.getElementById("scanBtn");
  btn.classList.add("scan-btn--loading");
  btn.disabled = true;

  if (pollTimer) {
    clearInterval(pollTimer);
  }

  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/scan/progress?t=${Date.now()}`);
      const p = await res.json();

      // If scanner is idle or not running, stop polling
      if (p.status !== "running") {
        stopProgressPolling();

        if (p.status === "done") {
          let targetResultsEndpoint;
          if (p.mode === "3sigma") {
            targetResultsEndpoint = "/api/scan/3sigma/results";
          } else if (p.mode === "2sigma") {
            targetResultsEndpoint = "/api/scan/2sigma/results";
          } else if (p.mode === "52w") {
            targetResultsEndpoint = "/api/scan/52w/results";
          }

          // Only display results if user is on the tab of the finished scan
          if (p.mode === scanMode && targetResultsEndpoint) {
            const resData = await fetch(`${targetResultsEndpoint}?t=${Date.now()}`);
            const data = await resData.json();
            if (data.ok) {
              displayResults(data);
            }
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

        const scanBtn = document.getElementById("scanBtn");
        scanBtn.classList.remove("scan-btn--loading");
        scanBtn.disabled = false;
        return;
      }

      // If running, update UI
      document.getElementById("progressPhase").textContent = p.phase_label || "Working...";
      document.getElementById("progressPct").textContent = `${p.pct}%`;
      document.getElementById("progressFill").style.width = `${p.pct}%`;
      document.getElementById("progressDetail").textContent =
        p.found > 0 ? `${p.found} signals found` : "";
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

async function runScan() {
  const btn = document.getElementById("scanBtn");
  btn.classList.add("scan-btn--loading");
  btn.disabled = true;

  document.getElementById("emptyState")?.classList.add("hidden");
  document.getElementById("results").innerHTML = "";

  const extHours = document.getElementById("extHoursToggle")?.checked || false;

  let endpoint, resultsEndpoint;
  if (scanMode === "3sigma") {
    endpoint = "/api/scan/3sigma";
    resultsEndpoint = "/api/scan/3sigma/results";
  } else if (scanMode === "2sigma") {
    endpoint = "/api/scan/2sigma";
    resultsEndpoint = "/api/scan/2sigma/results";
  } else if (scanMode === "52w") {
    endpoint = "/api/scan/52w";
    resultsEndpoint = "/api/scan/52w/results";
  }

  // Retry logic for Render cold starts
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
        body: JSON.stringify({ extended_hours: extHours })
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "Failed to start scan");
      startProgressPolling();
      return; // success — exit the retry loop
    } catch (err) {
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
    loadLast3SigmaScan();
  }
});

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

