/* ============================================================
   Stock Reversal Scanner – Frontend Logic (Full Market + Watchlist Editor)
   ============================================================ */

let scanData = [];
let currentFilter = "all";
let scanMode = "watchlist";
let pollTimer = null;
let watchlist = [];

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

// ── Local storage helpers for watchlist persistence ────────

function saveWatchlistLocal(list) {
  try {
    localStorage.setItem("scanner_watchlist", JSON.stringify(list));
  } catch (e) {
    console.warn("Failed to save watchlist to localStorage:", e);
  }
}

function loadWatchlistLocal() {
  try {
    const raw = localStorage.getItem("scanner_watchlist");
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length > 0) return parsed;
    }
  } catch (e) {
    console.warn("Failed to load watchlist from localStorage:", e);
  }
  return null;
}

// ── Load watchlist on startup ──────────────────────────────

async function loadWatchlist() {
  // 1. Check localStorage first (persists across server restarts)
  const localList = loadWatchlistLocal();

  if (localList) {
    // Use the locally saved watchlist and sync it to the server
    watchlist = localList;
    updateModeDesc();
    try {
      await fetch("/api/watchlist", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ watchlist: localList }),
      });
    } catch (e) {
      console.warn("Failed to sync watchlist to server:", e);
    }
  } else {
    // No local data — load from server (first visit)
    try {
      const res = await fetch("/api/watchlist");
      const data = await res.json();
      if (data.ok) {
        watchlist = data.watchlist;
        saveWatchlistLocal(watchlist);
        updateModeDesc();
      }
    } catch (e) {
      console.error("Failed to load watchlist:", e);
    }
  }
}

function updateModeDesc() {
  const desc = document.getElementById("modeDesc");
  const editBtn = document.getElementById("editWatchlistBtn");
  if (scanMode === "watchlist") {
    desc.textContent = `Scans ${watchlist.length} tickers — takes ~${Math.max(10, watchlist.length * 1.5).toFixed(0)}s`;
    editBtn.classList.remove("hidden");
  } else if (scanMode === "full") {
    desc.textContent = "Scans entire US market — takes 3-8 minutes";
    editBtn.classList.add("hidden");
  } else if (scanMode === "options") {
    desc.textContent = "Scans for high-probability options setups — takes 3-8 minutes";
    editBtn.classList.add("hidden");
  }
}

// ── Mode switching ─────────────────────────────────────────

async function setMode(mode, btn) {
  scanMode = mode;
  document.querySelectorAll(".mode-tab").forEach(b =>
    b.classList.remove("mode-tab--active"));
  btn.classList.add("mode-tab--active");

  const scanBtn = document.getElementById("scanBtn");
  if (mode === "watchlist") {
    scanBtn.querySelector(".scan-btn__text").textContent = "🔍  Scan Watchlist";
    // Clear results so they don't see full market results on the watchlist tab
    document.getElementById("results").innerHTML = `
      <div class="empty-state">
        <div class="empty-state__icon">🔍</div>
        <div class="empty-state__title">Ready to scan</div>
        <div class="empty-state__text">Click the button above to scan your watchlist</div>
      </div>
    `;
    hideAuxUI();
  } else if (mode === "full") {
    scanBtn.querySelector(".scan-btn__text").textContent = "🌐  Scan Full Market";
    // Auto-load the persistent full market scan results
    try {
      showSkeleton();
      const res = await fetch("/api/scan/full/results");
      if (res.ok) {
        const data = await res.json();
        if (data.ok && data.results) {
          displayResults(data);
          updateModeDesc();
          return;
        }
      }
    } catch (e) {
      console.error("No saved full scan available yet");
    }
    
    // If no saved scan exists yet
    document.getElementById("results").innerHTML = `
      <div class="empty-state">
        <div class="empty-state__icon">🌐</div>
        <div class="empty-state__title">Ready to scan</div>
        <div class="empty-state__text">Click the button above to scan the full market</div>
      </div>
    `;
    hideAuxUI();
  } else if (mode === "options") {
    scanBtn.querySelector(".scan-btn__text").textContent = "🎯  Scan Options";
    try {
      showSkeleton();
      const res = await fetch("/api/scan/options/full/results");
      if (res.ok) {
        const data = await res.json();
        if (data.ok && data.results) {
          displayResults(data);
          updateModeDesc();
          return;
        }
      }
    } catch (e) {
      console.error("No saved options scan available yet");
    }

    document.getElementById("results").innerHTML = `
      <div class="empty-state">
        <div class="empty-state__icon">🎯</div>
        <div class="empty-state__title">Ready to scan</div>
        <div class="empty-state__text">Click above to find high-probability options setups<br>DTE 20-60 · Delta 0.40-0.70 · IV Rank &lt;30%</div>
      </div>
    `;
    hideAuxUI();
  }
  updateModeDesc();
}

function hideAuxUI() {
  document.getElementById("statsBar").classList.add("hidden");
  document.getElementById("timestamp").classList.add("hidden");
  document.getElementById("filters").classList.add("hidden");
  document.getElementById("scanBadge").classList.add("hidden");
}

// ── Watchlist Editor ───────────────────────────────────────

function openWatchlistEditor() {
  document.getElementById("watchlistModal").classList.remove("hidden");
  document.body.style.overflow = "hidden";
  renderWatchlistEditor();
  // Focus input
  setTimeout(() => document.getElementById("addTickerInput").focus(), 200);
}

function closeWatchlistEditor(e) {
  if (e && e.target !== e.currentTarget) return;
  document.getElementById("watchlistModal").classList.add("hidden");
  document.body.style.overflow = "";
  updateModeDesc();
}

function renderWatchlistEditor() {
  const listEl = document.getElementById("watchlistList");
  const countEl = document.getElementById("watchlistCount");

  countEl.textContent = `${watchlist.length} ticker${watchlist.length !== 1 ? "s" : ""}`;

  if (watchlist.length === 0) {
    listEl.innerHTML = `<div class="modal__empty">No tickers yet. Add some above!</div>`;
    return;
  }

  listEl.innerHTML = watchlist.map(ticker => `
    <div class="ticker-chip" id="chip-${ticker}">
      <span class="ticker-chip__symbol">${ticker}</span>
      <button class="ticker-chip__remove" onclick="removeTicker('${ticker}')" title="Remove ${ticker}">&times;</button>
    </div>
  `).join("");
}

function showWatchlistMsg(msg, isError = false) {
  const el = document.getElementById("watchlistMsg");
  el.textContent = msg;
  el.className = `modal__msg ${isError ? "modal__msg--error" : "modal__msg--success"}`;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 2500);
}

async function addTicker() {
  const input = document.getElementById("addTickerInput");
  const ticker = input.value.trim().toUpperCase();

  if (!ticker) return;
  if (!/^[A-Z]{1,5}$/.test(ticker)) {
    showWatchlistMsg("Use 1-5 letters only (e.g. AAPL)", true);
    return;
  }

  try {
    const res = await fetch("/api/watchlist/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker }),
    });
    const data = await res.json();

    if (data.ok) {
      watchlist = data.watchlist;
      saveWatchlistLocal(watchlist);
      input.value = "";
      renderWatchlistEditor();
      showWatchlistMsg(`${ticker} added ✓`);
    } else {
      showWatchlistMsg(data.error || "Failed to add", true);
    }
  } catch (e) {
    showWatchlistMsg("Network error", true);
  }
}

async function removeTicker(ticker) {
  // Animate out
  const chip = document.getElementById(`chip-${ticker}`);
  if (chip) {
    chip.style.transform = "scale(0.8)";
    chip.style.opacity = "0";
  }

  try {
    const res = await fetch("/api/watchlist/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker }),
    });
    const data = await res.json();

    if (data.ok) {
      watchlist = data.watchlist;
      saveWatchlistLocal(watchlist);
      setTimeout(() => renderWatchlistEditor(), 200);
    }
  } catch (e) {
    showWatchlistMsg("Network error", true);
    if (chip) {
      chip.style.transform = "";
      chip.style.opacity = "";
    }
  }
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

  const bullish = parseSignals(item["Bullish Signals"]);
  const bearish = parseSignals(item["Bearish Signals"]);

  const rsi = item.RSI;
  const rsiCls = rsiClass(rsi);

  const maxVol = Math.max(...scanData.map(d => d.Volume), 1);
  const volPct = Math.round((item.Volume / maxVol) * 100);

  const grade = item.Grade || "B";
  const score = item.Score || 0;
  const gradeCls = grade === "A+" ? "grade--aplus" : grade === "A" ? "grade--a" : "grade--b";

  const bullPills = bullish.map(s =>
    `<span class="pill pill--bull">${s}</span>`
  ).join("");

  const bearPills = bearish.map(s =>
    `<span class="pill pill--bear">${s}</span>`
  ).join("");

  const bullIcon = "🟢";
  const bearIcon = "🔴";

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
      <div class="card__signals">
        ${bullish.length ? `<div class="signal-row"><span class="signal-row__icon">${bullIcon}</span>${bullPills}</div>` : ""}
        ${bearish.length ? `<div class="signal-row"><span class="signal-row__icon">${bearIcon}</span>${bearPills}</div>` : ""}
      </div>
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

  const catalystPills = (item["Catalyst Tags"] || "").split(" | ").filter(s => s).map(s =>
    `<span class="pill ${isBullish ? 'pill--bull' : 'pill--bear'}">${s}</span>`
  ).join("");

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

      <div class="card__signals">
        <div class="signal-row">
          <span class="signal-row__icon">⚡</span>
          ${catalystPills}
        </div>
      </div>
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
  } else if (data.mode === "full_market" || data.mode === "options_full") {
    badge.textContent = `Full market scan`;
    badge.classList.remove("hidden");
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

function startProgressPolling(resultsEndpoint) {
  const wrap = document.getElementById("progressWrap");
  wrap.classList.remove("hidden");

  pollTimer = setInterval(async () => {
    try {
      const res = await fetch("/api/scan/progress");
      const p = await res.json();

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

      if (p.status === "done" || p.status === "error") {
        stopProgressPolling();

        if (p.status === "done") {
          const resData = await fetch(`${resultsEndpoint}?t=${Date.now()}`);
          const data = await resData.json();
          if (data.ok) {
            displayResults(data);
          }
        } else {
          document.getElementById("results").innerHTML = `
            <div class="empty-state">
              <div class="empty-state__icon">⚠️</div>
              <div class="empty-state__title">Scan error</div>
              <div class="empty-state__text">${p.phase_label}</div>
            </div>
          `;
        }

        const btn = document.getElementById("scanBtn");
        btn.classList.remove("scan-btn--loading");
        btn.disabled = false;
        document.getElementById("modeTabs").classList.remove("hidden");
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
  setTimeout(() => {
    document.getElementById("progressWrap").classList.add("hidden");
  }, 800);
}

// ── Main scan ──────────────────────────────────────────────

async function runScan() {
  const btn = document.getElementById("scanBtn");
  btn.classList.add("scan-btn--loading");
  btn.disabled = true;

  document.getElementById("emptyState")?.classList.add("hidden");
  document.getElementById("results").innerHTML = "";
  document.getElementById("modeTabs").classList.add("hidden");

  const extHours = document.getElementById("extHoursToggle")?.checked || false;

  // All modes use the same async pattern
  const isOptions = scanMode === "options";
  
  let endpoint, resultsEndpoint;
  if (isOptions) {
    endpoint = "/api/scan/options/full";
    resultsEndpoint = "/api/scan/options/full/results";
  } else if (scanMode === "watchlist") {
    endpoint = "/api/scan";
    resultsEndpoint = "/api/scan/results";
  } else {
    endpoint = "/api/scan/full";
    resultsEndpoint = "/api/scan/full/results";
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
      startProgressPolling(resultsEndpoint);
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
        document.getElementById("modeTabs").classList.remove("hidden");
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

// ── Init ───────────────────────────────────────────────────

loadWatchlist();
