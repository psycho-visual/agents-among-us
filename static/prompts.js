/* Agents Among Us - prompt template editor (drawer). Vanilla JS.
 * Template text is only ever put into a <textarea> value or textContent (never innerHTML). */
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

  // DOM replaceChildren() would turn null into the text "null"; drop empty children first.
  const put = (node, ...kids) => node.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));

  const GROUP_ORDER = ["Rules", "Roles", "Tasks", "Other"];
  const P = {
    templates: [], byId: {}, drafts: {}, selected: null,
    validation: {},   // id -> {text, errors, warnings, pending}
    version: null, modified: false, current: null, tab: "edit",
    timer: null, seq: 0, flash: null,
  };

  const drawer = $("prompt-drawer");

  // ------------------------------------------------------------------ server calls
  async function api(path, body) {
    const opts = body === undefined ? { cache: "no-store" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    let res, data = null;
    try {
      res = await fetch(path, opts);
      try { data = await res.json(); } catch (_) { /* not JSON */ }
    } catch (e) {
      return { ok: false, status: 0, data: { message: "Could not reach the game server. Is run.py still running?" } };
    }
    return { ok: res.ok && !(data && data.ok === false), status: res.status, data: data || {} };
  }

  async function load() {
    const r = await api("/api/prompts");
    if (!r.ok) {
      put($("pd-list"), el("p", { class: "error-text small" }, r.data.message || "Could not load the prompt templates."));
      put($("pd-editor"), );
      return false;
    }
    P.templates = Array.isArray(r.data.templates) ? r.data.templates : [];
    P.byId = {};
    for (const t of P.templates) P.byId[t.id] = t;
    for (const id of Object.keys(P.drafts)) if (!P.byId[id]) delete P.drafts[id];
    P.version = r.data.prompt_version || null;
    P.modified = !!r.data.modified;
    P.current = r.data.current_game || null;
    if (!P.selected || !P.byId[P.selected]) P.selected = P.templates.length ? P.templates[0].id : null;
    renderVersion();
    renderList();
    renderEditor();
    return true;
  }

  // ------------------------------------------------------------------ helpers
  const draftOf = (id) => (id in P.drafts ? P.drafts[id] : (P.byId[id] ? P.byId[id].text : ""));
  const isDirty = (id) => id in P.drafts && P.byId[id] && P.drafts[id] !== P.byId[id].text;
  const dirtyIds = () => Object.keys(P.drafts).filter(isDirty);
  function valFor(id) {
    const v = P.validation[id];
    return v && v.text === draftOf(id) ? v : null;
  }

  function renderVersion() {
    const parts = [];
    if (P.version) parts.push(`Saved templates: version ${P.version}${P.modified ? " (customised)" : " (defaults)"}`);
    const cur = P.current;
    if (cur && cur.prompt_version) {
      parts.push(cur.prompt_version === P.version ? "current game uses these"
        : `current game uses ${cur.prompt_version} — click New game to use the saved version`);
    }
    $("pd-version").textContent = parts.join(" · ");
  }

  // ------------------------------------------------------------------ list
  function renderList() {
    const groups = {};
    for (const t of P.templates) (groups[t.group || "Other"] = groups[t.group || "Other"] || []).push(t);
    const names = Object.keys(groups).sort((a, b) => {
      const ia = GROUP_ORDER.indexOf(a), ib = GROUP_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
    });
    put($("pd-list"), ...names.map((g) => el("div", { class: "pd-group" },
      el("div", { class: "pd-group-h" }, g),
      groups[g].map((t) => el("button", {
        type: "button",
        class: "pd-item" + (t.id === P.selected ? " active" : ""),
        title: t.description || t.id,
        onclick: () => select(t.id),
      },
      el("span", { class: "pd-item-title" }, t.title || t.id),
      isDirty(t.id) ? el("span", { class: "pd-dot", title: "Unsaved changes" }, "●") : null,
      t.modified ? el("span", { class: "chip chip-edited", title: "You saved a changed version of this template" }, "edited") : null)))));
  }

  function select(id) {
    if (id === P.selected) return;
    P.selected = id;
    P.flash = null;
    renderList();
    renderEditor();
  }

  // ------------------------------------------------------------------ editor
  let ui = null; // refs to the editor's live parts

  function renderEditor() {
    const box = $("pd-editor");
    const t = P.byId[P.selected];
    if (!t) { put(box, el("p", { class: "muted" }, "No templates.")); ui = null; return; }
    const ta = el("textarea", {
      class: "pd-text", spellcheck: "false", "aria-label": `Text of ${t.title || t.id}`,
      autocomplete: "off", autocorrect: "off", autocapitalize: "off",
    });
    ta.value = draftOf(t.id);
    ta.addEventListener("input", () => {
      P.drafts[t.id] = ta.value;
      P.flash = null;
      scheduleValidate(t.id);
      renderState();
      renderList();
    });
    const chips = (t.placeholders || []).map((ph) => el("button", {
      type: "button", class: "ph-chip", title: (ph.description || "") + " — click to insert at the cursor",
      onclick: () => insertAtCursor(ta, `{${ph.name}}`),
    }, `{${ph.name}}`));
    const markers = (t.required_markers || []).filter(Boolean);
    ui = {
      id: t.id, ta,
      badge: el("span", { class: "pd-badges" }),
      val: el("div", { class: "pd-validation", "aria-live": "polite" }),
      save: el("button", { type: "button", class: "primary", onclick: save, title: "Save (Ctrl+S)" }, "Save"),
      revert: el("button", { type: "button", onclick: revert, title: "Throw away the edits you haven't saved" }, "Revert unsaved"),
      reset: el("button", { type: "button", class: "danger-ghost", onclick: reset, title: "Delete your saved version and use the built-in default" }, "Reset to default"),
      msg: el("span", { class: "pd-msg small" }),
    };
    put(box, 
      el("div", { class: "pd-editor-head" },
        el("h3", null, t.title || t.id), ui.badge),
      t.description ? el("p", { class: "pd-desc" }, t.description) : null,
      el("div", { class: "small muted" }, "File: ", el("code", null, `prompts/${t.id}.txt`),
        t.modified ? " (your saved version)" : " (not created yet — using the built-in default)"),
      chips.length ? el("div", { class: "pd-phs" }, el("span", { class: "small muted" }, "Fields (click to insert): "), chips) : null,
      markers.length ? el("div", { class: "small muted" }, "Must keep: ", markers.map((m, i) => [i ? ", " : "", el("code", null, m)]),
        " — the game reads this from the players' replies.") : null,
      el("div", { class: "small muted pd-help" }, "Text in {curly braces} is filled in by the game. To write a literal brace, type it twice: {{ or }}."),
      ta,
      ui.val,
      el("div", { class: "pd-actions" }, ui.save, ui.revert, el("span", { class: "spacer" }), ui.msg, ui.reset),
      t.modified ? el("details", { class: "pd-default" }, el("summary", null, "Show the built-in default text"),
        el("pre", { class: "pd-default-text" }, t.default_text || "")) : null);
    if (!valFor(t.id)) validateNow(t.id);
    renderState();
  }

  function renderState() {
    if (!ui) return;
    const t = P.byId[ui.id];
    const dirty = isDirty(ui.id);
    const v = valFor(ui.id);
    const errors = v ? v.errors : [];
    const warnings = v ? v.warnings : [];
    put(ui.badge, 
      dirty ? el("span", { class: "chip chip-unsaved" }, "● unsaved changes") : null,
      t.modified ? el("span", { class: "chip chip-edited" }, "edited (differs from default)") : el("span", { class: "chip" }, "default"));
    const items = [];
    if (!v) items.push(el("div", { class: "muted small" }, "Checking…"));
    else {
      for (const e of errors) items.push(el("div", { class: "pd-err" }, "✖ ", String(e)));
      for (const w of warnings) items.push(el("div", { class: "pd-warn" }, "⚠ ", String(w)));
      if (!errors.length && !warnings.length) items.push(el("div", { class: "pd-ok small" }, "✓ No problems found"));
    }
    put(ui.val, ...items);
    ui.save.disabled = !dirty || !v || errors.length > 0;
    ui.save.title = errors.length ? "Fix the errors before saving" : !dirty ? "Nothing to save" : "Save (Ctrl+S)";
    ui.revert.disabled = !dirty;
    ui.reset.disabled = !t.modified && !dirty;
    if (P.flash) { ui.msg.textContent = P.flash.text; ui.msg.className = "pd-msg small " + (P.flash.kind || ""); }
    else ui.msg.textContent = "";
  }

  function insertAtCursor(ta, text) {
    ta.focus();
    const start = ta.selectionStart ?? ta.value.length, end = ta.selectionEnd ?? ta.value.length;
    ta.setRangeText(text, start, end, "end");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // ------------------------------------------------------------------ validation
  function scheduleValidate(id) {
    clearTimeout(P.timer);
    P.timer = setTimeout(() => validateNow(id), 350);
  }
  async function validateNow(id) {
    const text = draftOf(id);
    const seq = ++P.seq;
    const r = await api("/api/prompts/validate", { id, text });
    if (seq !== P.seq && draftOf(id) !== text) return; // a newer check is on its way
    if (r.ok) P.validation[id] = { text, errors: r.data.errors || [], warnings: r.data.warnings || [] };
    else P.validation[id] = { text, errors: [r.data.message || "Could not check this template."], warnings: [] };
    if (ui && ui.id === id) renderState();
  }

  // ------------------------------------------------------------------ actions
  async function save() {
    if (!ui) return;
    const id = ui.id;
    if (!isDirty(id)) return;
    const v = valFor(id);
    if (!v) { await validateNow(id); }
    if ((valFor(id) || { errors: [] }).errors.length) { renderState(); return; }
    const text = draftOf(id);
    const r = await api("/api/prompts/save", { id, text });
    if (!r.ok) {
      P.validation[id] = { text, errors: r.data.errors && r.data.errors.length ? r.data.errors : [r.data.message || "Could not save."], warnings: r.data.warnings || [] };
      P.flash = { text: "Not saved.", kind: "bad" };
      renderState();
      return;
    }
    delete P.drafts[id];
    P.flash = { text: "✓ Saved — used from the next New game.", kind: "good" };
    await load();
  }

  async function revert() {
    if (!ui || !isDirty(ui.id)) return;
    const t = P.byId[ui.id];
    if (!confirm(`Discard your unsaved changes to “${t.title || t.id}”?`)) return;
    delete P.drafts[ui.id];
    P.flash = null;
    renderList();
    renderEditor();
  }

  async function reset() {
    if (!ui) return;
    const t = P.byId[ui.id];
    if (!t.modified) {  // only unsaved edits: same as revert
      if (isDirty(t.id) && confirm(`Discard your unsaved changes to “${t.title || t.id}”?`)) { delete P.drafts[t.id]; renderList(); renderEditor(); }
      return;
    }
    if (!confirm(`Reset “${t.title || t.id}” to the built-in default text?\n\nYour saved version of this template will be deleted` +
      (isDirty(t.id) ? " and your unsaved edits discarded." : "."))) return;
    const r = await api("/api/prompts/reset", { id: t.id });
    if (!r.ok) { P.flash = { text: r.data.message || "Could not reset.", kind: "bad" }; renderState(); return; }
    delete P.drafts[t.id];
    delete P.validation[t.id];
    P.flash = { text: "✓ Reset to default — used from the next New game.", kind: "good" };
    await load();
  }

  // ------------------------------------------------------------------ preview
  const PREVIEW_TITLES = {
    trio_system: "System prompt — trio member", imposter_system: "System prompt — imposter",
    intro: "Task: introduction (trio)", imposter_intro: "Task: introduction (imposter, sees trio intros)",
    question: "Task: ask a question", answer: "Task: answer a question", vote: "Task: vote",
  };
  const titleFor = (k) => PREVIEW_TITLES[k] || k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

  async function loadPreview() {
    const box = $("pd-preview");
    put(box, el("p", { class: "muted" }, "Rendering…"));
    const r = await api("/api/prompts/preview");
    if (!r.ok) { put(box, el("div", { class: "pd-err" }, r.data.message || "Could not render the preview.")); return; }
    const pv = r.data.preview || {};
    const blocks = [];
    const dirty = dirtyIds();
    blocks.push(el("p", { class: "small muted" }, "Rendered with your saved templates, the current config.toml and made-up sample players and transcript. ",
      "This is exactly the kind of text each player receives."));
    if (dirty.length) blocks.push(el("div", { class: "pd-warn" }, `⚠ ${dirty.length} template(s) have unsaved changes, which are not shown here. Save them first to preview.`));
    const section = (key, text, open) => el("details", { class: "pv-block", open: open || null },
      el("summary", null, titleFor(key)), el("pre", { class: "pv-text" }, text == null ? "" : String(text)));
    if (Array.isArray(pv.warnings) && pv.warnings.length) {
      for (const w of pv.warnings) blocks.push(el("div", { class: "pd-warn" }, "⚠ ", String(w)));
    }
    if (pv.prompt_version) {
      blocks.push(el("p", { class: "small muted" }, `Prompt version of these templates: ${pv.prompt_version}` +
        (pv.prompts_modified ? " (customised)." : " (defaults).")));
    }
    for (const [k, v] of Object.entries(pv)) {
      if (k === "prompt_version" || k === "prompts_modified" || k === "warnings") continue;
      if (k === "tasks" && v && typeof v === "object") {
        blocks.push(el("h3", { class: "pv-h" }, "Task messages (the part after the transcript)"));
        for (const [tk, tv] of Object.entries(v)) blocks.push(section(tk, typeof tv === "string" ? tv : JSON.stringify(tv, null, 2), false));
      } else {
        blocks.push(section(k, typeof v === "string" ? v : JSON.stringify(v, null, 2), k.endsWith("_system")));
      }
    }
    put(box, ...blocks);
  }

  function setTab(tab) {
    P.tab = tab;
    $("pd-tab-edit").classList.toggle("active", tab === "edit");
    $("pd-tab-preview").classList.toggle("active", tab === "preview");
    $("pd-tab-edit").setAttribute("aria-selected", String(tab === "edit"));
    $("pd-tab-preview").setAttribute("aria-selected", String(tab === "preview"));
    $("pd-edit").hidden = tab !== "edit";
    $("pd-preview").hidden = tab !== "preview";
    if (tab === "preview") loadPreview();
  }

  // ------------------------------------------------------------------ open / close
  function open() {
    drawer.hidden = false;
    document.body.classList.add("drawer-open");
    setTab("edit");
    load().then(() => { if (ui) ui.ta.focus({ preventScroll: true }); });
  }
  function close() {
    const dirty = dirtyIds();
    if (dirty.length) {
      const names = dirty.map((id) => `• ${(P.byId[id] && P.byId[id].title) || id}`).join("\n");
      if (!confirm(`You have unsaved changes in:\n${names}\n\nClose and discard them?`)) return;
      P.drafts = {};
    }
    drawer.hidden = true;
    document.body.classList.remove("drawer-open");
  }

  $("btn-prompts").addEventListener("click", open);
  $("pd-close").addEventListener("click", close);
  $("pd-backdrop").addEventListener("click", close);
  $("pd-tab-edit").addEventListener("click", () => setTab("edit"));
  $("pd-tab-preview").addEventListener("click", () => setTab("preview"));
  document.addEventListener("keydown", (e) => {
    if (drawer.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (P.tab === "edit") save(); }
  });
  window.addEventListener("beforeunload", (e) => {
    if (!drawer.hidden && dirtyIds().length) { e.preventDefault(); e.returnValue = ""; }
  });
})();
