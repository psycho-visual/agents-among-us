/* Agents Among Us - settings drawer (edits config.toml through the server). Vanilla JS.
 * Values are only ever placed in input values / textContent (never innerHTML). */
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") e.className = v;
        else if (k === "text") e.textContent = String(v);
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
  const clone = (x) => JSON.parse(JSON.stringify(x ?? null));
  const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
  const fmtPrice = (x) => (typeof x === "number" ? "$" + (x < 1 ? x.toFixed(3).replace(/0+$/, "").replace(/\.$/, ".0") : x.toFixed(2)) : "?");

  const drawer = $("settings-drawer");
  const S = {
    schema: [], byId: {}, saved: {}, values: {}, readErrors: [],
    errors: {}, warnings: {}, checkedFor: null, timer: null, seq: 0,
    running: false, models: null, modelsError: null, modelsLoading: false,
    closedGroups: new Set(), msg: null, saving: false,
  };

  // ------------------------------------------------------------------ server calls
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

  async function load() {
    const r = await api("/api/settings");
    if (!r.ok) {
      put($("sd-body"), el("div", { class: "pd-err" }, r.data.message || "Could not load the settings."));
      return false;
    }
    S.schema = Array.isArray(r.data.schema) ? r.data.schema : [];
    S.byId = {};
    for (const f of S.schema) S.byId[f.id] = f;
    S.saved = clone(r.data.values || {});
    S.values = clone(S.saved);
    S.readErrors = r.data.errors || [];
    S.running = !!r.data.running;
    if (r.data.config_path) $("sd-path").textContent = r.data.config_path;
    S.errors = {}; S.warnings = {}; S.checkedFor = null;
    render();
    validateNow();
    loadModels();
    return true;
  }

  async function loadModels() {
    if (S.models || S.modelsLoading) return;
    S.modelsLoading = true;
    const r = await api("/api/models");
    S.modelsLoading = false;
    S.models = Array.isArray(r.data.models) ? r.data.models : [];
    S.modelsError = r.data.error || (r.ok ? null : "Could not load the model list.");
    S.modelIndex = {};
    for (const m of S.models) S.modelIndex[m.id] = m;
    if (drawer.hidden) return;
    for (const p of S.pickers || []) p.refresh();
    for (const f of S.schema) if (f.key === "reasoning_effort") refreshField(f.id);
  }
  const modelInfo = (id) => (S.modelIndex && typeof id === "string" ? S.modelIndex[id.trim()] : null);

  // ------------------------------------------------------------------ state helpers
  const isDirty = () => !same(S.values, S.saved);
  const errorCount = () => Object.keys(S.errors).length;
  function visible(f, ignoreAdvanced) {
    if (f.advanced && !ignoreAdvanced && !$("sd-advanced").checked) return false;
    const vi = f.visible_if;
    if (vi && vi.id) {
      const v = S.values[vi.id];
      if ("equals" in vi && !same(v, vi.equals)) return false;
      if ("not_equals" in vi && same(v, vi.not_equals)) return false;
      if (Array.isArray(vi.in) && !vi.in.some((x) => same(x, v))) return false;
    }
    return true;
  }
  const hasDefault = (f) => Object.prototype.hasOwnProperty.call(f, "default");
  const differsFromDefault = (f) => hasDefault(f) && !same(S.values[f.id], f.default);

  // The reasoning effort of a role is limited to what that role's model supports (when known).
  function effortChoices(f) {
    const base = Array.isArray(f.choices) ? f.choices : [];
    if (f.key !== "reasoning_effort") return { choices: base, unsupported: false };
    const m = modelInfo(S.values[`${f.section}.model`]);
    if (!m || !Array.isArray(m.supported_efforts)) return { choices: base, unsupported: false };
    const ok = new Set(m.supported_efforts);
    const cur = S.values[f.id];
    const choices = base.filter((c) => c.value === "" || c.value == null || ok.has(c.value) || c.value === cur);
    const unsupported = cur != null && cur !== "" && !ok.has(cur);
    return { choices, unsupported, model: m };
  }

  function setValue(id, value, rerender) {
    S.values[id] = value;
    S.msg = null;
    scheduleValidate();
    if (rerender) render(); else { renderFieldState(id); renderFooter(); }
  }

  // ------------------------------------------------------------------ rendering
  const fieldNodes = {}; // id -> {wrap, msg, dot}

  function render() {
    const body = $("sd-body");
    const scroll = body.scrollTop;
    const groups = [];
    const byGroup = {};
    for (const f of S.schema) {
      const g = f.group || "Other";
      if (!byGroup[g]) { byGroup[g] = []; groups.push(g); }
      byGroup[g].push(f);
    }
    for (const k of Object.keys(fieldNodes)) delete fieldNodes[k];
    S.pickers = [];
    const sections = groups.map((g) => {
      const fields = byGroup[g].filter((f) => visible(f));
      const adv = $("sd-advanced").checked;
      const hiddenAdv = adv ? 0 : byGroup[g].filter((f) => f.advanced && visible(f, true)).length;
      // Trio and imposter options side by side, so each column is one role.
      const roleCols = ["trio", "imposter"].map((role) => fields.filter((f) => f.section === role));
      const useCols = roleCols[0].length && roleCols[1].length;
      const rest = useCols ? fields.filter((f) => f.section !== "trio" && f.section !== "imposter") : fields;
      if (!fields.length && !hiddenAdv) return null;
      const changed = byGroup[g].filter((f) => !same(S.values[f.id], S.saved[f.id])).length;
      const errs = byGroup[g].filter((f) => S.errors[f.id]).length;
      const det = el("details", { class: "sd-group", open: S.closedGroups.has(g) ? null : true },
        el("summary", null, el("span", { class: "sd-group-title" }, g),
          changed ? el("span", { class: "chip chip-unsaved" }, `${changed} changed`) : null,
          errs ? el("span", { class: "chip chip-out" }, `${errs} to fix`) : null),
        el("div", { class: "sd-fields" },
          g === "Players & models" && S.byId["trio.model"] && S.byId["imposter.model"]
            ? el("div", { class: "sd-swap" }, el("button", { type: "button", onclick: swapRoles,
              title: "Swap model, name, reasoning effort and output limit between the trio and the imposter (not saved until you click Save)" },
            "⇄ Swap trio ↔ imposter")) : null,
          useCols ? [
            el("div", { class: "sd-role-col" }, el("div", { class: "sd-role-h" }, el("span", { class: "chip role role-trio" }, "Trio"), " three copies of this model"), roleCols[0].map(fieldEl)),
            el("div", { class: "sd-role-col" }, el("div", { class: "sd-role-h" }, el("span", { class: "chip role role-imposter" }, "Imposter"), " the one different model"), roleCols[1].map(fieldEl)),
          ] : null,
          rest.map(fieldEl),
          hiddenAdv ? el("div", { class: "small muted sd-adv-note" }, `${hiddenAdv} advanced option(s) hidden — tick “Show advanced” to see them.`) : null));
      det.addEventListener("toggle", () => { if (det.open) S.closedGroups.delete(g); else S.closedGroups.add(g); });
      return det;
    });
    put(body,
      S.readErrors.length ? el("div", { class: "pd-err" }, "config.toml currently has problems: ", S.readErrors.join("; ")) : null,
      S.running ? el("div", { class: "pd-warn" }, "A game is running. You can still save; the changes apply to the next New game.") : null,
      el("div", { id: "sd-general", class: "sd-general" }),
      sections);
    renderGeneral();
    body.scrollTop = scroll;
    for (const id of Object.keys(fieldNodes)) renderFieldState(id);
    renderFooter();
  }

  const GENERAL_KEYS = ["_general", "_"];
  function renderGeneral() {
    const box = $("sd-general");
    if (!box) return;
    const items = [];
    for (const k of GENERAL_KEYS) {
      if (S.errors[k]) items.push(el("div", { class: "pd-err" }, "✖ ", String(S.errors[k])));
      if (S.warnings[k]) items.push(el("div", { class: "pd-warn" }, "⚠ ", String(S.warnings[k])));
    }
    put(box, items);
  }

  function fieldEl(f) {
    const id = "sd-f-" + f.id.replace(/[^\w-]/g, "_");
    const control = controlFor(f, id);
    const msg = el("div", { class: "sd-field-msg" });
    const dot = el("span", { class: "sd-default-dot", title: "Differs from the default" }, "●");
    const resetLink = hasDefault(f) ? el("button", {
      type: "button", class: "linkish small", title: `Default: ${fmtDefault(f)}`,
      onclick: () => setValue(f.id, clone(f.default), true),
    }, "reset to default") : null;
    const wrap = el("div", { class: "sd-field" + (f.type === "bool" ? " sd-bool" : ""), "data-id": f.id },
      el("div", { class: "sd-label-row" },
        f.type === "bool" ? control : null,
        el("label", { for: id, class: "sd-label" }, f.label || f.key),
        dot, resetLink,
        f.advanced ? el("span", { class: "chip" }, "advanced") : null),
      f.type !== "bool" ? control : null,
      f.help ? el("div", { class: "sd-help small muted" }, f.help) : null,
      msg);
    fieldNodes[f.id] = { wrap, msg, dot, reset: resetLink, field: f };
    return wrap;
  }

  function fmtDefault(f) {
    const d = f.default;
    if (d === "" || d == null) return f.nullable ? "(empty)" : "(empty)";
    if (Array.isArray(d)) return d.join(", ");
    if (typeof d === "boolean") return d ? "on" : "off";
    const c = (f.choices || []).find((x) => same(x.value, d));
    return c ? c.label : String(d);
  }

  // Re-create one field in place (e.g. the effort list after its model changed) without a full re-render.
  function refreshField(id) {
    const n = fieldNodes[id];
    if (!n) return;
    const fresh = fieldEl(n.field);
    n.wrap.replaceWith(fresh);
    renderFieldState(id);
  }

  function renderFieldState(id) {
    const n = fieldNodes[id];
    if (!n) return;
    const f = n.field;
    const diff = differsFromDefault(f);
    n.dot.hidden = !diff;
    if (n.reset) n.reset.hidden = !diff;
    n.wrap.classList.toggle("changed", !same(S.values[id], S.saved[id]));
    n.wrap.classList.toggle("has-error", !!S.errors[id]);
    const items = [];
    if (S.errors[id]) items.push(el("div", { class: "sd-err" }, "✖ ", String(S.errors[id])));
    if (S.warnings[id]) items.push(el("div", { class: "sd-warn" }, "⚠ ", String(S.warnings[id])));
    const eff = effortChoices(f);
    if (eff.unsupported) items.push(el("div", { class: "sd-warn" }, `⚠ ${eff.model.name} doesn't list “${S.values[id]}” as a supported effort ` +
      `(supported: ${eff.model.supported_efforts.join(", ")}).`));
    put(n.msg, items);
  }

  function controlFor(f, id) {
    const v = S.values[f.id];
    switch (f.type) {
      case "bool": {
        const cb = el("input", { type: "checkbox", id, class: "switch" });
        cb.checked = !!v;
        cb.addEventListener("change", () => setValue(f.id, cb.checked, true));
        return cb;
      }
      case "enum": {
        const { choices, unsupported } = effortChoices(f);
        const cur = String(v ?? "");
        const opts = choices.map((c) => el("option", { value: String(c.value ?? "") },
          (c.label ?? String(c.value)) + (unsupported && String(c.value ?? "") === cur ? " (not supported by this model)" : "")));
        if (!choices.some((c) => String(c.value ?? "") === cur)) opts.push(el("option", { value: cur }, cur || "(empty)"));
        const sel = el("select", { id }, opts);
        sel.value = cur;
        sel.addEventListener("change", () => {
          const c = choices.find((x) => String(x.value ?? "") === sel.value);
          setValue(f.id, c ? c.value : sel.value, true);
        });
        return sel;
      }
      case "int":
      case "float": {
        const inp = el("input", { type: "number", id, min: f.min, max: f.max, step: f.step ?? (f.type === "int" ? 1 : "any"),
          placeholder: f.nullable ? "(empty = use the default)" : null, inputmode: f.type === "int" ? "numeric" : "decimal" });
        inp.value = v == null ? "" : String(v);
        inp.addEventListener("input", () => {
          const raw = inp.value.trim();
          let val;
          if (raw === "") val = f.nullable ? null : "";
          else {
            const n = Number(raw);
            val = !isFinite(n) ? raw : f.type === "int" && !Number.isInteger(n) ? raw : n;
          }
          setValue(f.id, val, false);
        });
        return inp;
      }
      case "string_list": {
        const ta = el("textarea", { id, rows: Math.min(8, Math.max(3, (Array.isArray(v) ? v.length : 3))), class: "sd-list",
          spellcheck: "false", placeholder: "One per line (or separated by commas)" });
        ta.value = Array.isArray(v) ? v.join("\n") : String(v ?? "");
        ta.addEventListener("input", () => setValue(f.id, ta.value.split(/[\n,]/).map((x) => x.trim()).filter(Boolean), false));
        return ta;
      }
      case "model":
        return modelPicker(f, id);
      default: {
        const inp = el("input", { type: "text", id, spellcheck: "false" });
        inp.value = v == null ? "" : String(v);
        inp.addEventListener("input", () => setValue(f.id, inp.value, false));
        return inp;
      }
    }
  }

  // ------------------------------------------------------------------ model picker
  function modelPicker(f, id) {
    const inp = el("input", { type: "text", id, class: "sd-model-input", spellcheck: "false", autocomplete: "off",
      placeholder: "Type to search, e.g. claude haiku or gpt", role: "combobox", "aria-expanded": "false", "aria-autocomplete": "list" });
    inp.value = String(S.values[f.id] ?? "");
    const list = el("div", { class: "sd-model-list", role: "listbox", hidden: true });
    const info = el("div", { class: "sd-model-info small" });
    let active = -1, shown = [];

    function describe() {
      const m = modelInfo(inp.value);
      if (m) {
        put(info, el("b", null, m.name), " · ", `${fmtPrice(m.prompt_per_m)} in / ${fmtPrice(m.completion_per_m)} out per 1M tokens`,
          m.supported_efforts ? ` · reasoning efforts: ${m.supported_efforts.join(", ")}` : " · no reasoning settings listed");
        info.className = "sd-model-info small muted";
      } else if (S.models && S.models.length && inp.value.trim()) {
        put(info, "⚠ Not in OpenRouter's model list — check the spelling (it should look like company/model-name).");
        info.className = "sd-model-info small sd-warn-text";
      } else if (S.modelsError) {
        put(info, "Couldn't load OpenRouter's model list (offline?). Type the model id by hand, e.g. anthropic/claude-haiku-5.5.");
        info.className = "sd-model-info small muted";
      } else if (S.modelsLoading) {
        put(info, "Loading the model list…"); info.className = "sd-model-info small muted";
      } else put(info);
    }
    function filter() {
      const q = inp.value.trim().toLowerCase();
      const words = q.split(/\s+/).filter(Boolean);
      const all = S.models || [];
      const exact = modelInfo(inp.value);
      let res = words.length && !exact
        ? all.filter((m) => { const hay = (m.id + " " + m.name).toLowerCase(); return words.every((w) => hay.includes(w)); })
        : all;
      shown = res.slice(0, 60);
      active = -1;
      put(list, shown.length ? shown.map((m, i) => el("div", {
        class: "sd-model-opt" + (m.id === inp.value.trim() ? " current" : ""), role: "option", "data-i": i,
        onmousedown: (e) => { e.preventDefault(); choose(m); },
      },
      el("div", { class: "sd-model-name" }, m.name),
      el("div", { class: "sd-model-meta" }, el("code", null, m.id),
        el("span", null, `${fmtPrice(m.prompt_per_m)} / ${fmtPrice(m.completion_per_m)} per 1M`),
        m.supported_efforts ? el("span", { class: "chip" }, "reasoning") : null)))
        : el("div", { class: "sd-model-empty small muted" }, all.length ? "No matching models. You can still type any model id." : "Model list not available."),
      res.length > shown.length ? el("div", { class: "sd-model-empty small muted" }, `…and ${res.length - shown.length} more — keep typing to narrow it down.`) : null);
    }
    function openList() {
      if (!S.models || !S.models.length) return;
      filter(); list.hidden = false; inp.setAttribute("aria-expanded", "true");
    }
    function closeList() { list.hidden = true; inp.setAttribute("aria-expanded", "false"); }
    function choose(m) {
      inp.value = m.id;
      closeList();
      setValue(f.id, m.id, false);
      describe();
      refreshField(`${f.section}.reasoning_effort`);
    }
    function highlight() {
      [...list.querySelectorAll(".sd-model-opt")].forEach((n, i) => n.classList.toggle("active", i === active));
      const n = list.querySelector(".sd-model-opt.active");
      if (n) n.scrollIntoView({ block: "nearest" });
    }
    let focusValue = inp.value;
    inp.addEventListener("focus", () => { focusValue = inp.value; openList(); });
    inp.addEventListener("input", () => { S.values[f.id] = inp.value; S.msg = null; scheduleValidate(); renderFooter(); renderFieldState(f.id); describe(); openList(); });
    inp.addEventListener("blur", () => {
      closeList();
      if (inp.value !== focusValue) refreshField(`${f.section}.reasoning_effort`);
    });
    inp.addEventListener("keydown", (e) => {
      if (list.hidden && (e.key === "ArrowDown" || e.key === "ArrowUp")) { openList(); return; }
      if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(shown.length - 1, active + 1); highlight(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(0, active - 1); highlight(); }
      else if (e.key === "Enter" && !list.hidden && active >= 0) { e.preventDefault(); choose(shown[active]); }
      else if (e.key === "Escape" && !list.hidden) { e.preventDefault(); e.stopPropagation(); closeList(); }
    });
    describe();
    S.pickers.push({ refresh: () => { describe(); if (document.activeElement === inp) openList(); } });
    return el("div", { class: "sd-model" }, inp, list, info);
  }

  function swapRoles() {
    for (const key of ["model", "display_name", "reasoning_effort", "max_output_tokens"]) {
      const a = `trio.${key}`, b = `imposter.${key}`;
      if (!(a in S.byId) || !(b in S.byId)) continue;
      const tmp = S.values[a]; S.values[a] = S.values[b]; S.values[b] = tmp;
    }
    S.msg = { kind: "info", text: "Swapped trio and imposter — click Save to keep it." };
    scheduleValidate();
    render();
  }

  // ------------------------------------------------------------------ validation
  function scheduleValidate() {
    clearTimeout(S.timer);
    S.checkedFor = null;
    S.timer = setTimeout(validateNow, 300);
  }
  async function validateNow() {
    const snapshot = clone(S.values);
    const seq = ++S.seq;
    const r = await api("/api/settings/validate", { values: snapshot });
    if (seq !== S.seq) return;
    if (r.ok) { S.errors = r.data.errors || {}; S.warnings = r.data.warnings || {}; }
    else { S.errors = { _: r.data.message || "Could not check the settings." }; S.warnings = {}; }
    S.checkedFor = JSON.stringify(snapshot);
    for (const id of Object.keys(fieldNodes)) renderFieldState(id);
    renderGeneral();
    renderFooter();
    // keep group badges in sync without stealing focus from an input
    for (const det of document.querySelectorAll(".sd-group")) {
      const g = det.querySelector(".sd-group-title").textContent;
      const errs = S.schema.filter((f) => (f.group || "Other") === g && S.errors[f.id]).length;
      const sum = det.querySelector("summary");
      const old = sum.querySelector(".chip-out"); if (old) old.remove();
      if (errs) sum.append(el("span", { class: "chip chip-out" }, `${errs} to fix`));
    }
  }

  function renderFooter() {
    const dirty = isDirty();
    const errs = errorCount();
    const checked = S.checkedFor === JSON.stringify(S.values);
    $("sd-dirty").hidden = !dirty;
    $("sd-revert").disabled = !dirty || S.saving;
    const save = $("sd-save"), saveNew = $("sd-save-new");
    save.disabled = !dirty || errs > 0 || !checked || S.saving;
    save.title = errs ? "Fix the problems first" : !dirty ? "Nothing to save" : !checked ? "Checking…" : "Save to config.toml";
    saveNew.disabled = S.running || errs > 0 || !checked || S.saving;
    saveNew.textContent = dirty ? "Save & start new game" : "Start new game";
    saveNew.title = S.running ? "A game is running — pause it first (or just Save; changes apply to the next game)"
      : errs ? "Fix the problems first" : dirty ? "Save, then set up a fresh game with these settings" : "Set up a fresh game with these settings";
    const box = $("sd-msg");
    const items = [];
    if (errs) {
      const labels = Object.keys(S.errors).map((id) => [id, S.byId[id] ? S.byId[id].label : GENERAL_KEYS.includes(id) ? "see the message at the top" : id]);
      items.push(el("span", { class: "sd-err-text" }, `✖ ${errs} problem${errs > 1 ? "s" : ""} to fix: `),
        labels.map(([id, label], i) => [i ? ", " : "", el("button", { type: "button", class: "linkish", onclick: () => reveal(id) }, label)]));
    } else if (S.msg) items.push(el("span", { class: "sd-msg-" + S.msg.kind }, S.msg.text));
    else if (dirty) items.push(el("span", { class: "muted" }, "You have unsaved changes."));
    else items.push(el("span", { class: "muted" }, "All changes saved."));
    put(box, items);
  }

  function reveal(id) {
    const f = S.byId[id];
    if (!f) { $("sd-body").scrollTop = 0; return; }
    if (f.advanced && !$("sd-advanced").checked) { $("sd-advanced").checked = true; }
    S.closedGroups.delete(f.group || "Other");
    render();
    const n = fieldNodes[id];
    if (n) {
      n.wrap.scrollIntoView({ block: "center" });
      const c = n.wrap.querySelector("input, select, textarea");
      if (c) c.focus({ preventScroll: true });
    } else {
      S.msg = { kind: "bad", text: `“${f.label}” is hidden because of another setting — change that setting or fix this value in config.toml.` };
      renderFooter();
    }
  }

  // ------------------------------------------------------------------ actions
  async function save(startNewGame) {
    if (S.saving) return;
    clearTimeout(S.timer);
    if (S.checkedFor !== JSON.stringify(S.values)) await validateNow();
    if (errorCount()) { renderFooter(); return; }
    S.saving = true; renderFooter();
    const r = await api("/api/settings/save", { values: clone(S.values), start_new_game: !!startNewGame });
    S.saving = false;
    const d = r.data || {};
    if (!r.ok || !d.ok) {
      S.errors = d.errors && Object.keys(d.errors).length ? d.errors : { _: d.message || "Could not save." };
      S.warnings = d.warnings || {};
      S.msg = { kind: "bad", text: "Not saved." };
      render();
      return;
    }
    S.saved = clone(S.values);
    const parts = ["✓ Saved to config.toml."];
    if (Array.isArray(d.changed)) {
      parts.push(d.changed.length
        ? `Changed: ${d.changed.map((id) => (S.byId[id] ? S.byId[id].label : id)).join(", ")}.`
        : "Nothing in the file needed changing.");
    }
    if (d.backup) parts.push(`Previous version kept as ${String(d.backup).split(/[\\/]/).pop()}.`);
    if (d.new_game) parts.push(d.new_game.ok ? "New game set up." : d.new_game.message);
    else parts.push("Click New game to use it.");
    S.msg = { kind: d.new_game && !d.new_game.ok ? "bad" : "good", text: parts.join(" ") };
    render();
    if (startNewGame && d.new_game && d.new_game.ok) close();
  }

  function revert() {
    if (!isDirty()) return;
    if (!confirm("Throw away all the changes you haven't saved?")) return;
    S.values = clone(S.saved);
    S.msg = null;
    render();
    validateNow();
  }

  // ------------------------------------------------------------------ open / close
  function open() {
    for (const [name, d] of Object.entries(window.AAUDrawers || {})) {
      if (name !== "settings" && d.isOpen() && !d.close()) return;
    }
    drawer.hidden = false;
    document.body.classList.add("drawer-open");
    load();
  }
  function close() {
    if (isDirty() && !confirm("You have unsaved settings. Close and throw them away?")) return false;
    drawer.hidden = true;
    document.body.classList.remove("drawer-open");
    return true;
  }
  window.AAUDrawers = window.AAUDrawers || {};
  window.AAUDrawers.settings = { isOpen: () => !drawer.hidden, close };

  $("btn-settings").addEventListener("click", open);
  $("sd-close").addEventListener("click", close);
  $("sd-backdrop").addEventListener("click", close);
  $("sd-revert").addEventListener("click", revert);
  $("sd-save").addEventListener("click", () => save(false));
  $("sd-save-new").addEventListener("click", () => save(true));
  $("sd-advanced").addEventListener("change", render);
  try { $("sd-advanced").checked = localStorage.getItem("aau-settings-advanced") === "1"; } catch (_) { /* ignore */ }
  $("sd-advanced").addEventListener("change", () => {
    try { localStorage.setItem("aau-settings-advanced", $("sd-advanced").checked ? "1" : "0"); } catch (_) { /* ignore */ }
  });
  document.addEventListener("keydown", (e) => {
    if (drawer.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(false); }
  });
  document.addEventListener("aau:status", (e) => {
    const st = e.detail || {};
    const running = st.state === "running" || st.state === "pausing";
    if (running !== S.running) { S.running = running; if (!drawer.hidden) render(); }
  });
  window.addEventListener("beforeunload", (e) => {
    if (!drawer.hidden && isDirty()) { e.preventDefault(); e.returnValue = ""; }
  });
})();
