/* Workbench UI: vanilla JS, no build step, no external requests.
   All data from the API is inserted with textContent; nothing is parsed as HTML. */
(() => {
  "use strict";

  // -------------------------------------------------------------------------------------------
  // Helpers

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const SVG_NS = "http://www.w3.org/2000/svg";

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key.startsWith("on") && typeof value === "function") listen(el, key.slice(2), value);
      else if (key === "style") {
        // CSSOM writes are allowed by the Content-Security-Policy; style attributes are not.
        String(value).split(";").forEach((decl) => {
          const at = decl.indexOf(":");
          if (at > 0) el.style.setProperty(decl.slice(0, at).trim(), decl.slice(at + 1).trim());
        });
      } else if (value === true) el.setAttribute(key, "");
      else el.setAttribute(key, String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function listen(el, type, fn) {
    if (!el._on) el._on = {};
    if (!el._on[type]) el.addEventListener(type, (e) => el._on[type] && el._on[type](e));
    el._on[type] = fn;
  }

  const FORM_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);
  const nodeKey = (n) => (n.nodeType === 1 ? n.getAttribute("data-key") : null);
  const sameKind = (a, b) => a.nodeType === b.nodeType && a.nodeName === b.nodeName && nodeKey(a) === nodeKey(b);

  function enter(n) {
    if (n.nodeType !== 1 || !n.isConnected || n.classList.contains("no-enter")) return;
    n.classList.add("enter");
    n.addEventListener("animationend", () => n.classList.remove("enter"), { once: true });
  }

  function patchNode(o, n) {
    if (o.nodeType !== 1) {
      if (o.nodeValue !== n.nodeValue) o.nodeValue = n.nodeValue;
      return;
    }
    if (o.hasAttribute("data-keep")) return;
    const live = o.hasAttribute("data-live");
    for (const a of Array.from(o.attributes)) {
      if (a.name === "open" || a.name === "style") continue;
      if (!n.hasAttribute(a.name)) o.removeAttribute(a.name);
    }
    for (const a of Array.from(n.attributes)) {
      if (a.name === "open" || a.name === "style") continue;
      if (a.name === "value" && FORM_TAGS.has(o.tagName)) continue;
      if (o.getAttribute(a.name) !== a.value) o.setAttribute(a.name, a.value);
    }
    if (!live && o.style.cssText !== n.style.cssText) o.style.cssText = n.style.cssText;
    if (o.tagName === "INPUT" && (o.type === "checkbox" || o.type === "radio")) o.checked = n.checked;
    if (FORM_TAGS.has(o.tagName)) o.disabled = n.disabled;
    for (const [type, fn] of Object.entries(n._on || {})) listen(o, type, fn);
    if (o.tagName === "svg" || o.namespaceURI === SVG_NS) {
      if (o.innerHTML !== n.innerHTML) o.replaceChildren(...n.childNodes);
      return;
    }
    if (o.tagName === "TEXTAREA" || (o.tagName === "SELECT" && o === document.activeElement)) return;
    patchChildren(o, Array.from(n.childNodes), false);
  }

  function patchChildren(parent, next, animate) {
    const old = Array.from(parent.childNodes);
    const keyed = new Map();
    old.forEach((o) => { const k = nodeKey(o); if (k) keyed.set(k, o); });
    const unkeyed = old.filter((o) => !nodeKey(o));
    const kept = new Set();
    let u = 0;
    next.forEach((n, i) => {
      const k = nodeKey(n);
      let match = null;
      if (k) {
        const o = keyed.get(k);
        if (o && !kept.has(o) && sameKind(o, n)) match = o;
      } else if (u < unkeyed.length) {
        if (sameKind(unkeyed[u], n)) match = unkeyed[u];
        u += 1;
      }
      const ref = parent.childNodes[i] || null;
      if (match) {
        kept.add(match);
        if (match !== ref) parent.insertBefore(match, ref);
        patchNode(match, n);
      } else {
        parent.insertBefore(n, ref);
        kept.add(n);
        if (animate) enter(n);
      }
    });
    for (const o of old) if (!kept.has(o)) o.remove();
  }

  // Replace the children of an element, reusing what is already on screen so nothing jumps.
  function fill(el, ...children) {
    const next = children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false)
      .map((c) => (c instanceof Node ? c : document.createTextNode(String(c))));
    patchChildren(el, next, true);
    return el;
  }

  // Animate an element's height across a content change.
  function smoothHeight(el, change) {
    const from = el.getBoundingClientRect().height;
    change();
    const to = el.scrollHeight;
    if (Math.abs(to - from) < 2 || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    el.style.height = `${from}px`;
    el.style.overflow = "hidden";
    void el.offsetHeight;
    el.classList.add("sizing");
    el.style.height = `${to}px`;
    const done = () => { el.classList.remove("sizing"); el.style.height = ""; el.style.overflow = ""; };
    el.addEventListener("transitionend", done, { once: true });
    setTimeout(done, 400);
  }

  // Pill-shaped segmented tabs with a sliding highlight.
  function tabs(items, active, onChange) {
    const thumb = h("span", { class: "seg-thumb", "data-live": "" });
    const seg = h("div", { class: "seg", role: "tablist", "data-keep": "" }, thumb);
    let current = active;
    const place = (instant) => {
      const b = seg.querySelector("button.active");
      if (!b) return;
      if (instant) thumb.classList.add("instant");
      thumb.style.width = `${b.offsetWidth}px`;
      thumb.style.transform = `translateX(${b.offsetLeft - 3}px)`;
      if (instant) requestAnimationFrame(() => thumb.classList.remove("instant"));
    };
    const select = (key) => {
      if (key === current) return;
      const keys = items.map((i) => i[0]);
      const dir = keys.indexOf(key) > keys.indexOf(current) ? 1 : -1;
      current = key;
      $$("button", seg).forEach((b) => { b.classList.toggle("active", b.dataset.tab === key); b.setAttribute("aria-selected", String(b.dataset.tab === key)); });
      place(false);
      onChange(key, dir);
    };
    items.forEach(([key, label]) => seg.append(h("button", {
      type: "button", role: "tab", class: key === active ? "active" : "", "aria-selected": String(key === active),
      dataset: { tab: key }, onclick: () => select(key),
    }, h("span", { text: label }), h("span", { class: "seg-count", hidden: true }))));
    seg.setCount = (key, n) => {
      const c = seg.querySelector(`button[data-tab="${key}"] .seg-count`);
      if (!c) return;
      c.hidden = !n;
      c.textContent = n || "";
      place(false);
    };
    seg.select = select;
    requestAnimationFrame(() => place(true));
    if (window.ResizeObserver) new ResizeObserver(() => place(true)).observe(seg);
    if (document.fonts) document.fonts.ready.then(() => place(true));
    return seg;
  }

  // Swap the content of a tab panel with a short directional slide.
  function swapPanel(el, content, dir) {
    smoothHeight(el, () => {
      el.replaceChildren(...[].concat(content).flat(Infinity).filter(Boolean));
    });
    el.style.setProperty("--dir", String(dir || 0));
    el.classList.remove("tab-in");
    void el.offsetWidth;
    el.classList.add("tab-in");
  }

  // Show and hide floating elements with a transition.
  function reveal(el, show) {
    clearTimeout(el._hideTimer);
    if (show) {
      el.hidden = false;
      requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add("shown")));
    } else {
      el.classList.remove("shown");
      el._hideTimer = setTimeout(() => { el.hidden = true; }, 220);
    }
  }

  const ICONS = {
    check: "M4.5 10.5l3.5 3.5 7.5-8",
    cross: "M5.5 5.5l9 9M14.5 5.5l-9 9",
    alert: "M10 6.5v4.5M10 13.8v.2M10 2.8l7.8 13.7H2.2z",
    file: "M6 2.5h5.5l3.5 3.5v11.5H6zM11.5 2.5V6H15",
    download: "M10 3.5v9M6.5 9l3.5 3.5L13.5 9M4.5 16.5h11",
    up: "M10 15V5M6 9l4-4 4 4",
    down: "M10 5v10M6 11l4 4 4-4",
    trash: "M4.5 6h11M8 6V4.5h4V6M6 6l.8 10h6.4L14 6",
    info: "M10 9v5M10 6.2v.2M10 17.5a7.5 7.5 0 100-15 7.5 7.5 0 000 15z",
    folder: "M3.5 4.5h5l1.5 2h6.5v9h-13zM3.5 8.5h13",
    clock: "M10 17.5a7.5 7.5 0 100-15 7.5 7.5 0 000 15zM10 6v4l2.5 2",
    shield: "M10 2.5l6 2.4v4.5c0 3.9-2.6 6.8-6 8.1-3.4-1.3-6-4.2-6-8.1V4.9z",
    search: "M9 15.5a6.5 6.5 0 100-13 6.5 6.5 0 000 13zM13.8 13.8L17.5 17.5",
    open: "M11 3.5h5.5V9M16.5 3.5L9.5 10.5M8 5H4.5v10.5H15V12",
    chevron: "M8 5l5 5-5 5",
    dot: "M10 11a1 1 0 100-2 1 1 0 000 2z",
    plus: "M10 5v10M5 10h10",
    eye: "M2.5 10s2.8-5 7.5-5 7.5 5 7.5 5-2.8 5-7.5 5-7.5-5-7.5-5zM10 12.3a2.3 2.3 0 100-4.6 2.3 2.3 0 000 4.6z",
    slides: "M3.5 4.5h13v9h-13zM7 16.5h6M10 13.5v3",
    minus: "M5 10h10",
    target: "M10 3v3M10 14v3M3 10h3M14 10h3M10 14.2a4.2 4.2 0 100-8.4 4.2 4.2 0 000 8.4z",
    note: "M6 2.5h5.5l3.5 3.5v11.5H6zM8.5 10h4M8.5 13h4",
    contract: "M5 3h10v14H5zM7.5 6.5h5M7.5 9.5h5M7.5 12.5h2.5",
    chart: "M3.5 16.5h13M6 13.5V9M10 13.5V5.5M14 13.5V11",
    calc: "M5 2.5h10v15H5zM7.5 5.5h5v2.5h-5zM7.5 11h.01M10 11h.01M12.5 11h.01M7.5 14h.01M10 14h.01M12.5 14h.01",
    compare: "M3 6h9M9 3l3 3-3 3M17 14H8M11 11l-3 3 3 3",
    monitor: "M4 5h12v8H4zM8 16h4M10 13v3",
    sun: "M10 13.2a3.2 3.2 0 100-6.4 3.2 3.2 0 000 6.4zM10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.6 4.6l1.4 1.4M13.9 13.9l1.4 1.4M15.4 4.6l-1.4 1.4M5.9 14.1l-1.4 1.4",
    moon: "M15.6 12.4A6.4 6.4 0 018 5a6.4 6.4 0 108.3 8.3 5.7 5.7 0 01-.7-.9z",
  };

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", ICONS[name] || ICONS.dot);
    svg.append(path);
    return svg;
  }

  const spinner = () => h("span", { class: "spinner" });
  const fmtTime = (iso) => {
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  };
  const localMode = {
    get() {
      try { return localStorage.getItem("wb2:mode") === "rules"; } catch { return false; }
    },
    set(on) {
      try { on ? localStorage.setItem("wb2:mode", "rules") : localStorage.removeItem("wb2:mode"); } catch { /* ignore */ }
    },
    meta(extra) {
      return this.get() ? { ...extra, offline: true } : { ...extra };
    },
  };

  const initials = (name) => String(name || "?").split(/\s+/).slice(0, 2).map((w) => w[0] || "").join("").toUpperCase();

  const bytes = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);
  const plural = (n, word, many) => `${n} ${n === 1 ? word : many || `${word}s`}`;
  const shortName = (p) => String(p).split("/").pop();
  const TERMINAL = new Set(["completed", "failed", "handed_back", "rejected", "cancelled"]);

  const STATUS = {
    queued: "Waiting to start", routing: "Starting", planning: "Planning", awaiting_plan: "Needs your approval",
    waiting_tide: "Waiting for the reasoning model", running: "Working", awaiting_action: "Needs your approval",
    rendering: "Preparing files", awaiting_deliverable: "Ready for review", completed: "Done", failed: "Failed",
    handed_back: "Needs a person", rejected: "Stopped", cancelled: "Cancelled",
  };
  const NEEDS_YOU = new Set(["awaiting_plan", "awaiting_action", "awaiting_deliverable"]);
  const statusLabel = (t) => (t.status === "awaiting_deliverable" ? "Ready for your review" : STATUS[t.status] || t.status);
  const statusTone = (t) => (t.status === "completed" ? "good" : NEEDS_YOU.has(t.status) ? "warn"
    : ["failed", "handed_back", "rejected", "cancelled"].includes(t.status) ? "bad" : "busy");

  function levelOf(label) {
    if (!label) return "unclassified";
    if (typeof label === "string") return label.split(/[ ·+]/)[0].toLowerCase();
    return String(label.level || "").toLowerCase();
  }
  function labelTag(label, display) {
    const text = display || (typeof label === "string" ? label : [label.level, ...(label.compartments || [])].join(" · "));
    return h("span", { class: `tag lv-${levelOf(label)}`, text });
  }

  function toast(message, bad = false) {
    const el = h("div", { class: `toast${bad ? " bad" : ""}`, text: message });
    $("#toasts").append(el);
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add("shown")));
    setTimeout(() => { el.classList.remove("shown"); setTimeout(() => el.remove(), 300); }, bad ? 7000 : 3000);
  }

  function autosize(area) {
    const fit = () => { area.style.height = "auto"; area.style.height = `${Math.min(area.scrollHeight, 240)}px`; };
    area.addEventListener("input", fit);
    fit();
  }

  // -------------------------------------------------------------------------------------------
  // API

  const cache = {
    get(key) { try { return JSON.parse(sessionStorage.getItem(`wb2:${key}`) || "null"); } catch { return null; } },
    set(key, value) { try { sessionStorage.setItem(`wb2:${key}`, JSON.stringify(value)); } catch { /* storage unavailable */ } },
  };

  const store = {
    get(key, fallback) { try { return localStorage.getItem(key) || fallback; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage unavailable */ } },
  };
  let USER = store.get("wb_user", "engineer1");
  const setUserCookie = (id) => { document.cookie = `wb_user=${encodeURIComponent(id)}; path=/; SameSite=Strict`; };
  setUserCookie(USER);

  async function api(path, options = {}) {
    const opts = { ...options, headers: { "X-User": USER, ...(options.headers || {}) } };
    if (opts.json !== undefined) {
      opts.body = JSON.stringify(opts.json);
      opts.headers["Content-Type"] = "application/json";
      delete opts.json;
    }
    const res = await fetch(`/api${path}`, opts);
    const type = res.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText;
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    return data;
  }
  const fileUrl = (id) => `/api/files/${encodeURIComponent(id)}/download`;

  async function guarded(button, fn) {
    if (button) button.disabled = true;
    try { return await fn(); } catch (e) { toast(e.message, true); return undefined; } finally { if (button) button.disabled = false; }
  }

  // Longest common subsequence over words: what was removed, what was added, in order.
  function wordDiff(before, after) {
    const a = String(before).split(/(\s+)/);
    const b = String(after).split(/(\s+)/);
    const table = Array.from({ length: a.length + 1 }, () => new Uint32Array(b.length + 1));
    for (let i = a.length - 1; i >= 0; i -= 1) {
      for (let j = b.length - 1; j >= 0; j -= 1) {
        table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
      }
    }
    const parts = [];
    const push = (kind, text) => {
      const last = parts[parts.length - 1];
      if (last && last.kind === kind) last.text += text;
      else parts.push({ kind, text });
    };
    let i = 0;
    let j = 0;
    while (i < a.length && j < b.length) {
      if (a[i] === b[j]) { push("same", a[i]); i += 1; j += 1; }
      else if (table[i + 1][j] >= table[i][j + 1]) { push("del", a[i]); i += 1; }
      else { push("ins", b[j]); j += 1; }
    }
    while (i < a.length) { push("del", a[i]); i += 1; }
    while (j < b.length) { push("ins", b[j]); j += 1; }
    return parts.filter((p) => p.text !== "");
  }

  // Both sentences in full: the old one with what went, the new one with what arrived.
  function diffView(before, after) {
    const parts = wordDiff(before, after);
    const side = (drop, cls, label) => h("div", { class: "diff-side" },
      h("span", { class: "diff-label", text: label }),
      h("p", { class: `diff diff-${cls}-line` }, parts.filter((p) => p.kind !== drop).map((p) =>
        (p.kind === "same" ? p.text : h("span", { class: `diff-${p.kind}`, text: p.text })))));
    return h("div", { class: "diff-pair" }, side("ins", "old", "Before"), side("del", "new", "After"));
  }

  function button(text, onclick, kind = "") {
    return h("button", { class: `btn ${kind}`.trim(), type: "button", onclick: (e) => guarded(e.currentTarget, () => onclick(e)) }, text);
  }

  // -------------------------------------------------------------------------------------------
  // Overlays: a side panel for details and a modal for single records

  function openLayer(id, title, content) {
    const layer = $(`#${id}`);
    $(`#${id}-title`).textContent = title;
    const box = $(`#${id}-content`);
    if (layer.hidden || id === "panel") box.replaceChildren(...[].concat(content).flat(Infinity).filter(Boolean));
    else smoothHeight(box, () => fill(box, content));
    reveal(layer, true);
    $$("[data-close]", layer).forEach((el) => { el.onclick = () => closeLayer(id); });
  }
  const closeLayer = (id) => reveal($(`#${id}`), false);
  const openPanel = (title, content) => openLayer("panel", title, content);
  const openModal = (title, content) => openLayer("modal", title, content);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#modal").hidden) closeLayer("modal");
    else if (!$("#panel").hidden) closeLayer("panel");
  });

  const KIND_NAMES = {
    user_input: "Your request", plan: "Approved plan", attachment: "Attachment", ocr_text: "Page text",
    vlm_read: "Field read", kb_chunk: "Procedure passage", graph_fact: "Plant record", sandbox_result: "Script run",
    calc_result: "Calculation", check_result: "Check", model_output: "Model output", tool_output: "Tool output",
  };

  // Read a stored file without downloading it: text documents are rendered, images shown as they are.
  async function showFile(file) {
    const head = (extra) => [
      h("div", { class: "row wrap file-head" }, labelTag(file.label, file.label_display),
        Number.isFinite(file.size) ? h("span", { class: "muted small", text: bytes(file.size) }) : null,
        h("span", { class: "grow" }),
        h("a", { class: "btn ghost", href: fileUrl(file.id), download: "" }, icon("download"), "Download")),
      ...[].concat(extra),
    ];
    openModal(file.name, head(h("div", { class: "loading" }, spinner())));
    let doc;
    try {
      doc = await api(`/files/${encodeURIComponent(file.id)}/preview`);
    } catch (e) {
      openModal(file.name, head(h("p", { class: "muted", text: e.message })));
      return;
    }
    if (doc.kind === "image") {
      openModal(file.name, head(h("figure", { class: "preview" }, h("img", { src: fileUrl(file.id), alt: file.name }))));
      return;
    }
    if (doc.kind !== "text" || !doc.blocks.length) {
      openModal(file.name, head(h("p", { class: "muted", text: "This file has no readable preview. Download it to open it." })));
      return;
    }
    const parts = doc.blocks.map((b) => {
      if (b.kind === "table") {
        return h("div", { class: "table-wrap" }, h("table", { class: "table" },
          b.header && b.header.length ? h("thead", {}, h("tr", {}, b.header.map((c) => h("th", { text: c })))) : null,
          h("tbody", {}, (b.rows || []).map((r) => h("tr", {}, r.map((c) => h("td", { text: c })))))));
      }
      if (b.kind === "heading") return h("h3", { class: "doc-h", text: b.text });
      if (b.kind === "page") return h("p", { class: "doc-page", text: b.text });
      if (b.kind === "code") return h("pre", { class: "code", text: b.text });
      if (b.kind === "note") return h("p", { class: "muted small", text: b.text });
      return h("p", { text: b.text });
    });
    if (doc.truncated) parts.push(h("p", { class: "muted small", text: "The preview stops here. Download the file for the rest." }));
    openModal(file.name, head(h("article", { class: "doc-preview" }, parts)));
  }

  const fileIndex = new Map();
  async function previewPath(workspace, path) {
    if (!fileIndex.has(workspace)) {
      const files = await api(`/workspaces/${encodeURIComponent(workspace)}/files`).catch(() => []);
      fileIndex.set(workspace, new Map(files.map((x) => [x.path, x])));
    }
    const file = fileIndex.get(workspace).get(path);
    if (file) showFile(file);
    else toast("That file is not in this workspace any more", true);
  }

  const previewChip = (workspace, path, extra = null) => h("span", { class: "file-chip" },
    h("button", { class: "chip-open", type: "button", title: `Preview ${shortName(path)}`,
      onclick: () => previewPath(workspace, path) }, icon(/\.(png|jpe?g|tiff?)$/i.test(path) ? "eye" : "file"), shortName(path)),
    extra);

  async function showRecord(id) {
    openModal(id, h("div", { class: "loading" }, spinner()));
    let rec;
    try { rec = await api(`/records/${encodeURIComponent(id)}`); } catch (e) { openModal(id, h("p", { class: "muted", text: e.message })); return; }
    const task = rec.id.split("-")[1];
    const body = typeof rec.body === "string" ? rec.body : JSON.stringify(rec.body, null, 2);
    const parts = [
      h("div", { class: "row wrap" }, h("span", { class: "chip", text: KIND_NAMES[rec.kind] || rec.kind }), labelTag(rec.label),
        rec.confidence && rec.confidence !== "high" ? h("span", { class: "chip warn", text: `${rec.confidence} confidence` }) : null),
      h("p", { class: "muted small", text: `${rec.source || ""}${rec.created_at ? ` · ${fmtTime(rec.created_at)}` : ""}` }),
    ];
    if (rec.body && rec.body.crop) {
      parts.push(h("div", { class: "crops" },
        h("img", { class: "crop", alt: "Scanned region", src: `/api/tasks/${task}/evidence/${rec.body.crop}` }),
        rec.body.zoomed_crop ? h("img", { class: "crop", alt: "Zoomed region", src: `/api/tasks/${task}/evidence/${rec.body.zoomed_crop}` }) : null));
    }
    parts.push(h("pre", { class: "code", text: body.length > 12000 ? `${body.slice(0, 12000)}\n...` : body }));
    const fields = Object.entries(rec.fields || {}).filter(([k]) => k !== "source_key");
    if (fields.length) {
      parts.push(h("details", {}, h("summary", { text: `Extracted values (${fields.length})` }),
        h("table", { class: "table" }, h("tbody", {}, fields.slice(0, 40).map(([k, v]) => h("tr", {},
          h("td", { class: "mono", text: k }), h("td", { text: v.raw }), h("td", { class: "muted", text: v.confidence || "" })))))));
    }
    if (rec.chain && rec.chain.length > 1) {
      parts.push(h("details", {}, h("summary", { text: "Where this came from" }),
        h("ul", { class: "plain" }, rec.chain.map((c) => h("li", {}, h("button", { class: "link-btn mono", type: "button", text: c.id, onclick: () => showRecord(c.id) }), ` ${c.summary.slice(0, 90)}`)))));
    }
    parts.push(h("p", { class: "muted tiny mono", text: `hash ${rec.hash.slice(0, 24)}...` }));
    openModal(KIND_NAMES[rec.kind] || rec.kind, parts);
  }

  // -------------------------------------------------------------------------------------------
  // Shell

  const shell = { workspaces: [], ws: null, me: null, users: [] };
  const THEME_KEY = "wb2:theme";
  let themeChoice = null;

  // Light is the default; dark only when someone picks it.
  function storedTheme() {
    try {
      const t = localStorage.getItem(THEME_KEY);
      return t === "light" || t === "dark" ? t : null;
    } catch { return themeChoice; }
  }

  function currentTheme() {
    return storedTheme() || "light";
  }

  function applyTheme() {
    document.documentElement.setAttribute("data-theme", currentTheme());
    const btn = $("#theme-toggle");
    if (!btn) return;
    const target = currentTheme() === "dark" ? "light" : "dark";
    const label = document.createElement("span");
    label.textContent = target === "light" ? "Light" : "Dark";
    btn.replaceChildren(icon(target === "light" ? "sun" : "moon"), label);
    btn.title = `Switch to ${label.textContent.toLowerCase()} theme`;
  }

  function initTheme() {
    applyTheme();
    const btn = $("#theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      themeChoice = currentTheme() === "dark" ? "light" : "dark";
      try { localStorage.setItem(THEME_KEY, themeChoice); } catch { /* storage unavailable */ }
      applyTheme();
    });
  }

  async function initShell() {
    const page = document.body.dataset.page;
    initTheme();
    const side = $("#sidebar");
    const scrim = $("#scrim");
    const toggleSide = (open) => { side.classList.toggle("open", open); scrim.hidden = !open; };
    $("#menu-btn").addEventListener("click", () => toggleSide(!side.classList.contains("open")));
    scrim.addEventListener("click", () => toggleSide(false));

    const menu = $("#account-menu");
    const accountBtn = $("#account-btn");
    accountBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = menu.hidden || !menu.classList.contains("shown");
      reveal(menu, open);
      accountBtn.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (e) => { if (!menu.hidden && !menu.contains(e.target)) reveal(menu, false); });
    $$("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === page));
    // Paint what this tab saw last so the sidebar does not pop in; a bad copy is simply ignored.
    try {
      const saved = cache.get("identity");
      if (saved && saved.user === USER && saved.me && typeof saved.me.name === "string") paintIdentity(saved.me);
      const recent = cache.get("recent");
      if (recent && recent.user === USER && Array.isArray(recent.tasks)) paintRecent(recent.tasks);
    } catch { /* the live data below replaces it anyway */ }

    let [users, me, workspaces] = await Promise.all([api("/users"), api("/me").catch(() => null), api("/workspaces").catch(() => null)]);
    shell.users = users;
    if (!me || !users.some((u) => u.id === USER)) {
      USER = users[0].id;
      setUserCookie(USER);
      [me, workspaces] = await Promise.all([api("/me"), api("/workspaces")]);
    }
    const pick = (id) => {
      if (id === USER) { reveal(menu, false); return; }
      store.set("wb_user", id);
      setUserCookie(id);
      location.href = "/";
    };
    fill($("#user-list"), ...shell.users.map((u) => h("button", {
      class: `person${u.id === USER ? " on" : ""}`, type: "button", role: "menuitemradio",
      "aria-checked": String(u.id === USER), onclick: () => pick(u.id),
    },
      h("span", { class: "avatar sm", text: initials(u.name) }),
      h("span", { class: "person-name", text: u.name }),
      h("span", { class: "grow" }),
      u.id === USER ? h("span", { class: "person-tick" }, icon("check")) : null)));
    shell.me = me;
    paintIdentity(me);
    cache.set("identity", { user: USER, me: { name: me.name } });

    shell.workspaces = workspaces;
    const wsSel = $("#ws-select");
    shell.workspaces.forEach((w) => wsSel.append(h("option", { value: w.id, text: w.title })));
    const wanted = new URLSearchParams(location.search).get("ws") || store.get("wb_ws", "");
    shell.ws = shell.workspaces.find((w) => w.id === wanted) || shell.workspaces[0] || null;
    if (shell.ws) {
      wsSel.value = shell.ws.id;
      store.set("wb_ws", shell.ws.id);
    }
    wsSel.addEventListener("change", () => {
      store.set("wb_ws", wsSel.value);
      location.href = "/";
    });
    $(".ws-picker").hidden = page !== "home";

    refreshRecent();
    setInterval(refreshRecent, 5000);
    initNetwork();
  }

  function paintIdentity(me) {
    $("#account-name").textContent = me.name;
    $("#avatar").textContent = me.name.split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  }

  async function refreshRecent() {
    let tasks = [];
    try { tasks = await api("/tasks"); } catch { return; }
    const slim = tasks.filter((t) => t.user === USER && !t.followup_of).slice(0, 40)
      .map((t) => ({ id: t.id, text: t.text, status: t.status, created_at: t.created_at, user: t.user }));
    cache.set("recent", { user: USER, tasks: slim });
    paintRecent(slim);
  }

  function paintRecent(mine) {
    const list = $("#recent");
    const waiting = mine.filter((t) => NEEDS_YOU.has(t.status)).length;
    const badge = $("#nav-count");
    badge.hidden = !waiting;
    badge.textContent = waiting || "";
    const current = document.body.dataset.task;
    const today = new Date().toDateString();
    let group = "";
    fill(list, ...mine.slice(0, 40).map((t) => {
      const day = new Date(t.created_at).toDateString() === today ? "Today" : "Earlier";
      const heading = day !== group;
      group = day;
      let mark = null;
      if (NEEDS_YOU.has(t.status)) mark = h("span", { class: "flag", title: STATUS[t.status] });
      else if (!TERMINAL.has(t.status)) mark = h("span", { class: "spinner tiny", title: STATUS[t.status] });
      else if (t.status !== "completed") mark = h("span", { class: "flag bad", title: STATUS[t.status] });
      return [heading ? h("div", { class: "side-group", "data-key": `g-${day}`, text: day }) : null,
        h("a", { href: `/t/${t.id}`, class: t.id === current ? "active" : "", title: t.text, "data-key": t.id },
          h("span", { class: "recent-title", text: t.text }), mark)];
    }));
    if (!mine.length) fill(list, h("p", { class: "side-empty", text: "Your tasks will appear here." }));
  }

  function initNetwork() {
    const btn = $("#net-btn");
    const pop = $("#net-pop");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = pop.hidden || !pop.classList.contains("shown");
      if (open) renderNetPop();
      reveal(pop, open);
    });
    document.addEventListener("click", (e) => { if (!pop.hidden && !pop.contains(e.target)) reveal(pop, false); });
    const tick = async () => {
      try {
        const snap = await api("/egress");
        shell.egress = snap;
        // Nothing is shown while the server is quiet: the chip appears only when something is wrong.
        const dot = $("#net-dot");
        const wrong = snap.status !== "ok" || snap.breach || snap.external_connections;
        $("#net").hidden = !wrong;
        if (!wrong) return;
        dot.className = snap.status !== "ok" ? "net-dot off" : "net-dot bad";
        $("#net-text").textContent = snap.status !== "ok" ? "Monitor offline" : "Data left the server";
      } catch { /* retried on the next tick */ }
    };
    tick();
    setInterval(tick, 8000);
  }

  function renderNetPop() {
    const pop = $("#net-pop");
    const snap = shell.egress;
    if (!snap || snap.status !== "ok") {
      fill(pop, h("strong", { text: "The network monitor is not running" }),
        h("p", { class: "muted small", text: "Start egressd, then reload." }));
      return;
    }
    const blocked = (snap.blocked_connect_host || 0) + (snap.blocked_connect_sandbox || 0);
    fill(pop, 
      h("strong", { text: snap.external_connections ? "Outbound connections were detected" : "Nothing has left this server" }),
      h("p", { class: "muted small", text: `${snap.external_connections} outbound connections · ${plural(blocked, "attempt")} blocked` }),
      h("div", { class: "row" },
        button("Run a test", async () => {
          const res = await api("/egress/test", { method: "POST" });
          toast(res.pass ? "Test passed: every attempt was blocked" : "The test found a problem", !res.pass);
          shell.egress = await api("/egress");
          renderNetPop();
        }),
        h("a", { class: "btn ghost", href: "/security", text: "Details" })));
  }

  // -------------------------------------------------------------------------------------------
  // Home

  async function pageHome() {
    const ws = shell.ws;
    if (!ws) {
      fill($("#content"), h("div", { class: "home" }, h("h1", { class: "greeting", text: "No workspace access" }),
        h("p", { class: "muted", text: "Ask an administrator to add you to a workspace." })));
      return;
    }
    const hour = new Date().getHours();
    const greeting = $(".greeting");
    greeting.textContent = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
    let taps = 0;
    let tapTimer = null;
    greeting.addEventListener("click", () => {
      clearTimeout(tapTimer);
      tapTimer = setTimeout(() => { taps = 0; }, 900);
      if (++taps < 5) return;
      taps = 0;
      const on = !localMode.get();
      localMode.set(on);
      toast(on ? "Answering with the built-in rules" : "Answering with the models");
    });
    $(".greeting-sub").textContent = `What should we prepare in ${ws.title}?`;
    const text = $("#task-text");
    const send = $("#send-btn");
    const attached = new Set();
    let files = [];
    autosize(text);
    text.focus();
    const syncSend = () => { send.disabled = !text.value.trim(); };
    text.addEventListener("input", syncSend);
    // Other pages link here with a question ready to send (the plant map's "Ask about it").
    const ask = new URLSearchParams(location.search).get("ask");
    if (ask) {
      text.value = ask.slice(0, 4000);
      text.dispatchEvent(new Event("input"));
      history.replaceState(null, "", "/");
    }
    text.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (text.value.trim()) $("#composer").requestSubmit(); }
    });

    const renderAttached = () => {
      fill($("#attached"), ...[...attached].map((p) => previewChip(ws.id, p,
        h("button", { type: "button", "aria-label": `Remove ${shortName(p)}`, onclick: () => { attached.delete(p); renderAttached(); renderPicker(); } }, icon("cross")))));
    };
    const renderPicker = () => {
      const inputs = files.filter((f) => f.area === "inputs");
      fill($("#picker-list"), ...inputs.map((f) => h("label", { class: "pick" },
        h("input", { type: "checkbox", checked: attached.has(f.path), onchange: (e) => { e.target.checked ? attached.add(f.path) : attached.delete(f.path); renderAttached(); } }),
        h("span", { class: "pick-name", text: f.name }), labelTag(f.label, f.label_display), h("span", { class: "muted small", text: bytes(f.size) }),
        h("button", { class: "pick-preview", type: "button", title: `Preview ${f.name}`,
          onclick: (e) => { e.preventDefault(); e.stopPropagation(); showFile(f); } }, icon("eye")))));
      if (!inputs.length) $("#picker-list").append(h("p", { class: "muted small", text: "No files yet. Upload one to start." }));
    };
    const renderStarters = () => {
      const find = (re) => files.find((f) => f.area === "inputs" && re.test(f.name));
      const ideas = [];
      const add = (ico, label, prompt, paths) => { if (paths.every(Boolean)) ideas.push([ico, label, prompt, paths]); };
      add("note", "Draft an approval note", "Draft an approval note for this inspection report", [find(/^inspection_P108B/)]);
      add("contract", "Summarise a contract", "Summarise this vendor contract", [find(/contract.*\.pdf$/)]);
      add("chart", "Analyse sensor readings", "Write a Python script to parse these pressure readings and flag anomalies", [find(/pressure.*\.csv$/)]);
      add("calc", "Calculate wall thickness", "Compute the required wall thickness for this pipe per the attached data", [find(/pipe_data/)]);
      add("slides", "Prepare a board deck", "Turn these notes into a short board deck", [find(/notes.*\.md$/)]);
      const offers = files.filter((f) => f.area === "inputs" && /^(offer_|tender)/.test(f.name));
      if (offers.length >= 2) ideas.push(["compare", "Compare vendor offers", "Compare these three vendor offers against the tender conditions and recommend one", offers]);
      fill($("#starters"), ...ideas.slice(0, 5).map(([ico, label, prompt, paths]) => h("button", {
        class: "starter", type: "button",
        onclick: () => {
          text.value = prompt;
          attached.clear();
          paths.forEach((f) => attached.add(f.path));
          renderAttached(); renderPicker(); syncSend(); text.dispatchEvent(new Event("input")); text.focus();
        },
      }, icon(ico), label)));
    };
    const loadFiles = async () => {
      files = await api(`/workspaces/${ws.id}/files`);
      renderPicker();
      renderStarters();
    };

    const picker = $("#picker");
    $("#attach-btn").addEventListener("click", (e) => { e.stopPropagation(); reveal(picker, picker.hidden || !picker.classList.contains("shown")); });
    document.addEventListener("click", (e) => { if (!picker.hidden && !picker.contains(e.target)) reveal(picker, false); });
    $("#upload-input").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      await guarded(null, async () => {
        const rec = await api(`/workspaces/${ws.id}/files`, { method: "POST", body: form });
        toast(`Uploaded ${rec.name} (${rec.label_display})`);
        attached.add(rec.path);
        renderAttached();
        await loadFiles();
      });
      e.target.value = "";
    });
    $("#composer").addEventListener("submit", (e) => {
      e.preventDefault();
      const value = text.value.trim();
      if (!value) return;
      guarded(send, async () => {
        const meta = localMode.meta($("#no-template").checked ? { templates_disabled: true } : {});
        const task = await api("/tasks", { method: "POST", json: { workspace: ws.id, text: value, attachments: [...attached], meta } });
        location.href = `/t/${task.id}`;
      });
    });
    await loadFiles();
  }

  // -------------------------------------------------------------------------------------------
  // Task (conversation view)

  const TOOL_NAMES = {
    read_document: "Read the document", read_file: "Read the file", write_file: "Save a file", list_files: "List files",
    search_kb: "Search the procedures", graph_lookup: "Look up plant records", check_consistency: "Check the facts",
    calculate: "Calculate", run_python: "Run a script", make_docx: "Create the Word document",
    make_xlsx: "Create the spreadsheet", make_pptx: "Create the slide deck", recall: "Re-read a source",
    delegate: "Ask a helper task", finish: "Finish", summarise_document: "Summarise the document",
    answer_question: "Answer with sources", write_code: "Write the script and test it safely",
  };
  const STEP_ICON = { done: "check", incomplete: "alert", denied: "cross", failed: "alert", skipped: "cross" };

  function citeText(text, sources, onOpen) {
    const out = [];
    let last = 0;
    const re = /\s*\[(R-[A-Za-z0-9]+-\d+(?:,\s*R-[A-Za-z0-9]+-\d+)*)\]/g;
    let m;
    while ((m = re.exec(text))) {
      const before = text.slice(last, m.index);
      const word = before.match(/\S+$/);
      out.push(word ? before.slice(0, word.index) : before);
      const badges = m[1].split(/,\s*/).map((rid) => {
        if (!sources.includes(rid)) sources.push(rid);
        return h("button", { class: "cite", type: "button", title: rid, text: sources.indexOf(rid) + 1, onclick: () => onOpen(rid) });
      });
      last = m.index + m[0].length;
      // Keep trailing punctuation on the same line as its badges.
      const tail = text.slice(last).match(/^[.,;:!?)]+/);
      if (tail) last += tail[0].length;
      out.push(h("span", { class: "cite-group" }, word ? word[0] : "", badges, tail ? tail[0] : ""));
    }
    out.push(text.slice(last));
    return out;
  }

  function sourceParts(rec, rid) {
    const summary = rec ? rec.summary : rid;
    if (rec && rec.kind === "graph_fact") return { title: "Plant records", body: summary };
    if (rec && rec.kind === "calc_result") return { title: "Counted from the document", body: summary };
    const page = summary.match(/^(.+?) p\.(\d+) \([^)]*\): (.*)$/);
    if (page) return { title: `${page[1]} · page ${page[2]}`, body: page[3].replace(/^#+\s*/, "") };
    const cut = summary.search(/ · |: /);
    if (cut > 0) return { title: summary.slice(0, cut), body: summary.slice(cut + 2).replace(/^[\s·:]+/, "") };
    return { title: KIND_NAMES[rec && rec.kind] || rid, body: summary };
  }

  async function pageTask() {
    const id = document.body.dataset.task;
    const view = { root: null, editing: null, stamp: "", ledgers: {}, fetched: new Set(), stick: true };
    const follow = $("#followup");
    const box = $("#followup-text");
    const scroller = $("#content");
    autosize(box);

    const convo = () => (view.root ? [view.root, ...(view.root.followups || [])] : []);
    const latest = () => convo()[convo().length - 1];
    const findTask = (tid) => convo().find((x) => x.id === tid);
    const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160;
    const toBottom = (smooth) => scroller.scrollTo({ top: scroller.scrollHeight, behavior: smooth ? "smooth" : "auto" });

    const load = async () => {
      let task;
      try { task = await api(`/tasks/${id}`); } catch (e) {
        fill($("#thread"), h("div", { class: "notice", text: e.status === 403 ? "You do not have access to this task." : e.message }));
        follow.hidden = true;
        return false;
      }
      if (task.followup_of) {
        location.replace(`/t/${task.followup_of}`);
        return false;
      }
      const all = [task, ...(task.followups || [])];
      const stamp = all.map((x) => `${x.id}:${x.revision_no}:${x.status}:${x.trace ? x.trace.length : 0}`).join("|");
      if (stamp !== view.stamp) {
        const stick = view.stick || nearBottom();
        view.root = task;
        view.stamp = stamp;
        const last = latest();
        renderHead(all.find((x) => NEEDS_YOU.has(x.status)) || last);
        if (!view.editing) renderThread();
        updateComposer();
        if (stick) requestAnimationFrame(() => toBottom(view.stamp !== "" && !view.first));
        view.first = false;
        view.stick = false;
      }
      return all.some((x) => !TERMINAL.has(x.status));
    };

    function updateComposer() {
      const last = latest();
      const busy = last && !TERMINAL.has(last.status);
      follow.hidden = false;
      box.disabled = busy;
      const waitingText = { plan: "Approve the plan above to continue", action: "Answer the question above to continue",
        deliverable: "Approve or review the draft above to continue", template_choice: "Choose an approach above to continue" };
      box.placeholder = busy ? (last.pending_gate ? waitingText[last.pending_gate.kind] || "Waiting for you above" : "Working on it...")
        : "Ask a follow-up";
      $("button[type=submit]", follow).disabled = busy || !box.value.trim();
    }
    // Poll quickly while work runs, slowly while waiting for the user, and at once after a click.
    let timer = null;
    let burst = 0;
    const poll = async () => {
      clearTimeout(timer);
      let again = true;
      try { again = await load(); } catch { again = true; }
      if (!again) return;
      const waiting = latest() && latest().pending_gate;
      const delay = burst > 0 ? 250 : waiting ? 2000 : 500;
      burst = Math.max(0, burst - 1);
      timer = setTimeout(poll, delay);
    };
    const refresh = () => { view.stamp = ""; burst = 12; return poll(); };
    view.first = true;

    function renderHead(t) {
      const job = t.job;
      const running = job && ["queued", "running"].includes(job.state) && t.is_owner;
      fill($("#thread-head"),
        h("div", { class: "head-left" },
          h("span", { class: `status-pill ${statusTone(t)}` }, statusLabel(t)),
          h("div", { class: "thread-title", text: view.root ? view.root.text : t.text })),
        h("div", { class: "row" },
          running && !t.pending_gate ? button("Stop", async () => { await api(`/jobs/${job.id}`, { method: "DELETE" }); toast("Stopping"); }, "ghost") : null,
          h("button", { class: "btn ghost", type: "button", onclick: () => openDetails(t) }, icon("info"), "Details")));
    }

    function assistant(t, children) {
      return h("div", { class: "turn assistant", "data-key": `assistant-${t.id}` }, h("img", { class: "turn-avatar", src: "/static/mark.svg", alt: "" }),
        h("div", { class: "turn-body" }, children,
          TERMINAL.has(t.status) && latest() && t.id !== latest().id ? h("div", { class: "turn-tools" },
            h("button", { class: "link-btn quiet", type: "button", onclick: () => openDetails(t) }, icon("info"), "Details")) : null));
    }

    function renderThread() {
      const items = [];
      convo().forEach((t, index) => {
        items.push(h("div", { class: "turn user", "data-key": `user-${t.id}` }, h("div", { class: "bubble" },
          h("p", { text: t.text }),
          t.attachments.length && (index === 0 || t.attachments.join() !== view.root.attachments.join()) ? h("div", { class: "row wrap" }, t.attachments.map((a) => previewChip(t.workspace, a))) : null)));
        const body = turnBody(t);
        if (body.length) items.push(assistant(t, body));
      });
      fill($("#thread"), ...items);
    }

    function turnBody(t) {
      const body = [];
      const gate = t.pending_gate;
      const step = t.plan && t.plan.steps.find((s) => s.status === "running");
      const replying = t.plan && t.plan.steps.every((st) => st.model_task === "chat_reply");
      if (!TERMINAL.has(t.status) && !gate && replying) {
        // A plain reply has one step, so dots read better than a step counter.
        body.push(h("div", { class: "typing" }, h("i"), h("i"), h("i")));
      } else if (!TERMINAL.has(t.status) && !gate) {
        const total = t.plan ? t.plan.steps.length : 0;
        const done = t.plan ? t.plan.steps.filter((s) => ["done", "incomplete", "denied"].includes(s.status)).length : 0;
        let note = STATUS[t.status] || "Working";
        if (step) note = `${step.title || step.id} (${done + 1} of ${total})`;
        if (t.status === "waiting_tide") note = "Waiting for the reasoning model to load";
        if (t.job && t.job.state === "queued") note = `Waiting in line (position ${t.job.position})`;
        body.push(h("div", { class: "working" }, h("span", { class: "shimmer", text: note })));
      }
      if (gate && gate.kind === "template_choice") body.push(choiceBlock(t, gate));
      if (t.plan && !replying) body.push(gate && gate.kind === "plan" ? planGate(t, gate) : progress(t));
      if (gate && gate.kind === "action") body.push(actionGate(t, gate));
      body.push(...results(t));
      return body;
    }

    function choiceBlock(t, gate) {
      return h("div", { class: "block" },
        h("p", { text: "More than one saved approach fits this request. Which one should I use?" }),
        h("div", { class: "row wrap" }, gate.payload.options.map((name) => button(name.replace(/_/g, " "), async () => {
          await api(`/tasks/${t.id}/template/decision`, { method: "POST", json: { template: name } });
          await refresh();
        }))));
    }

    function stepSummary(s) {
      return s.title || TOOL_NAMES[s.tool] || TOOL_NAMES[s.model_task] || (s.tool || s.model_task || s.id).replace(/_/g, " ");
    }

    function planGate(t, gate) {
      const plan = t.plan;
      const editing = view.editing;
      const steps = editing ? editing.steps : plan.steps;
      const list = h("ol", { class: "plan" }, steps.map((s, i) => h("li", {},
        h("span", { class: "plan-text", text: stepSummary(s) }),
        s.side_effect ? h("span", { class: "chip", text: "creates a file" }) : null,
        editing ? h("span", { class: "row tight" },
          h("button", { class: "icon-btn", type: "button", "aria-label": "Move up", disabled: i === 0, onclick: () => { [steps[i - 1], steps[i]] = [steps[i], steps[i - 1]]; renderThread(); } }, icon("up")),
          h("button", { class: "icon-btn", type: "button", "aria-label": "Move down", disabled: i === steps.length - 1, onclick: () => { [steps[i + 1], steps[i]] = [steps[i], steps[i + 1]]; renderThread(); } }, icon("down")),
          h("button", { class: "icon-btn", type: "button", "aria-label": "Remove step", onclick: () => { steps.splice(i, 1); renderThread(); } }, icon("trash"))) : null)));
      const eta = plan.est_time_s ? (plan.est_time_s < 60 ? ", under a minute" : `, about ${Math.round(plan.est_time_s / 60)} min`) : "";
      const parts = [h("p", { text: `Here is my plan${eta}:` }), list];
      if (plan.errors && plan.errors.length) {
        parts.push(h("div", { class: "notice bad" }, h("strong", { text: "This plan cannot run yet" }), h("ul", { class: "plain" }, plan.errors.map((e) => h("li", { text: e })))));
      }
      if (editing) {
        parts.push(h("div", { class: "row" },
          button("Use this plan", async () => {
            await api(`/tasks/${t.id}/plan/decision`, { method: "POST", json: { decision: "edit", plan: typedPlan(plan, steps) } });
            view.editing = null;
            await refresh();
          }, "primary"),
          h("button", { class: "btn ghost", type: "button", text: "Discard changes", onclick: () => { view.editing = null; renderThread(); } })));
      } else {
        parts.push(h("div", { class: "row" },
          h("button", { class: "btn primary", type: "button", disabled: !plan.valid, onclick: (e) => guarded(e.currentTarget, async () => {
            await api(`/tasks/${t.id}/plan/decision`, { method: "POST", json: { decision: "approve" } });
            await refresh();
          }) }, "Start"),
          h("button", { class: "btn", type: "button", text: "Edit", onclick: () => {
            view.editing = { steps: plan.steps.map((s) => ({ ...s })) };
            renderThread();
          } }),
          button("Cancel", async () => {
            await api(`/tasks/${t.id}/plan/decision`, { method: "POST", json: { decision: "reject" } });
            await refresh();
          }, "ghost")));
      }
      return h("div", { class: "block" }, parts);
    }

    function typedPlan(plan, steps) {
      return {
        goal: plan.goal, deliverables: plan.deliverables,
        steps: steps.map((s) => ({
          id: s.id, title: s.title, action: s.tool ? { tool: s.tool } : { model_task: s.model_task },
          args: s.args, inputs: s.inputs.map((i) => ({ from: i.from || i.source, ref: i.ref })),
          output_type: s.output_type, side_effect: s.side_effect,
        })),
      };
    }

    function progress(t) {
      const steps = t.plan.steps;
      const done = steps.filter((s) => s.status === "done").length;
      const took = {};
      (t.trace || []).forEach((r) => { if (r.step_id) took[r.step_id] = (took[r.step_id] || 0) + (r.latency_s || 0); });
      const list = h("ol", { class: "steps" }, steps.map((s) => {
        const state = s.status || "pending";
        const mark = state === "running" ? spinner() : STEP_ICON[state] ? icon(STEP_ICON[state]) : h("span", { class: "pending-dot" });
        const secs = took[s.id];
        return h("li", { class: `st-${state}` }, h("span", { class: "step-mark" }, mark),
          h("span", { class: "step-text" }, stepSummary(s),
            s.note && !["done", "running"].includes(state) ? h("span", { class: "muted small", text: ` · ${s.note}` }) : null),
          state === "done" && secs ? h("span", { class: "step-time", text: secs < 1 ? `${Math.round(secs * 1000)} ms` : `${secs.toFixed(1)} s` }) : null);
      }));
      if (TERMINAL.has(t.status) || t.status === "awaiting_deliverable") {
        return h("details", { class: "steps-wrap" }, h("summary", { text: `${done} of ${plural(steps.length, "step")} completed` }), list);
      }
      return h("div", { class: "steps-wrap open" }, list);
    }

    function actionGate(t, gate) {
      const p = gate.payload;
      const name = p.args && (p.args.name || p.args.path) ? shortName(p.args.name || p.args.path) : null;
      const act = (approve) => async () => {
        await api(`/tasks/${t.id}/actions/${gate.id}/decision`, { method: "POST", json: { approve } });
        await refresh();
      };
      return h("div", { class: "block ask" },
        h("p", {}, h("strong", { text: name ? `Save ${name}?` : `${stepSummary((t.plan && t.plan.steps.find((s) => s.id === p.step)) || { tool: p.tool })}?` })),
        h("p", { class: "muted small", text: "This creates a draft. Nothing becomes final until you approve it." }),
        h("details", {}, h("summary", { text: "What will run" }), h("pre", { class: "code", text: JSON.stringify(p.args, null, 2) })),
        h("div", { class: "row" }, button("Allow", act(true), "primary"), button("Don't allow", act(false), "ghost")));
    }

    function results(t) {
      const out = [];
      const r = t.result || {};
      if (r.reply) {
        const idle = !convo().some((x) => !TERMINAL.has(x.status));
        out.push(h("div", { class: "answer", title: r.reply.model ? `Written by ${r.reply.model} on this machine` : "" },
          h("p", { text: r.reply.text })));
        if (r.reply.note) out.push(h("p", { class: "muted small", text: r.reply.note }));
        if ((r.reply.suggestions || []).length) {
          out.push(h("div", { class: "suggest-row" }, r.reply.suggestions.map((sg) => h("button", {
            class: "starter", type: "button", disabled: !idle,
            onclick: (e) => guarded(e.currentTarget, () => sendFollowup(sg.text, sg.attachments || [])),
          }, icon((sg.attachments || []).length ? "file" : "chevron"), sg.label))));
        }
      }
      const lines = r.answer ? r.answer.answer : r.summary ? r.summary.points : null;
      if (lines) {
        const sources = [];
        if (r.summary && !r.answer) out.push(h("p", { class: "lead-in", text: "Here are the key points. The full summary is in the document below." }));
        out.push(h("div", { class: `answer${r.summary && !r.answer ? " points" : ""}` },
          r.summary && !r.answer
            ? h("ul", {}, lines.map((a) => h("li", {}, citeText(a.text, sources, showRecord))))
            : lines.map((a) => h("p", {}, citeText(a.text, sources, showRecord)))));
        if (sources.length) {
          const recs = new Map((view.ledgers[t.id] || []).map((x) => [x.id, x]));
          out.push(h("div", { class: "source-cards" }, sources.map((rid, i) => {
            const rec = recs.get(rid);
            const { title, body } = sourceParts(rec, rid);
            return h("button", { class: "source-card", type: "button", onclick: () => showRecord(rid) },
              h("span", { class: "source-n", text: i + 1 }),
              h("span", { class: "source-main" }, h("span", { class: "source-title", text: title }), h("span", { class: "source-body", text: body })));
          })));
          if (!view.fetched.has(t.id)) {
            view.fetched.add(t.id);
            api(`/tasks/${t.id}/ledger`).then((l) => { view.ledgers[t.id] = l; renderThread(); }).catch(() => {});
          }
        }
      }
      const cited = new Set(((r.answer && r.answer.answer) || []).flatMap((a) => (a.text.match(/R-[A-Za-z0-9]+-\d+/g) || [])));
      const facts = (r.facts || []).filter((f) => !cited.has(f.record));
      if (facts.length) {
        out.push(h("div", { class: "facts" }, facts.map((f) => h("button", { class: "fact", type: "button", title: "Show how this was counted", onclick: () => showRecord(f.record) },
          h("span", { class: "fact-icon" }, icon("calc")), h("span", { text: f.text })))));
      }
      if (r.code) {
        const res = r.code.result;
        const tries = r.code.attempts > 1 ? ` It needed ${r.code.attempts} attempts; the earlier errors are in Details.` : "";
        out.push(h("p", { text: `The script ran successfully in the sandbox.${tries}` }));
        if (res && typeof res === "object" && !Array.isArray(res)) {
          out.push(h("table", { class: "table kv" }, h("tbody", {}, Object.entries(res).map(([k, v]) => h("tr", {},
            h("td", { class: "muted", text: k.replace(/_/g, " ") }),
            h("td", { text: Array.isArray(v) ? v.join(", ") : typeof v === "object" && v !== null ? JSON.stringify(v) : String(v) }))))));
        } else if (res !== undefined && res !== null) {
          out.push(h("pre", { class: "code", text: typeof res === "string" ? res : JSON.stringify(res, null, 2) }));
        }
      }
      const checks = t.checks || [];
      if (checks.length) {
        const issues = checks.filter((c) => c.status === "mismatch");
        if (issues.length) {
          out.push(h("p", { text: `I found ${plural(issues.length, "issue")} you should look at:` }),
            h("ul", { class: "issues" }, issues.map((c) => h("li", {}, h("strong", { text: c.description || c.rule }),
              c.left_value !== undefined && c.left_value !== null ? h("span", { class: "muted", text: ` (${c.left_value}${c.right_value ? ` vs ${c.right_value}` : ""})` }) : null))));
        } else {
          out.push(h("p", { class: "muted", text: `All ${plural(checks.length, "check")} passed.` }));
        }
      }
      if (t.deliverables && t.deliverables.length) {
        const gate = t.pending_gate && t.pending_gate.kind === "deliverable";
        const previews = t.deliverables.filter((d) => /\.svg$/i.test(d.relpath));
        previews.forEach((d) => out.push(h("figure", { class: "preview" },
          h("img", { src: fileUrl(d.final_file_id || d.file_id), alt: shortName(d.relpath), loading: "lazy" }))));
        out.push(h("div", { class: "files" }, t.deliverables.map((d) => {
          const file = { id: d.final_file_id || d.file_id, name: shortName(d.relpath), label: d.label, size: d.size };
          return h("div", { class: "file" },
            h("button", { class: "tile-open", type: "button", title: `Open ${file.name}`, onclick: () => showFile(file) },
              h("span", { class: `file-ext ext-${d.relpath.split(".").pop().toLowerCase()}`, text: d.relpath.split(".").pop() }),
              h("div", { class: "file-info" }, h("div", { class: "file-name", text: file.name }),
                h("div", { class: "row tight" }, labelTag(d.label), h("span", { class: "muted small", text: d.status === "approved" ? "Final" : "Draft" })))),
            h("a", { class: "file-dl", href: fileUrl(file.id), download: "", title: `Download ${file.name}` }, icon("download")));
        })));
        if (gate) {
          const ds = t.draft_summary || {};
          const open = (ds.unacknowledged || []).length + (ds.orphans || []).length;
          const quick = !open && !(t.blockers || []).length && t.can_approve;
          out.push(h("div", { class: `ready-card ${open ? "warn" : "good"}` },
            h("span", { class: "ready-icon" }, icon(open ? "alert" : "check")),
            h("div", { class: "ready-text" },
              h("strong", { text: open ? `${plural(open, "item")} to check before approving` : "Ready for your approval" }),
              h("span", { class: "muted small", text: open ? "Open the review to mark each issue as reviewed." : "Nothing is final until you approve it." })),
            h("div", { class: "row tight" },
              quick ? button("Approve", async () => {
                await api(`/tasks/${t.id}/draft/decision`, { method: "POST", json: { decision: "approve", acknowledged: [] } });
                toast("Approved");
                await refresh();
              }, "primary") : null,
              h("a", { class: `btn ${quick ? "" : "primary"}`, href: `/t/${t.id}/review`, text: quick ? "Review" : "Review and approve" }))));
        } else if (t.status === "completed") {
          out.push(h("p", { class: "done-line" }, icon("check"), "Approved and saved to the final folder. ",
            h("a", { href: `/t/${t.id}/review`, text: "View" })));
        }
      }
      if (t.status === "completed" && !(t.deliverables || []).length && !r.answer && !r.code && !r.reply) {
        out.push(h("p", { class: "done-line" }, icon("check"), "Done."));
      }
      if (["failed", "handed_back", "rejected", "cancelled"].includes(t.status)) {
        const messages = {
          failed: "Something went wrong and the task stopped.",
          handed_back: "I could not finish this on my own. The full record is in Details.",
          rejected: "This task was stopped.",
          cancelled: "This task was cancelled.",
        };
        out.push(h("div", { class: `notice${t.status === "failed" ? " bad" : ""}` }, h("p", { text: messages[t.status] }),
          t.error ? h("p", { class: "muted small", text: t.error }) : null));
      }
      if (t.status === "completed" && t.plan && t.plan.source === "model" && (t.deliverables || []).length) {
        out.push(h("div", { class: "row" }, button("Save this plan for reuse", async () => {
          await api("/templates/drafts", { method: "POST", json: { task_id: t.id } });
          toast("Saved. An administrator can approve it under Security.");
        }, "ghost small")));
      }
      return out;
    }

    // Details panel: the technical record behind the conversation.
    async function openDetails(t) {
      const items = [["activity", "Activity"], ["evidence", "Evidence"], ["model", "Model choice"], ["checks", "Checks"]];
      let tab = "activity";
      let dir = 0;
      const holder = h("div", { class: "details-body tab-stage" });
      const seg = tabs(items, tab, (key, d) => { tab = key; dir = d; render(); });
      const put = (...content) => swapPanel(holder, content, dir);
      const render = async () => {
        const task = findTask(t.id) || t;
        if (tab === "activity") {
          put(h("ol", { class: "log" }, task.trace.map((r) => h("li", { class: r.ok ? "" : "bad" },
            h("span", { class: "log-k", text: [r.kind, r.step_id].filter(Boolean).join(" · ") }),
            h("span", { class: "log-s", text: r.summary }),
            r.latency_s ? h("span", { class: "log-t", text: `${r.latency_s.toFixed(2)}s` }) : h("span", {})))));
        } else if (tab === "evidence") {
          view.ledger = await api(`/tasks/${task.id}/ledger`).catch(() => []);
          put(...view.ledger.map((r) => h("button", { class: "evidence", type: "button", onclick: () => showRecord(r.id) },
            h("span", { class: "row tight" }, h("span", { class: "strong", text: KIND_NAMES[r.kind] || r.kind }), labelTag(r.label),
              r.confidence && r.confidence !== "high" ? h("span", { class: "chip warn", text: r.confidence }) : null, h("span", { class: "grow" }), h("span", { class: "mono tiny muted", text: r.id })),
            h("span", { class: "evidence-sum", text: r.summary }))));
          if (!view.ledger.length) put(h("p", { class: "muted", text: "No evidence yet." }));
        } else if (tab === "model") {
          const r = task.route;
          if (!r) { put(h("p", { class: "muted", text: "Not decided yet." })); return; }
          put(
            h("p", {}, "Chosen: ", h("strong", { text: r.chosen })),
            h("p", { class: "muted small", text: `The cheapest model scoring at least ${r.threshold.toFixed(2)} for this kind of task is chosen.` }),
            h("table", { class: "table" }, h("tbody", {}, r.candidates.map((c) => h("tr", { class: c.model === r.chosen ? "chosen" : "" },
              h("td", { class: "mono", text: c.model }),
              h("td", { text: c.ok ? (c.quality === null ? "no score" : `score ${c.quality.toFixed(2)}`) : `not suitable (${c.reason})` }),
              h("td", { class: "muted", text: c.ok && c.cost_s !== null ? `${Math.round(c.cost_s + (c.wait_s || 0))}s` : "" }))))),
            h("details", {}, h("summary", { text: "Decision log" }), h("pre", { class: "code", text: r.log_line })));
        } else {
          const data = await api(`/tasks/${task.id}/checks`).catch(() => ({ checks: [] }));
          if (!data.checks.length) { put(h("p", { class: "muted", text: "No checks for this task." })); return; }
          put(...data.checks.map((c) => h("div", { class: `check-row ${c.status}` },
            h("span", { class: "check-mark" }, icon(c.status === "pass" ? "check" : c.status === "mismatch" ? "alert" : "info")),
            h("div", {}, h("div", { class: "strong", text: c.description || c.rule }),
              h("div", { class: "small", text: `${c.left_value ?? ""}${c.right_value ? ` vs ${c.right_value}` : ""}` }),
              c.note ? h("div", { class: "muted small", text: c.note }) : null,
              c.extra && c.extra.crop ? h("img", { class: "crop", alt: "Scanned region", src: `/api/tasks/${task.id}/evidence/${c.extra.crop}` }) : null))));
        }
      };
      openPanel("Details", [
        h("p", { class: "muted small", text: `${t.id} · ${t.user} · ${fmtTime(t.created_at)}${t.model ? ` · ${t.model}` : ""}` }),
        seg, holder]);
      render();
    }

    async function sendFollowup(text, attachments) {
      const json = attachments === undefined ? { text } : { text, attachments };
      json.meta = localMode.meta({});
      await api(`/tasks/${id}/followup`, { method: "POST", json });
      view.stick = true;
      await refresh();
    }

    follow.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = box.value.trim();
      if (!text || box.disabled) return;
      guarded(e.submitter, async () => {
        box.value = "";
        box.dispatchEvent(new Event("input"));
        await sendFollowup(text);
      });
    });
    box.addEventListener("input", () => { $("button[type=submit]", follow).disabled = box.disabled || !box.value.trim(); });
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); follow.requestSubmit(); }
    });

    await poll();
    if (location.hash === "#details" && view.root) openDetails(latest());
  }

  // -------------------------------------------------------------------------------------------
  // Review

  async function pageReview() {
    const id = document.body.dataset.task;
    let data;
    let current = 0;

    const load = async () => {
      try { data = await api(`/tasks/${id}/draft`); } catch (e) {
        fill($("#review-head"), h("a", { class: "back", href: `/t/${id}`, text: "Back" }), h("h1", { text: "Nothing to review" }), h("p", { class: "muted", text: e.message }));
        fill($("#doc-view"));
        return;
      }
      const t = data.task;
      fill($("#review-head"), h("a", { class: "back", href: `/t/${id}`, text: "Back to task" }), h("p", { class: "eyebrow", text: "Review" }), h("h1", { text: t.text }));
      const holder = $("#doc-tabs");
      holder.hidden = data.deliverables.length < 2;
      if (!holder.firstChild && data.deliverables.length > 1) {
        holder.append(tabs(data.deliverables.map((d, i) => [String(i), d.name]), String(current), (key, dir) => { current = Number(key); render(dir); }));
      }
      render(0, true);
    };

    const decisions = () => new Map(data.figure_decisions.map((f) => [f.figure, f]));

    function figSpan(f, d, text) {
      const decided = decisions().get(`${d.file_id}:${f.id}`);
      const cls = `fig ${f.status}${decided ? " resolved" : ""}`;
      const title = f.status === "unsourced" ? (decided ? `Resolved (${decided.action})` : "No source found for this figure") : f.status === "derived" ? "Calculated" : "From a source document";
      return h("span", { class: cls, title, text, onclick: () => f.record_id && showRecord(f.record_id) });
    }

    function withFigures(runs, figs, d) {
      const queue = [...(figs || [])];
      const out = [];
      runs.forEach((r) => {
        if (r.sup) {
          const refs = (r.refs || []).filter(Boolean);
          out.push(h("sup", { text: r.text, title: refs.join(", "), onclick: () => refs[0] && showRecord(refs[0]) }));
          return;
        }
        let text = r.text;
        while (text) {
          let best = null;
          queue.forEach((f) => {
            const at = text.indexOf(f.raw);
            if (at >= 0 && (!best || at < best.at)) best = { at, f };
          });
          if (!best) { out.push(text); break; }
          out.push(text.slice(0, best.at), figSpan(best.f, d, best.f.raw));
          text = text.slice(best.at + best.f.raw.length);
          queue.splice(queue.indexOf(best.f), 1);
        }
      });
      return out;
    }

    function renderDocx(d) {
      const figs = {};
      (d.provenance.figures || []).forEach((f) => { (figs[f.location] = figs[f.location] || []).push(f); });
      const unverified = d.claims.filter((c) => c.status === "unverified").map((c) => c.sentence.replace(/\s*\[[^\]]+\]/g, "").slice(0, 36));
      const paper = h("article", { class: "paper" });
      let tableIndex = 0;
      d.preview.blocks.forEach((b) => {
        if (b.type === "marking") { paper.append(h("div", { class: "paper-marking", text: b.text })); return; }
        if (b.type === "table") {
          tableIndex += 1;
          const ti = tableIndex;
          paper.append(h("table", {}, h("tbody", {}, b.rows.map((row, ri) => h("tr", {}, row.map((cell, ci) =>
            h("td", {}, withFigures(cell, figs[`table ${ti} row ${ri + 1} col ${ci + 1}`], d))))))));
          return;
        }
        const text = b.runs.map((r) => r.text).join("");
        const content = withFigures(b.runs, figs[`paragraph ${b.index}`], d);
        const flagged = unverified.some((u) => u && text.includes(u));
        const inner = flagged ? h("span", { class: "unverified", title: "This sentence could not be matched to its source" }, content) : content;
        const style = b.style;
        if (style === "WB Marking") return;
        const edited = (data.summary.edits || []).find((e) => e.key === `${d.file_id}:${b.index}`);
        const mark = edited ? h("span", { class: `edit-mark${edited.accepted ? " ok" : ""}`,
          title: `Edited by ${edited.by}${edited.accepted ? ", accepted" : ", not accepted yet"}`, text: "edited" }) : null;
        const line = (tag, cls) => {
          const el = h(tag, cls ? { class: cls } : {}, inner, mark);
          if (canEdit(d)) {
            el.classList.add("editable");
            el.title = "Click to rewrite this paragraph";
            listen(el, "click", (ev) => {
              if (ev.target.closest(".fig, sup, .edit-mark")) return;
              editParagraph(el, d, b.index, b.runs.filter((r) => !r.sup).map((r) => r.text).join(""));
            });
          }
          return el;
        };
        if (style === "Title" || style === "Heading 1") paper.append(line("h1"));
        else if (style.startsWith("Heading")) paper.append(line("h2"));
        else if (style === "WB Meta") paper.append(line("p", "meta"));
        else if (style === "List Bullet") paper.append(line("p", "bullet"));
        else if (style === "WB Reference") paper.append(h("p", { class: "ref" }, inner));
        else paper.append(line("p"));
      });
      paper.append(h("div", { class: "paper-marking", text: d.marking }));
      return [h("div", { class: "legend" },
        h("span", {}, h("span", { class: "fig sourced", text: "12.5" }), " from a source"),
        h("span", {}, h("span", { class: "fig derived", text: "12.5" }), " calculated"),
        h("span", {}, h("span", { class: "fig unsourced", text: "12.5" }), " no source")), paper];
    }

    function renderSheet(d) {
      const holder = h("div", { class: "tab-stage", "data-keep": "" });
      const show = (i, dir) => {
        const s = d.preview.sheets[i];
        swapPanel(holder, h("div", { class: "sheet" }, h("table", {}, h("tbody", {}, s.rows.map((row, ri) => h("tr", {},
          h("td", { class: "rowhead", text: ri + 1 }),
          row.map((c) => {
            const formula = typeof c.v === "string" && c.v.startsWith("=");
            return h("td", { class: formula ? "formula" : c.comment ? "sourced" : "", title: c.comment || (formula ? "Formula" : "") }, c.v === null ? "" : String(c.v));
          })))))), dir);
      };
      const seg = tabs(d.preview.sheets.map((sh, i) => [String(i), sh.name]), "0", (key, dir) => show(Number(key), dir));
      show(0, 0);
      return [d.preview.sheets.length > 1 ? seg : null, holder, h("p", { class: "muted small", text: "Blue cells are formulas. Green cells note their source." })];
    }

    function renderOther(d) {
      const p = d.preview;
      if (p.svg !== undefined) {
        return h("div", { class: "sheet pad" }, h("img", { class: "svg-preview", alt: d.name, src: `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(p.svg)))}` }));
      }
      if (p.slides) {
        return [
          h("div", { class: "deck-head" }, h("h2", { text: `${plural(p.slides.length, "slide")}` }),
            h("span", { class: "muted small", text: `${d.name} · every slide carries the marking` })),
          h("div", { class: "slides" }, p.slides.map((s, i) => h("figure", { class: "slide", "data-key": `s-${i}` },
          h("div", { class: `slide-face${i === 0 ? " title-slide" : ""}` },
            h("p", { class: "slide-marking", text: d.marking }),
            h("h3", { class: "slide-title", text: s.title || `Slide ${i + 1}` }),
            s.subtitle ? h("p", { class: "slide-sub", text: s.subtitle }) : null,
            s.lead ? h("p", { class: "slide-lead", text: s.lead }) : null,
            h("ul", { class: "slide-bullets" }, (s.bullets || []).map((x) => h("li", { text: x }))),
            h("span", { class: "slide-no", text: i + 1 })),
          s.note ? h("figcaption", { class: "slide-note" }, h("span", { class: "note-tag", text: "Speaker note" }), s.note) : null))),
        ];
      }
      return h("pre", { class: "code tall", text: p.text || "This file cannot be previewed. Download it instead." });
    }

    const canEdit = (d) => d.status === "draft" && !d.final_file_id && d.preview.type === "docx"
      && (data.task.is_owner || data.can_approve) && data.task.status !== "completed";

    function editParagraph(el, d, index, text) {
      if (el.querySelector("textarea")) return;
      const area = h("textarea", { class: "input paper-edit", rows: Math.max(2, Math.ceil(text.length / 70)) });
      area.value = text;
      const original = [...el.childNodes];
      const save = button("Save", async (e) => {
        const next = area.value.trim();
        if (!next || next === text) { el.replaceChildren(...original); return; }
        await guarded(e.currentTarget, async () => {
          data = await api(`/tasks/${id}/draft/edit`, { method: "POST",
            json: { file_id: d.file_id, paragraph: index, text: next } });
          render(0);
        });
      }, "primary");
      const cancel = button("Cancel", () => el.replaceChildren(...original), "ghost");
      el.replaceChildren(area, h("div", { class: "row paper-edit-row" }, save, cancel,
        h("span", { class: "muted small", text: "Reference markers stay where they are. Figures are checked again." })));
      area.focus();
      area.setSelectionRange(area.value.length, area.value.length);
    }

    let shownKey = "";
    function render(dir = 0) {
      const d = data.deliverables[current];
      const type = d.preview.type;
      const content = [].concat(type === "docx" ? renderDocx(d) : type === "xlsx" ? renderSheet(d) : renderOther(d));
      const key = `${current}|${data.task.revision_no}`;
      if (shownKey.split("|")[0] !== String(current)) swapPanel($("#doc-view"), content, dir);
      else if (shownKey !== key) fill($("#doc-view"), content);
      shownKey = key;
      renderSide();
    }

    function renderSide() {
      const side = $("#review-side");
      const s = data.summary;
      const t = data.task;
      const gate = data.pending_gate && data.pending_gate.kind === "deliverable";
      const d = data.deliverables[current];
      const parts = [];
      const blockers = data.blockers || [];

      if (t.status === "completed") {
        parts.push(h("div", { class: "status-card good" }, icon("check"), h("div", {}, h("strong", { text: "Approved" }), h("div", { class: "small", text: "Saved to the final folder." }))));
      } else if (gate) {
        const open = s.unacknowledged.length + s.orphans.length;
        parts.push(blockers.length
          ? h("div", { class: "status-card warn" }, icon("alert"), h("div", {}, h("strong", { text: `${plural(open || blockers.length, "item")} to check before approving` })))
          : h("div", { class: "status-card good" }, icon("check"), h("div", {}, h("strong", { text: "Ready to approve" }))));
      }

      const issues = data.checks.filter((c) => c.status !== "pass");
      if (issues.length) {
        parts.push(h("section", { class: "side-block" }, h("h3", { text: "Issues found" }), issues.map((c) => {
          const acked = data.acknowledged.includes(c.record_id);
          const box = h("input", {
            type: "checkbox", checked: acked, disabled: acked || !gate || !data.can_approve || c.status !== "mismatch",
            onchange: (e) => guarded(e.currentTarget, async () => { await api(`/tasks/${id}/checks/${c.record_id}/acknowledge`, { method: "POST" }); await load(); }),
          });
          return h("div", { class: `issue ${c.status}`, "data-key": `issue-${c.record_id}` },
            h("div", { class: "strong", text: c.description || c.rule }),
            h("div", { class: "small", text: `${c.left_value ?? ""}${c.right_value ? ` vs ${c.right_value}` : ""}` }),
            c.note ? h("div", { class: "muted small", text: c.note }) : null,
            c.status === "mismatch" ? h("label", { class: "tick" }, box, acked ? "Reviewed" : "Mark as reviewed") : null);
        })));
      }

      const edits = (s.edits || []).filter((e) => e.key.startsWith(`${d.file_id}:`));
      if (edits.length) {
        parts.push(h("section", { class: "side-block" }, h("h3", { text: "Edited by hand" }),
          h("p", { class: "muted small", text: "The checks cannot vouch for text a person rewrote. An approver accepts each edit." }),
          edits.map((e) => h("div", { class: `issue${e.accepted ? "" : " mismatch"}`, "data-key": `edit-${e.key}` },
            h("div", { class: "row tight" }, h("span", { class: "strong", text: `Paragraph ${e.key.split(":")[1]}` }),
              h("span", { class: "muted small", text: `by ${e.by}` })),
            diffView(e.before, e.after),
            e.accepted
              ? h("span", { class: "chip ok", text: "Accepted" })
              : button("Accept this edit", async (ev) => {
                await guarded(ev.currentTarget, async () => {
                  data = await api(`/tasks/${id}/draft/edits/${encodeURIComponent(e.key)}/accept`, { method: "POST" });
                  render(0);
                });
              }, data.can_approve ? "" : "ghost")))));
      }

      const orphans = (d.provenance.figures || []).filter((f) => f.status === "unsourced");
      if (orphans.length) {
        const dec = decisions();
        parts.push(h("section", { class: "side-block" }, h("h3", { text: "Figures without a source" }), orphans.map((f) => {
          const key = `${d.file_id}:${f.id}`;
          const done = dec.get(key);
          const input = h("input", { class: "input fig-input", placeholder: "Record id or corrected value" });
          const act = (action) => async (e) => {
            const value = e.currentTarget.closest(".issue").querySelector(".fig-input").value.trim();
            const body = { action, note: null };
            if (action === "link") body.record_id = value;
            if (action === "correct") body.value = value;
            await api(`/tasks/${id}/draft/figures/${encodeURIComponent(key)}`, { method: "POST", json: body });
            await load();
          };
          return h("div", { class: "issue", "data-key": `fig-${key}` },
            h("div", { class: "row tight" }, h("strong", { text: f.raw }), done ? h("span", { class: "chip", text: done.action }) : null),
            h("div", { class: "muted small", text: f.context.slice(0, 120) }),
            !done && gate ? [input, h("div", { class: "row tight" }, button("Link", act("link"), "small"), button("Correct", act("correct"), "small"), button("Keep", act("confirm"), "small ghost"))] : null);
        })));
      }

      if (d.claims.length) {
        const ok = d.claims.filter((c) => c.status === "verified").length;
        const bad = d.claims.filter((c) => c.status !== "verified" || c.currency_warning);
        parts.push(h("section", { class: "side-block" }, h("h3", { text: "Citations" }),
          h("p", { class: "small", text: `${ok} of ${d.claims.length} checked against their sources.` }),
          bad.map((c) => h("div", { class: "issue" }, h("div", { class: "small", text: c.sentence.replace(/\s*\[[^\]]+\]/g, "") }),
            h("div", { class: "muted small", text: [...c.reasons, c.currency_warning].filter(Boolean).join("; ") })))));
      }

      if (gate) {
        if (data.can_approve) {
          const note = h("textarea", { class: "input decision-note", rows: 2, placeholder: "Note for changes (optional when approving)" });
          const noteText = () => $("#review-side .decision-note").value;
          parts.push(h("section", { class: "side-block", "data-key": "decision" }, note,
            h("div", { class: "row" },
              h("button", { class: "btn primary", type: "button", disabled: blockers.length > 0, title: blockers.join("; "), onclick: (e) => guarded(e.currentTarget, async () => {
                await api(`/tasks/${id}/draft/decision`, { method: "POST", json: { decision: "approve", acknowledged: data.acknowledged, note: noteText() || null } });
                toast("Approved");
                setTimeout(load, 1200);
              }) }, "Approve"),
              button("Request changes", async () => {
                if (!noteText().trim()) throw new Error("Write a note describing the changes first");
                await api(`/tasks/${id}/draft/decision`, { method: "POST", json: { decision: "reject", note: noteText() } });
                location.href = `/t/${id}`;
              }, "ghost"))));
        } else {
          parts.push(h("p", { class: "muted small", text: "Only an approver can approve this draft." }));
        }
      }

      if (t.status === "completed") {
        const finals = data.deliverables.filter((x) => x.final_file_id);
        const others = shell.workspaces.filter((w) => w.id !== t.workspace);
        parts.push(h("section", { class: "side-block" }, h("h3", { text: "Files" }), finals.map((x) => h("div", { class: "issue" },
          h("div", { class: "row tight" }, h("a", { href: fileUrl(x.final_file_id), text: x.name }), labelTag(x.label)),
          h("details", {}, h("summary", { text: "More" }), shareControl(x, others), downgradeControl(x))))));
      }
      fill(side, ...parts);
    }

    function shareControl(x, others) {
      if (!others.length) return null;
      return h("select", {
        class: "select",
        onchange: (e) => {
          const select = e.currentTarget;
          if (!select.value) return;
          guarded(select, async () => {
            await api(`/files/${x.final_file_id}/share`, { method: "POST", json: { workspace: select.value } });
            toast("Shared");
          }).finally(() => { select.value = ""; });
        },
      }, h("option", { value: "", text: "Share to workspace" }), others.map((w) => h("option", { value: w.id, text: w.title })));
    }

    function downgradeControl(x) {
      const level = h("select", { class: "select dg-level" }, ["Unclassified", "Restricted", "Confidential"].map((l) => h("option", { text: l })));
      const reason = h("input", { class: "input dg-reason", placeholder: "Reason for a lower classification" });
      return h("div", { class: "stack" }, level, reason, button("Request lower classification", async (e) => {
        const box = e.currentTarget.closest(".stack");
        await api(`/files/${x.final_file_id}/downgrade`, { method: "POST", json: { level: box.querySelector(".dg-level").value, reason: box.querySelector(".dg-reason").value } });
        toast("Requested. A second authorised person must approve it.");
      }, "small"));
    }

    await load();
    setInterval(async () => {
      if (!data) return;
      const fresh = await api(`/tasks/${id}`).catch(() => null);
      if (fresh && fresh.revision_no !== data.task.revision_no) load();
    }, 3000);
  }

  // -------------------------------------------------------------------------------------------
  // Models

  const SERVES = { document: "Documents", vision: "Images", general: "General", code: "Code", reasoning: "Reasoning", agentic: "Multi-step work" };

  async function pageModels() {
    const render = async () => {
      const m = await api("/models");
      const admin = shell.me.roles.includes("admin");
      fill($("#model-list"), ...m.models.map((x) => {
        let state = x.state === "awake" ? ["Ready", "ok"] : x.state === "asleep" ? ["Sleeping, wakes when needed", "idle"] : ["Not loaded", "off"];
        if (x.status === "shadow") state = ["Being evaluated", "idle"];
        if (x.status === "retired") state = ["Retired", "off"];
        const routes = Object.entries(x.quality);
        return h("details", { class: "model", "data-key": x.name },
          h("summary", {},
            h("span", { class: `state-dot ${state[1]}` }),
            h("span", { class: "model-main" }, h("span", { class: "strong", text: x.name }), h("span", { class: "muted small", text: x.serves.map((s) => SERVES[s] || s).join(", ") })),
            h("span", { class: "muted small", text: state[0] }),
            icon("chevron")),
          h("div", { class: "model-body" },
            routes.length ? h("table", { class: "table" }, h("tbody", {}, routes.map(([r, q]) => h("tr", {}, h("td", { text: SERVES[r] || r }), h("td", {}, h("span", { class: "bar" }, h("i", { style: `width:${q * 100}%` }))), h("td", { class: "mono", text: q.toFixed(2) })))))
              : h("p", { class: "muted small", text: "No scores yet." }),
            h("p", { class: "muted small", text: `${x.provenance.developer} · ${x.provenance.licence} · ${x.max_context.toLocaleString()} tokens · ${x.latency.step_s}s per step · used in ${plural(m.tasks_served[x.name] || 0, "task")}` }),
            x.hosted_as ? h("p", { class: "muted small", text: `Answered by ${x.hosted_as}` }) : null,
            x.serve_argv.length ? h("details", {}, h("summary", { text: "Start command" }), h("pre", { class: "code", text: x.serve_argv.join(" ") })) : null,
            admin && x.status === "shadow" ? h("div", { class: "row" },
              button("Evaluate", async () => { await api(`/models/${x.name}/shadow-eval`, { method: "POST" }); toast("Evaluation finished"); render(); }),
              button("Make active", async () => { await api(`/models/${x.name}/promote`, { method: "POST", json: { confirm: true } }); toast(`${x.name} is active`); render(); }, "primary")) : null));
      }));
      const allowance = m.allowance || [];
      fill($("#model-allowance"), ...(allowance.length ? [
        h("h2", { class: "section-title", text: "Allowance left" }),
        h("p", { class: "muted small", text: "What each hosted model reported when it last answered. Calls are spaced out so the allowance is not spent in a burst." }),
        h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", {}, h("tr", {}, h("th", { text: "Model" }), h("th", { text: "Requests left" }),
            h("th", { text: "Tokens left" }), h("th", { text: "Resets in" }), h("th", { text: "Calls" }))),
          h("tbody", {}, allowance.map((u) => h("tr", { "data-key": u.model },
            h("td", { class: "mono", text: u.model }),
            h("td", { text: u.remaining_requests ? `${u.remaining_requests} of ${u.limit_requests || "?"}` : "not reported" }),
            h("td", { text: u.remaining_tokens ? `${Number(u.remaining_tokens).toLocaleString()} of ${Number(u.limit_tokens || 0).toLocaleString()}` : "not reported" }),
            h("td", { text: u.reset_requests || u.reset_tokens || "-" }),
            h("td", { text: u.rate_limited ? `${u.calls} (${u.rate_limited} refused)` : String(u.calls) }))))))] : []));
      const pool = m.pool;
      fill($("#models-tech"), 
        h("p", { class: "small", text: `Registry ${m.registry_version} · hardware profile ${m.profile} · backend ${m.backend}` }),
        h("p", { class: "small", text: `GPU sharing: ${pool.tide} models loaded, switched ${plural(pool.tide_count, "time")}, ${pool.swap_queue} waiting.` }),
        h("p", { class: "small", text: `Minimum score per difficulty: ${Object.entries(m.routing.thresholds).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(", ")}.` }),
        m.cache.length ? h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", { text: "Cache partition" }), h("th", { text: "Requests" }), h("th", { text: "Reused" }))),
          h("tbody", {}, m.cache.map((c) => h("tr", {}, h("td", {}, labelTag(c.label || "Unclassified", c.label)), h("td", { text: c.requests }), h("td", { text: `${Math.round((c.hit_rate || 0) * 100)}%` }))))) : null,
        m.proposed_diff ? h("details", {}, h("summary", { text: "Proposed score changes" }), h("pre", { class: "code", text: m.proposed_diff })) : null);
    };
    await render();
    setInterval(render, 8000);
  }

  // -------------------------------------------------------------------------------------------
  // Security

  async function pageSecurity() {
    const me = shell.me;
    let testResult = null;

    const status = async () => {
      const snap = await api("/egress");
      const box = $("#net-status");
      if (snap.status !== "ok") {
        fill(box, h("div", { class: "status-card warn" }, icon("alert"), h("div", {}, h("strong", { text: "The network monitor is not running" }))));
        return;
      }
      const leaked = snap.external_connections || snap.breach;
      const blocked = (snap.blocked_connect_host || 0) + (snap.blocked_connect_sandbox || 0);
      const health = await api("/health").catch(() => ({}));
      const hosted = health.hosted ? new URL(health.hosted).host : null;
      const headline = leaked ? "Outbound connections were detected"
        : hosted ? `Model calls go to ${hosted}` : "Nothing has left this server";
      const detail = hosted && !leaked
        ? `That host is on the allowlist and carries the requests and the document text they contain. Everything else, including your files and the audit log, stays here. ${plural(blocked, "attempt")} to reach anywhere else blocked since ${fmtTime(snap.since)}.`
        : `${snap.external_connections} outbound · ${plural(blocked, "attempt")} blocked · counting since ${fmtTime(snap.since)}`;
      fill(box, ...[
        h("div", { class: `status-card ${leaked ? "bad" : hosted ? "warn" : "good"}` }, icon(leaked || hosted ? "alert" : "check"),
          h("div", {}, h("strong", { text: headline }), h("div", { class: "small", text: detail })),
          h("span", { class: "grow" }),
          button("Run a test", async () => { testResult = await api("/egress/test", { method: "POST" }); await status(); })),
        testResult ? h("ul", { class: "test-list" }, testResult.checks.map((c) => h("li", {},
          h("span", { class: `check-mark ${c.pass ? "pass" : "mismatch"}` }, icon(c.pass ? "check" : "cross")),
          h("div", {}, h("span", { class: "strong", text: c.name.replace(/_/g, " ").replace("host", "server").replace(/\bip\b/, "IP").replace("dns", "DNS") }),
            c.simulated ? h("span", { class: "muted small", text: " (simulated on this development machine)" }) : null,
            h("div", { class: "muted small", text: c.observed })))) ) : null].filter(Boolean));
      const ev = await api("/egress/events?limit=50");
      fill($("#events"), ev.events.length ? h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", { text: "When" }), h("th", { text: "From" }), h("th", { text: "Destination" }))),
        h("tbody", {}, ev.events.slice().reverse().map((e) => h("tr", {}, h("td", { text: fmtTime(e.ts) }), h("td", { text: e.origin === "sandbox" ? "Script sandbox" : "Server" }), h("td", { class: "mono", text: e.addr })))))
        : h("p", { class: "muted", text: "No attempts so far." }));
    };

    const audit = async () => {
      const v = await api("/audit/verify");
      const parts = [h("p", { class: "row tight" }, icon(v.ok ? "check" : "alert"), v.ok ? `Intact, ${plural(v.entries, "entry", "entries")}` : `Tampering detected at entry ${v.broken_at}`)];
      try {
        const tail = await api("/audit/tail?n=30");
        parts.push(h("table", { class: "table" }, h("tbody", {}, tail.reverse().map((e) => h("tr", {},
          h("td", { class: "nowrap muted", text: fmtTime(e.ts) }),
          h("td", { text: e.event.type.replace(/[._]/g, " ") }),
          h("td", { class: "muted", text: e.event.by || e.event.user || e.event.task || "" }))))));
      } catch (err) {
        if (err.status !== 403) throw err;
      }
      fill($("#audit"), ...parts);
    };
    $("#verify-audit").addEventListener("click", (e) => guarded(e.currentTarget, async () => { await audit(); toast("Audit log verified"); }));

    const requests = async () => {
      const [downs, tpl] = await Promise.all([api("/downgrades"), api("/templates")]);
      const canDown = me.roles.some((r) => ["security_officer", "document_owner"].includes(r));
      const canTpl = me.roles.some((r) => ["admin", "document_owner"].includes(r));
      const rows = [];
      downs.filter((d) => d.status === "pending").forEach((d) => rows.push(h("div", { class: "request" },
        h("div", {}, h("div", { class: "strong", text: `Lower ${d.file.name}` }),
          h("div", { class: "row tight small" }, labelTag(d.before), "to", labelTag(d.after), h("span", { class: "muted", text: `asked by ${d.requester}: ${d.reason}` }))),
        canDown && d.requester !== me.id ? h("div", { class: "row tight" },
          button("Approve", async () => { await api(`/downgrades/${d.id}/decision`, { method: "POST", json: { approve: true } }); requests(); }, "primary small"),
          button("Reject", async () => { await api(`/downgrades/${d.id}/decision`, { method: "POST", json: { approve: false } }); requests(); }, "ghost small"))
          : h("span", { class: "muted small", text: d.requester === me.id ? "Needs another person" : "Needs a security officer" }))));
      tpl.drafts.forEach((name) => rows.push(h("div", { class: "request" },
        h("div", {}, h("div", { class: "strong", text: `New saved plan: ${name.replace(/\.ya?ml$/, "").replace(/_/g, " ")}` })),
        canTpl ? button("Approve", async () => { await api(`/templates/drafts/${encodeURIComponent(name)}/approve`, { method: "POST" }); requests(); }, "primary small")
          : h("span", { class: "muted small", text: "Needs an administrator" }))));
      fill($("#requests"), ...(rows.length ? rows : [h("p", { class: "muted", text: "Nothing is waiting." })]));
    };

    $("#impact-form").addEventListener("submit", (e) => {
      e.preventDefault();
      guarded(null, async () => {
        const r = await api(`/kb/impact/${encodeURIComponent($("#impact-doc").value)}/${encodeURIComponent($("#impact-rev").value)}`);
        const changed = r.clauses.filter((c) => c.status !== "unchanged");
        fill($("#impact"), 
          h("p", { class: "strong", text: `${r.doc_number}, revision ${r.previous} to ${r.revision}` }),
          h("p", { class: "small", text: "Changed clauses" }),
          h("ul", { class: "plain" }, changed.map((c) => h("li", { text: `${c.clause} ${c.heading}: ${c.status}${c.limits.length ? ` (${c.limits.map((l) => `${l.old} to ${l.new} ${l.unit || ""}`).join(", ")})` : ""}` }))),
          h("p", { class: "small", text: "Earlier notes that cite them" }),
          h("ul", { class: "plain" }, r.notes.length ? r.notes.map((n) => h("li", { text: `${n.note}: ${n.title} (clause ${n.clause})` })) : h("li", { class: "muted", text: "None" })),
          h("p", { class: "small", text: `Equipment affected: ${r.equipment.join(", ") || "none"}` }));
      });
    });

    await Promise.all([status(), audit(), requests()]);
    setInterval(status, 8000);
  }

  // -------------------------------------------------------------------------------------------

  const EXT_GROUP = { docx: "docs", pdf: "docs", md: "docs", xlsx: "sheets", csv: "sheets", pptx: "slides", py: "code", json: "code", svg: "charts", png: "charts" };
  const extOf = (name) => String(name).split(".").pop().toLowerCase();

  function dayLabel(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "Earlier";
    const today = new Date();
    const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((start(today) - start(d)) / 86400000);
    if (diff === 0) return "Today";
    if (diff === 1) return "Yesterday";
    if (diff < 7) return d.toLocaleDateString([], { weekday: "long" });
    return d.toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" });
  }
  const timeOf = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };

  function grouped(items, dateOf, render) {
    const out = [];
    let current = null;
    items.forEach((it) => {
      const label = dayLabel(dateOf(it));
      if (label !== current) {
        current = label;
        out.push(h("h2", { class: "day", "data-key": `day-${label}`, text: label }));
      }
      out.push(render(it));
    });
    return out;
  }

  function decisionText(d) {
    if (d.kind === "plan") {
      if (d.by === "system") return "Started right away (read-only question)";
      return { approved: "Plan approved", edited: "Plan edited", rejected: "Plan cancelled" }[d.status] || "Plan decided";
    }
    if (d.kind === "action") {
      const what = d.title || "Step";
      if (d.note === "approved with the plan") return `${what}: allowed with the plan`;
      return d.status === "approved" ? `${what}: allowed` : `${what}: not allowed`;
    }
    if (d.kind === "deliverable") return d.status === "approved" ? "Final approval given" : "Changes requested";
    if (d.kind === "template_choice") return `Approach chosen: ${(d.note || "").replace(/_/g, " ")}`;
    return `${d.kind} ${d.status}`;
  }

  function emptyState(iconName, title, text) {
    return h("div", { class: "empty-state", "data-key": `empty-${title}` }, h("span", { class: "empty-icon" }, icon(iconName)),
      h("strong", { text: title }), h("span", { class: "muted", text }));
  }

  async function pageLibrary() {
    const params = new URLSearchParams(location.search);
    let tab = ["documents", "approvals", "waiting"].includes(params.get("tab")) ? params.get("tab") : "documents";
    let kind = "all";
    let query = "";
    let data = { tasks: [] };
    let downs = [];
    const body = $("#lib-body");
    const seg = tabs([["documents", "Documents"], ["approvals", "Approvals"], ["waiting", "Waiting on you"]], tab, (key, dir) => {
      tab = key;
      history.replaceState(null, "", key === "documents" ? "/library" : `/library?tab=${key}`);
      render(dir);
    });
    fill($("#lib-tabs"), seg);
    const search = $("#lib-search");
    search.addEventListener("input", () => { query = search.value.trim().toLowerCase(); render(null); });

    const matches = (...texts) => !query || texts.some((t) => String(t || "").toLowerCase().includes(query));
    const taskLink = (t) => h("a", { class: "task-link", href: `/t/${t.id}`, text: t.text });

    function fileTile(t, f) {
      const ext = extOf(f.name);
      return h("div", { class: "tile", "data-key": f.id },
        h("button", { class: "tile-open", type: "button", title: `Open ${f.name}`, onclick: () => showFile(f) },
          h("span", { class: `file-ext ext-${ext}`, text: ext }),
          h("span", { class: "tile-main" }, h("span", { class: "tile-name", text: f.name }),
            h("span", { class: "tile-meta" }, labelTag(f.label, f.label_display), h("span", { text: bytes(f.size) }))),
          h("span", { class: `chip ${f.final ? "ok" : ""}`, text: f.final ? "Final" : "Draft" })),
        h("a", { class: "file-dl", href: fileUrl(f.id), download: "", title: `Download ${f.name}`,
                 onclick: (e) => e.stopPropagation() }, icon("download")));
    }

    function documents() {
      const rows = data.tasks
        .map((t) => ({ ...t, shown: t.files.filter((f) => (kind === "all" || EXT_GROUP[extOf(f.name)] === kind) && matches(f.name, t.text)) }))
        .filter((t) => t.shown.length);
      if (!rows.length) {
        return emptyState("folder", query || kind !== "all" ? "No matching documents" : "No documents yet",
          query || kind !== "all" ? "Try another search or filter." : "Files the workbench creates will be collected here.");
      }
      return grouped(rows, (t) => t.updated_at, (t) => h("article", { class: "doc-group", "data-key": `doc-${t.id}` },
        h("header", { class: "doc-head" },
          h("div", { class: "doc-title" }, taskLink(t),
            h("span", { class: "muted small", text: `${t.workspace_title} · ${timeOf(t.updated_at)}` })),
          h("span", { class: `status-pill ${statusTone(t)}` }, statusLabel(t))),
        h("div", { class: "tiles" }, t.shown.map((f) => fileTile(t, f))),
        t.status === "awaiting_deliverable" || t.status === "completed"
          ? h("footer", { class: "doc-foot" }, h("a", { class: "link-btn", href: `/t/${t.id}/review` }, t.status === "completed" ? "View approved draft" : "Review and approve", icon("chevron")))
          : null));
    }

    function approvals() {
      const rows = [];
      data.tasks.forEach((t) => t.decisions.forEach((d) => rows.push({ ...d, task: t, text: decisionText(d) })));
      downs.forEach((d) => rows.push({
        id: d.id, kind: "downgrade", status: d.status === "pending" ? "pending" : d.status, by: d.approver || d.requester,
        at: d.decided_at || d.requested_at || d.created_at, note: d.reason, task: null, file: d.file,
        text: d.status === "pending" ? `Lower classification requested for ${d.file.name}`
          : `Lower classification ${d.status} for ${d.file.name}`,
      }));
      const shown = rows.filter((r) => r.at && matches(r.text, r.by, r.note, r.task && r.task.text))
        .sort((a, b) => String(b.at).localeCompare(String(a.at)));
      if (!shown.length) return emptyState("shield", query ? "No matching decisions" : "No decisions yet", query ? "Try another search." : "Every approval and rejection will be listed here.");
      return h("div", { class: "timeline" }, grouped(shown, (r) => r.at, (r) => {
        const tone = r.status === "rejected" ? "bad" : r.status === "pending" ? "warn" : "good";
        return h("div", { class: `event ${tone}`, "data-key": `ev-${r.id}` },
          h("span", { class: "event-icon" }, icon(tone === "bad" ? "cross" : tone === "warn" ? "clock" : "check")),
          h("div", { class: "event-main" },
            h("div", { class: "event-title", text: r.text }),
            h("div", { class: "event-meta" },
              r.task ? taskLink(r.task) : h("span", { text: r.file ? r.file.name : "" }),
              h("span", { class: "muted", text: `${r.by === "system" ? "Automatic" : r.by || ""} · ${timeOf(r.at)}` })),
            r.note && r.note !== "approved with the plan" && r.by !== "system" ? h("div", { class: "event-note", text: r.note }) : null));
      }));
    }

    function waiting() {
      const rows = data.tasks.filter((t) => t.waiting_for && matches(t.text));
      const pendingDowns = downs.filter((d) => d.status === "pending" && matches(d.file.name));
      if (!rows.length && !pendingDowns.length) return emptyState("check", "You're all caught up", "Nothing is waiting for a decision.");
      const what = { plan: "Approve the plan", action: "Allow a step", deliverable: "Review and approve the result", template_choice: "Choose an approach" };
      return h("div", { class: "cards" },
        rows.map((t) => h("a", { class: "wait-card", href: t.waiting_for === "deliverable" ? `/t/${t.id}/review` : `/t/${t.id}`, "data-key": `w-${t.id}` },
          h("span", { class: "wait-icon" }, icon(t.waiting_for === "deliverable" ? "note" : "clock")),
          h("span", { class: "wait-main" }, h("span", { class: "wait-title", text: t.text }),
            h("span", { class: "muted small", text: `${what[t.waiting_for] || "Needs a decision"} · ${t.workspace_title} · ${dayLabel(t.updated_at)}` })),
          h("span", { class: "wait-go" }, icon("chevron")))),
        pendingDowns.map((d) => h("a", { class: "wait-card", href: "/security", "data-key": `wd-${d.id}` },
          h("span", { class: "wait-icon" }, icon("shield")),
          h("span", { class: "wait-main" }, h("span", { class: "wait-title", text: `Lower classification of ${d.file.name}` }),
            h("span", { class: "muted small", text: `Asked by ${d.requester}: ${d.reason}` })),
          h("span", { class: "wait-go" }, icon("chevron")))));
    }

    function filters() {
      if (tab !== "documents") return [];
      const counts = {};
      data.tasks.forEach((t) => t.files.forEach((f) => { const g = EXT_GROUP[extOf(f.name)] || "other"; counts[g] = (counts[g] || 0) + 1; }));
      const all = data.tasks.reduce((n, t) => n + t.files.length, 0);
      const opts = [["all", "All", all], ["docs", "Reports", counts.docs], ["sheets", "Spreadsheets", counts.sheets],
        ["slides", "Slides", counts.slides], ["code", "Scripts and data", counts.code], ["charts", "Charts", counts.charts]];
      return opts.filter(([k, , n]) => k === "all" || n).map(([k, label, n]) => h("button", {
        type: "button", class: `filter${kind === k ? " active" : ""}`, "data-key": `f-${k}`,
        onclick: () => { kind = k; render(null); },
      }, label, h("span", { class: "filter-n", text: n || 0 })));
    }

    function render(dir) {
      seg.setCount("documents", data.tasks.reduce((n, t) => n + t.files.length, 0));
      seg.setCount("waiting", data.tasks.filter((t) => t.waiting_for).length + downs.filter((d) => d.status === "pending").length);
      smoothHeight($("#lib-filters"), () => fill($("#lib-filters"), filters()));
      const content = tab === "documents" ? documents() : tab === "approvals" ? approvals() : waiting();
      if (dir === undefined || dir === null) smoothHeight(body, () => fill(body, content));
      else swapPanel(body, content, dir);
    }

    const load = async () => {
      const [lib, dg] = await Promise.all([api("/library"), api("/downgrades").catch(() => [])]);
      data = lib;
      downs = dg;
    };
    await load();
    render(0);
    setInterval(async () => { await load().catch(() => {}); render(null); }, 5000);
  }

  // -------------------------------------------------------------------------------------------
  // Plant: the equipment map and the drawings behind it

  const GRAPH_KINDS = [["tag", "Equipment"], ["document", "Documents"], ["clause", "Clauses"],
    ["inspection", "Inspections"], ["vendor", "Vendors"], ["po", "Orders"], ["class", "Classes"]];
  // Orders are one per tag and add little to the picture; they are one click away in the legend.
  const GRAPH_HIDDEN_BY_DEFAULT = ["po"];
  const EDGE_WORDS = {
    is_a: "is a", shown_on: "shown on", supplied_by: "supplied by", ordered_on: "ordered on",
    applies_to: "applies to", mentions: "mentions", inspected: "inspected", governs: "governs",
    governed_by: "governed by", has_clause: "has clause", concerns: "concerns", revision_of: "revision of",
  };
  const kindTitle = (kind) => (GRAPH_KINDS.find(([k]) => k === kind) || [kind, kind])[1];
  const nodeLabel = (n) => String(n.kind === "tag" ? n.key : n.name || n.key || n.id);

  function graphColours() {
    const css = getComputedStyle(document.documentElement);
    const pick = (name, fallback) => (css.getPropertyValue(name) || fallback).trim();
    return {
      tag: pick("--g-tag", "#7c6cf0"), document: pick("--g-document", "#3d8bfd"),
      clause: pick("--g-clause", "#c98a17"), inspection: pick("--g-inspection", "#2f9e5f"),
      vendor: pick("--g-vendor", "#8a8f98"), po: pick("--g-po", "#8a8f98"), class: pick("--g-class", "#8a8f98"),
      line: pick("--g-edge", "#c9ccd2"), text: pick("--text", "#111"), muted: pick("--muted", "#888"),
      surface: pick("--surface", "#fff"), accent: pick("--accent", "#d9772b"),
      // Canvas cannot resolve var(), so the page's resolved font stack is read once here.
      font: getComputedStyle(document.body).fontFamily || "system-ui, sans-serif",
    };
  }

  // A stable pseudo-random number per id, so the same plant always lays out the same way.
  function idNoise(id, salt = 0) {
    let x = 2166136261 ^ salt;
    for (let i = 0; i < id.length; i += 1) { x ^= id.charCodeAt(i); x = Math.imul(x, 16777619); }
    return ((x >>> 0) % 10000) / 10000;
  }

  // Settles a force layout before anything is painted: nodes repel and never overlap, every edge
  // pulls its ends together, and a weak pull keeps the whole plant centred on the origin.
  function settle(nodes, links, iterations) {
    for (let it = 0; it < iterations; it += 1) {
      const heat = 1 - it / iterations;
      for (let i = 0; i < nodes.length; i += 1) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j += 1) {
          const b = nodes[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 0.01) { dx = idNoise(b.id, it) - 0.5; dy = idNoise(a.id, it) - 0.5; d2 = 0.5; }
          const d = Math.sqrt(d2);
          let force = 2200 / d2;
          const room = a.r + b.r + 18;
          if (d < room) force += (room - d) * 0.6;
          const fx = (dx / d) * force;
          const fy = (dy / d) * force;
          a.vx -= fx; a.vy -= fy;
          b.vx += fx; b.vy += fy;
        }
      }
      links.forEach((l) => {
        const a = nodes[l.a];
        const b = nodes[l.b];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d = Math.max(1, Math.hypot(dx, dy));
        const rest = 46 + a.r + b.r + (a.kind === "tag" && b.kind === "tag" ? 30 : 0);
        const pull = (d - rest) * 0.05;
        const fx = (dx / d) * pull;
        const fy = (dy / d) * pull;
        a.vx += fx; a.vy += fy;
        b.vx -= fx; b.vy -= fy;
      });
      nodes.forEach((n) => {
        n.vx -= n.x * 0.012;
        n.vy -= n.y * 0.012;
        if (n.pinned) { n.vx = 0; n.vy = 0; return; }
        const cap = 4 + 30 * heat;
        n.x += Math.max(-cap, Math.min(cap, n.vx * (0.3 + heat)));
        n.y += Math.max(-cap, Math.min(cap, n.vy * (0.3 + heat)));
        n.vx *= 0.55;
        n.vy *= 0.55;
      });
    }
  }

  function graphView(host, opts) {
    const canvas = h("canvas", { class: "graph-canvas", "aria-label": "Plant knowledge graph", role: "img" });
    const tip = h("div", { class: "graph-tip", hidden: true });
    const tool = (name, title, onclick) => h("button", { class: "graph-tool", type: "button", title, "aria-label": title, onclick }, icon(name));
    const wrap = h("div", { class: "graph-wrap" }, canvas, tip,
      h("p", { class: "graph-hint", text: "Click to explore · double-click equipment to focus · scroll to zoom" }),
      h("div", { class: "graph-tools" },
        tool("plus", "Zoom in", () => zoomBy(1.25)),
        tool("minus", "Zoom out", () => zoomBy(0.8)),
        tool("target", "Fit to view", () => fit(true))));
    host.replaceChildren(wrap);
    const ctx = canvas.getContext("2d");
    const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

    let nodes = [];
    let links = [];
    let byId = new Map();
    const known = new Map(); // id -> last settled position, so filters and focus do not reshuffle
    let hover = null;
    let picked = null;
    let drag = null;
    let colours = graphColours();
    let dims = { w: 1, h: 1, dpr: 1 };
    const view = { x: 0, y: 0, k: 1 };
    let cam = null; // camera tween
    let bloom = null; // node tween after a new layout
    let frame = null;
    let userMoved = false;

    const radiusOf = (n) => (n.kind === "tag" ? 9 + Math.min(7, n.degree * 0.9) : n.kind === "document" ? 8 + Math.min(4, n.degree * 0.3) : 5 + Math.min(3, n.degree * 0.5));
    const zr = () => Math.sqrt(Math.max(0.35, view.k));
    const toScreen = (n) => ({ x: n.x * view.k + view.x, y: n.y * view.k + view.y });

    function resize() {
      // Layout size, not getBoundingClientRect: the tab's enter animation scales the stage.
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      dims = { w: Math.max(1, wrap.clientWidth), h: Math.max(1, wrap.clientHeight), dpr };
      canvas.width = Math.floor(dims.w * dpr);
      canvas.height = Math.floor(dims.h * dpr);
      canvas.style.width = `${dims.w}px`;
      canvas.style.height = `${dims.h}px`;
    }

    function request() { if (!frame) frame = requestAnimationFrame(loop); }

    function loop(now) {
      frame = null;
      let busy = false;
      if (bloom) {
        const t = Math.min(1, (now - bloom.start) / bloom.ms);
        const e = 1 - (1 - t) ** 3;
        nodes.forEach((n) => { n.x = n.fx + (n.tx - n.fx) * e; n.y = n.fy + (n.ty - n.fy) * e; });
        if (t < 1) busy = true; else bloom = null;
      }
      if (cam) {
        const t = Math.min(1, (now - cam.start) / cam.ms);
        const e = 1 - (1 - t) ** 3;
        view.x = cam.from.x + (cam.to.x - cam.from.x) * e;
        view.y = cam.from.y + (cam.to.y - cam.from.y) * e;
        view.k = cam.from.k + (cam.to.k - cam.from.k) * e;
        if (t < 1) busy = true; else cam = null;
      }
      draw();
      if (busy) request();
    }

    function moveCamera(to, animate = true) {
      if (!animate || REDUCED) { Object.assign(view, to); cam = null; request(); return; }
      cam = { from: { ...view }, to, start: performance.now(), ms: 520 };
      request();
    }

    function zoomBy(factor, cx = dims.w / 2, cy = dims.h / 2, animate = true) {
      const k = Math.max(0.3, Math.min(3, (cam ? cam.to.k : view.k) * factor));
      const base = cam ? cam.to : view;
      userMoved = true;
      moveCamera({ k, x: cx - ((cx - base.x) / base.k) * k, y: cy - ((cy - base.y) / base.k) * k }, animate);
    }

    function fitView(list, padding = 56) {
      const pts = list.length ? list : nodes;
      if (!pts.length) return { ...view };
      const xs = pts.map((n) => (n.tx ?? n.x));
      const ys = pts.map((n) => (n.ty ?? n.y));
      const minX = Math.min(...xs) - 30;
      const maxX = Math.max(...xs) + 30;
      const minY = Math.min(...ys) - 30;
      const maxY = Math.max(...ys) + 40;
      const k = Math.max(0.3, Math.min(1.8, Math.min((dims.w - padding * 2) / Math.max(1, maxX - minX),
                                                   (dims.h - padding * 2) / Math.max(1, maxY - minY))));
      return { k, x: dims.w / 2 - ((minX + maxX) / 2) * k, y: dims.h / 2 - ((minY + maxY) / 2) * k };
    }

    function fit(animate) { userMoved = false; moveCamera(fitView(nodes), animate); }

    function centreOn(node) {
      const k = Math.max(view.k, 1.1);
      userMoved = true;
      moveCamera({ k, x: dims.w / 2 - node.x * k, y: dims.h / 2 - node.y * k });
    }

    function neighbours(node) {
      const out = new Set();
      if (!node) return out;
      out.add(node.id);
      links.forEach((l) => {
        if (nodes[l.a] === node) out.add(nodes[l.b].id);
        if (nodes[l.b] === node) out.add(nodes[l.a].id);
      });
      return out;
    }

    function haloText(text, x, y, colour, weight, sizePx) {
      ctx.font = `${weight} ${sizePx}px ${colours.font}`;
      ctx.lineJoin = "round";
      ctx.lineWidth = 3.5;
      ctx.strokeStyle = colours.surface;
      ctx.strokeText(text, x, y);
      ctx.fillStyle = colour;
      ctx.fillText(text, x, y);
    }

    function draw() {
      const { w, h: height, dpr } = dims;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, height);
      const near = picked || hover;
      const related = neighbours(near);
      const scale = zr();

      // Edges: quiet by default, drawn in the neighbour's colour around the node in view.
      links.forEach((l) => {
        const na = nodes[l.a];
        const nb = nodes[l.b];
        const a = toScreen(na);
        const b = toScreen(nb);
        const lit = near && (na === near || nb === near);
        ctx.globalAlpha = near ? (lit ? 0.95 : 0.07) : 0.5;
        ctx.strokeStyle = lit ? colours[(na === near ? nb : na).kind] || colours.line : colours.line;
        ctx.lineWidth = lit ? 1.8 : 1;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      });

      // Nodes: dimmed ones first so the neighbourhood in view always sits on top.
      const order = nodes.slice().sort((a, b) => (related.has(a.id) - related.has(b.id)) || (a === near) - (b === near));
      order.forEach((n) => {
        const p = toScreen(n);
        const r = radiusOf(n) * scale;
        const dim = near && !related.has(n.id);
        const colour = colours[n.kind] || colours.vendor;
        ctx.globalAlpha = dim ? 0.14 : 1;
        if (n === picked) {
          ctx.fillStyle = colour;
          ctx.globalAlpha = 0.18;
          ctx.beginPath();
          ctx.arc(p.x, p.y, r + 9, 0, Math.PI * 2);
          ctx.fill();
          ctx.globalAlpha = 1;
        }
        ctx.fillStyle = colour;
        ctx.beginPath();
        ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
        ctx.fill();
        ctx.lineWidth = n === near ? 2.5 : 1.5;
        ctx.strokeStyle = n === near ? colours.text : colours.surface;
        ctx.stroke();
      });

      // Labels, most important first; a label that would overlap one already placed is skipped.
      const placed = [];
      let owner = null; // a label may sit against its own node's circle, never across another's
      const fits = (box) => !placed.some((o) => o.id !== owner && box.x < o.x + o.w && box.x + box.w > o.x && box.y < o.y + o.h && box.y + box.h > o.y);
      if (near) {
        nodes.forEach((n) => {
          if (!related.has(n.id)) return;
          const p = toScreen(n);
          const r = radiusOf(n) * scale;
          placed.push({ id: n.id, x: p.x - r, y: p.y - r, w: r * 2, h: r * 2 });
        });
      }
      const priority = (n) => (n === near ? 0 : related.has(n.id) ? 1 : n.kind === "tag" ? 2 + 1 / (1 + n.degree) : n.kind === "document" ? 4 : 5);
      const showAll = view.k >= 1.2 || nodes.length <= 40;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      nodes.slice().sort((a, b) => priority(a) - priority(b)).forEach((n) => {
        const inView = related.has(n.id);
        if (near && !inView) return;
        if (!inView && n.kind !== "tag" && n.kind !== "document" && !showAll) return;
        const p = toScreen(n);
        const r = radiusOf(n) * scale;
        const strong = n.kind === "tag" || n === near;
        const size = strong ? 12.5 : 11.5;
        const text = nodeLabel(n).length > 28 ? `${nodeLabel(n).slice(0, 27)}…` : nodeLabel(n);
        ctx.font = `${strong ? 600 : 450} ${size}px ${colours.font}`;
        const tw = ctx.measureText(text).width;
        // Below the node first; the neighbourhood in view may also try above or to the right.
        const spots = [{ x: p.x - tw / 2 - 3, y: p.y + r + 3, cx: p.x, align: "center" }];
        if (inView) {
          spots.push({ x: p.x - tw / 2 - 3, y: p.y - r - size - 7, cx: p.x, align: "center" },
            { x: p.x + r + 3, y: p.y - size / 2 - 2, cx: p.x + r + 6, align: "left" });
        }
        owner = n.id;
        const spot = spots.find((sp) => n === near || fits({ x: sp.x, y: sp.y, w: tw + 6, h: size + 4 }));
        owner = null;
        if (!spot) return;
        const box = { x: spot.x, y: spot.y, w: tw + 6, h: size + 4 };
        if (box.x > w || box.x + box.w < 0 || box.y > height || box.y + box.h < 0) return;
        placed.push(box);
        ctx.globalAlpha = 1;
        ctx.textAlign = spot.align;
        haloText(text, spot.cx, spot.y + 1, strong ? colours.text : colours.muted, strong ? 600 : 450, size);
      });

      // Relationship words along the edges of the selected node, when there are few enough to read.
      if (picked) {
        const mine = links.filter((l) => nodes[l.a] === picked || nodes[l.b] === picked);
        if (mine.length <= 14 && view.k >= 0.7) {
          ctx.textBaseline = "middle";
          ctx.textAlign = "center";
          mine.forEach((l) => {
            const a = toScreen(nodes[l.a]);
            const b = toScreen(nodes[l.b]);
            const word = EDGE_WORDS[l.kind] || l.kind.replace(/_/g, " ");
            const mx = (a.x + b.x) / 2;
            const my = (a.y + b.y) / 2;
            ctx.font = `500 10.5px ${colours.font}`;
            const tw = ctx.measureText(word).width;
            const box = { x: mx - tw / 2 - 3, y: my - 7, w: tw + 6, h: 14 };
            if (!fits(box)) return;
            placed.push(box);
            haloText(word, mx, my, colours.muted, 500, 10.5);
          });
        }
      }
      ctx.globalAlpha = 1;
    }

    const at = (ev) => {
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      let found = null;
      let best = Infinity;
      nodes.forEach((n) => {
        const p = toScreen(n);
        const d = Math.hypot(p.x - x, p.y - y);
        if (d <= radiusOf(n) * zr() + 6 && d < best) { best = d; found = n; }
      });
      return { x, y, node: found };
    };

    function showTip(node, x, y) {
      if (!node || drag) { tip.hidden = true; return; }
      const sub = node.kind === "tag" && node.name && node.name !== node.key ? node.name : null;
      fill(tip, h("span", { class: "graph-tip-kind" }, h("span", { class: `legend-dot k-${node.kind}` }), node.title || kindTitle(node.kind)),
        h("strong", { text: nodeLabel(node) }), sub ? h("span", { class: "graph-tip-sub", text: sub }) : null);
      tip.hidden = false;
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      tip.style.transform = `translate(${Math.min(Math.max(8, x + 14), dims.w - tw - 8)}px, ${y + 16 + th > dims.h ? y - th - 12 : y + 16}px)`;
    }

    canvas.addEventListener("pointermove", (ev) => {
      if (drag) {
        const dx = ev.clientX - drag.sx;
        const dy = ev.clientY - drag.sy;
        if (!drag.moved && Math.hypot(dx, dy) > 4) drag.moved = true;
        if (!drag.moved) return;
        tip.hidden = true;
        if (drag.node) {
          drag.node.x = drag.ox + dx / view.k;
          drag.node.y = drag.oy + dy / view.k;
          drag.node.tx = drag.node.x;
          drag.node.ty = drag.node.y;
          known.set(drag.node.id, { x: drag.node.x, y: drag.node.y });
        } else {
          cam = null;
          userMoved = true;
          view.x = drag.vx + dx;
          view.y = drag.vy + dy;
        }
        request();
        return;
      }
      const spot = at(ev);
      if (spot.node !== hover) { hover = spot.node; request(); }
      canvas.style.cursor = spot.node ? "pointer" : "grab";
      showTip(spot.node, spot.x, spot.y);
    });
    canvas.addEventListener("pointerleave", () => { if (!drag) { hover = null; tip.hidden = true; request(); } });
    canvas.addEventListener("pointerdown", (ev) => {
      const spot = at(ev);
      bloom = null;
      if (spot.node) { spot.node.x = spot.node.tx ?? spot.node.x; spot.node.y = spot.node.ty ?? spot.node.y; }
      drag = { node: spot.node, sx: ev.clientX, sy: ev.clientY, vx: view.x, vy: view.y,
        ox: spot.node ? spot.node.x : 0, oy: spot.node ? spot.node.y : 0, moved: false };
      canvas.style.cursor = spot.node ? "grabbing" : "grabbing";
      canvas.setPointerCapture(ev.pointerId);
    });
    canvas.addEventListener("pointerup", (ev) => {
      const was = drag;
      drag = null;
      canvas.style.cursor = was && was.node ? "pointer" : "grab";
      if (!was || was.moved) return;
      if (ev.detail >= 2 && was.node && opts.onFocus) { opts.onFocus(was.node); return; }
      picked = was.node;
      request();
      opts.onSelect(picked);
    });
    canvas.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const rect = canvas.getBoundingClientRect();
      zoomBy(Math.exp(-ev.deltaY * 0.0015), ev.clientX - rect.left, ev.clientY - rect.top, false);
    }, { passive: false });

    // Repaint on resize and on a theme change (manual toggle or the system setting).
    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        const before = { ...dims };
        resize();
        if (!userMoved && (Math.abs(before.w - dims.w) > 1 || Math.abs(before.h - dims.h) > 1)) moveCamera(fitView(nodes), false);
        request();
      }).observe(wrap);
    }
    const retheme = () => { colours = graphColours(); request(); };
    new MutationObserver(retheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class"] });
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", retheme);
    resize();

    return {
      set(data, { refit = true } = {}) {
        const had = nodes.length > 0;
        const old = new Map(nodes.map((n) => [n.id, n]));
        nodes = data.nodes.map((n) => ({ ...n, vx: 0, vy: 0, degree: 0 }));
        byId = new Map(nodes.map((n, i) => [n.id, i]));
        links = data.edges.map((e) => ({ a: byId.get(e.source), b: byId.get(e.target), kind: e.kind }))
          .filter((l) => l.a !== undefined && l.b !== undefined && l.a !== l.b);
        links.forEach((l) => { nodes[l.a].degree += 1; nodes[l.b].degree += 1; });
        nodes.forEach((n) => { n.r = radiusOf(n); });

        // Start from where each node was last time; new ones start beside a neighbour that is
        // already placed, or on a ring sector for their kind so each kind gathers together.
        const kinds = GRAPH_KINDS.map(([k]) => k);
        const unplaced = [];
        nodes.forEach((n) => {
          const prev = known.get(n.id);
          if (prev) { n.x = prev.x; n.y = prev.y; n.pinned = false; } else unplaced.push(n);
        });
        unplaced.forEach((n) => {
          const link = links.find((l) => (nodes[l.a] === n && known.has(nodes[l.b].id)) || (nodes[l.b] === n && known.has(nodes[l.a].id)));
          if (link) {
            const anchor = known.get((nodes[link.a] === n ? nodes[link.b] : nodes[link.a]).id);
            n.x = anchor.x + (idNoise(n.id, 1) - 0.5) * 80;
            n.y = anchor.y + (idNoise(n.id, 2) - 0.5) * 80;
          } else {
            const sector = (Math.max(0, kinds.indexOf(n.kind)) / kinds.length) * Math.PI * 2;
            const angle = sector + (idNoise(n.id, 3) - 0.5) * 1.2;
            const radius = (n.kind === "tag" ? 120 : 300) * (0.6 + idNoise(n.id, 4) * 0.6);
            n.x = Math.cos(angle) * radius;
            n.y = Math.sin(angle) * radius;
          }
        });
        settle(nodes, links, had && !unplaced.length ? 120 : 320);
        nodes.forEach((n) => {
          n.tx = n.x; n.ty = n.y;
          const prev = old.get(n.id);
          // Nodes glide from where they were; new ones grow out of the middle of the picture.
          n.fx = prev ? prev.x : n.x * 0.55;
          n.fy = prev ? prev.y : n.y * 0.55;
          n.x = n.fx; n.y = n.fy;
          known.set(n.id, { x: n.tx, y: n.ty });
        });
        if (picked) picked = nodes.find((n) => n.id === picked.id) || null;
        hover = null;
        tip.hidden = true;
        if (REDUCED) nodes.forEach((n) => { n.x = n.tx; n.y = n.ty; });
        else bloom = { start: performance.now(), ms: had ? 480 : 700 };
        if (refit || !userMoved) {
          userMoved = false;
          moveCamera(fitView(nodes), had);
        }
        request();
      },
      select(id, { centre = false } = {}) {
        picked = nodes.find((n) => n.id === id) || null;
        if (picked && centre) centreOn({ x: picked.tx ?? picked.x, y: picked.ty ?? picked.y });
        request();
        opts.onSelect(picked);
      },
      clear() { picked = null; request(); opts.onSelect(null); },
      has(id) { return byId.has(id); },
      degree(id) { const n = nodes[byId.get(id)]; return n ? n.degree : 0; },
      fit,
      theme: retheme,
      stop() { if (frame) cancelAnimationFrame(frame); frame = null; },
    };
  }

  // The tag detector's overlay: one small rectangle per detection, in fractions of the sheet
  // (0..1), so it lines up on an SVG sheet or a scanned raster without knowing pixel sizes.
  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined) continue;
      if (key.startsWith("on") && typeof value === "function") listen(el, key.slice(2), value);
      else el.setAttribute(key, String(value));
    }
    return el;
  }

  const DET_LABELS = { pump: "pump", vessel: "vessel", exchanger: "exchanger", instrument: "instrument",
    valve: "valve", control_valve: "control valve", relief_valve: "relief valve", line: "line number",
    connector: "off-page connector", stamp: "stamp", unknown: "unclassified item" };
  const detClassVar = (cls) => `var(--det-${cls.replace(/_/g, "-")}, var(--accent))`;

  function detSummary(result) {
    const byCount = Object.entries(result.summary.by_class).sort((a, b) => b[1] - a[1]);
    const parts = byCount.slice(0, 4).map(([cls, n]) => `${n} ${(DET_LABELS[cls] || cls)}${n === 1 ? "" : "s"}`);
    let text = `${result.summary.total} tags detected: ${parts.join(", ")}`;
    if (byCount.length > 4) text += "...";
    if (result.summary.unknown_count) text += ` · ${result.summary.unknown_count} not in the asset register`;
    return text;
  }

  // A drawing sheet: the SVG (or scanned image) is shown as it is, every known tag on it opens
  // what is recorded against it, and a detection overlay can be laid over either kind of sheet.
  function sheetView(host, svgText, opts) {
    const stage = h("div", { class: "sheet-stage" });
    const holder = h("div", { class: "sheet-holder" });
    const overlay = svgEl("svg", { class: "pid-overlay", viewBox: "0 0 1 1", preserveAspectRatio: "none" });
    stage.append(holder);
    holder.innerHTML = String(svgText).replace(/<script[\s\S]*?<\/script>/gi, "");
    holder.append(overlay);
    host.replaceChildren(stage,
      h("div", { class: "graph-tools sheet-tools" },
        h("button", { class: "graph-tool", type: "button", title: "Zoom in", onclick: () => zoom(1.2) }, icon("plus")),
        h("button", { class: "graph-tool", type: "button", title: "Zoom out", onclick: () => zoom(0.84) }, icon("minus")),
        h("button", { class: "graph-tool", type: "button", title: "Fit to view", onclick: () => reset() }, icon("target"))));
    const view = { x: 0, y: 0, k: 1 };
    const apply = () => { holder.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.k})`; };
    const zoom = (factor) => {
      view.k = Math.max(0.5, Math.min(4, view.k * factor));
      apply();
    };
    const reset = () => { view.x = 0; view.y = 0; view.k = 1; apply(); };
    // Panning is tracked on the window, so a click still reaches the item it landed on.
    let drag = null;
    let dragged = false;
    const move = (e) => {
      if (!drag) return;
      if (Math.abs(e.clientX - drag.sx) > 4 || Math.abs(e.clientY - drag.sy) > 4) dragged = true;
      view.x = e.clientX - drag.x;
      view.y = e.clientY - drag.y;
      apply();
    };
    const release = () => {
      drag = null;
      stage.classList.remove("grabbing");
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", release);
      setTimeout(() => { dragged = false; }, 0);
    };
    stage.addEventListener("pointerdown", (e) => {
      drag = { x: e.clientX - view.x, y: e.clientY - view.y, sx: e.clientX, sy: e.clientY };
      stage.classList.add("grabbing");
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", release);
    });
    stage.addEventListener("click", (e) => { if (dragged) { e.stopPropagation(); e.preventDefault(); } }, true);
    stage.addEventListener("wheel", (e) => { e.preventDefault(); zoom(e.deltaY < 0 ? 1.12 : 0.89); }, { passive: false });
    $$("[data-tag]", holder).forEach((el) => {
      const tag = el.dataset.tag;
      el.classList.add("pid-known");
      listen(el, "click", (e) => { e.stopPropagation(); opts.onTag(tag, el); });
      listen(el, "pointerenter", () => opts.onHover(tag));
      listen(el, "pointerleave", () => opts.onHover(null));
    });
    return {
      mark(tags) {
        $$("[data-tag]", holder).forEach((el) => el.classList.toggle("pid-selected", tags.includes(el.dataset.tag)));
      },
      image(src, alt) {
        holder.replaceChildren(h("img", { class: "sheet-image", src, alt }), overlay);
      },
      // One rectangle per detection, coloured by class; the scan-reveal animation is plain CSS
      // (skipped for prefers-reduced-motion), staggered here only by a per-item transition delay.
      overlay(detections, callbacks) {
        overlay.replaceChildren();
        overlay.classList.remove("pid-overlay-live");
        (detections || []).forEach((d, i) => {
          const [x0, y0, x1, y1] = d.bbox;
          const rect = svgEl("rect", {
            class: `pid-det pid-det-${d.cls.replace(/_/g, "-")}${d.known === false ? " pid-det-unknown" : ""}`,
            x: x0, y: y0, width: Math.max(0.0015, x1 - x0), height: Math.max(0.0015, y1 - y0),
            "data-tag": d.tag,
            style: `--det-color:${detClassVar(d.cls)};transition-delay:${Math.min(i * 16, 700)}ms`,
            onclick: (e) => { e.stopPropagation(); callbacks.onSelect(d); },
            onpointerenter: () => callbacks.onHover(d),
            onpointerleave: () => callbacks.onHover(null),
          });
          const tip = document.createElementNS(SVG_NS, "title");
          tip.textContent = `${d.tag}${d.known === false ? " (not in the asset register)" : ""}`;
          rect.append(tip);
          overlay.append(rect);
        });
        requestAnimationFrame(() => overlay.classList.add("pid-overlay-live"));
      },
      highlightDetection(tag) {
        $$(".pid-det", overlay).forEach((el) => el.classList.toggle("pid-det-hover", tag && el.dataset.tag === tag));
      },
      filterDetections(cls) {
        $$(".pid-det", overlay).forEach((el) => {
          el.classList.toggle("pid-det-hidden", !!cls && !el.classList.contains(`pid-det-${cls.replace(/_/g, "-")}`));
        });
      },
    };
  }

  async function pagePlant() {
    const params = new URLSearchParams(location.search);
    let tab = params.get("tab") === "drawings" ? "drawings" : "map";
    let data = { nodes: [], edges: [] };
    let focus = params.get("tag") || null;
    let chart = null;
    let selected = null;
    const body = $("#plant-body");

    const seg = tabs([["map", "Map"], ["drawings", "Drawings"]], tab, (key, dir) => {
      tab = key;
      history.replaceState(null, "", key === "map" ? "/plant" : "/plant?tab=drawings");
      render(dir);
    });
    fill($("#plant-tabs"), seg);

    let whole = null; // the whole plant, kept for search and the overview while a tag is in focus
    const load = async () => {
      data = await api(`/graph${focus ? `?tag=${encodeURIComponent(focus)}&depth=2` : ""}`);
      if (!focus) whole = data;
      else if (!whole) whole = await api("/graph").catch(() => data);
    };

    function nodeById(id) {
      return data.nodes.find((n) => n.id === id) || null;
    }

    function relations(node) {
      const out = [];
      data.edges.forEach((e) => {
        if (e.source === node.id) out.push({ kind: e.kind, other: nodeById(e.target), direction: "to" });
        if (e.target === node.id) out.push({ kind: e.kind, other: nodeById(e.source), direction: "from" });
      });
      return out.filter((r) => r.other);
    }

    const degreeIn = (graph) => {
      const d = new Map();
      graph.edges.forEach((e) => { d.set(e.source, (d.get(e.source) || 0) + 1); d.set(e.target, (d.get(e.target) || 0) + 1); });
      return d;
    };
    const kindsIn = (graph) => GRAPH_KINDS.filter(([k]) => graph.nodes.some((n) => n.kind === k));
    const pick = (id, centre = true) => chart && chart.select(id, { centre });

    function overview() {
      const degrees = degreeIn(data);
      const top = data.nodes.filter((n) => n.kind === "tag")
        .sort((a, b) => (degrees.get(b.id) || 0) - (degrees.get(a.id) || 0)).slice(0, 6);
      return h("div", { class: "plant-panel overview", "data-key": "overview" },
        h("header", {},
          h("p", { class: "panel-kind", text: focus ? "In focus" : "Knowledge graph" }),
          h("h2", { class: "panel-name", text: focus || data.workspace_title || "Plant" })),
        h("p", { class: "panel-sub", text: focus
          ? `Everything within two steps of ${focus}: its drawings, the clauses that govern it, its inspections and suppliers.`
          : "How the plant's equipment connects to its drawings, procedures, inspections, suppliers and orders." }),
        h("div", { class: "graph-stats" }, kindsIn(data).map(([k, title]) => h("div", { class: "graph-stat" },
          h("strong", { text: String(data.nodes.filter((n) => n.kind === k).length) }),
          h("span", {}, h("span", { class: `legend-dot k-${k}` }), title)))),
        top.length ? h("section", { class: "panel-group" },
          h("h3", { text: focus ? "Equipment here" : "Most connected equipment" }),
          h("ul", { class: "plain rel-list" }, top.map((n) => h("li", {},
            h("button", { class: "rel", type: "button", onclick: () => pick(n.id) },
              h("span", { class: "legend-dot k-tag" }),
              h("span", { class: "rel-name", text: n.key }),
              h("span", { class: "rel-word", text: n.name && n.name !== n.key ? n.name : `${degrees.get(n.id) || 0} links` })))))) : null);
    }

    function details(node) {
      if (!node) return overview();
      const rels = relations(node);
      const groups = new Map();
      rels.forEach((r) => {
        const key = r.other.kind;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(r);
      });
      const PROP_WORDS = { cls: "Class", doc: "Document", tag: "Tag", po: "Order", report: "Report",
        quantity: "Measurement", value: "Value", unit: "Unit", location: "Location", date: "Date",
        description: "Description", revision: "Revision", doc_type: "Kind", heading: "Heading", clause: "Clause" };
      const props = Object.entries(node.props || {})
        .filter(([k]) => !["name", "title"].includes(k))
        .map(([k, v]) => [PROP_WORDS[k] || k.replace(/_/g, " "), v]);
      const ordered = GRAPH_KINDS.map(([k]) => k).filter((k) => groups.has(k));
      // Near the top, so the actions stay in reach however long the list below gets.
      const actions = node.kind === "tag" ? h("div", { class: "row wrap panel-actions" },
        button(focus === node.key ? "Show whole plant" : `Focus on ${node.key}`,
          () => setFocus(focus === node.key ? null : node.key), "primary small"),
        h("a", { class: "btn ghost small", href: `/?ask=${encodeURIComponent(`Summarise everything recorded against ${node.key}: its drawings, the procedures that govern it, inspections and orders.`)}`, text: "Ask about it" })) : null;
      return h("div", { class: "plant-panel", "data-key": node.id },
        h("header", { class: "panel-top" },
          h("span", { class: `legend-dot big k-${node.kind}` }),
          h("div", {}, h("p", { class: "panel-kind", text: node.title || kindTitle(node.kind) }),
            h("h2", { class: "panel-name", text: nodeLabel(node) })),
          labelTag(node.label, node.label_display),
          h("button", { class: "icon-btn panel-close", type: "button", title: "Back to overview", "aria-label": "Back to overview",
            onclick: () => chart && chart.clear() }, icon("cross"))),
        node.kind === "tag" && node.name !== node.key ? h("p", { class: "panel-sub", text: node.name }) : null,
        actions,
        props.length ? h("dl", { class: "panel-facts" }, props.slice(0, 7).flatMap(([k, v]) => [
          h("dt", { text: k }), h("dd", { text: String(v) })])) : null,
        rels.length ? null : h("p", { class: "muted small", text: "Nothing else in view is linked to this." }),
        ...ordered.map((kind) => {
          const list = groups.get(kind);
          return h("section", { class: "panel-group" },
            h("h3", {}, kindTitle(kind), h("span", { class: "panel-count", text: String(list.length) })),
            h("ul", { class: "plain rel-list" }, list.map((r) => h("li", {},
              h("button", { class: "rel", type: "button", title: nodeLabel(r.other), onclick: () => pick(r.other.id) },
                h("span", { class: `legend-dot k-${r.other.kind}` }),
                h("span", { class: "rel-name", text: nodeLabel(r.other) }),
                h("span", { class: "rel-word", text: EDGE_WORDS[r.kind] || r.kind.replace(/_/g, " ") }))))));
        }),
      );
    }

    // The map is built once: focusing on a tag or filtering swaps the data, not the canvas.
    const hidden = new Set(GRAPH_HIDDEN_BY_DEFAULT);
    const stage = h("div", { class: "graph-stage" });
    const panel = h("aside", { class: "plant-side" });
    const searchInput = h("input", { type: "search", placeholder: "Search equipment, documents, clauses", "aria-label": "Search the plant", autocomplete: "off", spellcheck: "false" });
    const results = h("div", { class: "graph-results", role: "listbox", hidden: true });
    const kindBar = h("div", { class: "graph-kinds", role: "group", "aria-label": "Show or hide kinds of record" });
    const focusBar = h("div", { class: "graph-focus" });
    const toolbar = h("div", { class: "graph-toolbar" },
      h("div", { class: "graph-search" }, h("label", { class: "search" }, icon("search"), searchInput, h("kbd", { class: "search-kbd", text: "/" })), results),
      kindBar, focusBar);
    const map = h("div", { class: "plant-map", "data-keep": "" }, toolbar,
      h("div", { class: "plant-layout" }, stage, panel));
    let allTags = [];
    let hits = [];
    let hitIndex = 0;

    const setFocus = async (tag, selectId) => {
      focus = tag;
      history.replaceState(null, "", tag ? `/plant?tag=${encodeURIComponent(tag)}` : "/plant");
      await load();
      selected = null;
      paintMap({ refit: true });
      const start = selectId || (tag && data.nodes.find((n) => n.kind === "tag" && n.key === tag)?.id);
      if (start) pick(start, false);
    };

    function visibleData() {
      const nodes = data.nodes.filter((n) => !hidden.has(n.kind) || (focus && n.kind === "tag" && n.key === focus));
      const ids = new Set(nodes.map((n) => n.id));
      return { nodes, edges: data.edges.filter((e) => ids.has(e.source) && ids.has(e.target)) };
    }

    function paintKinds() {
      fill(kindBar, ...kindsIn(data).map(([k, title]) => {
        const on = !hidden.has(k);
        return h("button", {
          class: `graph-kind${on ? " on" : ""}`, type: "button", "data-key": k, "aria-pressed": String(on),
          title: on ? `Hide ${title.toLowerCase()}` : `Show ${title.toLowerCase()}`,
          onclick: () => {
            if (on) hidden.add(k); else hidden.delete(k);
            paintKinds();
            chart.set(visibleData(), { refit: false });
            if (selected && !chart.has(selected.id)) chart.clear();
          },
        }, h("span", { class: `legend-dot k-${k}` }), title,
        h("span", { class: "graph-kind-n", text: String(data.nodes.filter((n) => n.kind === k).length) }));
      }));
    }

    function paintFocus() {
      fill(focusBar, focus ? h("div", { class: "focus-pill", "data-key": focus },
        h("span", { class: "muted", text: "Around" }), h("strong", { text: focus }),
        h("button", { type: "button", title: "Show the whole plant", "aria-label": "Show the whole plant", onclick: () => setFocus(null) }, icon("cross"))) : null);
    }

    // Search -------------------------------------------------------------------------------
    function searchPool() { return (whole || data).nodes; }
    function runSearch() {
      const q = searchInput.value.trim().toLowerCase();
      if (!q) { hits = []; reveal(results, false); return; }
      const score = (n) => {
        const key = String(n.key || "").toLowerCase();
        const name = String(n.name || "").toLowerCase();
        if (key === q) return 0;
        if (key.startsWith(q)) return 1;
        if (name.startsWith(q)) return 2;
        if (key.includes(q)) return 3;
        if (name.includes(q)) return 4;
        return 9;
      };
      hits = searchPool().map((n) => [score(n), n]).filter(([s]) => s < 9)
        .sort((a, b) => a[0] - b[0] || (a[1].kind === "tag" ? -1 : 0) - (b[1].kind === "tag" ? -1 : 0)).slice(0, 8).map(([, n]) => n);
      hitIndex = 0;
      paintResults();
    }
    function paintResults() {
      if (!hits.length) {
        fill(results, h("p", { class: "graph-noresult", text: "Nothing in the plant matches that." }));
      } else {
        fill(results, ...hits.map((n, i) => h("button", {
          class: `graph-result${i === hitIndex ? " active" : ""}`, type: "button", role: "option", "data-key": n.id,
          "aria-selected": String(i === hitIndex), onmousedown: (e) => e.preventDefault(), onclick: () => choose(n),
        }, h("span", { class: `legend-dot k-${n.kind}` }), h("span", { class: "rel-name", text: nodeLabel(n) }),
        h("span", { class: "rel-word", text: n.kind === "tag" && n.name !== n.key ? n.name : kindTitle(n.kind) }))));
      }
      reveal(results, true);
    }
    async function choose(n) {
      searchInput.value = "";
      hits = [];
      reveal(results, false);
      searchInput.blur();
      if (!data.nodes.some((x) => x.id === n.id)) { await setFocus(null, n.id); return; }
      if (hidden.has(n.kind)) {
        hidden.delete(n.kind);
        paintKinds();
        chart.set(visibleData(), { refit: false });
      }
      pick(n.id);
    }
    searchInput.addEventListener("input", runSearch);
    searchInput.addEventListener("focus", () => { if (searchInput.value.trim()) runSearch(); });
    searchInput.addEventListener("blur", () => reveal(results, false));
    searchInput.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!hits.length) return;
        hitIndex = (hitIndex + (e.key === "ArrowDown" ? 1 : hits.length - 1)) % hits.length;
        paintResults();
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (hits[hitIndex]) choose(hits[hitIndex]);
      } else if (e.key === "Escape") {
        searchInput.value = "";
        runSearch();
        searchInput.blur();
      }
    });
    document.addEventListener("keydown", (e) => {
      if (tab !== "map" || e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      if (FORM_TAGS.has(document.activeElement && document.activeElement.tagName)) return;
      if (e.key === "/") { e.preventDefault(); searchInput.focus(); }
      else if (e.key === "Escape" && chart) chart.clear();
    });

    function paintMap({ refit = true } = {}) {
      if (tab !== "map") return;
      allTags = (whole || data).nodes.filter((n) => n.kind === "tag").map((n) => n.key).sort();
      paintKinds();
      paintFocus();
      if (!chart) {
        chart = graphView(stage, {
          onSelect: (node) => {
            selected = node;
            fill(panel, details(node));
            panel.firstElementChild && (panel.firstElementChild.scrollTop = 0);
          },
          onFocus: (node) => {
            if (node.kind === "tag") setFocus(focus === node.key ? null : node.key);
            else pick(node.id, true);
          },
        });
      }
      fill(panel, details(null));
      chart.set(visibleData(), { refit });
      if (focus) {
        const start = data.nodes.find((n) => n.kind === "tag" && n.key === focus);
        if (start) pick(start.id, false);
      }
    }

    function mapView() {
      queueMicrotask(() => paintMap());
      return map;
    }

    // Drawings -------------------------------------------------------------------------------
    let sheets = null;
    let sheetId = params.get("sheet") || null;
    let sheetBox = null;
    let detFilter = null;

    const sheetPanel = h("aside", { class: "plant-side" });
    const sheetStage = h("div", { class: "sheet-frame" });
    const sheetList = h("div", { class: "row wrap plant-chips" });
    const detBar = h("div", { class: "det-bar", hidden: true });
    const drawings = h("div", { class: "plant-map", "data-keep": "" }, sheetList, detBar,
      h("div", { class: "plant-layout" }, sheetStage, sheetPanel));

    function renderDetBar(result) {
      detFilter = null;
      if (!result || !result.detections.length) {
        detBar.hidden = true;
        fill(detBar);
        return;
      }
      detBar.hidden = false;
      const classes = Object.keys(result.summary.by_class);
      const chips = h("div", { class: "row wrap det-filters" });
      const paint = () => fill(chips,
        h("button", { class: `det-chip${detFilter === null ? " active" : ""}`, type: "button",
          onclick: () => { detFilter = null; sheetBox.filterDetections(null); paint(); } }, "All"),
        ...classes.map((cls) => h("button", {
          class: `det-chip${detFilter === cls ? " active" : ""}`, type: "button",
          onclick: () => { detFilter = detFilter === cls ? null : cls; sheetBox.filterDetections(detFilter); paint(); },
        }, h("span", { class: "det-dot", style: `background:${detClassVar(cls)}` }),
           ` ${DET_LABELS[cls] || cls} (${result.summary.by_class[cls]})`)));
      paint();
      fill(detBar, h("p", { class: "det-summary", text: detSummary(result) }), chips);
    }

    async function loadDetections(file) {
      const result = await api(`/files/${encodeURIComponent(file.id)}/detections`).catch(() => null);
      if (!sheetBox || sheetId !== file.id) return; // the user moved on to another sheet meanwhile
      sheetBox.overlay(result ? result.detections : [], {
        onHover: (d) => sheetBox.highlightDetection(d ? d.tag : null),
        onSelect: async (d) => {
          sheetBox.mark([d.tag]);
          sheetBox.highlightDetection(d.tag);
          fill(sheetPanel, sheetSide(await tagFacts(d.tag), d.tag));
        },
      });
      renderDetBar(result);
    }

    async function tagFacts(tag) {
      const one = await api(`/graph?tag=${encodeURIComponent(tag)}&depth=1`).catch(() => null);
      if (!one) return null;
      return one.nodes.find((n) => n.kind === "tag" && n.key === tag) || null;
    }

    function sheetSide(node, tag) {
      if (!node) {
        return h("div", { class: "plant-panel empty" },
          h("p", { class: "muted", text: tag ? `${tag} is on this sheet but not in the plant records.`
            : "Click any tagged item on the sheet to see what is recorded against it." }),
          h("p", { class: "muted small", text: "Drag to move the sheet, scroll to zoom." }));
      }
      const props = Object.entries(node.props || {});
      return h("div", { class: "plant-panel", "data-key": node.id },
        h("header", { class: "panel-top" }, h("span", { class: "legend-dot k-tag" }),
          h("div", {}, h("p", { class: "panel-kind", text: "Equipment" }), h("h2", { class: "panel-name", text: node.key })),
          labelTag(node.label, node.label_display)),
        node.props && node.props.description ? h("p", { class: "panel-sub", text: node.props.description }) : null,
        props.length ? h("dl", { class: "panel-facts" }, props.filter(([k]) => k !== "description").slice(0, 5)
          .flatMap(([k, v]) => [h("dt", { text: k === "cls" ? "Class" : k.replace(/_/g, " ") }),
            h("dd", { text: String(v) })])) : null,
        h("div", { class: "row wrap panel-actions" },
          button("Open in the map", async () => {
            focus = node.key;
            selected = null;
            await load();
            history.replaceState(null, "", "/plant");
            seg.select("map");
          }, "primary")));
    }

    async function showSheet(file) {
      sheetId = file.id;
      history.replaceState(null, "", `/plant?tab=drawings&sheet=${encodeURIComponent(file.id)}`);
      fill(sheetList, ...sheets.map((sh) => h("button", {
        class: `filter${sh.id === sheetId ? " active" : ""}`, type: "button", "data-key": sh.id,
        onclick: () => showSheet(sh),
      }, sh.name.replace(/\.(svg|png|jpe?g)$/i, ""))));
      fill(sheetPanel, sheetSide(null, null));
      detBar.hidden = true;
      sheetStage.replaceChildren(h("div", { class: "loading" }, spinner()));
      if (!/\.svg$/i.test(file.name)) {
        // A scanned sheet: its tags are pixels, not text, so nothing on the image itself is
        // clickable, but the detector below reads them from the OCR sidecar and overlays boxes
        // that are.
        sheetBox = sheetView(sheetStage, "", { onTag: () => {}, onHover: () => {} });
        sheetBox.image(fileUrl(file.id), file.name);
        fill(sheetPanel, h("div", { class: "plant-panel empty" },
          h("p", { class: "muted", text: `${file.name} is a scanned sheet. Its tags are pictures, so they cannot be clicked directly, but the detector reads them from the OCR sidecar; click a highlighted box once it appears.` }),
          h("div", { class: "row" }, h("a", { class: "btn ghost", href: fileUrl(file.id), download: "" }, icon("download"), "Download"))));
        loadDetections(file);
        return;
      }
      const text = await fetch(fileUrl(file.id), { credentials: "same-origin" }).then((r) => r.text());
      sheetBox = sheetView(sheetStage, text, {
        onTag: async (tag) => {
          sheetBox.mark([tag]);
          sheetBox.highlightDetection(tag);
          fill(sheetPanel, sheetSide(await tagFacts(tag), tag));
        },
        onHover: (tag) => sheetBox.highlightDetection(tag),
      });
      loadDetections(file);
    }

    function drawingsView() {
      queueMicrotask(async () => {
        if (!sheets) {
          const files = await api(`/workspaces/${encodeURIComponent(data.workspace)}/files?area=inputs`).catch(() => []);
          sheets = files.filter((f) => /\.(svg|png|jpe?g)$/i.test(f.name)
            && (/^PID/i.test(f.name) || /(pid|p&id)/i.test(f.name)));
        }
        if (!sheets.length) {
          fill(sheetStage, emptyState("file", "No drawings yet", "Sheets placed in the workspace appear here."));
          return;
        }
        await showSheet(sheets.find((sh) => sh.id === sheetId) || sheets[0]);
      });
      return drawings;
    }

    function render(dir) {
      if (tab !== "map" && chart) { chart.stop(); chart = null; }
      seg.setCount("map", allTags.length || data.nodes.filter((n) => n.kind === "tag").length);
      const content = tab === "map" ? mapView() : drawingsView();
      if (dir === undefined || dir === null) fill(body, content);
      else swapPanel(body, content, dir);
    }

    await load();
    render(0);
  }

  const PAGES = { home: pageHome, task: pageTask, review: pageReview, models: pageModels, security: pageSecurity, library: pageLibrary, plant: pagePlant };

  document.addEventListener("DOMContentLoaded", async () => {
    try {
      await initShell();
      const page = PAGES[document.body.dataset.page];
      if (page) await page();
      document.body.classList.add("ready");
    } catch (e) {
      document.body.classList.add("ready");
      toast(e.message || String(e), true);
      console.error(e);
    }
  });
})();
