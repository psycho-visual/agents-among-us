/* Agents Among Us - batch mode: the "Batch ▶▶" dialog and the batch progress panel. Vanilla JS. */
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") e.className = v;
        else if (k === "style") for (const [p, val] of Object.entries(v)) e.style.setProperty(p, val);
        else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
        else e.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      e.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return e;
  }
  const put = (node, ...kids) => node.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
  const App = () => window.AAUApp || {};
  function usd(x) {
    if (typeof x !== "number" || !isFinite(x)) return "—";
    return App().fmtUSD ? App().fmtUSD(x) : "$" + x.toFixed(4);
  }
  const pct = (x) => (typeof x === "number" && isFinite(x) ? Math.round(x * 100) + "%" : "—");

  const STOP_TEXT = {
    games_limit: "it reached the game limit",
    budget: "another game could have gone over the spending limit",
    credit: "your OpenRouter credit fell below the minimum",
    errors: "too many games in a row ended with an error",
    stopped: "you stopped it",
    aborted: "you aborted it",
  };
  const OUTCOME_LABEL = {
    trio_win: "Trio wins", imposter_win: "Imposter wins", no_decision: "No decision",
    trio_win_no_imposter: "No imposter, trio right", trio_loss_no_imposter: "No imposter, copy voted out",
    error: "Errors", aborted: "Aborted",
  };

  // ------------------------------------------------------------------ state
  let B = null;
  function fresh() {
    B = { batchId: null, started: null, progress: null, finished: null, warnings: [], currentIndex: null,
      currentSwapped: false, mock: false, stopReasons: {} };
  }
  fresh();

  async function api(path, body) {
    const opts = body === undefined ? { cache: "no-store" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    try {
      const res = await fetch(path, opts);
      let data = {};
      try { data = await res.json(); } catch (_) { /* not JSON */ }
      return { ok: res.ok && data.ok !== false, status: res.status, data };
    } catch (e) {
      return { ok: false, status: 0, data: { message: "Could not reach the game server. Is run.py still running?" } };
    }
  }

  // ------------------------------------------------------------------ events from the server
  function handle(ev) {
    if (ev.batch_id && B.batchId && ev.batch_id !== B.batchId && ev.type === "batch_started") fresh();
    B.batchId = ev.batch_id || B.batchId;
    switch (ev.type) {
      case "batch_started":
        B.started = ev; B.finished = null; B.warnings = []; B.mock = !!ev.mock;
        App().clearBanner && App().clearBanner("batch-finished");
        break;
      case "batch_progress":
        B.progress = ev;
        break;
      case "batch_game_started":
        B.currentIndex = ev.batch_index; B.currentSwapped = !!ev.roles_swapped;
        break;
      case "batch_game_finished":
        if (App().loadStats) App().loadStats();
        break;
      case "batch_warning":
        B.warnings.push(String(ev.message || ""));
        break;
      case "batch_finished":
        B.finished = ev;
        if (App().loadStats) App().loadStats();
        finishedBanner(ev);
        break;
      default:
        break;
    }
    render();
  }

  function finishedBanner(ev) {
    const t = ev.totals || {};
    const reason = STOP_TEXT[ev.stop_reason] || ev.stop_reason || "it finished";
    const parts = [`${t.games_finished ?? "?"} game${t.games_finished === 1 ? "" : "s"} played`,
      `${usd(t.spent_usd)} spent${B.mock ? " (mock)" : ""}`];
    if (typeof t.imposter_caught_rate === "number") parts.push(`imposter caught ${pct(t.imposter_caught_rate)} of decisive games (random voting: 25%)`);
    if (App().banner) {
      App().banner(ev.stop_reason === "errors" || ev.stop_reason === "aborted" ? "warn" : "info",
        `It stopped because ${reason}. ${parts.join(" · ")}.` + (ev.record_file ? ` Batch record: ${ev.record_file}` : ""),
        { id: "batch-finished", title: "Batch finished." });
    }
  }

  function label() {
    const p = B.progress || {};
    const i = B.currentIndex || p.games_started || 0;
    const max = p.max_games ?? (B.started && B.started.max_games);
    return `Batch: game ${i}${max ? ` of ${max}` : ""}`;
  }

  // ------------------------------------------------------------------ panel
  function render() {
    const panel = $("batch-panel");
    if (!B.started && !B.progress) { panel.hidden = true; return; }
    panel.hidden = false;
    const p = B.progress || {};
    const st = App().status ? App().status() : null;
    const active = !!(st && st.batch && st.batch.active) && !B.finished;
    const stopping = !!(p.stop_requested || (st && st.batch && st.batch.stopping)) && active;
    const budget = typeof p.budget_usd === "number" ? p.budget_usd : (B.started && B.started.budget_usd);
    const spent = typeof p.spent_usd === "number" ? p.spent_usd : 0;
    const max = p.max_games ?? (B.started && B.started.max_games);
    const frac = budget ? Math.min(1, spent / budget) : null;
    const outcomes = Object.entries(p.outcomes || {}).filter(([, n]) => n);
    const decisive = (p.outcomes ? (p.outcomes.trio_win || 0) + (p.outcomes.imposter_win || 0) : 0);

    const chip = B.finished ? el("span", { class: "chip" }, "finished")
      : stopping ? el("span", { class: "chip chip-unsaved" }, "stopping after this game")
        : active ? el("span", { class: "chip chip-running" }, "running") : el("span", { class: "chip" }, "ended");
    const rows = [];
    rows.push(el("div", { class: "batch-big" },
      B.finished ? `${p.games_finished ?? 0} game${p.games_finished === 1 ? "" : "s"} played`
        : `Game ${B.currentIndex || p.games_started || 1}${max ? ` of ${max}` : ""}`,
      !B.finished && B.currentSwapped ? el("span", { class: "chip", title: "Trio and imposter models are swapped in this game" }, "roles swapped") : null));
    rows.push(el("div", { class: "batch-bar-wrap" },
      el("div", { class: "batch-bar-label small" },
        el("span", null, `${usd(spent)} spent`, B.mock ? " (mock)" : ""),
        el("span", { class: "muted" }, budget ? `limit ${usd(budget)}` : "no spending limit")),
      budget ? el("div", { class: "batch-bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(budget), "aria-valuenow": String(spent) },
        el("div", { class: "batch-bar-fill", style: { width: (frac * 100).toFixed(1) + "%" } })) : null));
    const kv = [];
    if (!B.finished && typeof p.estimated_games_left === "number") kv.push(["Games left (about)", String(p.estimated_games_left)]);
    kv.push(["Imposter caught", decisive ? `${pct(p.imposter_caught_rate)} of ${decisive} decisive (random: 25%)` : "no decisive games yet"]);
    kv.push(["Average cost per game", typeof p.mean_cost_usd === "number" ? usd(p.mean_cost_usd) : "—"]);
    if (typeof p.estimated_next_game_usd === "number" && !B.finished) kv.push(["Next game estimate", usd(p.estimated_next_game_usd)]);
    if (p.credit_remaining_usd != null) kv.push(["OpenRouter credit left", usd(p.credit_remaining_usd)]);
    rows.push(el("dl", { class: "kv batch-kv" }, kv.map(([k, v]) => [el("dt", null, k), el("dd", null, v)])));
    if (outcomes.length) {
      rows.push(el("div", { class: "batch-outcomes" }, outcomes.map(([k, n]) =>
        el("span", { class: "chip outcome-" + k }, `${OUTCOME_LABEL[k] || k}: ${n}`))));
    }
    for (const w of B.warnings.slice(-2)) rows.push(el("div", { class: "pd-warn small" }, "⚠ ", w));
    if (B.finished) {
      const reason = STOP_TEXT[B.finished.stop_reason] || B.finished.stop_reason;
      rows.push(el("div", { class: "batch-done small" }, `Stopped because ${reason}.`));
      rows.push(el("div", { class: "batch-actions" },
        el("button", { type: "button", onclick: openDialog }, "Run another batch ▶▶")));
    } else if (active) {
      rows.push(el("div", { class: "batch-actions" },
        el("button", { type: "button", onclick: stopBatch, disabled: stopping || null,
          title: "Let the current game finish, then stop" }, stopping ? "Stopping after this game…" : "Stop after this game"),
        el("button", { type: "button", class: "danger-ghost", onclick: abortBatch,
          title: "Stop the current game right now (it is saved as aborted)" }, "Abort now")));
    }
    put(panel,
      el("div", { class: "panel-head" }, el("h2", { id: "batch-h" }, "Batch"), chip),
      rows);
  }

  async function stopBatch() {
    const r = await api("/api/batch/stop", {});
    if (!r.ok && App().banner) App().banner("warn", r.data.message || "Could not stop the batch.", { id: "action-msg" });
    render();
  }
  async function abortBatch() {
    if (!confirm("Abort the batch now?\n\nThe game in progress stops immediately and is saved as “aborted”. " +
      "Games already finished are kept.")) return;
    const r = await api("/api/batch/abort", {});
    if (!r.ok && App().banner) App().banner("warn", r.data.message || "Could not abort the batch.", { id: "action-msg" });
    render();
  }

  // ------------------------------------------------------------------ dialog
  const dlg = $("batch-dialog");
  let planTimer = null, planSeq = 0, planOk = false, info = null;

  function readForm() {
    const v = (id) => $(id).value.trim();
    return {
      games: v("bd-games") === "" ? 0 : v("bd-games"),
      budget_usd: v("bd-budget") === "" ? null : v("bd-budget"),
      min_credit_usd: v("bd-min-credit") === "" ? null : v("bd-min-credit"),
      stop_after_errors: v("bd-errors") === "" ? 0 : v("bd-errors"),
      alternate_roles: $("bd-alternate").checked,
    };
  }
  function fill(d) {
    d = d || {};
    $("bd-games").value = d.games ?? 20;
    $("bd-budget").value = d.budget_usd ?? "";
    $("bd-min-credit").value = d.min_credit_usd ?? "";
    $("bd-errors").value = d.stop_after_errors ?? 3;
    $("bd-alternate").checked = !!d.alternate_roles;
  }

  async function openDialog() {
    for (const d of Object.values(window.AAUDrawers || {})) if (d.isOpen() && !d.close()) return;
    dlg.hidden = false;
    document.body.classList.add("drawer-open");
    put($("bd-plan"), el("span", { class: "muted" }, "Loading…"));
    put($("bd-msg"));
    const r = await api("/api/batch");
    info = r.data || {};
    if (!r.ok) {
      put($("bd-plan"), el("div", { class: "pd-err" }, info.message || "Batch mode is not available."));
      $("bd-start").disabled = true;
      return;
    }
    fill(info.defaults);
    $("bd-credit-field").hidden = !!info.mock;
    if (info.config_error) put($("bd-msg"), el("span", { class: "sd-err-text" }, info.config_error));
    refreshPlan();
    loadCredit();
    $("bd-games").focus();
  }
  function closeDialog() {
    dlg.hidden = true;
    document.body.classList.remove("drawer-open");
  }

  async function loadCredit() {
    const box = $("bd-credit");
    if (info && info.mock) { box.textContent = "Mock mode: no real credit is used."; return; }
    box.textContent = "Checking your credit…";
    const r = await api("/api/credit");
    const d = r.data || {};
    box.textContent = typeof d.credit_usd === "number"
      ? `Your OpenRouter credit right now: ${usd(d.credit_usd)}. Leave empty to not check it.`
      : `${d.message || "Your credit is unknown."} Leave empty to not check it.`;
  }

  function refreshPlan() {
    clearTimeout(planTimer);
    planOk = false;
    updateStart();
    planTimer = setTimeout(async () => {
      const seq = ++planSeq;
      const r = await api("/api/batch/plan", { options: readForm() });
      if (seq !== planSeq) return;
      const d = r.data || {};
      planOk = r.ok && d.ok !== false;
      put($("bd-plan"), planOk
        ? [el("div", { class: "bd-plan-h" }, "The plan"), el("div", null, d.plan || "")]
        : el("div", { class: "pd-err" }, "✖ ", d.message || "These options don't work."));
      updateStart();
    }, 250);
  }
  function updateStart() {
    const st = App().status ? App().status() : null;
    const busy = !!(st && (st.state === "running" || st.state === "pausing" || (st.batch && st.batch.active)));
    const btn = $("bd-start");
    btn.disabled = !planOk || busy;
    btn.title = busy ? "A game or batch is running — wait for it or pause it first" : !planOk ? "Fix the options first" : "Start the batch";
  }

  async function startBatch() {
    if ($("bd-start").disabled) return;
    $("bd-start").disabled = true;
    const r = await api("/api/batch/start", { options: readForm() });
    if (!r.ok) {
      put($("bd-msg"), el("span", { class: "sd-err-text" }, "✖ ", r.data.message || "Could not start the batch."));
      updateStart();
      return;
    }
    closeDialog();
  }

  for (const id of ["bd-games", "bd-budget", "bd-min-credit", "bd-errors"]) $(id).addEventListener("input", refreshPlan);
  $("bd-alternate").addEventListener("change", refreshPlan);
  $("btn-batch").addEventListener("click", openDialog);
  $("bd-close").addEventListener("click", closeDialog);
  $("bd-cancel").addEventListener("click", closeDialog);
  $("bd-backdrop").addEventListener("click", closeDialog);
  $("bd-start").addEventListener("click", startBatch);
  dlg.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeDialog(); }
    else if (e.key === "Enter" && e.target.tagName === "INPUT" && e.target.type !== "checkbox") { e.preventDefault(); startBatch(); }
  });
  document.addEventListener("aau:status", () => { render(); if (!dlg.hidden) updateStart(); });
  window.AAUDrawers = window.AAUDrawers || {};
  window.AAUDrawers.batch = { isOpen: () => !dlg.hidden, close: () => { closeDialog(); return true; } };

  window.AAUBatch = { handle, reset: () => { fresh(); render(); }, label };
})();
