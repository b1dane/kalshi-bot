/* ── Kalshi Bot Monitor — frontend JS ───────────────────────── */
/* global */ var API_BASE = "";

/* ── State ──────────────────────────────────────────────────── */
var state = {
  activeScreen: "dashboard",
  settings: {},
  saving: false
};

/* ── Init ────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", function() {
  // ── navigation ──
  var navButtons = document.querySelectorAll(".nav-btn");
  navButtons.forEach(function(btn) {
    btn.addEventListener("click", function(e) {
      var screen = btn.getAttribute("data-screen");
      switchScreen(screen);
    });
  });

  // ── settings: conviction picker ──
  var convBtns = document.querySelectorAll(".conv-btn");
  convBtns.forEach(function(b) {
    b.addEventListener("click", function() {
      convBtns.forEach(function(x) { x.classList.remove("active"); });
      b.classList.add("active");
    });
  });

  // ── settings: save button ──
  document.getElementById("saveSettings").addEventListener("click", saveSettings);

  // ── show default screen ──
  switchScreen("dashboard");

  // ── initial data load ──
  fetchStatus();
  fetchDecisions();
  fetchSettings();

  // ── start polling ──
  setInterval(fetchStatus, 4000);
  setInterval(fetchDecisions, 4000);
});

/* ── Screen Switching ───────────────────────────────────────── */
function switchScreen(name) {
  state.activeScreen = name;
  var screens = document.querySelectorAll(".screen");
  screens.forEach(function(s) { s.classList.remove("active"); });
  var target = document.getElementById("screen-" + name);
  if (target) target.classList.add("active");

  var navBtns = document.querySelectorAll(".nav-btn");
  navBtns.forEach(function(b) {
    b.classList.remove("active");
    if (b.getAttribute("data-screen") === name) b.classList.add("active");
  });
}

/* ── API helpers ────────────────────────────────────────────── */
function apiGet(path, cb) {
  var xhr = new XMLHttpRequest();
  xhr.open("GET", API_BASE + path, true);
  xhr.setRequestHeader("Accept", "application/json");
  xhr.onload = function() {
    if (xhr.status >= 200 && xhr.status < 400) {
      try { cb(null, JSON.parse(xhr.responseText)); }
      catch(e) { cb(e); }
    } else {
      cb(new Error(xhr.status + " " + xhr.responseText.slice(0,80)));
    }
  };
  xhr.onerror = function() { cb(new Error("Network error")); };
  xhr.send();
}

function apiPost(path, body, cb) {
  var xhr = new XMLHttpRequest();
  xhr.open("POST", API_BASE + path, true);
  xhr.setRequestHeader("Content-Type", "application/json");
  xhr.setRequestHeader("Accept", "application/json");
  xhr.onload = function() {
    if (xhr.status >= 200 && xhr.status < 400) {
      try { cb(null, JSON.parse(xhr.responseText)); }
      catch(e) { cb(e); }
    } else {
      try {
        var errBody = JSON.parse(xhr.responseText);
        cb(new Error(errBody.error || xhr.responseText.slice(0,80)));
      } catch(e) { cb(new Error(xhr.status + " " + xhr.responseText.slice(0,80))); }
    }
  };
  xhr.onerror = function() { cb(new Error("Network error")); };
  xhr.send(JSON.stringify(body));
}

/* ── Fetch Status ───────────────────────────────────────────── */
function fetchStatus() {
  apiGet("/api/status", function(err, data) {
    var statusEl = document.getElementById("topStatus");
    if (err || !data) {
      statusEl.textContent = "offline";
      statusEl.className = "top-status offline";
      return;
    }
    statusEl.textContent = data.bot_running ? "bot running" : "bot idle";
    statusEl.className = "top-status " + (data.bot_running ? "online" : "offline");
    renderDashboard(data);
  });
}

/* ── Fetch Decisions (Feed + Trades) ────────────────────────── */
function fetchDecisions() {
  apiGet("/api/decisions?limit=50", function(err, data) {
    if (err || !data || !data.decisions) return;
    renderFeed(data.decisions);
    renderTrades(data.decisions);
  });
}

/* ── Fetch Settings ─────────────────────────────────────────── */
function fetchSettings() {
  apiGet("/api/settings", function(err, data) {
    if (err || !data) return;
    state.settings = data;
    renderSettings(data);
  });
}

/* ── Save Settings ──────────────────────────────────────────── */
function saveSettings() {
  if (state.saving) return;
  state.saving = true;
  var btn = document.getElementById("saveSettings");
  btn.textContent = "Saving…";
  btn.style.opacity = "0.5";

  var modeCheck = document.getElementById("modeToggle");
  var mode = modeCheck.checked ? "live" : "paper";
  var posSize = parseInt(document.getElementById("positionSize").value, 10) || 1;
  var activeConv = document.querySelector(".conv-btn.active");
  var conv = activeConv ? parseInt(activeConv.getAttribute("data-v"), 10) : 1;

  apiPost("/api/settings", {
    mode: mode,
    position_size: posSize,
    conviction_floor: conv
  }, function(err, result) {
    var msg = document.getElementById("saveMsg");
    if (err) {
      msg.textContent = "Error: " + err.message;
      msg.style.color = "#ef4444";
    } else {
      msg.textContent = "Settings saved ✓";
      msg.style.color = "#34d399";
      state.settings = result;
    }
    btn.textContent = "Save Settings";
    btn.style.opacity = "1";
    state.saving = false;
    setTimeout(function() { msg.textContent = ""; }, 3000);
  });
}

/* ── Render Dashboard ───────────────────────────────────────── */
function renderDashboard(d) {
  var bal = d.balance || 0;
  var pnl = d.pnl || 0;
  document.getElementById("dashBalance").textContent = "$" + fmtNum(bal);
  document.getElementById("dashBalance").className = "stat-value " + (pnl >= 0 ? "positive" : "negative");

  document.getElementById("dashPnl").textContent = (pnl >= 0 ? "+" : "") + "$" + fmtNum(pnl);
  document.getElementById("dashPnl").className = "stat-value " + (pnl >= 0 ? "positive" : "negative");

  document.getElementById("dashWinRate").textContent = (d.win_rate || 0) + "%";
  document.getElementById("dashWinRate").className = "stat-value " + ((d.win_rate || 0) >= 50 ? "positive" : "negative");

  document.getElementById("dashTrades").textContent = d.total_trades || 0;
  document.getElementById("dashOpen").textContent = d.open_positions || 0;
  document.getElementById("dashWinLoss").textContent = (d.wins || 0) + " / " + (d.losses || 0);

  renderSparkline(d.balance_history);
}

/* ── Sparkline ──────────────────────────────────────────────── */
function renderSparkline(history) {
  var canvas = document.getElementById("sparkline");
  if (!canvas || !history || history.length < 2) {
    if (canvas) { var ctx = canvas.getContext("2d"); ctx.clearRect(0, 0, canvas.width, canvas.height); }
    return;
  }

  var rect = canvas.parentElement.getBoundingClientRect();
  var w = Math.max(rect.width - 16, 60);
  var h = 80;
  canvas.style.width = w + "px";
  canvas.style.height = h + "px";
  var dpr = (window.devicePixelRatio || 1);
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);

  var ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.scale(dpr, dpr);

  var vals = history.map(function(p) { return p.b; });
  var min = Math.min.apply(null, vals);
  var max = Math.max.apply(null, vals);
  var range = max - min || 1;
  var pad = 10;

  var plotW = w - pad * 2;
  var plotH = h - pad * 2;

  // gradient fill
  ctx.beginPath();
  ctx.moveTo(pad, pad + plotH);
  for (var i = 0; i < vals.length; i++) {
    var x = pad + (i / (vals.length - 1)) * plotW;
    var y = pad + plotH - ((vals[i] - min) / range) * plotH;
    ctx.lineTo(x, y);
  }
  ctx.lineTo(pad + plotW, pad + plotH);
  ctx.closePath();

  var grad = ctx.createLinearGradient(0, pad, 0, pad + plotH);
  var alpha = 0.25 + 0.35 * (max - min) / Math.max(max, 1);
  grad.addColorStop(0, "rgba(52,211,153," + alpha.toFixed(2) + ")");
  grad.addColorStop(1, "rgba(52,211,153,0.02)");
  ctx.fillStyle = grad;
  ctx.fill();

  // line
  ctx.beginPath();
  for (var i = 0; i < vals.length; i++) {
    var x = pad + (i / (vals.length - 1)) * plotW;
    var y = pad + plotH - ((vals[i] - min) / range) * plotH;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = "#34d399";
  ctx.lineWidth = 2;
  ctx.stroke();

  // dots (first + last)
  ctx.fillStyle = "#34d399";
  [[0,0], [vals.length-1,1]].forEach(function(pair) {
    var i = pair[0];
    var x = pad + (i / (vals.length - 1)) * plotW;
    var y = pad + plotH - ((vals[i] - min) / range) * plotH;
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fill();
  });
}

/* ── Render Feed ────────────────────────────────────────────── */
function renderFeed(decisions) {
  var container = document.getElementById("feedList");
  var items = decisions.slice(0, 40);
  if (!items.length) {
    container.innerHTML = '<div class="list-empty">Waiting for data…</div>';
    return;
  }

  var html = "";
  for (var i = 0; i < items.length; i++) {
    var d = items[i];
    var pred = d.prediction || "PASS";
    var predClass = pred === "UP" ? "pred-up" : (pred === "DOWN" ? "pred-down" : "pred-pass");
    var statusBadge = "";
    if (d.status === "WIN") statusBadge = '<span class="status-badge badge-win">WIN</span>';
    else if (d.status === "LOSS") statusBadge = '<span class="status-badge badge-loss">LOSS</span>';
    else statusBadge = '<span class="status-badge badge-open">OPEN</span>';

    var entryPrice = d.entry_price ? "$" + fmtPrice(d.entry_price) : "";
    var convStars = "★".repeat(d.conviction || 0) + "☆".repeat(3 - (d.conviction || 0));
    var eventInfo = d.event_ticker || d.market_ticker || "";
    var timeStr = d.entry_time ? fmtTime(d.entry_time) : "";

    html += '<div class="list-item">' +
      '<div class="pred ' + predClass + '">' + pred + '</div>' +
      '<div class="info">' + convStars + ' · ' + entryPrice +
        '<span class="muted"> ' + eventInfo + '</span>' +
        '<br><span class="muted">' + timeStr + '</span></div>' +
      statusBadge +
    '</div>';
  }
  container.innerHTML = html;
}

/* ── Render Trades (full table) ─────────────────────────────── */
function renderTrades(decisions) {
  var container = document.getElementById("tradesList");
  var items = decisions.slice(0, 100);
  if (!items.length) {
    container.innerHTML = '<div class="list-empty">No trades yet.</div>';
    return;
  }

  var html = "";
  for (var i = 0; i < items.length; i++) {
    var d = items[i];
    var pred = (d.prediction || "PASS").toLowerCase();
    var predClass = "tr-" + pred;
    var predDisp = (d.prediction || "PASS").toUpperCase();

    var resultClass = "result-" + (d.status === "WIN" ? "win" : (d.status === "LOSS" ? "loss" : "open"));
    var resultDisp = d.status || "OPEN";

    var entryPrice = d.entry_price ? "$" + fmtPrice(d.entry_price) : "-";
    var pnlStr = "";
    if (d.pnl !== null && d.pnl !== undefined) {
      pnlStr = (d.pnl >= 0 ? "+" : "") + "$" + fmtPrice(d.pnl);
    }
    var timeStr = d.entry_time ? fmtTime(d.entry_time) : "";
    var market = d.market_ticker || "";

    html += '<div class="trade-row">' +
      '<div class="tr-pred ' + predClass + '">' + predDisp + '</div>' +
      '<div class="tr-detail">' + entryPrice +
        (pnlStr ? ' <span class="muted">(' + pnlStr + ')</span>' : '') +
        '<br><span class="muted">' + timeStr + ' ' + market + '</span></div>' +
      '<div class="tr-result ' + resultClass + '">' + resultDisp + '</div>' +
    '</div>';
  }
  container.innerHTML = html;
}

/* ── Render Settings ────────────────────────────────────────── */
function renderSettings(s) {
  // mode toggle
  var toggle = document.getElementById("modeToggle");
  toggle.checked = (s.mode === "live");
  document.getElementById("modeText").textContent = s.mode === "live" ? "Live" : "Paper";

  // position size
  document.getElementById("positionSize").value = s.position_size || 1;

  // conviction floor
  var conv = s.conviction_floor || 1;
  var btns = document.querySelectorAll(".conv-btn");
  btns.forEach(function(b) {
    b.classList.remove("active");
    if (parseInt(b.getAttribute("data-v"), 10) === conv) b.classList.add("active");
  });
}

/* ── Helpers ────────────────────────────────────────────────── */
function fmtNum(n) {
  if (n >= 1000) return n.toFixed(2);
  return n.toFixed(2);
}
function fmtPrice(n) {
  if (n === 0) return "0.00";
  return n < 1 ? n.toFixed(4) : n.toFixed(2);
}
function fmtTime(iso) {
  try {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    var now = new Date();
    var diff = (now.getTime() - d.getTime()) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    var month = (d.getMonth() + 1).toString();
    var day = d.getDate().toString();
    var hour = d.getHours().toString();
    var min = d.getMinutes().toString();
    return month.padStart(2,'0') + "/" + day.padStart(2,'0') + " " + hour.padStart(2,'0') + ":" + min.padStart(2,'0');
  } catch(e) { return iso; }
}