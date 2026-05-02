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

// ── Load watchlist on startup ──────────────────────────────

async function loadWatchlist() {
  try {
    const res = await fetch("/api/watchlist");
    const data = await res.json();
    if (data.ok) {
      watchlist = data.watchlist;
      updateModeDesc();
    }
  } catch (e) {
    console.error("Failed to load watchlist:", e);
  }
}

function updateModeDesc() {
  const desc = document.getElementById("modeDesc");
  const editBtn = document.getElementById("editWatchlistBtn");
  if (scanMode === "watchlist") {
    desc.textContent = `Scans ${watchlist.length} tickers — takes ~${Math.max(10, watchlist.length * 1.5).toFixed(0)}s`;
    editBtn.classList.remove("hidden");
  } else {
    desc.textContent = "Scans entire US market — takes 3-8 minutes";
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
    document.getElementById("statsBar").classList.add("hidden");
    document.getElementById("timestamp").classList.add("hidden");
    document.getElementById("filters").classList.add("hidden");
    document.getElementById("scanBadge").classList.add("hidden");
  } else {
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
    document.getElementById("statsBar").classList.add("hidden");
    document.getElementById("timestamp").classList.add("hidden");
    document.getElementById("filters").classList.add("hidden");
    document.getElementById("scanBadge").classList.add("hidden");
  }
  updateModeDesc();
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
    // Split by pipe first, then comma to get all individual reasons
    return str.split(" | ").flatMap(s => s.split(", ")).map(s => s.trim());
  };

  const bullish = parseSignals(item["Bullish Signals"]);
  const bearish = parseSignals(item["Bearish Signals"]);

  const rsi = item.RSI;
  const rsiCls = rsiClass(rsi);

  const maxVol = Math.max(...scanData.map(d => d.Volume), 1);
  const volPct = Math.round((item.Volume / maxVol) * 100);

  const bullPills = bullish.map(s =>
    `<span class="pill pill--bull">${s}</span>`
  ).join("");

  const bearPills = bearish.map(s =>
    `<span class="pill pill--bear">${s}</span>`
  ).join("");

  return `
    <div class="card" style="animation-delay: ${Math.min(index * 0.04, 1.2)}s">
      <div class="card__top">
        <div class="card__ticker">${item.Ticker}</div>
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
        ${bullish.length ? `<div class="signal-row"><span class="signal-row__icon">🟢</span>${bullPills}</div>` : ""}
        ${bearish.length ? `<div class="signal-row"><span class="signal-row__icon">🔴</span>${bearPills}</div>` : ""}
      </div>
    </div>
  `;
}

// ── Render results ─────────────────────────────────────────

function renderResults() {
  const el = document.getElementById("results");

  let filtered = scanData;
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

  el.innerHTML = `<div class="cards">${filtered.map(buildCard).join("")}</div>`;
}

// ── Stats bar ──────────────────────────────────────────────

function updateStats() {
  const bullCount = scanData.filter(d =>
    d["Bullish Signals"] && d["Bullish Signals"] !== "—").length;
  const bearCount = scanData.filter(d =>
    d["Bearish Signals"] && d["Bearish Signals"] !== "—").length;

  document.getElementById("statTotal").textContent = scanData.length;
  document.getElementById("statBull").textContent = bullCount;
  document.getElementById("statBear").textContent = bearCount;
}

// ── Display results (shared logic) ─────────────────────────

function displayResults(data) {
  scanData = data.results || [];

  document.getElementById("statsBar").classList.remove("hidden");
  document.getElementById("timestamp").classList.remove("hidden");
  document.getElementById("filters").classList.remove("hidden");
  document.getElementById("tsValue").textContent = data.timestamp;

  const badge = document.getElementById("scanBadge");
  if (data.tickers_scanned) {
    badge.textContent = `Scanned ${data.tickers_scanned.toLocaleString()} tickers`;
    badge.classList.remove("hidden");
  } else if (data.mode === "full_market") {
    badge.textContent = `Full market scan`;
    badge.classList.remove("hidden");
  }

  updateStats();

  currentFilter = "all";
  document.querySelectorAll(".filter-btn").forEach(b =>
    b.classList.remove("filter-btn--active"));
  document.querySelector('[data-filter="all"]').classList.add("filter-btn--active");

  if (scanData.length === 0) {
    document.getElementById("results").innerHTML = `
      <div class="empty-state">
        <div class="empty-state__icon">✅</div>
        <div class="empty-state__title">All clear</div>
        <div class="empty-state__text">No reversal setups found right now.<br>Check back after the next session.</div>
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

  // Both modes now use the same async pattern
  const endpoint = scanMode === "watchlist" ? "/api/scan" : "/api/scan/full";
  const resultsEndpoint = scanMode === "watchlist"
    ? "/api/scan/results"
    : "/api/scan/full/results";

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
        document.getElementById("results").innerHTML = `
          <div class="empty-state">
            <div class="empty-state__icon">⚠️</div>
            <div class="empty-state__title">Scan error</div>
            <div class="empty-state__text">${err.message}<br><small>The server may be starting up — try again in 30 seconds.</small></div>
          </div>
        `;
        btn.classList.remove("scan-btn--loading");
        btn.disabled = false;
        document.getElementById("modeTabs").classList.remove("hidden");
      }
    }
  }
}

// ── Init ───────────────────────────────────────────────────

loadWatchlist();
