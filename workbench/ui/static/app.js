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
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
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

  function fill(el, ...children) {
    el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
    return el;
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
    chevron: "M8 5l5 5-5 5",
    dot: "M10 11a1 1 0 100-2 1 1 0 000 2z",
    note: "M6 2.5h5.5l3.5 3.5v11.5H6zM8.5 10h4M8.5 13h4",
    contract: "M5 3h10v14H5zM7.5 6.5h5M7.5 9.5h5M7.5 12.5h2.5",
    chart: "M3.5 16.5h13M6 13.5V9M10 13.5V5.5M14 13.5V11",
    calc: "M5 2.5h10v15H5zM7.5 5.5h5v2.5h-5zM7.5 11h.01M10 11h.01M12.5 11h.01M7.5 14h.01M10 14h.01M12.5 14h.01",
    compare: "M3 6h9M9 3l3 3-3 3M17 14H8M11 11l-3 3 3 3",
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
    setTimeout(() => el.remove(), bad ? 7000 : 3000);
  }

  function autosize(area) {
    const fit = () => { area.style.height = "auto"; area.style.height = `${Math.min(area.scrollHeight, 240)}px`; };
    area.addEventListener("input", fit);
    fit();
  }

  // -------------------------------------------------------------------------------------------
  // API

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

  function button(text, onclick, kind = "") {
    return h("button", { class: `btn ${kind}`.trim(), type: "button", onclick: (e) => guarded(e.currentTarget, () => onclick(e)) }, text);
  }

  // -------------------------------------------------------------------------------------------
  // Overlays: a side panel for details and a modal for single records

  function openLayer(id, title, content) {
    const layer = $(`#${id}`);
    $(`#${id}-title`).textContent = title;
    fill($(`#${id}-content`), content);
    layer.hidden = false;
    $$("[data-close]", layer).forEach((el) => { el.onclick = () => { layer.hidden = true; }; });
  }
  const openPanel = (title, content) => openLayer("panel", title, content);
  const openModal = (title, content) => openLayer("modal", title, content);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#modal").hidden) $("#modal").hidden = true;
    else $("#panel").hidden = true;
  });

  const KIND_NAMES = {
    user_input: "Your request", plan: "Approved plan", attachment: "Attachment", ocr_text: "Page text",
    vlm_read: "Field read", kb_chunk: "Procedure passage", graph_fact: "Plant record", sandbox_result: "Script run",
    calc_result: "Calculation", check_result: "Check", model_output: "Model output", tool_output: "Tool output",
  };

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

  function setMarking(label, display) {
    const el = $("#marking");
    el.hidden = !display;
    el.className = `marking lv-${levelOf(label)}`;
    el.textContent = display || "";
    el.title = "Classification of what you are viewing";
  }

  async function initShell() {
    const page = document.body.dataset.page;
    const side = $("#sidebar");
    const scrim = $("#scrim");
    const toggleSide = (open) => { side.classList.toggle("open", open); scrim.hidden = !open; };
    $("#menu-btn").addEventListener("click", () => toggleSide(!side.classList.contains("open")));
    scrim.addEventListener("click", () => toggleSide(false));

    const menu = $("#account-menu");
    const accountBtn = $("#account-btn");
    accountBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
      accountBtn.setAttribute("aria-expanded", String(!menu.hidden));
    });
    document.addEventListener("click", (e) => { if (!menu.hidden && !menu.contains(e.target)) menu.hidden = true; });

    let [users, me, workspaces] = await Promise.all([api("/users"), api("/me").catch(() => null), api("/workspaces").catch(() => null)]);
    shell.users = users;
    if (!me || !users.some((u) => u.id === USER)) {
      USER = users[0].id;
      setUserCookie(USER);
      [me, workspaces] = await Promise.all([api("/me"), api("/workspaces")]);
    }
    const userSel = $("#user-select");
    shell.users.forEach((u) => userSel.append(h("option", { value: u.id, text: u.name })));
    userSel.value = USER;
    userSel.addEventListener("change", () => {
      store.set("wb_user", userSel.value);
      setUserCookie(userSel.value);
      location.href = "/";
    });
    shell.me = me;
    $("#account-name").textContent = shell.me.name;
    $("#avatar").textContent = shell.me.name.split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();

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

  async function refreshRecent() {
    const list = $("#recent");
    let tasks = [];
    try { tasks = await api("/tasks"); } catch { return; }
    const mine = tasks.filter((t) => t.user === USER);
    const current = document.body.dataset.task;
    const today = new Date().toDateString();
    let group = "";
    fill(list, ...mine.slice(0, 40).map((t) => {
      const day = new Date(t.created_at).toDateString() === today ? "Today" : "Earlier";
      const heading = day !== group ? h("div", { class: "side-group", text: day }) : null;
      group = day;
      let mark = null;
      if (NEEDS_YOU.has(t.status)) mark = h("span", { class: "flag", title: STATUS[t.status] });
      else if (!TERMINAL.has(t.status)) mark = h("span", { class: "spinner tiny", title: STATUS[t.status] });
      else if (t.status !== "completed") mark = h("span", { class: "flag bad", title: STATUS[t.status] });
      return [heading, h("a", { href: `/t/${t.id}`, class: t.id === current ? "active" : "", title: t.text },
        h("span", { class: "recent-title", text: t.text }), mark)];
    }));
    if (!mine.length) list.append(h("p", { class: "side-empty", text: "Your tasks will appear here." }));
  }

  function initNetwork() {
    const btn = $("#net-btn");
    const pop = $("#net-pop");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      pop.hidden = !pop.hidden;
      if (!pop.hidden) renderNetPop();
    });
    document.addEventListener("click", (e) => { if (!pop.hidden && !pop.contains(e.target)) pop.hidden = true; });
    const tick = async () => {
      try {
        const snap = await api("/egress");
        shell.egress = snap;
        const dot = $("#net-dot");
        if (snap.status !== "ok") {
          dot.className = "net-dot off";
          $("#net-text").textContent = "Monitor offline";
        } else if (snap.breach || snap.external_connections) {
          dot.className = "net-dot bad";
          $("#net-text").textContent = "Data left the server";
        } else {
          dot.className = "net-dot ok";
          $("#net-text").textContent = "Local only";
        }
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
    $(".greeting").textContent = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
    $(".greeting-sub").textContent = `What should we prepare in ${ws.title}?`;
    const text = $("#task-text");
    const send = $("#send-btn");
    const attached = new Set();
    let files = [];
    autosize(text);
    text.focus();
    const syncSend = () => { send.disabled = !text.value.trim(); };
    text.addEventListener("input", syncSend);
    text.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (text.value.trim()) $("#composer").requestSubmit(); }
    });

    const renderAttached = () => {
      fill($("#attached"), ...[...attached].map((p) => h("span", { class: "file-chip" }, icon("file"), shortName(p),
        h("button", { type: "button", "aria-label": `Remove ${shortName(p)}`, onclick: () => { attached.delete(p); renderAttached(); renderPicker(); } }, icon("cross")))));
    };
    const renderPicker = () => {
      const inputs = files.filter((f) => f.area === "inputs");
      fill($("#picker-list"), ...inputs.map((f) => h("label", { class: "pick" },
        h("input", { type: "checkbox", checked: attached.has(f.path), onchange: (e) => { e.target.checked ? attached.add(f.path) : attached.delete(f.path); renderAttached(); } }),
        h("span", { class: "pick-name", text: f.name }), labelTag(f.label, f.label_display), h("span", { class: "muted small", text: bytes(f.size) }))));
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
      const offers = files.filter((f) => f.area === "inputs" && /^(offer_|tender)/.test(f.name));
      if (offers.length >= 2) ideas.push(["compare", "Compare vendor offers", "Compare these three vendor offers against the tender conditions and recommend one", offers]);
      fill($("#starters"), ...ideas.slice(0, 4).map(([ico, label, prompt, paths]) => h("button", {
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
    $("#attach-btn").addEventListener("click", (e) => { e.stopPropagation(); picker.hidden = !picker.hidden; });
    document.addEventListener("click", (e) => { if (!picker.hidden && !picker.contains(e.target)) picker.hidden = true; });
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
        const meta = $("#no-template").checked ? { templates_disabled: true } : {};
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
      out.push(text.slice(last, m.index));
      m[1].split(/,\s*/).forEach((rid) => {
        if (!sources.includes(rid)) sources.push(rid);
        out.push(h("button", { class: "cite", type: "button", title: rid, text: sources.indexOf(rid) + 1, onclick: () => onOpen(rid) }));
      });
      last = m.index + m[0].length;
    }
    out.push(text.slice(last));
    return out;
  }

  function sourceParts(rec, rid) {
    const summary = rec ? rec.summary : rid;
    if (rec && rec.kind === "graph_fact") return { title: "Plant records", body: summary };
    const page = summary.match(/^(.+?) p\.(\d+) \([^)]*\): (.*)$/);
    if (page) return { title: `${page[1]} · page ${page[2]}`, body: page[3].replace(/^#+\s*/, "") };
    const cut = summary.search(/ · |: /);
    if (cut > 0) return { title: summary.slice(0, cut), body: summary.slice(cut + 2).replace(/^[\s·:]+/, "") };
    return { title: KIND_NAMES[rec && rec.kind] || rid, body: summary };
  }

  async function pageTask() {
    const id = document.body.dataset.task;
    const view = { task: null, editing: null, stamp: "", ledger: [] };
    const follow = $("#followup");
    autosize($("#followup-text"));

    const load = async () => {
      let task;
      try { task = await api(`/tasks/${id}`); } catch (e) {
        fill($("#thread"), h("div", { class: "notice", text: e.status === 403 ? "You do not have access to this task." : e.message }));
        return false;
      }
      const stamp = `${task.revision_no}|${task.status}|${task.trace ? task.trace.length : 0}`;
      if (stamp !== view.stamp) {
        view.task = task;
        view.stamp = stamp;
        renderHead(task);
        if (!view.editing) renderThread(task);
        setMarking(task.label, task.label_display);
        follow.hidden = task.status !== "completed" || !task.attachments.length;
      }
      return !TERMINAL.has(task.status);
    };
    // Poll quickly while work runs, slowly while waiting for the user, and at once after a click.
    let timer = null;
    let burst = 0;
    const poll = async () => {
      clearTimeout(timer);
      let again = true;
      try { again = await load(); } catch { again = true; }
      if (!again) return;
      const waiting = view.task && view.task.pending_gate;
      const delay = burst > 0 ? 250 : waiting ? 2000 : 500;
      burst = Math.max(0, burst - 1);
      timer = setTimeout(poll, delay);
    };
    const refresh = () => { view.stamp = ""; burst = 12; return poll(); };

    function renderHead(t) {
      const job = t.job;
      const running = job && ["queued", "running"].includes(job.state) && t.is_owner;
      fill($("#thread-head"),
        h("div", { class: "head-left" },
          h("span", { class: `status-pill ${statusTone(t)}` }, h("span", { class: "status-dot" }), statusLabel(t)),
          h("div", { class: "thread-title", text: t.text })),
        h("div", { class: "row" },
          running && !t.pending_gate ? button("Stop", async () => { await api(`/jobs/${job.id}`, { method: "DELETE" }); toast("Stopping"); }, "ghost") : null,
          h("button", { class: "btn ghost", type: "button", onclick: () => openDetails(t) }, icon("info"), "Details")));
    }

    function assistant(...children) {
      return h("div", { class: "turn assistant" }, h("img", { class: "turn-avatar", src: "/static/mark.svg", alt: "" }),
        h("div", { class: "turn-body" }, children));
    }

    function renderThread(t) {
      const items = [];
      items.push(h("div", { class: "turn user" }, h("div", { class: "bubble" },
        h("p", { text: t.text }),
        t.attachments.length ? h("div", { class: "row wrap" }, t.attachments.map((a) => h("span", { class: "file-chip", title: a }, icon("file"), shortName(a)))) : null)));

      const body = [];
      const gate = t.pending_gate;
      const step = t.plan && t.plan.steps.find((s) => s.status === "running");
      if (!TERMINAL.has(t.status) && !gate) {
        const total = t.plan ? t.plan.steps.length : 0;
        const done = t.plan ? t.plan.steps.filter((s) => ["done", "incomplete", "denied"].includes(s.status)).length : 0;
        let note = STATUS[t.status] || "Working";
        if (step) note = `${step.title || step.id} (${done + 1} of ${total})`;
        if (t.status === "waiting_tide") note = "Waiting for the reasoning model to load";
        if (t.job && t.job.state === "queued") note = `Waiting in line (position ${t.job.position})`;
        body.push(h("div", { class: "working" }, h("span", { class: "shimmer", text: note })));
      }
      if (gate && gate.kind === "template_choice") body.push(choiceBlock(t, gate));
      if (t.plan) body.push(gate && gate.kind === "plan" ? planGate(t, gate) : progress(t));
      if (gate && gate.kind === "action") body.push(actionGate(t, gate));
      body.push(...results(t));
      if (body.length) items.push(assistant(body));
      fill($("#thread"), ...items);
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
          h("button", { class: "icon-btn", type: "button", "aria-label": "Move up", disabled: i === 0, onclick: () => { [steps[i - 1], steps[i]] = [steps[i], steps[i - 1]]; renderThread(t); } }, icon("up")),
          h("button", { class: "icon-btn", type: "button", "aria-label": "Move down", disabled: i === steps.length - 1, onclick: () => { [steps[i + 1], steps[i]] = [steps[i], steps[i + 1]]; renderThread(t); } }, icon("down")),
          h("button", { class: "icon-btn", type: "button", "aria-label": "Remove step", onclick: () => { steps.splice(i, 1); renderThread(t); } }, icon("trash"))) : null)));
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
          h("button", { class: "btn ghost", type: "button", text: "Discard changes", onclick: () => { view.editing = null; renderThread(t); } })));
      } else {
        parts.push(h("div", { class: "row" },
          h("button", { class: "btn primary", type: "button", disabled: !plan.valid, onclick: (e) => guarded(e.currentTarget, async () => {
            await api(`/tasks/${t.id}/plan/decision`, { method: "POST", json: { decision: "approve" } });
            await refresh();
          }) }, "Start"),
          h("button", { class: "btn", type: "button", text: "Edit", onclick: () => {
            view.editing = { steps: plan.steps.map((s) => ({ ...s })) };
            renderThread(t);
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
      if (r.answer) {
        const sources = [];
        const answer = r.answer;
        out.push(h("div", { class: "answer" }, answer.answer.map((a) => h("p", {}, citeText(a.text, sources, showRecord)))));
        if (sources.length) {
          const recs = new Map(view.ledger.map((x) => [x.id, x]));
          out.push(h("div", { class: "source-cards" }, sources.map((rid, i) => {
            const rec = recs.get(rid);
            const { title, body } = sourceParts(rec, rid);
            return h("button", { class: "source-card", type: "button", onclick: () => showRecord(rid) },
              h("span", { class: "source-n", text: i + 1 }),
              h("span", { class: "source-main" }, h("span", { class: "source-title", text: title }), h("span", { class: "source-body", text: body })));
          })));
          if (!view.ledgerLoaded) {
            view.ledgerLoaded = true;
            api(`/tasks/${t.id}/ledger`).then((l) => { view.ledger = l; renderThread(t); }).catch(() => {});
          }
        }
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
        out.push(h("div", { class: "files" }, t.deliverables.map((d) => h("a", { class: "file", href: fileUrl(d.final_file_id || d.file_id), title: `Download ${shortName(d.relpath)}` },
          h("span", { class: `file-ext ext-${d.relpath.split(".").pop().toLowerCase()}`, text: d.relpath.split(".").pop() }),
          h("div", { class: "file-info" }, h("div", { class: "file-name", text: shortName(d.relpath) }),
            h("div", { class: "row tight" }, labelTag(d.label), h("span", { class: "muted small", text: d.status === "approved" ? "Final" : "Draft" }))),
          h("span", { class: "file-dl" }, icon("download"))))));
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
      if (t.status === "completed" && !(t.deliverables || []).length && !r.answer && !r.code) {
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
      const tabs = [["activity", "Activity"], ["evidence", "Evidence"], ["model", "Model choice"], ["checks", "Checks"]];
      let tab = "activity";
      const seg = h("div", { class: "seg" });
      const holder = h("div", { class: "details-body" });
      const render = async () => {
        $$("button", seg).forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
        fill(holder, h("div", { class: "loading" }, spinner()));
        const task = view.task;
        if (tab === "activity") {
          fill(holder, h("ol", { class: "log" }, task.trace.map((r) => h("li", { class: r.ok ? "" : "bad" },
            h("span", { class: "log-k", text: [r.kind, r.step_id].filter(Boolean).join(" · ") }),
            h("span", { class: "log-s", text: r.summary }),
            r.latency_s ? h("span", { class: "log-t", text: `${r.latency_s.toFixed(2)}s` }) : h("span", {})))));
        } else if (tab === "evidence") {
          view.ledger = await api(`/tasks/${task.id}/ledger`).catch(() => []);
          fill(holder, ...view.ledger.map((r) => h("button", { class: "evidence", type: "button", onclick: () => showRecord(r.id) },
            h("span", { class: "row tight" }, h("span", { class: "strong", text: KIND_NAMES[r.kind] || r.kind }), labelTag(r.label),
              r.confidence && r.confidence !== "high" ? h("span", { class: "chip warn", text: r.confidence }) : null, h("span", { class: "grow" }), h("span", { class: "mono tiny muted", text: r.id })),
            h("span", { class: "evidence-sum", text: r.summary }))));
          if (!view.ledger.length) fill(holder, h("p", { class: "muted", text: "No evidence yet." }));
        } else if (tab === "model") {
          const r = task.route;
          if (!r) { fill(holder, h("p", { class: "muted", text: "Not decided yet." })); return; }
          fill(holder, 
            h("p", {}, "Chosen: ", h("strong", { text: r.chosen })),
            h("p", { class: "muted small", text: `The cheapest model scoring at least ${r.threshold.toFixed(2)} for this kind of task is chosen.` }),
            h("table", { class: "table" }, h("tbody", {}, r.candidates.map((c) => h("tr", { class: c.model === r.chosen ? "chosen" : "" },
              h("td", { class: "mono", text: c.model }),
              h("td", { text: c.ok ? (c.quality === null ? "no score" : `score ${c.quality.toFixed(2)}`) : `not suitable (${c.reason})` }),
              h("td", { class: "muted", text: c.ok && c.cost_s !== null ? `${Math.round(c.cost_s + (c.wait_s || 0))}s` : "" }))))),
            h("details", {}, h("summary", { text: "Decision log" }), h("pre", { class: "code", text: r.log_line })));
        } else {
          const data = await api(`/tasks/${task.id}/checks`).catch(() => ({ checks: [] }));
          if (!data.checks.length) { fill(holder, h("p", { class: "muted", text: "No checks for this task." })); return; }
          fill(holder, ...data.checks.map((c) => h("div", { class: `check-row ${c.status}` },
            h("span", { class: "check-mark" }, icon(c.status === "pass" ? "check" : c.status === "mismatch" ? "alert" : "info")),
            h("div", {}, h("div", { class: "strong", text: c.description || c.rule }),
              h("div", { class: "small", text: `${c.left_value ?? ""}${c.right_value ? ` vs ${c.right_value}` : ""}` }),
              c.note ? h("div", { class: "muted small", text: c.note }) : null,
              c.extra && c.extra.crop ? h("img", { class: "crop", alt: "Scanned region", src: `/api/tasks/${task.id}/evidence/${c.extra.crop}` }) : null))));
        }
      };
      tabs.forEach(([key, label]) => seg.append(h("button", { type: "button", dataset: { tab: key }, text: label, onclick: () => { tab = key; render(); } })));
      openPanel("Details", [
        h("p", { class: "muted small", text: `${t.id} · ${t.user} · ${fmtTime(t.created_at)}${t.model ? ` · ${t.model}` : ""}` }),
        seg, holder]);
      render();
    }

    follow.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = $("#followup-text").value.trim();
      if (!text) return;
      guarded(e.submitter, async () => {
        const child = await api(`/tasks/${id}/followup`, { method: "POST", json: { text } });
        location.href = `/t/${child.id}`;
      });
    });
    $("#followup-text").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); follow.requestSubmit(); }
    });

    await poll();
    if (location.hash === "#details" && view.task) openDetails(view.task);
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
      setMarking(t.label, t.label_display);
      fill($("#review-head"), h("a", { class: "back", href: `/t/${id}`, text: "Back to task" }), h("h1", { text: t.text }));
      const tabs = $("#doc-tabs");
      tabs.hidden = data.deliverables.length < 2;
      fill(tabs, ...data.deliverables.map((d, i) => h("button", { type: "button", class: i === current ? "active" : "", text: d.name, onclick: () => { current = i; render(); } })));
      render();
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
        if (style === "Title" || style === "Heading 1") paper.append(h("h1", {}, inner));
        else if (style.startsWith("Heading")) paper.append(h("h2", {}, inner));
        else if (style === "WB Meta") paper.append(h("p", { class: "meta" }, inner));
        else if (style === "List Bullet") paper.append(h("p", { class: "bullet" }, inner));
        else if (style === "WB Reference") paper.append(h("p", { class: "ref" }, inner));
        else paper.append(h("p", {}, inner));
      });
      paper.append(h("div", { class: "paper-marking", text: d.marking }));
      return [h("div", { class: "legend" },
        h("span", {}, h("span", { class: "fig sourced", text: "12.5" }), " from a source"),
        h("span", {}, h("span", { class: "fig derived", text: "12.5" }), " calculated"),
        h("span", {}, h("span", { class: "fig unsourced", text: "12.5" }), " no source")), paper];
    }

    function renderSheet(d) {
      const seg = h("div", { class: "seg" });
      const holder = h("div", {});
      const show = (i) => {
        $$("button", seg).forEach((b, j) => b.classList.toggle("active", i === j));
        const s = d.preview.sheets[i];
        fill(holder, h("div", { class: "sheet" }, h("table", {}, h("tbody", {}, s.rows.map((row, ri) => h("tr", {},
          h("td", { class: "rowhead", text: ri + 1 }),
          row.map((c) => {
            const formula = typeof c.v === "string" && c.v.startsWith("=");
            return h("td", { class: formula ? "formula" : c.comment ? "sourced" : "", title: c.comment || (formula ? "Formula" : "") }, c.v === null ? "" : String(c.v));
          })))))));
      };
      d.preview.sheets.forEach((s, i) => seg.append(h("button", { type: "button", text: s.name, onclick: () => show(i) })));
      show(0);
      return [d.preview.sheets.length > 1 ? seg : null, holder, h("p", { class: "muted small", text: "Blue cells are formulas. Green cells note their source." })];
    }

    function renderOther(d) {
      const p = d.preview;
      if (p.svg !== undefined) {
        return h("div", { class: "sheet pad" }, h("img", { class: "svg-preview", alt: d.name, src: `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(p.svg)))}` }));
      }
      if (p.slides) {
        return h("div", { class: "slides" }, p.slides.map((s, i) => h("div", { class: "slide" }, h("strong", { text: `${i + 1}. ${s[0] || ""}` }), h("ul", { class: "plain" }, s.slice(1).map((x) => h("li", { text: x }))))));
      }
      return h("pre", { class: "code tall", text: p.text || "This file cannot be previewed. Download it instead." });
    }

    function render() {
      $$("#doc-tabs button").forEach((b, i) => b.classList.toggle("active", i === current));
      const d = data.deliverables[current];
      const type = d.preview.type;
      fill($("#doc-view"), ...[].concat(type === "docx" ? renderDocx(d) : type === "xlsx" ? renderSheet(d) : renderOther(d)));
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
          const box = h("input", { type: "checkbox", checked: acked, disabled: acked || !gate || !data.can_approve || c.status !== "mismatch" });
          box.addEventListener("change", () => guarded(box, async () => { await api(`/tasks/${id}/checks/${c.record_id}/acknowledge`, { method: "POST" }); await load(); }));
          return h("div", { class: `issue ${c.status}` },
            h("div", { class: "strong", text: c.description || c.rule }),
            h("div", { class: "small", text: `${c.left_value ?? ""}${c.right_value ? ` vs ${c.right_value}` : ""}` }),
            c.note ? h("div", { class: "muted small", text: c.note }) : null,
            c.status === "mismatch" ? h("label", { class: "tick" }, box, acked ? "Reviewed" : "Mark as reviewed") : null);
        })));
      }

      const orphans = (d.provenance.figures || []).filter((f) => f.status === "unsourced");
      if (orphans.length) {
        const dec = decisions();
        parts.push(h("section", { class: "side-block" }, h("h3", { text: "Figures without a source" }), orphans.map((f) => {
          const key = `${d.file_id}:${f.id}`;
          const done = dec.get(key);
          const input = h("input", { class: "input", placeholder: "Record id or corrected value" });
          const act = (action) => async () => {
            const body = { action, note: null };
            if (action === "link") body.record_id = input.value.trim();
            if (action === "correct") body.value = input.value.trim();
            await api(`/tasks/${id}/draft/figures/${encodeURIComponent(key)}`, { method: "POST", json: body });
            await load();
          };
          return h("div", { class: "issue" },
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
          const note = h("textarea", { class: "input", rows: 2, placeholder: "Note for changes (optional when approving)" });
          parts.push(h("section", { class: "side-block" }, note,
            h("div", { class: "row" },
              h("button", { class: "btn primary", type: "button", disabled: blockers.length > 0, title: blockers.join("; "), onclick: (e) => guarded(e.currentTarget, async () => {
                await api(`/tasks/${id}/draft/decision`, { method: "POST", json: { decision: "approve", acknowledged: data.acknowledged, note: note.value || null } });
                toast("Approved");
                setTimeout(load, 1200);
              }) }, "Approve"),
              button("Request changes", async () => {
                if (!note.value.trim()) throw new Error("Write a note describing the changes first");
                await api(`/tasks/${id}/draft/decision`, { method: "POST", json: { decision: "reject", note: note.value } });
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
      const select = h("select", { class: "select" }, h("option", { value: "", text: "Share to workspace" }), others.map((w) => h("option", { value: w.id, text: w.title })));
      select.addEventListener("change", () => {
        if (!select.value) return;
        guarded(select, async () => {
          await api(`/files/${x.final_file_id}/share`, { method: "POST", json: { workspace: select.value } });
          toast("Shared");
        }).finally(() => { select.value = ""; });
      });
      return select;
    }

    function downgradeControl(x) {
      const level = h("select", { class: "select" }, ["Unclassified", "Restricted", "Confidential"].map((l) => h("option", { text: l })));
      const reason = h("input", { class: "input", placeholder: "Reason for a lower classification" });
      return h("div", { class: "stack" }, level, reason, button("Request lower classification", async () => {
        await api(`/files/${x.final_file_id}/downgrade`, { method: "POST", json: { level: level.value, reason: reason.value } });
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
        return h("details", { class: "model" },
          h("summary", {},
            h("span", { class: `state-dot ${state[1]}` }),
            h("span", { class: "model-main" }, h("span", { class: "strong", text: x.name }), h("span", { class: "muted small", text: x.serves.map((s) => SERVES[s] || s).join(", ") })),
            h("span", { class: "muted small", text: state[0] }),
            icon("chevron")),
          h("div", { class: "model-body" },
            routes.length ? h("table", { class: "table" }, h("tbody", {}, routes.map(([r, q]) => h("tr", {}, h("td", { text: SERVES[r] || r }), h("td", {}, h("span", { class: "bar" }, h("i", { style: `width:${q * 100}%` }))), h("td", { class: "mono", text: q.toFixed(2) })))))
              : h("p", { class: "muted small", text: "No scores yet." }),
            h("p", { class: "muted small", text: `${x.provenance.developer} · ${x.provenance.licence} · ${x.max_context.toLocaleString()} tokens · ${x.latency.step_s}s per step · used in ${plural(m.tasks_served[x.name] || 0, "task")}` }),
            x.serve_argv.length ? h("details", {}, h("summary", { text: "Start command" }), h("pre", { class: "code", text: x.serve_argv.join(" ") })) : null,
            admin && x.status === "shadow" ? h("div", { class: "row" },
              button("Evaluate", async () => { await api(`/models/${x.name}/shadow-eval`, { method: "POST" }); toast("Evaluation finished"); render(); }),
              button("Make active", async () => { await api(`/models/${x.name}/promote`, { method: "POST", json: { confirm: true } }); toast(`${x.name} is active`); render(); }, "primary")) : null));
      }));
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
    setInterval(() => { if (!$$("#model-list details[open]").length) render(); }, 8000);
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
      fill(box, ...[
        h("div", { class: `status-card ${leaked ? "bad" : "good"}` }, icon(leaked ? "alert" : "check"),
          h("div", {}, h("strong", { text: leaked ? "Outbound connections were detected" : "Nothing has left this server" }),
            h("div", { class: "small", text: `${snap.external_connections} outbound · ${plural(blocked, "attempt")} blocked · counting since ${fmtTime(snap.since)}` })),
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

  const PAGES = { home: pageHome, task: pageTask, review: pageReview, models: pageModels, security: pageSecurity };

  document.addEventListener("DOMContentLoaded", async () => {
    try {
      await initShell();
      const page = PAGES[document.body.dataset.page];
      if (page) await page();
    } catch (e) {
      toast(e.message || String(e), true);
      console.error(e);
    }
  });
})();
