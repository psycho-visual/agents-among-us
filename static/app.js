/* Agents Among Us - browser GUI. Vanilla JS, no build step.
 * All model text is inserted with textContent (never innerHTML). */
"use strict";
(function () {
  // ------------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") e.className = v;
        else if (k === "text") e.textContent = String(v);
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

  const fmtInt = (n) => (typeof n === "number" && isFinite(n) ? Math.round(n).toLocaleString("en-US") : "—");
  function fmtUSD(x) {
    if (typeof x !== "number" || !isFinite(x)) return "—";
    if (x === 0) return "$0";
    const a = Math.abs(x);
    if (a < 0.01) return "$" + x.toFixed(6);
    if (a < 1) return "$" + x.toFixed(4);
    return "$" + x.toFixed(2);
  }
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  function joinNames(names) {
    if (names.length <= 1) return names.join("");
    return names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
  }
  const humanize = (k) => (/^[a-z0-9_]+$/.test(String(k))
    ? String(k).replace(/_/g, " ").replace(/\bpct\b/, "%").replace(/^./, (c) => c.toUpperCase())
    : String(k));

  const SEAT_COLORS = {
    red: "#e5484d", blue: "#3e63dd", green: "#2f9e63", yellow: "#d9a400", orange: "#f76b15",
    purple: "#8e4ec6", pink: "#d6409f", teal: "#12a594", cyan: "#0797b9", brown: "#a07553",
    gray: "#8b8d98", grey: "#8b8d98", black: "#555", white: "#aaa", violet: "#6e56cf", lime: "#7cb518",
  };
  const PALETTE = ["#e5484d", "#3e63dd", "#2f9e63", "#d9a400", "#8e4ec6", "#f76b15", "#12a594", "#d6409f"];
  function seatColor(name) {
    if (!name) return "#8b8d98";
    const k = String(name).toLowerCase();
    if (SEAT_COLORS[k]) return SEAT_COLORS[k];
    const i = S.order.indexOf(name);
    return PALETTE[(i >= 0 ? i : 0) % PALETTE.length];
  }

  const ACTION_TEXT = { intro: "writing an intro", question: "asking a question", answer: "answering", vote: "voting" };
  const OUTCOME_SHORT = {
    trio_win: "Trio wins!", imposter_win: "Imposter wins!", no_decision: "No decision",
    error: "Ended with an error", aborted: "Aborted",
  };

  // ------------------------------------------------------------------ state
  const S = {};
  function freshState(gameId) {
    Object.assign(S, {
      gameId: gameId || null,
      lastSeq: -Infinity,
      players: {},          // name -> {name, seat, role, model, display_name, reasoning_effort, votes:[], eliminated, thinking, violations}
      order: [],            // names in seat order
      config: null,
      mock: S.mock || false,
      calls: {},            // call_id -> [api_call events]
      callTargets: {},      // call_id -> [{cost, chips}]
      violations: {},       // call_id -> [rule_violation events]
      turns: {},            // question_id -> {el, answers, asker}
      introBox: null,
      votes: {},            // vote_number -> {el, ballots, result}
      tot: { cost: 0, costKnown: false, inTok: 0, outTok: 0, reasonTok: 0, calls: 0, byModel: {}, sources: new Set(), unknownCost: 0 },
      gameOver: null,
      finished: false,
    });
  }
  S.order = [];
  freshState(null);
  let status = null;
  let connected = false;
  let autoCreated = false;
  let busy = false;

  const T = $("transcript");

  // ------------------------------------------------------------------ scrolling
  function nearBottom() {
    const doc = document.documentElement;
    return window.innerHeight + window.scrollY >= doc.scrollHeight - 140;
  }
  function withAutoScroll(fn) {
    const stick = nearBottom();
    fn();
    if (stick) window.scrollTo(0, document.documentElement.scrollHeight);
    else $("jump-latest").hidden = false;
  }
  window.addEventListener("scroll", () => { if (nearBottom()) $("jump-latest").hidden = true; }, { passive: true });
  $("jump-latest").addEventListener("click", () => {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
    $("jump-latest").hidden = true;
  });

  // ------------------------------------------------------------------ banners
  function banner(kind, message, opts = {}) {
    const box = $("banners");
    if (opts.id) {
      const old = document.getElementById(opts.id);
      if (old) old.remove();
    }
    const b = el("div", { class: `banner banner-${kind}`, id: opts.id || null, role: kind === "error" ? "alert" : "status" },
      el("div", { class: "banner-msg" }, opts.title ? el("strong", { text: opts.title + " " }) : null, el("span", { class: "pre", text: message })),
      opts.sticky ? null : el("button", { class: "banner-close", type: "button", title: "Dismiss", "aria-label": "Dismiss", onclick: () => b.remove() }, "×"));
    if (opts.gameScoped) b.dataset.game = "1";
    box.append(b);
    return b;
  }
  function clearBanner(id) { const b = document.getElementById(id); if (b) b.remove(); }

  // ------------------------------------------------------------------ reset
  function resetGame(gameId) {
    freshState(gameId);
    T.replaceChildren();
    document.body.classList.remove("game-over");
    document.querySelectorAll('#banners [data-game="1"]').forEach((b) => b.remove());
    $("players").replaceChildren(el("p", { class: "muted small" }, gameId ? "Waiting for the game to start…" : "No game yet."));
    $("settings").replaceChildren();
    $("jump-latest").hidden = true;
    renderEmptyState();
    renderCost();
  }
  function renderEmptyState() {
    if (T.querySelector(".empty-state")) return;
    T.append(el("div", { class: "empty-state", id: "empty-state" },
      el("h2", null, "Four AIs. One imposter."),
      el("p", null, "Three players are copies of the same model; one is a different model trying to blend in. " +
        "They introduce themselves, take turns asking each other questions, and then vote someone out."),
      el("p", null, "Click ", el("b", null, "Next round ▶"), " to play step by step, or ", el("b", null, "Run whole game ⏩"), " to watch it all.")));
  }
  function dropEmptyState() { const e = $("empty-state"); if (e) e.remove(); }

  // ------------------------------------------------------------------ players
  function nameTag(name, extraClass) {
    if (!name) return el("span", { class: "name-tag none" }, "nobody");
    return el("span", { class: "name-tag " + (extraClass || ""), style: { "--seat": seatColor(name) } },
      el("span", { class: "dot", "aria-hidden": "true" }), name);
  }
  function roleChip(role) {
    if (!role) return null;
    return el("span", { class: `chip role role-${role}` }, role === "imposter" ? "Imposter" : "Trio");
  }

  function renderPlayers() {
    const box = $("players");
    if (!S.order.length) return;
    box.replaceChildren(...S.order.map((name, i) => {
      const p = S.players[name];
      const card = el("div", {
        class: "player" + (p.eliminated ? " eliminated" : "") + (p.thinking ? " is-thinking" : ""),
        style: { "--seat": seatColor(name) },
      });
      const top = el("div", { class: "player-top" },
        el("span", { class: "swatch", "aria-hidden": "true" }),
        el("span", { class: "player-name" }, name),
        el("span", { class: "muted small" }, `seat ${i + 1}`),
        el("span", { class: "spacer" }),
        el("span", { class: "role-info" }, roleChip(p.role)),
        el("span", { class: "role-hidden chip" }, "role hidden"));
      const model = el("div", { class: "player-model small role-info" },
        el("span", null, p.display_name || p.model || "unknown model"),
        p.display_name && p.model && p.display_name !== p.model ? el("span", { class: "muted" }, ` · ${p.model}`) : null,
        p.reasoning_effort ? el("span", { class: "muted" }, ` · effort: ${p.reasoning_effort}`) : null);
      const bits = [];
      if (p.thinking) bits.push(el("span", { class: "thinking" }, el("span", { class: "spinner", "aria-hidden": "true" }), `thinking… (${ACTION_TEXT[p.thinking] || p.thinking})`));
      if (p.eliminated) bits.push(el("span", { class: "chip chip-out" }, "✖ voted out"));
      if (p.votes.length) {
        bits.push(el("span", { class: "small votes-received", title: "Votes received in each vote" },
          "votes received: ", p.votes.map((v) => `${v.count}`).join(" / ")));
      }
      if (p.violations) bits.push(el("span", { class: "chip chip-warn", title: "Rule violations (named a model or company)" }, `⚠ ${p.violations}`));
      card.append(top, model);
      if (bits.length) card.append(el("div", { class: "player-status" }, bits));
      return card;
    }));
  }

  function setThinking(name, action) {
    const p = S.players[name];
    if (!p) return;
    p.thinking = action || null;
    renderPlayers();
    renderStatus();
  }
  function clearAllThinking() {
    let changed = false;
    for (const p of Object.values(S.players)) if (p.thinking) { p.thinking = null; changed = true; }
    if (changed) { renderPlayers(); renderStatus(); }
  }

  function renderSettings() {
    const c = S.config || {};
    const r = c.rules || c;
    const parts = [];
    const trio = c.trio || {}, imp = c.imposter || {};
    if (trio.display_name || trio.model) parts.push(el("div", null, el("b", null, "Matchup: "),
      `trio ${trio.display_name || trio.model}${trio.reasoning_effort ? ` (${trio.reasoning_effort})` : ""}`,
      " vs imposter ", `${imp.display_name || imp.model || "?"}${imp.reasoning_effort ? ` (${imp.reasoning_effort})` : ""}`));
    const rules = [];
    if (r.name_rule) rules.push(r.name_rule === "no_names" ? "no model names allowed" : "lying about names allowed");
    if (r.answer_mode) rules.push(`${r.answer_mode} answers`);
    if (r.rounds_before_vote) rules.push(`vote every ${plural(r.rounds_before_vote, "round")}`);
    if (r.vote_threshold) rules.push(`${plural(r.vote_threshold, "vote")} to eliminate`);
    if (r.on_no_decision) rules.push(r.on_no_decision === "continue" ? `keep going if undecided (max ${r.max_rounds} rounds)` : "ends if undecided");
    if (rules.length) parts.push(el("div", null, el("b", null, "Rules: "), rules.join(" · ")));
    $("settings").replaceChildren(...parts);
  }

  // ------------------------------------------------------------------ cost panel
  function renderCost() {
    const t = S.tot;
    $("cost-total").textContent = t.calls ? fmtUSD(t.cost) : "$0";
    $("tok-in").textContent = fmtInt(t.inTok);
    $("tok-out").textContent = fmtInt(t.outTok);
    $("tok-reason").textContent = fmtInt(t.reasonTok);
    $("num-calls").textContent = fmtInt(t.calls);
    const notes = [];
    if (S.mock || t.sources.has("mock")) notes.push("(mock)");
    if (t.sources.has("estimated")) notes.push("(estimated)");
    if (t.unknownCost) notes.push(`(${t.unknownCost} calls without cost)`);
    $("cost-note").textContent = notes.join(" ");
    const rows = Object.entries(t.byModel).sort((a, b) => b[1].cost - a[1].cost);
    const box = $("cost-by-model");
    if (!rows.length) { box.replaceChildren(); return; }
    box.replaceChildren(el("div", { class: "small muted by-model-h" }, "By model"),
      ...rows.map(([model, v]) => el("div", { class: "by-model-row" },
        el("span", { class: "by-model-name" }, model, v.role ? el("span", { class: "muted" }, ` (${v.role})`) : null),
        el("span", { class: "muted small" }, plural(v.calls, "call")),
        el("span", { class: "by-model-cost" }, fmtUSD(v.cost)))));
  }

  function callLine(callId) {
    const evs = S.calls[callId];
    if (!evs || !evs.length) return null;
    let inT = 0, outT = 0, rT = 0, cost = 0, costKnown = false, lat = 0;
    for (const e of evs) {
      inT += e.input_tokens || 0; outT += e.output_tokens || 0; rT += e.reasoning_tokens || 0;
      if (typeof e.cost_usd === "number") { cost += e.cost_usd; costKnown = true; }
      lat += e.latency_s || 0;
    }
    const last = evs[evs.length - 1];
    const parts = [`${fmtInt(inT)} in`, `${fmtInt(outT)} out`];
    if (rT) parts.push(`${fmtInt(rT)} reasoning`);
    parts.push(costKnown ? fmtUSD(cost) + (last.cost_source === "estimated" ? " est." : "") : "cost n/a");
    if (lat) parts.push(`${lat.toFixed(1)}s`);
    if (evs.length > 1) parts.push(`${evs.length} attempts`);
    return el("span", { class: "call-line", title: `call ${callId}` }, parts.join(" · "),
      last.model ? el("span", { class: "role-info" }, " · " + last.model) : null);
  }

  function registerCallTarget(callId, costSlot, chipSlot) {
    if (!callId) return;
    (S.callTargets[callId] = S.callTargets[callId] || []).push({ costSlot, chipSlot });
    fillCallTargets(callId);
  }
  function fillCallTargets(callId) {
    for (const t of S.callTargets[callId] || []) {
      const line = callLine(callId);
      t.costSlot.replaceChildren(...(line ? [line] : []));
      t.chipSlot.replaceChildren(...(S.violations[callId] || []).map(violationChip));
    }
  }
  function violationChip(v) {
    const matches = Array.isArray(v.matches) ? v.matches.join(", ") : "";
    return el("span", { class: "chip chip-warn", title: "Rule violation: named a model or company" + (v.redacted ? " (redacted before other players saw it)" : "") },
      `⚠ named ${matches || "a model"}${v.redacted ? " · redacted" : ""}`);
  }

  // ------------------------------------------------------------------ transcript pieces
  function reasoningBlock(reasoning, label) {
    if (reasoning == null) return el("div", { class: "no-reasoning small" }, "No reasoning returned by API");
    if (String(reasoning).trim() === "") return el("div", { class: "no-reasoning small" }, "Reasoning returned, but empty");
    const words = String(reasoning).trim().split(/\s+/).length;
    return el("div", null,
      el("details", { class: "reasoning spoiler" },
        el("summary", null, label || "Reasoning", el("span", { class: "muted small" }, ` · ${fmtInt(words)} words`)),
        el("div", { class: "reasoning-text pre" }, String(reasoning))),
      el("div", { class: "spoiler-note small" }, "Reasoning hidden until the game is over (detective mode)"));
  }

  function insertRanked(container, node, rank) {
    node.dataset.rank = String(rank);
    for (const child of container.children) {
      if (Number(child.dataset.rank) > rank) { container.insertBefore(node, child); return; }
    }
    container.append(node);
  }
  const seatIndex = (name) => { const i = S.order.indexOf(name); return i >= 0 ? i : 99; };

  function messageEl(ev) {
    const costSlot = el("span", { class: "cost-slot" });
    const chipSlot = el("span", { class: "chip-slot" });
    const verb = { intro: "introduces themself", question: "asks", answer: "answers" }[ev.kind] || ev.kind;
    const node = el("article", { class: `msg kind-${ev.kind}`, style: { "--seat": seatColor(ev.player) } },
      el("div", { class: "msg-head" }, nameTag(ev.player), el("span", { class: "muted small verb" }, verb), chipSlot,
        el("span", { class: "spacer" }), costSlot),
      el("div", { class: "msg-text pre" }, ev.text == null ? "" : String(ev.text)),
      ev.original_text != null && ev.original_text !== ev.text
        ? el("details", { class: "original" }, el("summary", null, "Original text (before redaction)"),
          el("div", { class: "pre" }, String(ev.original_text)))
        : null,
      reasoningBlock(ev.reasoning));
    registerCallTarget(ev.call_id, costSlot, chipSlot);
    return node;
  }

  function phaseHeader(text, kind) {
    return el("div", { class: `phase phase-${kind || "x"}` }, el("span", null, text));
  }

  function voteBlock(n) {
    const key = String(n ?? "?");
    if (S.votes[key]) return S.votes[key];
    dropEmptyState();
    const ballots = el("div", { class: "ballots" });
    const result = el("div", { class: "vote-result-slot" });
    const node = el("section", { class: "vote-block" },
      el("h3", null, `Vote ${n ?? ""}`.trim(), el("span", { class: "muted small" }, " · secret ballot, everyone votes at once")),
      ballots, result);
    T.append(node);
    return (S.votes[key] = { el: node, ballots, result });
  }

  // ------------------------------------------------------------------ event handlers
  const handlers = {
    game_started(ev) {
      if (typeof ev.mock === "boolean") S.mock = ev.mock;
      S.config = ev.config || null;
      const ps = Array.isArray(ev.players) ? ev.players.slice() : [];
      ps.sort((a, b) => (a.seat ?? 0) - (b.seat ?? 0));
      S.order = ps.map((p) => p.name);
      S.players = {};
      for (const p of ps) S.players[p.name] = Object.assign({ votes: [], eliminated: false, thinking: null, violations: 0 }, p);
      renderPlayers();
      renderSettings();
      renderCost();
      renderMockBadge();
    },

    phase(ev) {
      clearAllThinking();
      dropEmptyState();
      const label = ev.label || (ev.phase === "intro" ? "Introductions" : ev.phase === "vote" ? "Vote" : `Round ${ev.round ?? ""}`);
      T.append(phaseHeader(label, ev.phase));
      if (ev.phase === "intro") {
        S.introBox = el("div", { class: "intros" });
        T.append(S.introBox);
      }
    },

    thinking(ev) { setThinking(ev.player, ev.action || "thinking"); },

    message(ev) {
      if (S.players[ev.player]) S.players[ev.player].thinking = null;
      dropEmptyState();
      const node = messageEl(ev);
      if (ev.kind === "intro") {
        if (!S.introBox) { S.introBox = el("div", { class: "intros" }); T.append(S.introBox); }
        // Insert in seat order so arrival order never reveals the imposter.
        insertRanked(S.introBox, node, seatIndex(ev.player));
      } else if (ev.kind === "question") {
        const answers = el("div", { class: "answers" });
        const title = [ev.round != null ? `Round ${ev.round}` : null, ev.turn != null ? `Turn ${ev.turn}` : null].filter(Boolean).join(" · ");
        const turn = el("section", { class: "turn", style: { "--seat": seatColor(ev.player) } },
          el("div", { class: "turn-head small muted" }, title ? title + " — " : "", nameTag(ev.player), " asks everyone"),
          node, answers);
        S.turns[ev.question_id || `q-${ev.call_id}`] = { el: turn, answers, asker: ev.player };
        T.append(turn);
      } else if (ev.kind === "answer" && ev.question_id && S.turns[ev.question_id]) {
        const t = S.turns[ev.question_id];
        const n = S.order.length || 4;
        const asker = ev.asker || t.asker;
        // Answer order: seat order starting after the asker (the asker answers last).
        const rank = (seatIndex(ev.player) - seatIndex(asker) - 1 + n) % n;
        insertRanked(t.answers, node, rank);
      } else {
        T.append(node);
      }
      renderPlayers();
      renderStatus();
    },

    vote_cast(ev) {
      if (S.players[ev.player]) S.players[ev.player].thinking = null;
      const vb = voteBlock(ev.vote_number);
      const costSlot = el("span", { class: "cost-slot" });
      const chipSlot = el("span", { class: "chip-slot" });
      const node = el("div", { class: "ballot", style: { "--seat": seatColor(ev.player) } },
        el("div", { class: "msg-head" }, nameTag(ev.player), el("span", { class: "arrow" }, "votes for"),
          ev.target ? nameTag(ev.target) : el("span", { class: "chip chip-abstain" }, "abstains"),
          ev.valid === false ? el("span", { class: "chip chip-warn", title: "The vote was invalid (even after one retry) and counts as an abstention" }, "invalid vote") : null,
          chipSlot, el("span", { class: "spacer" }), costSlot),
        ev.raw_text ? el("div", null,
          el("details", { class: "spoiler" }, el("summary", null, "Their explanation"), el("div", { class: "pre" }, String(ev.raw_text))),
          el("div", { class: "spoiler-note small" }, "Explanation hidden until the game is over (detective mode)")) : null,
        reasoningBlock(ev.reasoning));
      registerCallTarget(ev.call_id, costSlot, chipSlot);
      insertRanked(vb.ballots, node, seatIndex(ev.player));
      renderPlayers();
      renderStatus();
    },

    vote_result(ev) {
      clearAllThinking();
      const vb = voteBlock(ev.vote_number);
      const tally = ev.tally || {};
      const names = S.order.length ? S.order : Object.keys(tally);
      for (const name of names) {
        const p = S.players[name];
        if (p) p.votes.push({ n: ev.vote_number, count: tally[name] || 0 });
      }
      const elim = ev.eliminated;
      if (elim && S.players[elim]) S.players[elim].eliminated = true;
      const role = ev.eliminated_role;
      const thr = (S.config && (S.config.rules || S.config).vote_threshold) || 3;
      let headline, cls;
      switch (ev.decision) {
        case "trio_win": headline = [nameTag(elim), " was voted out — and was the imposter! The trio wins."]; cls = "good"; break;
        case "imposter_win": headline = [nameTag(elim), " was voted out — but was a trio member. The imposter wins!"]; cls = "bad"; break;
        case "continue": headline = [`Nobody reached ${thr} votes — the game continues.`]; cls = "neutral"; break;
        case "no_decision": headline = [`Nobody reached ${thr} votes — no decision.`]; cls = "neutral"; break;
        default: headline = [elim ? [nameTag(elim), ` was voted out (${role || "unknown role"}).`] : "No one was voted out."]; cls = "neutral";
      }
      const tallyRows = Object.entries(tally).sort((a, b) => b[1] - a[1] || seatIndex(a[0]) - seatIndex(b[0]));
      vb.result.replaceChildren(el("div", { class: `vote-result ${cls}` },
        el("div", { class: "vote-headline" }, headline),
        el("div", { class: "tally" }, el("span", { class: "muted small" }, "Tally: "),
          tallyRows.length ? tallyRows.map(([n, c]) => el("span", { class: "tally-item" }, nameTag(n), ` ${c}`)) : el("span", { class: "muted small" }, "no valid votes")),
        ev.rounds_completed != null ? el("div", { class: "muted small" }, `After ${plural(ev.rounds_completed, "complete round")}.`) : null));
      renderPlayers();
    },

    api_call(ev) {
      const t = S.tot;
      (S.calls[ev.call_id] = S.calls[ev.call_id] || []).push(ev);
      t.calls += 1;
      t.reasonTok += ev.reasoning_tokens || 0;
      if (ev.cost_source) t.sources.add(ev.cost_source);
      if (typeof ev.cost_usd !== "number") t.unknownCost += 1;
      t.cost = typeof ev.total_cost_usd === "number" ? ev.total_cost_usd : t.cost + (ev.cost_usd || 0);
      t.inTok = typeof ev.total_input_tokens === "number" ? ev.total_input_tokens : t.inTok + (ev.input_tokens || 0);
      t.outTok = typeof ev.total_output_tokens === "number" ? ev.total_output_tokens : t.outTok + (ev.output_tokens || 0);
      const p = S.players[ev.player];
      const model = (p && p.model) || ev.model || "unknown";
      const m = (t.byModel[model] = t.byModel[model] || { cost: 0, calls: 0, role: p ? (p.role === "imposter" ? "imposter" : "trio") : null });
      m.cost += ev.cost_usd || 0;
      m.calls += 1;
      fillCallTargets(ev.call_id);
      renderCost();
    },

    rule_violation(ev) {
      (S.violations[ev.call_id] = S.violations[ev.call_id] || []).push(ev);
      const p = S.players[ev.player];
      if (p) p.violations = (p.violations || 0) + 1;
      fillCallTargets(ev.call_id);
      renderPlayers();
    },

    error(ev) {
      if (ev.player && S.players[ev.player]) S.players[ev.player].thinking = null;
      const who = ev.player ? `${ev.player}: ` : "";
      banner("error", who + (ev.message || "Unknown error"), { title: ev.fatal ? "Error (game stopped):" : "Error:", gameScoped: true });
      dropEmptyState();
      T.append(el("div", { class: "error-line" }, el("strong", null, ev.fatal ? "Error (game stopped): " : "Error: "), el("span", { class: "pre" }, who + (ev.message || ""))));
      renderPlayers();
    },

    game_over(ev) {
      S.gameOver = ev;
      S.finished = true;
      clearAllThinking();
      document.body.classList.add("game-over");
      if (ev.eliminated && S.players[ev.eliminated]) S.players[ev.eliminated].eliminated = true;
      renderGameOver(ev);
      renderPlayers();
      renderStatus();
      loadStats();
    },
  };

  function renderGameOver(ev) {
    const old = document.getElementById("gameover-card");
    if (old) old.remove();
    const imposter = S.order.find((n) => S.players[n] && S.players[n].role === "imposter");
    const ip = imposter ? S.players[imposter] : null;
    const titles = {
      trio_win: "The trio wins! 🎉", imposter_win: "The imposter wins! 🕵️", no_decision: "No decision 🤷",
      error: "Game ended with an error", aborted: "Game aborted",
    };
    const cls = { trio_win: "good", imposter_win: "bad", error: "err", aborted: "err" }[ev.outcome] || "neutral";
    const rows = [];
    if (ev.eliminated) {
      const ep = S.players[ev.eliminated] || {};
      rows.push(["Voted out", [nameTag(ev.eliminated), ` — ${ev.eliminated_role || ep.role || "?"}`, ep.display_name || ep.model ? ` (${ep.display_name || ep.model})` : ""]]);
      if (ev.rounds_survived != null) rows.push(["Rounds survived", String(ev.rounds_survived)]);
    } else {
      rows.push(["Voted out", "nobody"]);
    }
    if (ip) rows.push(["The imposter was", [nameTag(imposter), ` — ${ip.display_name || ip.model || ""}${ip.reasoning_effort ? ` (effort ${ip.reasoning_effort})` : ""}`]]);
    if (ev.rounds_completed != null) rows.push(["Rounds played", String(ev.rounds_completed)]);
    const costNote = S.tot.sources.has("estimated") ? " (estimated)" : (S.mock || S.tot.sources.has("mock")) ? " (mock)" : "";
    rows.push(["Total cost", fmtUSD(typeof ev.total_cost_usd === "number" ? ev.total_cost_usd : S.tot.cost) + costNote]);
    rows.push(["Tokens", `${fmtInt(ev.total_input_tokens ?? S.tot.inTok)} in · ${fmtInt(ev.total_output_tokens ?? S.tot.outTok)} out · ${fmtInt(ev.num_api_calls ?? S.tot.calls)} API calls`]);
    if (ev.log_file) rows.push(["Full log", el("code", { class: "path" }, String(ev.log_file))]);
    if (ev.transcript_file) rows.push(["Transcript", el("code", { class: "path" }, String(ev.transcript_file))]);
    const card = el("section", { class: `gameover ${cls}`, id: "gameover-card" },
      el("div", { class: "gameover-kicker small" }, "Game over"),
      el("h2", null, titles[ev.outcome] || `Game over: ${ev.outcome}`),
      el("dl", { class: "kv gameover-kv" }, rows.map(([k, v]) => [el("dt", null, k), el("dd", null, v)])),
      el("div", { class: "small muted" }, "Click ", el("b", null, "New game"), " to play again (config.toml is re-read, so edits apply)."));
    withAutoScroll(() => T.append(card));
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ------------------------------------------------------------------ status / controls
  function renderMockBadge() { $("mock-badge").hidden = !(S.mock || (status && status.mock)); }

  function thinkingText() {
    const groups = {};
    for (const name of S.order) {
      const p = S.players[name];
      if (p && p.thinking) (groups[p.thinking] = groups[p.thinking] || []).push(name);
    }
    const parts = Object.entries(groups).map(([action, names]) =>
      `${joinNames(names)} ${names.length > 1 ? "are" : "is"} ${ACTION_TEXT[action] || "thinking"}`);
    return parts.length ? parts.join("; ") + "…" : "";
  }

  function renderStatus() {
    const st = status;
    const dot = $("status-dot");
    let text, next = "", state = st ? st.state : "connecting";
    if (!connected) { text = "Connecting to the game server…"; state = "connecting"; }
    else if (!st) text = "Loading…";
    else if (!st.has_game) text = st.config_error ? "Can't start a game yet — see the message above." : "Setting up a new game…";
    else if (st.state === "running") {
      const th = thinkingText();
      text = "Running — " + (th || "working…");
      if (st.mode === "round") next = "will pause at the end of this round";
    } else if (st.state === "pausing") text = "Pausing after the current turn…";
    else if (st.state === "paused") { text = "Paused"; next = st.next_label ? `Up next: ${st.next_label}` : ""; }
    else if (st.state === "finished" || st.finished) {
      const o = S.gameOver && S.gameOver.outcome;
      text = "Finished" + (o ? `: ${OUTCOME_SHORT[o] || o}` : "");
    } else {
      text = "Ready — click “Next round ▶” to play step by step, or “Run whole game ⏩”.";
      next = st.next_label ? `Up next: ${st.next_label}` : "";
    }
    $("status-text").textContent = text;
    $("status-next").textContent = next;
    dot.className = "status-dot state-" + state;
    document.body.dataset.state = state;
  }

  function renderControls() {
    const st = status;
    const has = !!(st && st.has_game);
    const state = st ? st.state : "connecting";
    const runningish = state === "running" || state === "pausing";
    const canPlay = connected && has && !busy && (state === "idle" || state === "paused") && !st.finished;
    $("btn-new").disabled = !connected || busy || runningish;
    $("btn-next").disabled = !canPlay;
    $("btn-all").disabled = !(canPlay || (connected && !busy && state === "running" && st.mode === "round"));
    $("btn-pause").disabled = !(connected && state === "running");
    const parts = st && st.next_label ? String(st.next_label).split(" · ") : [];
    const label = parts[0] || "";
    // "Round 2 · Green asks" where Green is not the first seat means we stopped mid-round.
    const asker = parts[1] ? parts[1].replace(/\s+asks$/, "") : "";
    const midRound = /^Round\b/.test(label) && asker && S.order.length && S.order[0] !== asker;
    const nb = $("btn-next");
    nb.textContent = label && !st.finished ? `${midRound ? "Finish" : "Next:"} ${label} ▶` : "Next round ▶";
    nb.title = st && st.next_label ? `Play until the end of: ${st.next_label}` : "Play until the end of the next round (or vote)";
    $("btn-pause").textContent = state === "pausing" ? "Pausing…" : "Pause ⏸";
  }

  function onStatus(st) {
    if (st.game_id && st.game_id !== S.gameId) resetGame(st.game_id);
    status = st;
    if (typeof st.mock === "boolean") S.mock = S.mock || st.mock;
    if (st.state !== "running" && st.state !== "pausing") clearAllThinking();
    if (st.config_error) banner("error", st.config_error, { id: "config-error", sticky: true, title: st.has_game ? "Couldn't start a new game:" : "Setup problem:" });
    else clearBanner("config-error");
    renderMockBadge();
    renderStatus();
    renderControls();
    if (!st.has_game && !st.config_error && !autoCreated) {
      autoCreated = true;
      post("/api/new_game");
    }
  }

  async function post(path) {
    busy = true;
    renderControls();
    try {
      const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      let data = null;
      try { data = await res.json(); } catch (_) { /* ignore */ }
      if (!res.ok || (data && data.ok === false)) {
        const msg = (data && data.message) || `Request failed (${res.status})`;
        if (!(data && data.status && data.status.config_error)) banner("warn", msg, { id: "action-msg" });
      } else {
        clearBanner("action-msg");
      }
      if (data && data.status) onStatus(data.status);
    } catch (e) {
      banner("error", "Could not reach the game server. Is run.py still running?", { id: "action-msg" });
    } finally {
      busy = false;
      renderControls();
    }
  }

  $("btn-new").addEventListener("click", () => {
    if (S.order.length && !S.finished && status && status.state === "paused" &&
        !confirm("Start a new game? The current game hasn't finished.")) return;
    post("/api/new_game");
  });
  $("btn-next").addEventListener("click", () => post("/api/next_round"));
  $("btn-all").addEventListener("click", () => post("/api/run_all"));
  $("btn-pause").addEventListener("click", () => post("/api/pause"));

  // ------------------------------------------------------------------ detective toggle
  const hide = $("hide-roles");
  try { hide.checked = localStorage.getItem("aau-hide-roles") === "1"; } catch (_) { /* storage blocked */ }
  function applyDetective() { document.body.classList.toggle("detective", hide.checked); }
  hide.addEventListener("change", () => {
    applyDetective();
    try { localStorage.setItem("aau-hide-roles", hide.checked ? "1" : "0"); } catch (_) { /* ignore */ }
  });
  applyDetective();

  // ------------------------------------------------------------------ past games (stats)
  const statsPanel = $("stats-panel");
  statsPanel.addEventListener("toggle", () => { if (statsPanel.open) loadStats(); });

  function isScalar(v) { return v == null || ["string", "number", "boolean"].includes(typeof v); }
  function fmtStat(key, v) {
    if (v == null) return "—";
    if (typeof v === "boolean") return v ? "yes" : "no";
    if (typeof v === "number") {
      // key may be a path like "cost_usd.mean"; only look at the last two parts.
      const k = String(key).toLowerCase().split(".").slice(-2).join(".");
      if (k.includes("cost") || k.includes("usd")) return fmtUSD(v);
      if ((k.includes("rate") || k.includes("pct") || k.includes("baseline") || k.includes("fraction") || k.includes("share")) && v >= 0 && v <= 1) return (v * 100).toFixed(1) + "%";
      return Number.isInteger(v) ? fmtInt(v) : v.toFixed(2);
    }
    return String(v);
  }
  function statTable(obj, ctx, skip) {
    const rows = Object.entries(obj);
    const cols = [];
    for (const [, r] of rows) for (const c of Object.keys(r)) if (!cols.includes(c) && isScalar(r[c]) && !(skip || []).includes(c)) cols.push(c);
    return el("div", { class: "table-wrap" }, el("table", { class: "stats-table" },
      el("thead", null, el("tr", null, el("th", null, ""), cols.map((c) => el("th", null, humanize(c))))),
      el("tbody", null, rows.map(([k, r]) => el("tr", null, el("th", null, humanize(k)), cols.map((c) => el("td", null, fmtStat(ctx + "." + c, r[c]))))))));
  }
  function statValue(key, v, depth) {
    if (isScalar(v)) return el("span", null, fmtStat(key, v));
    if (Array.isArray(v)) {
      if (v.length && v.every((x) => x && typeof x === "object" && !Array.isArray(x))) {
        const obj = {};
        const pair = v.every((x) => x.trio_model && x.imposter_model);
        v.forEach((x, i) => {
          obj[pair ? `trio ${x.trio_model} vs imposter ${x.imposter_model}` : (x.model || x.name || x.pairing || String(i + 1))] = x;
        });
        return statTable(obj, key, pair ? ["trio_model", "imposter_model"] : []);
      }
      return el("span", null, v.map((x) => (isScalar(x) ? fmtStat(key, x) : JSON.stringify(x))).join(", "));
    }
    const entries = Object.entries(v);
    if (!entries.length) return el("span", { class: "muted" }, "—");
    if (entries.every(([, x]) => isScalar(x))) {
      return el("dl", { class: "kv" }, entries.map(([k, x]) => [el("dt", null, humanize(k)), el("dd", null, fmtStat(key + "." + k, x))]));
    }
    if (entries.every(([, x]) => x && typeof x === "object" && !Array.isArray(x) && Object.values(x).every(isScalar))) {
      return statTable(v, key);
    }
    return el("div", { class: "stats-nested" }, entries.map(([k, x]) => statSection(k, x, depth + 1, key)));
  }
  function statSection(key, v, depth, parent) {
    const path = parent ? parent + "." + key : key;
    if (isScalar(v)) return el("dl", { class: "kv" }, el("dt", null, humanize(key)), el("dd", null, fmtStat(path, v)));
    return el("div", { class: "stats-section" }, el(depth > 0 ? "h4" : "h3", null, humanize(key)), statValue(key, v, depth));
  }

  const isObj = (x) => x && typeof x === "object" && !Array.isArray(x);
  const pct = (x) => (typeof x === "number" && isFinite(x) ? (x * 100).toFixed(0) + "%" : "—");

  /* Friendly view of the keys compute_stats is known to return; anything else is rendered generically. */
  function renderStats(data, which) {
    const st = isObj(data.stats) ? data.stats : {};
    const used = new Set(["num_games", "num_mock_games"]);
    const out = [el("p", { class: "muted small" }, plural(data.num_games, which + " game") +
      (data.mock ? " (mock mode shows mock games only)." : " (mock games excluded)."))];
    const head = [];
    const ic = st.imposter_caught;
    if (isObj(ic)) {
      used.add("imposter_caught");
      head.push(["Imposter caught", ic.decisive_games
        ? `${ic.caught ?? "?"} of ${ic.decisive_games} decisive games (${pct(ic.rate)})` : "no decisive games yet"]);
      head.push(["Random-voting baseline", pct(ic.random_baseline ?? 0.25)]);
    }
    const tb = st.trio_ballots;
    if (isObj(tb)) {
      used.add("trio_ballots");
      head.push(["Trio ballots on the imposter", tb.valid_trio_ballots
        ? `${tb.on_imposter ?? "?"} of ${tb.valid_trio_ballots} (${pct(tb.rate)}; random ${pct(tb.random_baseline)})` : "—"]);
    }
    if (isObj(st.rounds_survived) && st.rounds_survived.count) {
      used.add("rounds_survived");
      head.push(["Avg rounds survived (voted out)", fmtStat("rounds", st.rounds_survived.mean)]);
    }
    if (isObj(st.cost_usd)) {
      used.add("cost_usd");
      head.push(["Avg cost per game", fmtUSD(st.cost_usd.mean)]);
      head.push(["Total spent", fmtUSD(st.cost_usd.total)]);
    }
    if (head.length) out.push(el("dl", { class: "kv" }, head.map(([k, v]) => [el("dt", null, k), el("dd", null, v)])));

    if (isObj(st.outcomes)) {
      used.add("outcomes");
      out.push(el("div", { class: "small" }, el("b", null, "Outcomes: "),
        Object.entries(st.outcomes).map(([k, v]) => `${OUTCOME_SHORT[k] || humanize(k)} ${v}`).join(" · ")));
    }
    const bm = st.by_model;
    if (isObj(bm) && Object.values(bm).every((v) => isObj(v) && (isObj(v.as_trio) || isObj(v.as_imposter)))) {
      used.add("by_model");
      const rows = [];
      for (const [model, rec] of Object.entries(bm)) {
        for (const [side, label] of [["as_trio", "trio"], ["as_imposter", "imposter"]]) {
          const r = rec[side];
          if (!isObj(r) || !r.games) continue;
          rows.push(el("tr", null, el("th", null, model, el("span", { class: "muted" }, ` as ${label}`)),
            el("td", null, fmtInt(r.games)), el("td", null, `${r.wins ?? 0}–${r.losses ?? 0}`),
            el("td", null, fmtInt(r.no_decision ?? 0))));
        }
      }
      if (rows.length) out.push(el("div", { class: "stats-section" }, el("h3", null, "Win–loss by model"),
        el("div", { class: "table-wrap" }, el("table", { class: "stats-table" },
          el("thead", null, el("tr", null, el("th", null, ""), el("th", { title: "games" }, "Games"), el("th", null, "W–L"), el("th", { title: "no decision" }, "Undec."))),
          el("tbody", null, rows)))));
    }
    const bp = st.by_pairing;
    if (Array.isArray(bp) && bp.length && bp.every((p) => isObj(p) && p.trio_model && p.imposter_model)) {
      used.add("by_pairing");
      out.push(el("div", { class: "stats-section" }, el("h3", null, "By matchup"),
        el("div", { class: "table-wrap" }, el("table", { class: "stats-table" },
          el("thead", null, el("tr", null, el("th", null, "Trio vs imposter"), el("th", null, "Games"), el("th", null, "Trio"), el("th", null, "Imp."), el("th", null, "Undec."))),
          el("tbody", null, bp.map((p) => el("tr", null,
            el("th", null, p.trio_model, el("span", { class: "muted" }, " vs "), p.imposter_model),
            el("td", null, fmtInt(p.games)), el("td", null, fmtInt(p.trio_wins ?? 0)),
            el("td", null, fmtInt(p.imposter_wins ?? 0)), el("td", null, fmtInt(p.no_decision ?? 0)))))))));
    }
    const rest = Object.entries(st).filter(([k]) => !used.has(k));
    if (rest.length) {
      out.push(el("details", { class: "more-stats" }, el("summary", null, "More numbers"),
        el("div", { class: "stats-body" },
          rest.filter(([, v]) => isScalar(v)).map(([k, v]) => el("dl", { class: "kv" }, el("dt", null, humanize(k)), el("dd", null, fmtStat(k, v)))),
          rest.filter(([, v]) => !isScalar(v)).map(([k, v]) => statSection(k, v, 0)))));
    }
    return out;
  }

  async function loadStats() {
    const body = $("stats-body");
    try {
      const res = await fetch("/api/stats", { cache: "no-store" });
      const data = await res.json();
      const which = data.mock ? "mock" : "real";
      $("stats-count").textContent = data.num_games != null ? ` · ${data.num_games} ${which}` : "";
      if (!statsPanel.open) return;
      if (data.ok === false) { body.replaceChildren(el("p", { class: "error-text small" }, data.error || "Could not load stats.")); return; }
      if (!data.num_games || !data.stats) {
        body.replaceChildren(el("p", { class: "muted small" }, `No finished ${which} games yet. Finished games are summarised here.`));
        return;
      }
      body.replaceChildren(...renderStats(data, which));
    } catch (e) {
      body.replaceChildren(el("p", { class: "error-text small" }, "Could not load past games."));
    }
  }

  // ------------------------------------------------------------------ SSE
  function dispatch(ev) {
    if (!ev || typeof ev !== "object") return;
    if (ev.type === "status") { onStatus(ev); return; }
    if (ev.game_id && ev.game_id !== S.gameId) resetGame(ev.game_id);
    if (typeof ev.seq === "number") {
      if (ev.seq <= S.lastSeq) return; // duplicate (e.g. replay overlap)
      S.lastSeq = ev.seq;
    }
    const fn = handlers[ev.type];
    if (!fn) return;
    try { withAutoScroll(() => fn(ev)); } catch (e) { console.error("Failed to render event", ev, e); }
  }

  let es = null;
  function connect() {
    es = new EventSource("/api/events");
    es.onopen = () => {
      connected = true;
      clearBanner("conn-lost");
      // The server replays the whole current game on every (re)connect.
      S.gameId = null;
      resetGame(null);
      renderStatus();
      renderControls();
    };
    es.onmessage = (msg) => {
      let ev;
      try { ev = JSON.parse(msg.data); } catch (_) { return; }
      dispatch(ev);
    };
    es.onerror = () => {
      if (connected) {
        connected = false;
        banner("warn", "Lost connection to the game server — retrying… (Is run.py still running?)", { id: "conn-lost", sticky: true });
        renderStatus();
        renderControls();
      }
    };
  }

  // Keep the sticky sidebar just below the (possibly wrapping) sticky header.
  const topEl = document.querySelector(".sticky-top");
  function syncTop() { document.documentElement.style.setProperty("--top-h", topEl.offsetHeight + "px"); }
  if (window.ResizeObserver) new ResizeObserver(syncTop).observe(topEl);
  window.addEventListener("resize", syncTop);
  syncTop();

  renderCost();
  renderStatus();
  renderControls();
  loadStats();
  connect();
})();
