/* Sovereign Workbench UI: vanilla JS, no build step, no external requests.
   All user data is inserted with textContent; nothing from the API is parsed as HTML. */
(() => {
  "use strict";

  // ---------------------------------------------------------------------------------------------
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
        // CSSOM writes are allowed under the page's Content-Security-Policy; style attributes are not.
        String(value).split(";").forEach((decl) => {
          const at = decl.indexOf(":");
          if (at > 0) el.style.setProperty(decl.slice(0, at).trim(), decl.slice(at + 1).trim());
        });
      }
      else if (value === true) el.setAttribute(key, "");
      else el.setAttribute(key, String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  const ICONS = {
    check: "M4 10.5l4 4 8-9",
    cross: "M5 5l10 10M15 5L5 15",
    route: "M4 16c0-6 12-6 12-12M13 4h3v3",
    plan: "M5 5h10M5 10h10M5 15h6",
    play: "M6 4l10 6-10 6z",
    shield: "M10 2l6 3v5c0 4-3 7-6 8-3-1-6-4-6-8V5z",
    doc: "M6 2h6l4 4v12H6zM12 2v4h4",
    alert: "M10 3l8 14H2zM10 8v4M10 14.5v.5",
    user: "M10 10a3 3 0 100-6 3 3 0 000 6zM4 17c1-3 3-4 6-4s5 1 6 4",
    spark: "M10 2v4M10 14v4M2 10h4M14 10h4M5 5l2.5 2.5M12.5 12.5L15 15M5 15l2.5-2.5M12.5 7.5L15 5",
    list: "M7 5h9M7 10h9M7 15h9M4 5h.01M4 10h.01M4 15h.01",
    up: "M10 15V5M6 9l4-4 4 4",
    down: "M10 5v10M6 11l4 4 4-4",
    trash: "M4 6h12M8 6V4h4v2M6 6l1 10h6l1-10",
    download: "M10 3v10M6 9l4 4 4-4M4 16h12",
    share: "M14 6l-8 4 8 4M15 7a2 2 0 100-4 2 2 0 000 4zM15 17a2 2 0 100-4 2 2 0 000 4zM5 12a2 2 0 100-4 2 2 0 000 4z",
    clock: "M10 18a8 8 0 100-16 8 8 0 000 16zM10 6v4l3 2",
    code: "M7 6l-4 4 4 4M13 6l4 4-4 4",
    answer: "M4 4h12v9H9l-4 3v-3H4z",
  };

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", ICONS[name] || ICONS.spark);
    svg.append(path);
    return svg;
  }

  const fmtTime = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  };
  const ago = (iso) => {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return d.toLocaleDateString();
  };
  const bytes = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
  const pct = (x) => (x === null || x === undefined ? "n/a" : `${Math.round(x * 100)}%`);
  const statusText = (s) => String(s || "").replace(/_/g, " ");
  const TERMINAL = new Set(["completed", "failed", "handed_back", "rejected", "cancelled"]);

  function badge(status, text) {
    return h("span", { class: `badge st-${status}`, text: text || statusText(status) });
  }

  function levelOf(label) {
    if (!label) return "unclassified";
    if (typeof label === "string") return label.split(/[ ·+]/)[0].toLowerCase();
    return String(label.level || "").toLowerCase();
  }

  function labelChip(label, display) {
    const text = display || (typeof label === "string" ? label : [label.level, ...(label.compartments || [])].join(" · "));
    return h("span", { class: `label-chip lv-${levelOf(label)}`, text });
  }

  function toast(message, bad = false) {
    const el = h("div", { class: `toast${bad ? " bad" : ""}`, text: message });
    $("#toasts").append(el);
    setTimeout(() => el.remove(), bad ? 7000 : 3500);
  }

  // ---------------------------------------------------------------------------------------------
  // API

  const store = {
    get(key, fallback) { try { return localStorage.getItem(key) || fallback; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  let USER = store.get("wb_user", "engineer1");
  document.cookie = `wb_user=${encodeURIComponent(USER)}; path=/; SameSite=Strict`;

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
    try {
      return await fn();
    } catch (e) {
      toast(e.message, true);
      return undefined;
    } finally {
      if (button) button.disabled = false;
    }
  }

  // ---------------------------------------------------------------------------------------------
  // Shell

  const shell = { workspaces: [], ws: null, me: null };

  function setBanner(label, text) {
    const banner = $("#banner");
    banner.style.setProperty("--lv", `var(--lv-${levelOf(label)})`);
    $("#banner-text").textContent = text;
  }

  async function initShell() {
    const page = document.body.dataset.page;
    $$(`[data-nav]`).forEach((a) => a.classList.toggle("active", a.dataset.nav === page));
    $("#menu-btn").addEventListener("click", () => $("#side").classList.toggle("open"));
    $("#new-task").addEventListener("click", () => {
      if (page === "home") $("#task-text").focus();
      else location.href = "/";
    });

    const users = await api("/users");
    const userSel = $("#user-select");
    users.forEach((u) => userSel.append(h("option", { value: u.id, text: `${u.name} (${u.id})` })));
    if (!users.some((u) => u.id === USER)) USER = users[0].id;
    userSel.value = USER;
    userSel.addEventListener("change", () => {
      store.set("wb_user", userSel.value);
      document.cookie = `wb_user=${encodeURIComponent(userSel.value)}; path=/; SameSite=Strict`;
      location.reload();
    });
    shell.me = await api("/me");

    shell.workspaces = await api("/workspaces");
    const wsSel = $("#ws-select");
    shell.workspaces.forEach((w) => wsSel.append(h("option", { value: w.id, text: w.title })));
    const wanted = new URLSearchParams(location.search).get("ws") || store.get("wb_ws", "");
    shell.ws = shell.workspaces.find((w) => w.id === wanted) || shell.workspaces[0] || null;
    if (shell.ws) wsSel.value = shell.ws.id;
    wsSel.addEventListener("change", () => {
      store.set("wb_ws", wsSel.value);
      location.href = `/?ws=${encodeURIComponent(wsSel.value)}`;
    });
    if (shell.ws) {
      store.set("wb_ws", shell.ws.id);
      setBanner(shell.ws.ceiling, `${shell.ws.title} · ceiling ${shell.ws.ceiling_display} · your access ${shell.ws.retrieval_ceiling}`);
    } else {
      setBanner("Unclassified", "No workspace access for this user");
    }
    refreshRecent();
    setInterval(refreshRecent, 6000);
    initEgress();
  }

  async function refreshRecent() {
    if (!shell.ws) return;
    let tasks = [];
    try { tasks = await api(`/tasks?workspace=${encodeURIComponent(shell.ws.id)}`); } catch { return; }
    const list = $("#recent");
    const current = document.body.dataset.task;
    list.replaceChildren(...tasks.slice(0, 14).map((t) => h("li", {},
      h("a", { href: `/t/${t.id}`, class: t.id === current ? "active" : "", title: t.text },
        h("span", { class: `dot st-${t.status}` }), h("span", { class: "title", text: t.text })))));
    if (!tasks.length) list.append(h("li", { class: "muted small", text: "No tasks yet" }));
  }

  async function initEgress() {
    const pill = $("#egress-pill");
    const pop = $("#egress-pop");
    pill.addEventListener("click", () => {
      pop.hidden = !pop.hidden;
      if (!pop.hidden) renderEgressPop();
    });
    document.addEventListener("click", (e) => {
      if (!pop.hidden && !$("#egress").contains(e.target)) pop.hidden = true;
    });
    const tick = async () => {
      try {
        const snap = await api("/egress");
        shell.egress = snap;
        const dot = $("#egress-dot");
        if (snap.status !== "ok") {
          dot.className = "pulse off";
          $("#egress-text").textContent = "Egress monitor offline";
        } else {
          const blocked = (snap.blocked_connect_host || 0) + (snap.blocked_connect_sandbox || 0);
          dot.className = `pulse ${snap.breach || snap.external_connections ? "bad" : "ok"}`;
          $("#egress-text").textContent = `${snap.external_connections} external · ${blocked} blocked`;
        }
      } catch { /* shown on next tick */ }
    };
    tick();
    setInterval(tick, 8000);
  }

  function egressCounters(snap) {
    const packets = snap.blocked_packets === null || snap.blocked_packets === undefined ? "n/a" : snap.blocked_packets;
    return h("div", { class: "counter-grid" },
      h("div", { class: "counter" }, h("b", { text: snap.external_connections }), h("span", { text: "External connections" })),
      h("div", { class: "counter" }, h("b", { text: packets }), h("span", { text: "Blocked packets (nftables)" })),
      h("div", { class: "counter" }, h("b", { text: snap.blocked_connect_host }), h("span", { text: "Blocked connect(), host" })),
      h("div", { class: "counter" }, h("b", { text: snap.blocked_connect_sandbox }), h("span", { text: "Blocked connect(), sandbox" })));
  }

  function renderEgressPop() {
    const pop = $("#egress-pop");
    const snap = shell.egress;
    if (!snap || snap.status !== "ok") {
      pop.replaceChildren(h("strong", { text: "Monitor offline" }),
        h("p", { class: "muted", text: "egressd is not reachable. Start it with the Go services (see the README)." }));
      return;
    }
    const btn = h("button", { class: "btn btn-primary btn-sm", type: "button", text: "Run egress test" });
    btn.addEventListener("click", () => guarded(btn, async () => {
      const res = await api("/egress/test", { method: "POST" });
      toast(res.pass ? "Egress test passed: every attempt was blocked and counted" : "Egress test reported a failure", !res.pass);
      shell.egress = await api("/egress");
      renderEgressPop();
    }));
    pop.replaceChildren(
      h("div", { class: "card-head" }, h("strong", { text: "Sovereignty proof" }), badge(snap.breach ? "failed" : "completed", snap.breach ? "breach" : `mode ${snap.mode}`)),
      egressCounters(snap),
      h("p", { class: "muted small", text: `Counting since ${fmtTime(snap.since)}. Collectors: ${Object.entries(snap.collectors || {}).map(([k, v]) => `${k} ${v}`).join(", ")}` }),
      h("div", { class: "actions" }, btn, h("a", { class: "btn btn-ghost btn-sm", href: "/security", text: "Details" })));
  }

  // ---------------------------------------------------------------------------------------------
  // Record drawer

  function openDrawer(title, body) {
    const drawer = $("#drawer");
    $("#drawer-title").textContent = title;
    $("#drawer-body").replaceChildren(...[].concat(body));
    drawer.hidden = false;
    $$("[data-close]", drawer).forEach((el) => { el.onclick = () => { drawer.hidden = true; }; });
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#drawer").hidden = true; });

  function recLink(id, taskId) {
    return h("button", { class: "reclink", type: "button", text: id, onclick: (e) => { e.stopPropagation(); showRecord(id, taskId); } });
  }

  async function showRecord(id, taskId) {
    openDrawer(id, h("div", { class: "skeleton skeleton-title" }));
    let rec;
    try { rec = await api(`/records/${encodeURIComponent(id)}`); } catch (e) { openDrawer(id, h("p", { class: "muted", text: e.message })); return; }
    const task = taskId || rec.id.split("-")[1];
    const body = typeof rec.body === "string" ? rec.body : JSON.stringify(rec.body, null, 2);
    const parts = [
      h("div", { class: "meta-row" }, h("span", { class: "kind", text: rec.kind }),
        h("span", { class: rec.trust === "control" ? "trust-control" : "muted", text: rec.trust }),
        labelChip(rec.label), rec.confidence ? badge(rec.confidence === "uncertain" ? "not_checked" : rec.confidence === "high" ? "pass" : "derived", `${rec.confidence} confidence`) : null),
      h("dl", { class: "kv" },
        h("dt", { text: "Source" }), h("dd", { text: rec.source }),
        h("dt", { text: "Produced by" }), h("dd", { text: rec.produced_by || "" }),
        h("dt", { text: "Created" }), h("dd", { text: fmtTime(rec.created_at) }),
        h("dt", { text: "Hash" }), h("dd", { class: "mono small", text: rec.hash.slice(0, 32) + "…" })),
    ];
    if (rec.body && rec.body.crop) {
      parts.push(h("div", { class: "crop-pair" },
        h("strong", { text: "Scan region" }),
        h("img", { class: "crop", alt: "cropped region", src: `/api/tasks/${task}/evidence/${rec.body.crop}` }),
        rec.body.zoomed_crop ? h("img", { class: "crop", alt: "zoomed re-read", src: `/api/tasks/${task}/evidence/${rec.body.zoomed_crop}` }) : null));
    }
    parts.push(h("h3", { text: "Body" }), h("pre", { class: "code", text: body.length > 12000 ? body.slice(0, 12000) + "\n…" : body }));
    const fields = Object.entries(rec.fields || {});
    if (fields.length) {
      parts.push(h("h3", { text: "Typed fields" }), h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", { text: "Field" }), h("th", { text: "Raw" }), h("th", { text: "Normalised" }), h("th", { text: "Confidence" }))),
        h("tbody", {}, fields.slice(0, 40).map(([k, v]) => h("tr", {}, h("td", { class: "mono", text: k }), h("td", { text: v.raw }), h("td", { text: v.normalised }), h("td", { text: v.confidence })))))));
    }
    if (rec.chain && rec.chain.length > 1) {
      parts.push(h("h3", { text: "Derivation chain" }), h("ul", { class: "chain" },
        rec.chain.map((c) => h("li", {}, recLink(c.id, task), h("span", { class: "kind", text: c.kind }), h("span", { class: "muted small", text: c.summary.slice(0, 90) })))));
    }
    openDrawer(`${rec.id} · ${rec.kind}`, parts);
  }

  // ---------------------------------------------------------------------------------------------
  // Home

  async function pageHome() {
    const ws = shell.ws;
    if (!ws) {
      $("#content").replaceChildren(h("div", { class: "card empty", text: "This user has no workspace access." }));
      return;
    }
    $("#hero-ws").textContent = ws.title;
    const attached = new Set();
    let files = [];
    let area = "inputs";

    const renderAttached = () => {
      $("#attached").replaceChildren(...[...attached].map((p) => h("span", { class: "chip" }, p.replace(/^inputs\//, ""),
        h("button", { type: "button", "aria-label": "Remove", onclick: () => { attached.delete(p); renderAttached(); renderPicker(); } }, icon("cross")))));
    };
    const renderPicker = () => {
      const inputs = files.filter((f) => f.area === "inputs");
      $("#picker-list").replaceChildren(...inputs.map((f) => h("li", {}, h("label", {},
        h("input", { type: "checkbox", checked: attached.has(f.path), onchange: (e) => { e.target.checked ? attached.add(f.path) : attached.delete(f.path); renderAttached(); } }),
        h("span", { class: "name", text: f.name }), labelChip(f.label, f.label_display), h("span", { class: "muted small", text: bytes(f.size) })))));
      if (!inputs.length) $("#picker-list").append(h("li", { class: "muted small", text: "No inputs yet. Upload a file." }));
    };
    const renderFiles = () => {
      const rows = files.filter((f) => f.area === area);
      const wrap = $("#file-table");
      if (!rows.length) { wrap.replaceChildren(h("div", { class: "empty", text: `No files in ${area}/` })); return; }
      const others = shell.workspaces.filter((w) => w.id !== ws.id);
      wrap.replaceChildren(h("table", {},
        h("thead", {}, h("tr", {}, h("th", { text: "Name" }), h("th", { text: "Label" }), h("th", { text: "Size" }), h("th", { text: "" }))),
        h("tbody", {}, rows.map((f) => h("tr", {},
          h("td", {}, h("div", { text: f.name }), f.task_id ? h("a", { class: "small", href: `/t/${f.task_id}`, text: `from ${f.task_id}` }) : null),
          h("td", {}, labelChip(f.label, f.label_display)),
          h("td", { class: "num muted", text: bytes(f.size) }),
          h("td", {}, h("div", { class: "row-actions" },
            h("a", { class: "btn btn-ghost btn-sm", href: fileUrl(f.id), title: "Download" }, icon("download")),
            area !== "inputs" && others.length ? shareControl(f, others) : null)))))));
    };
    const loadFiles = async () => {
      files = await api(`/workspaces/${ws.id}/files`);
      renderFiles();
      renderPicker();
      renderSuggestions();
    };
    const renderSuggestions = () => {
      const has = (re) => files.find((f) => f.area === "inputs" && re.test(f.name));
      const ideas = [];
      const scan = has(/^inspection_P108B/);
      if (scan) ideas.push(["Draft an approval note for this inspection report", [scan.path]]);
      const csv = has(/pressure.*\.csv$/);
      if (csv) ideas.push(["Write a Python script to parse these pressure readings and flag anomalies", [csv.path]]);
      const contract = has(/contract.*\.pdf$/);
      if (contract) ideas.push(["Summarise this vendor contract", [contract.path]]);
      const pipe = has(/pipe_data/);
      if (pipe) ideas.push(["Compute the required wall thickness for this pipe per the attached data", [pipe.path]]);
      const offers = files.filter((f) => f.area === "inputs" && /^(offer_|tender)/.test(f.name));
      if (offers.length >= 2) ideas.push(["Compare these three vendor offers against the tender conditions and recommend one", offers.map((f) => f.path)]);
      const notes = has(/board_notes/);
      if (notes) ideas.push(["Turn these notes into a board deck", [notes.path]]);
      ideas.push(["Which pumps are governed by SOP-MECH-014?", []]);
      $("#suggestions").replaceChildren(...ideas.map(([text, paths]) => h("button", {
        class: "suggestion", type: "button", text,
        onclick: () => { $("#task-text").value = text; attached.clear(); paths.forEach((p) => attached.add(p)); renderAttached(); renderPicker(); $("#task-text").focus(); },
      })));
    };

    $$("#file-tabs button").forEach((b) => b.addEventListener("click", () => {
      area = b.dataset.area;
      $$("#file-tabs button").forEach((x) => x.classList.toggle("active", x === b));
      renderFiles();
    }));
    $("#attach-btn").addEventListener("click", () => { $("#picker").hidden = !$("#picker").hidden; });
    $("#upload-input").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      const level = $("#upload-level").value;
      if (level) form.append("level", level);
      await guarded(null, async () => {
        const rec = await api(`/workspaces/${ws.id}/files`, { method: "POST", body: form });
        toast(`Uploaded ${rec.name} as ${rec.label_display}`);
        attached.add(rec.path);
        renderAttached();
        await loadFiles();
      });
      e.target.value = "";
    });
    $("#task-text").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("#composer").requestSubmit();
    });
    $("#composer").addEventListener("submit", (e) => {
      e.preventDefault();
      const text = $("#task-text").value.trim();
      if (!text) return;
      guarded($("#submit-task"), async () => {
        const meta = $("#no-template").checked ? { templates_disabled: true } : {};
        const task = await api("/tasks", { method: "POST", json: { workspace: ws.id, text, attachments: [...attached], meta } });
        location.href = `/t/${task.id}`;
      });
    });

    const renderActivity = async () => {
      const [tasks, jobs] = await Promise.all([api(`/tasks?workspace=${ws.id}`), api("/jobs")]);
      const waiting = jobs.filter((j) => j.state === "queued").length;
      const running = jobs.filter((j) => j.state === "running").length;
      $("#queue-note").textContent = `${running} running · ${waiting} queued`;
      const list = $("#activity");
      if (!tasks.length) { list.replaceChildren(h("li", { class: "empty", text: "Tasks you start appear here." })); return; }
      list.replaceChildren(...tasks.slice(0, 10).map((t) => h("li", {}, h("a", { href: `/t/${t.id}` },
        h("span", { class: `dot st-${t.status}` }),
        h("div", { style: "min-width:0" }, h("div", { class: "title", text: t.text }), h("div", { class: "sub", text: `${t.id} · ${t.user} · ${ago(t.created_at)}${t.model ? " · " + t.model : ""}` })),
        h("div", { class: "meta-row" }, labelChip(t.label, t.label_display), badge(t.status))))));
    };
    await loadFiles();
    await renderActivity();
    setInterval(renderActivity, 5000);
    if (new URLSearchParams(location.search).get("focus")) $("#task-text").focus();
  }

  function shareControl(file, workspaces) {
    const select = h("select", { class: "select select-sm", "aria-label": "Share to workspace" },
      h("option", { value: "", text: "Share to…" }), workspaces.map((w) => h("option", { value: w.id, text: `${w.title} (${w.ceiling_display})` })));
    select.addEventListener("change", () => {
      const target = select.value;
      if (!target) return;
      guarded(select, async () => {
        await api(`/files/${file.id}/share`, { method: "POST", json: { workspace: target } });
        toast(`Shared ${file.name} to ${target}`);
      }).finally(() => { select.value = ""; });
    });
    return select;
  }

  // ---------------------------------------------------------------------------------------------
  // Task page

  const KIND_ICON = { route: "route", plan: "plan", decide: "spark", tool: "play", model: "spark", default: "alert", gate: "shield", escalation: "up", info: "list", error: "alert", sandbox: "code", check: "check", finish: "check" };

  async function pageTask() {
    const id = document.body.dataset.task;
    const view = { task: null, editing: null, tab: "evidence", ledger: [], checks: null, filter: "all", lastUpdate: "" };

    const load = async () => {
      let task;
      try { task = await api(`/tasks/${id}`); } catch (e) {
        $("#task-head").replaceChildren(h("h1", { text: "Task unavailable" }), h("p", { class: "muted", text: e.message }));
        return false;
      }
      const changed = task.updated_at !== view.lastUpdate;
      view.task = task;
      view.lastUpdate = task.updated_at;
      if (changed) {
        renderHead(task);
        if (!view.editing) renderTimeline(task);
        view.ledger = await api(`/tasks/${id}/ledger`).catch(() => []);
        if (task.checks && task.checks.length) view.checks = await api(`/tasks/${id}/checks`).catch(() => null);
        renderInspector();
        setBanner(task.label, `${task.marking} · task ${task.id} inherits the highest source marking`);
        $("#followup").hidden = !(task.status === "completed" && task.attachments.length);
      }
      return !TERMINAL.has(task.status);
    };

    const poll = async () => {
      const again = await load();
      const t = view.task;
      if (!t) return;
      const waiting = t.pending_gate || t.status === "waiting_tide";
      if (again) setTimeout(poll, waiting ? 2500 : 1000);
    };

    function renderHead(t) {
      const job = t.job;
      const cancel = job && ["queued", "running"].includes(job.state) && t.is_owner
        ? h("button", { class: "btn btn-danger btn-sm", type: "button", text: "Cancel", onclick: (e) => guarded(e.target, async () => { await api(`/jobs/${job.id}`, { method: "DELETE" }); toast("Cancellation requested"); }) })
        : null;
      const done = t.plan ? t.plan.steps.filter((s) => ["done", "incomplete", "denied"].includes(s.status)).length : 0;
      const total = t.plan ? t.plan.steps.length : 0;
      $("#task-head").replaceChildren(
        h("div", { class: "meta-row" }, h("span", { class: "mono", text: t.id }), h("span", { text: "·" }), h("span", { text: t.workspace }), h("span", { text: "·" }), h("span", { text: `${t.user}, ${fmtTime(t.created_at)}` })),
        h("h1", { text: t.text }),
        h("div", { class: "meta-row" }, badge(t.status), labelChip(t.label, t.label_display),
          t.model ? h("span", { class: "pill model", text: t.model }) : null,
          job && job.state === "queued" ? h("span", { class: "muted", text: `queue position ${job.position}, about ${job.eta_s}s` }) : null,
          t.status_note ? h("span", { class: "muted", text: t.status_note }) : null, h("span", { class: "grow" }), cancel),
        total ? h("div", { class: "progress", title: `${done} of ${total} steps` }, h("i", { style: `width:${Math.round((done / total) * 100)}%` })) : null);
    }

    function tl(iconName, tone, card) {
      return h("div", { class: "tl" }, h("div", { class: `tl-icon ${tone || ""}` }, icon(iconName)), card);
    }

    function renderTimeline(t) {
      const items = [];
      items.push(tl("user", "accent", h("section", { class: "card" },
        h("p", { class: "request-text", text: t.text }),
        t.attachments.length ? h("div", { class: "chips" }, t.attachments.map((a) => h("span", { class: "chip", text: a.replace(/^inputs\//, "") }))) : null)));
      if (t.route) items.push(tl("route", "", routeCard(t)));
      const gate = t.pending_gate;
      if (gate && gate.kind === "template_choice") items.push(tl("alert", "warn", templateChoiceCard(t, gate)));
      if (t.plan) items.push(tl("plan", gate && gate.kind === "plan" ? "warn" : "", planCard(t, gate && gate.kind === "plan" ? gate : null)));
      if (gate && gate.kind === "action") items.push(tl("shield", "warn", actionCard(t, gate)));
      if (t.status === "waiting_tide") items.push(tl("clock", "warn", h("section", { class: "card attention" }, h("h2", { text: "Waiting for the reasoning model" }), h("p", { class: "muted", text: t.status_note || "The swap-slot model wakes at the next tide." }))));
      const exec = t.trace.filter((r) => !["route", "plan"].includes(r.kind));
      if (exec.length) items.push(tl("play", "", traceCard(t, exec)));
      if (t.checks && t.checks.length) items.push(tl("check", t.checks.some((c) => c.status === "mismatch") ? "bad" : "good", checksSummary(t)));
      if (t.deliverables && t.deliverables.length) items.push(tl("doc", gate && gate.kind === "deliverable" ? "warn" : "good", deliverablesCard(t, gate && gate.kind === "deliverable" ? gate : null)));
      const result = resultCard(t);
      if (result) items.push(result);
      $("#timeline").replaceChildren(...items);
    }

    function routeCard(t) {
      const r = t.route;
      const p = r.profile;
      const chosen = r.candidates.find((c) => c.model === r.chosen) || {};
      return h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Routed to ", h("span", { class: "pill model", text: r.chosen })),
          h("span", { class: "muted small", text: `threshold ${r.threshold.toFixed(2)}${r.below_threshold ? " · below threshold" : ""}${r.queued_for_tide ? " · queued for tide" : ""}` })),
        h("div", { class: "meta-row" },
          h("span", { class: "pill", text: `route ${p.task_type}` }), p.rule ? h("span", { class: "pill", text: `rule ${p.rule}` }) : h("span", { class: "pill", text: "classifier" }),
          h("span", { class: "pill", text: `modality ${p.modalities.join("+")}${p.decoupled ? " (decoupled)" : ""}` }),
          h("span", { class: "pill", text: `≈${Math.round(p.est_input_tokens / 1000)}k tokens` }),
          h("span", { class: "pill", text: `lang ${p.languages.join(",")}` }), h("span", { class: "pill", text: `complexity ${p.complexity}` })),
        chosen.quality !== undefined && chosen.quality !== null ? h("p", { class: "muted small", text: `Quality ${chosen.quality.toFixed(2)} on this route, expected step cost ${chosen.cost_s}s. See the Routing tab for every candidate.` }) : null);
    }

    function templateChoiceCard(t, gate) {
      return h("section", { class: "card attention" },
        h("h2", { text: "More than one template fits" }),
        h("p", { class: "muted", text: "Choose the plan template to use for this request." }),
        h("div", { class: "actions" }, gate.payload.options.map((name) => h("button", {
          class: "btn btn-ghost", type: "button", text: name.replace(/_/g, " "),
          onclick: (e) => guarded(e.target, async () => { await api(`/tasks/${t.id}/template/decision`, { method: "POST", json: { template: name } }); poll(); }),
        }))));
    }

    function typedPlan(plan) {
      return {
        goal: plan.goal, deliverables: plan.deliverables,
        steps: plan.steps.map((s) => ({ id: s.id, title: s.title, action: s.tool ? { tool: s.tool } : { model_task: s.model_task },
          args: s.args, inputs: s.inputs.map((i) => ({ from: i.from || i.source, ref: i.ref })), output_type: s.output_type, side_effect: s.side_effect })),
      };
    }

    function planCard(t, gate) {
      const plan = t.plan;
      const editing = view.editing;
      const steps = editing ? editing.steps : plan.steps.map((s) => ({ ...s }));
      const header = h("div", { class: "card-head" },
        h("h2", {}, plan.source === "template" ? `Plan from template ${plan.template} v${plan.template_version}` : "Compiled plan"),
        h("span", { class: "muted small", text: `${plan.steps.length} steps · about ${Math.round(plan.est_time_s)}s${plan.repairs ? ` · ${plan.repairs} repair` : ""}` }));
      const list = h("ol", { class: "steps" }, steps.map((s, i) => h("li", { class: `step ${s.status || ""}` },
        h("span", { class: "step-n", text: i + 1 }),
        h("div", {},
          h("div", { class: "step-title", text: s.title || s.id }),
          h("div", { class: "step-sub" }, h("span", { class: "pill", text: s.tool || `model: ${s.model_task}` }),
            (s.inputs || []).filter((x) => (x.from || x.source) === "step").length ? h("span", { text: `uses ${(s.inputs || []).filter((x) => (x.from || x.source) === "step").map((x) => x.ref).join(", ")}` }) : null,
            s.side_effect ? h("span", { class: "badge st-awaiting_action plain", text: "needs approval" }) : null,
            s.note ? h("span", { text: s.note }) : null)),
        editing ? h("div", { class: "step-edit" },
          h("button", { class: "icon-btn", type: "button", "aria-label": "Move up", disabled: i === 0, onclick: () => { [steps[i - 1], steps[i]] = [steps[i], steps[i - 1]]; renderTimeline(t); } }, icon("up")),
          h("button", { class: "icon-btn", type: "button", "aria-label": "Move down", disabled: i === steps.length - 1, onclick: () => { [steps[i + 1], steps[i]] = [steps[i], steps[i + 1]]; renderTimeline(t); } }, icon("down")),
          h("button", { class: "icon-btn", type: "button", "aria-label": "Remove", onclick: () => { steps.splice(i, 1); renderTimeline(t); } }, icon("trash")))
          : badge(s.status || "pending"))));
      const parts = [header];
      if (plan.errors && plan.errors.length) parts.push(h("ul", { class: "errors" }, plan.errors.map((e) => h("li", { text: e }))));
      if (plan.notes && plan.notes.length) parts.push(h("ul", { class: "notes" }, plan.notes.map((e) => h("li", { text: e }))));
      if (t.plan_history && t.plan_history.length > 1) {
        parts.push(h("details", {}, h("summary", { class: "muted small", text: "Compiler and repair history" }),
          h("ul", { class: "notes" }, t.plan_history.map((x) => h("li", { text: `attempt ${x.attempt}: ${x.errors.length ? x.errors.join("; ") : "valid"}` })))));
      }
      parts.push(list);
      if (gate) {
        if (editing) {
          const json = h("textarea", { class: "mono", rows: 10 });
          json.value = JSON.stringify(typedPlan({ ...plan, steps }), null, 2);
          parts.push(h("details", {}, h("summary", { class: "muted small", text: "Advanced: edit the typed plan as JSON" }), json));
          parts.push(h("div", { class: "actions" },
            h("button", { class: "btn btn-primary", type: "button", text: "Submit edited plan", onclick: (e) => guarded(e.target, async () => {
              let typed;
              try { typed = JSON.parse(json.value); } catch { throw new Error("The JSON is not valid"); }
              if (json.parentElement && !json.parentElement.open) typed = typedPlan({ ...plan, steps });
              await api(`/tasks/${t.id}/plan/decision`, { method: "POST", json: { decision: "edit", plan: typed } });
              view.editing = null;
              toast("Edited plan sent to the compiler");
              poll();
            }) }),
            h("button", { class: "btn btn-ghost", type: "button", text: "Discard changes", onclick: () => { view.editing = null; renderTimeline(t); } })));
        } else {
          const note = h("input", { class: "input", placeholder: "Optional note for the audit log" });
          parts.push(h("div", { class: "actions" },
            h("button", { class: "btn btn-primary", type: "button", disabled: !plan.valid, onclick: (e) => guarded(e.target, async () => {
              await api(`/tasks/${t.id}/plan/decision`, { method: "POST", json: { decision: "approve", note: note.value || null } });
              toast("Plan approved"); poll();
            }) }, icon("check"), "Approve plan"),
            h("button", { class: "btn btn-ghost", type: "button", text: "Edit", onclick: () => { view.editing = { steps: plan.steps.map((s) => ({ ...s, inputs: s.inputs.map((i) => ({ from: i.from || i.source, ref: i.ref })) })) }; renderTimeline(t); } }),
            h("button", { class: "btn btn-danger", type: "button", text: "Reject", onclick: (e) => guarded(e.target, async () => {
              await api(`/tasks/${t.id}/plan/decision`, { method: "POST", json: { decision: "reject", note: note.value || null } });
              poll();
            }) }), h("span", { class: "grow" })), note);
        }
      }
      return h("section", { class: `card${gate ? " attention" : ""}` }, parts);
    }

    function actionCard(t, gate) {
      const p = gate.payload;
      const act = (approve) => (e) => guarded(e.target, async () => {
        await api(`/tasks/${t.id}/actions/${gate.id}/decision`, { method: "POST", json: { approve } });
        toast(approve ? "Action approved" : "Action denied"); poll();
      });
      return h("section", { class: "card attention" },
        h("div", { class: "card-head" }, h("h2", {}, "Approve ", h("span", { class: "pill", text: p.tool }), ` for step ${p.step}`), badge("awaiting_action", "side effect")),
        h("p", { class: "muted", text: "This action writes into drafts/. Nothing moves to final/ until the deliverable is approved." }),
        h("pre", { class: "code", text: JSON.stringify(p.args, null, 2) }),
        h("div", { class: "actions" },
          h("button", { class: "btn btn-primary", type: "button", onclick: act(true) }, icon("check"), "Approve"),
          h("button", { class: "btn btn-danger", type: "button", text: "Deny", onclick: act(false) })));
    }

    function traceCard(t, rows) {
      const lastN = rows.length > 60 ? rows.slice(-60) : rows;
      return h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", { text: "Live trace" }), h("span", { class: "muted small", text: `${rows.length} events · ${t.template_fallbacks} fallback(s) · ${t.escalations.length} escalation(s)` })),
        h("div", { class: "trace" }, lastN.map((r) => h("div", { class: `trace-row${r.ok ? "" : " err"}${/TEMPLATE_DEFAULT|DENIED|escalat/.test(r.summary) ? " hl" : ""}` },
          h("span", { class: "n", text: r.n }),
          h("span", { class: "k", text: [r.kind, r.step_id].filter(Boolean).join(" · ") }),
          h("span", { class: "s" }, r.tool ? h("span", { class: "pill", text: r.tool }) : r.purpose ? h("span", { class: "pill", text: r.purpose }) : null, " ", r.summary,
            r.records && r.records.length ? h("span", {}, " ", r.records.slice(0, 4).map((x) => [recLink(x, t.id), " "]), r.records.length > 4 ? `+${r.records.length - 4}` : "") : null),
          h("span", { class: "t", text: r.latency_s ? `${r.latency_s.toFixed(2)}s` : "" })))));
    }

    function checksSummary(t) {
      const counts = {};
      t.checks.forEach((c) => { counts[c.status] = (counts[c.status] || 0) + 1; });
      const mismatches = t.checks.filter((c) => c.status === "mismatch");
      return h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", { text: "Consistency checks" }),
          h("div", { class: "meta-row" }, Object.entries(counts).map(([k, v]) => badge(k, `${v} ${statusText(k)}`)))),
        mismatches.length ? h("ul", { class: "blockers" }, mismatches.map((c) => h("li", {}, badge("mismatch", t.acknowledged.includes(c.record_id) ? "acknowledged" : "mismatch"),
          h("span", {}, h("strong", { text: c.description || c.rule }), `: ${c.left_value ?? ""}${c.right_value ? ` vs ${c.right_value}` : ""}`)))) : h("p", { class: "muted", text: "No mismatches." }),
        h("button", { class: "btn btn-ghost btn-sm", type: "button", text: "Open checks panel", onclick: () => switchTab("checks") }));
    }

    function deliverablesCard(t, gate) {
      const summary = t.draft_summary || {};
      const rows = t.deliverables.map((d) => {
        const ext = d.relpath.split(".").pop();
        const c = d.provenance.counts || {};
        const verified = d.claims.filter((x) => x.status === "verified").length;
        return h("div", { class: "deliverable" },
          h("span", { class: `file-ico ${ext}`, text: ext }),
          h("div", {}, h("div", { class: "step-title", text: d.relpath }),
            h("div", { class: "step-sub" }, labelChip(d.label), (c.sourced || c.derived || c.unsourced) ? h("span", { text: `${c.sourced || 0} sourced · ${c.derived || 0} computed · ${c.unsourced || 0} unsourced` }) : null,
              d.claims.length ? h("span", { text: `${verified}/${d.claims.length} claims verified` }) : null, d.status !== "draft" ? badge(d.status) : null)),
          h("div", { class: "row-actions" },
            h("a", { class: "btn btn-ghost btn-sm", href: fileUrl(d.final_file_id || d.file_id), title: "Download" }, icon("download"))));
      });
      const head = gate
        ? h("div", { class: "card-head" }, h("h2", { text: "Draft ready for review" }), badge("awaiting_deliverable", `${(summary.unacknowledged || []).length} to acknowledge · ${(summary.orphans || []).length} unsourced`))
        : h("div", { class: "card-head" }, h("h2", { text: t.status === "completed" ? "Approved deliverables" : "Deliverables" }), t.status === "completed" ? badge("completed", "in final/") : null);
      return h("section", { class: `card${gate ? " attention" : ""}` }, head, rows,
        h("div", { class: "actions" }, h("a", { class: `btn ${gate ? "btn-primary" : "btn-ghost"}`, href: `/t/${t.id}/review` }, icon("doc"), gate ? "Review and approve" : "Open review")));
    }

    function citedText(text) {
      const out = [];
      let last = 0;
      const re = /\[(R-[A-Za-z0-9]+-\d+(?:,\s*R-[A-Za-z0-9]+-\d+)*)\]/g;
      let m;
      while ((m = re.exec(text))) {
        out.push(text.slice(last, m.index));
        m[1].split(/,\s*/).forEach((rid) => out.push(h("button", { class: "reclink cite", type: "button", text: rid, onclick: () => showRecord(rid, id) })));
        last = m.index + m[0].length;
      }
      out.push(text.slice(last));
      return out;
    }

    function resultCard(t) {
      const parts = [];
      if (t.result && t.result.answer) {
        const claims = t.result.claims || [];
        const verified = claims.filter((c) => c.status === "verified").length;
        parts.push(tl("answer", t.result.answer.not_found ? "warn" : "good", h("section", { class: "card answer" },
          h("div", { class: "card-head" }, h("h2", { text: "Answer" }), claims.length ? badge(verified === claims.length ? "verified" : "unverified", `${verified}/${claims.length} citations verified`) : null),
          t.result.answer.answer.map((a) => h("p", {}, citedText(a.text))))));
      }
      if (t.result && t.result.code) {
        const c = t.result.code;
        parts.push(tl("code", "good", h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", { text: "Script passed in the sandbox" }), h("span", { class: "muted small", text: `${c.attempts} attempt(s)` })),
          h("pre", { class: "code", text: JSON.stringify(c.result, null, 2) }))));
      }
      if (["failed", "handed_back", "rejected", "cancelled"].includes(t.status)) {
        parts.push(tl("alert", "bad", h("section", { class: "card" }, h("h2", { text: t.status === "handed_back" ? "Handed back with the full trace" : `Task ${statusText(t.status)}` }),
          h("p", { class: "muted", text: t.error || t.status_note || "" }))));
      }
      if (t.status === "completed" && t.plan && t.plan.source === "model") {
        parts.push(tl("spark", "", h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Reuse this plan" }),
          h("button", { class: "btn btn-ghost btn-sm", type: "button", text: "Save as template draft", onclick: (e) => guarded(e.target, async () => {
            const r = await api("/templates/drafts", { method: "POST", json: { task_id: t.id } });
            toast(`Saved ${r.draft}; a reviewer approves it on the Security page`);
          }) })), h("p", { class: "muted small", text: "The compiled plan becomes a versioned template once a reviewer approves it." }))));
      }
      return parts.length ? parts : null;
    }

    // Inspector
    function switchTab(tab) {
      view.tab = tab;
      $$("#insp-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
      renderInspector();
    }
    $$("#insp-tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

    function renderInspector() {
      const body = $("#insp-body");
      const t = view.task;
      if (!t) return;
      if (view.tab === "evidence") {
        const kinds = ["all", ...new Set(view.ledger.map((r) => r.kind))];
        const rows = view.ledger.filter((r) => view.filter === "all" || r.kind === view.filter);
        body.replaceChildren(
          h("div", { class: "filter-row" }, kinds.map((k) => h("button", { type: "button", class: view.filter === k ? "active" : "", text: k === "all" ? `all ${view.ledger.length}` : k, onclick: () => { view.filter = k; renderInspector(); } }))),
          ...rows.map((r) => h("button", { class: "record", type: "button", onclick: () => showRecord(r.id, t.id) },
            h("span", { class: "r-top" }, h("span", { class: "mono", text: r.id }), h("span", { class: `kind${r.trust === "control" ? " trust-control" : ""}`, text: r.kind }), labelChip(r.label), r.confidence && r.confidence !== "high" ? badge(r.confidence === "uncertain" ? "not_checked" : "derived", r.confidence) : null),
            h("span", { class: "r-sum", text: r.summary }))));
        if (!rows.length) body.append(h("p", { class: "muted", text: "No records yet." }));
      } else if (view.tab === "routing") {
        if (!t.route) { body.replaceChildren(h("p", { class: "muted", text: "Not routed yet." })); return; }
        const r = t.route;
        body.replaceChildren(
          h("p", { class: "muted small", text: `Threshold ${r.threshold.toFixed(2)} for complexity ${r.profile.complexity} (classifier confidence ${r.profile.classifier_confidence.toFixed(2)}). Cheapest candidate that meets it wins.` }),
          ...r.candidates.map((c) => h("div", { class: `cand${c.model === r.chosen ? " chosen" : ""}` },
            h("span", { class: "mono", text: c.model }),
            c.ok ? h("span", { class: "muted small", text: c.quality === null ? "no quality" : `q ${c.quality.toFixed(2)} · ${c.cost_s}s` }) : badge("failed", `✗ ${c.reason}`),
            c.ok && c.quality !== null ? h("div", { class: "qbar" }, h("i", { style: `width:${c.quality * 100}%;${c.meets_threshold ? "" : "background:var(--warn)"}` }), h("b", { style: `left:${r.threshold * 100}%` })) : null,
            c.ok && c.wait_s ? h("span", { class: "why", text: `includes an expected tide wait of ${Math.round(c.wait_s)}s` }) : null)),
          h("h3", { text: "Log line" }), h("pre", { class: "logline mono small", text: r.log_line }),
          h("p", { class: "muted small", text: `Registry version ${r.registry_version}` }));
      } else {
        const data = view.checks;
        if (!data || !data.checks.length) { body.replaceChildren(h("p", { class: "muted", text: "No consistency checks for this task." })); return; }
        body.replaceChildren(...data.checks.map((c) => {
          const crop = c.extra && c.extra.crop ? h("img", { class: "crop", alt: "scan region", src: `/api/tasks/${t.id}/evidence/${c.extra.crop}` }) : null;
          return h("div", { class: `item${c.status === "mismatch" ? " mismatch" : ""}` },
            h("div", { class: "item-top" }, h("strong", { text: c.description || c.rule }), badge(c.status)),
            h("div", { class: "small" }, `${c.left_value ?? "n/a"}`, c.right_value ? ` vs ${c.right_value}` : ""),
            h("div", { class: "muted small", text: c.note }),
            crop,
            h("div", { class: "meta-row" }, c.left_record ? recLink(c.left_record, t.id) : null, c.right_record ? recLink(c.right_record, t.id) : null, recLink(c.record_id, t.id)));
        }));
      }
    }

    $("#followup").addEventListener("submit", (e) => {
      e.preventDefault();
      const text = $("#followup-text").value.trim();
      if (!text) return;
      guarded(e.submitter, async () => {
        const child = await api(`/tasks/${id}/followup`, { method: "POST", json: { text } });
        location.href = `/t/${child.id}`;
      });
    });

    await poll();
  }

  // ---------------------------------------------------------------------------------------------
  // Review page

  async function pageReview() {
    const id = document.body.dataset.task;
    let data;
    let current = 0;

    const load = async () => {
      try { data = await api(`/tasks/${id}/draft`); } catch (e) {
        $("#review-head").replaceChildren(h("h1", { text: "No draft to review" }), h("p", { class: "muted", text: e.message }), h("a", { href: `/t/${id}`, text: "Back to the task" }));
        return;
      }
      const t = data.task;
      setBanner(t.label, `${t.marking} · draft of ${t.id}`);
      $("#review-head").replaceChildren(
        h("a", { class: "small", href: `/t/${id}`, text: "← Back to the task" }),
        h("h1", { text: t.text }),
        h("div", { class: "meta-row" }, badge(t.status), labelChip(t.label, t.label_display), h("span", { class: "mono", text: t.id })));
      $("#doc-tabs").replaceChildren(...data.deliverables.map((d, i) => h("button", { type: "button", class: i === current ? "active" : "", text: d.name, onclick: () => { current = i; render(); } })));
      render();
    };

    const figureMap = (d) => {
      const map = {};
      (d.provenance.figures || []).forEach((f) => { (map[f.location] = map[f.location] || []).push(f); });
      return map;
    };
    const decisions = () => new Map(data.figure_decisions.map((f) => [f.figure, f]));

    function figSpan(f, d, text) {
      const key = `${d.file_id}:${f.id}`;
      const decided = decisions().get(key);
      const cls = `fig fig-${f.status}${decided ? " resolved" : ""}`;
      const title = f.status === "unsourced" ? (decided ? `Resolved: ${decided.action}` : "No evidence found for this figure") : `${f.status} from ${f.record_id || "formula"}`;
      return h("i", { class: cls, title, text, onclick: () => (f.record_id ? showRecord(f.record_id, id) : focusFigure(key)) });
    }

    function runsWithFigures(runs, figs, d) {
      const queue = [...(figs || [])];
      const out = [];
      runs.forEach((r) => {
        if (r.sup) {
          const refs = (r.refs || []).filter(Boolean);
          out.push(h("sup", { text: r.text, title: refs.join(", "), onclick: () => refs[0] && showRecord(refs[0], id) }));
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
          out.push(text.slice(0, best.at));
          out.push(figSpan(best.f, d, best.f.raw));
          text = text.slice(best.at + best.f.raw.length);
          queue.splice(queue.indexOf(best.f), 1);
        }
      });
      return out;
    }

    function renderDocx(d) {
      const figs = figureMap(d);
      const unverified = d.claims.filter((c) => c.status === "unverified").map((c) => c.sentence.replace(/\s*\[[^\]]+\]/g, "").slice(0, 36));
      const paper = h("article", { class: "paper" });
      let tableIndex = 0;
      d.preview.blocks.forEach((b) => {
        if (b.type === "marking") { paper.append(h("div", { class: "marking", text: b.text })); return; }
        if (b.type === "table") {
          tableIndex += 1;
          paper.append(h("table", {}, h("tbody", {}, b.rows.map((row, ri) => h("tr", {}, row.map((cell, ci) =>
            h("td", {}, runsWithFigures(cell, figs[`table ${tableIndex} row ${ri + 1} col ${ci + 1}`], d))))))));
          return;
        }
        const text = b.runs.map((r) => r.text).join("");
        const content = runsWithFigures(b.runs, figs[`paragraph ${b.index}`], d);
        const flagged = unverified.some((u) => u && text.includes(u));
        const inner = flagged ? h("i", { class: "claim-unverified", title: "This claim did not pass citation verification" }, content) : content;
        const style = b.style;
        if (style === "WB Marking") return;
        if (style === "Title" || style === "Heading 1") paper.append(h("h1", {}, inner));
        else if (style.startsWith("Heading")) paper.append(h("h2", {}, inner));
        else if (style === "WB Meta") paper.append(h("p", { class: "meta" }, inner));
        else if (style === "List Bullet") paper.append(h("p", { class: "bullet" }, inner));
        else if (style === "WB Reference") paper.append(h("p", { class: "ref" }, inner));
        else if (style === "WB Signature") paper.append(h("p", { class: "sig" }, inner));
        else paper.append(h("p", {}, inner));
      });
      paper.append(h("div", { class: "marking bottom", text: d.marking }));
      return paper;
    }

    function renderSheet(d) {
      const figs = {};
      (d.provenance.figures || []).forEach((f) => { figs[f.location] = f; });
      const tabs = h("div", { class: "tabs" });
      const holder = h("div", {});
      const show = (i) => {
        $$("button", tabs).forEach((b, j) => b.classList.toggle("active", i === j));
        const s = d.preview.sheets[i];
        holder.replaceChildren(h("div", { class: "sheet-view" }, h("table", {}, h("tbody", {}, s.rows.map((row, ri) => h("tr", {},
          h("td", { class: "rowhead", text: ri + 1 }),
          row.map((c) => {
            const f = figs[`${s.name}!${c.ref}`];
            const formula = typeof c.v === "string" && c.v.startsWith("=");
            const cls = formula ? "formula" : c.comment ? "sourced" : "";
            return h("td", { class: cls, title: c.comment || (formula ? "live formula" : f ? f.status : "") }, c.v === null ? "" : String(c.v));
          })))))));
      };
      d.preview.sheets.forEach((s, i) => tabs.append(h("button", { type: "button", text: s.name, onclick: () => show(i) })));
      show(0);
      return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: d.name }), tabs),
        h("p", { class: "muted small", text: "Green cells carry a source comment; blue cells are live formulas that recompute in Excel or LibreOffice." }), holder);
    }

    function renderOther(d) {
      const p = d.preview;
      if (p.svg !== undefined) {
        const img = h("img", { class: "svg-preview", alt: d.name, src: `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(p.svg)))}` });
        return h("div", { class: "card" }, h("h2", { text: d.name }), img);
      }
      if (p.slides) {
        return h("div", { class: "card" }, h("h2", { text: d.name }), p.slides.map((s, i) => h("div", { class: "item" }, h("strong", { text: `Slide ${i + 1}: ${s[0] || ""}` }), h("pre", { class: "code", text: s.slice(1).join("\n") }))));
      }
      return h("div", { class: "card" }, h("h2", { text: d.name }), h("pre", { class: "code", text: p.text || "(binary file)" }));
    }

    function render() {
      $$("#doc-tabs button").forEach((b, i) => b.classList.toggle("active", i === current));
      const d = data.deliverables[current];
      const t = d.preview.type;
      $("#doc-view").replaceChildren(t === "docx" ? renderDocx(d) : t === "xlsx" ? renderSheet(d) : renderOther(d));
      renderPanel();
    }

    function focusFigure(key) {
      const el = document.querySelector(`[data-fig="${CSS.escape(key)}"]`);
      if (el) { el.scrollIntoView({ block: "center" }); el.querySelector("input")?.focus(); }
    }

    function renderPanel() {
      const panel = $("#review-panel");
      const s = data.summary;
      const gate = data.pending_gate && data.pending_gate.kind === "deliverable" ? data.pending_gate : null;
      const t = data.task;
      const cards = [];
      if (t.status === "completed") {
        cards.push(h("div", { class: "approved-banner" }, icon("check"), "Approved and moved to final/"));
      }
      const blockers = data.blockers || [];
      cards.push(h("section", { class: "card" },
        h("h2", { text: "Approval checklist" }),
        h("ul", { class: "blockers" },
          h("li", {}, badge(s.unacknowledged.length ? "mismatch" : "pass", s.unacknowledged.length ? `${s.unacknowledged.length} open` : "done"), "Every mismatch acknowledged"),
          h("li", {}, badge(s.orphans.length ? "unsourced" : "pass", s.orphans.length ? `${s.orphans.length} open` : "done"), "Every unsourced figure resolved"),
          h("li", {}, badge(s.unverified_claims.length ? "derived" : "pass", s.unverified_claims.length ? `${s.unverified_claims.length} flagged` : "all verified"), "Citations verified against their records"))));

      const checks = data.checks.filter((c) => c.status !== "pass");
      if (checks.length) {
        cards.push(h("section", { class: "card" }, h("h2", { text: "Consistency findings" }), checks.map((c) => {
          const acked = data.acknowledged.includes(c.record_id);
          const box = h("input", { type: "checkbox", checked: acked, disabled: acked || !gate || !data.can_approve || c.status !== "mismatch" });
          box.addEventListener("change", () => guarded(box, async () => { await api(`/tasks/${id}/checks/${c.record_id}/acknowledge`, { method: "POST" }); await load(); }));
          return h("div", { class: `item${c.status === "mismatch" ? " mismatch" : ""}` },
            h("div", { class: "item-top" }, h("strong", { text: c.description || c.rule }), badge(c.status)),
            h("div", { class: "small", text: `${c.left_value ?? "n/a"}${c.right_value ? ` vs ${c.right_value}` : ""}` }),
            h("div", { class: "muted small", text: c.note }),
            c.status === "mismatch" ? h("label", { class: "check" }, box, acked ? "Acknowledged" : "I have read this finding") : null);
        })));
      }

      const d = data.deliverables[current];
      const orphans = (d.provenance.figures || []).filter((f) => f.status === "unsourced");
      if (orphans.length) {
        const dec = decisions();
        cards.push(h("section", { class: "card" }, h("h2", { text: `Unsourced figures in ${d.name}` }), orphans.map((f) => {
          const key = `${d.file_id}:${f.id}`;
          const done = dec.get(key);
          const input = h("input", { class: "input", placeholder: "R-… or corrected value" });
          const send = (action) => (e) => guarded(e.target, async () => {
            const body = { action, note: null };
            if (action === "link") body.record_id = input.value.trim();
            if (action === "correct") body.value = input.value.trim();
            await api(`/tasks/${id}/draft/figures/${encodeURIComponent(key)}`, { method: "POST", json: body });
            toast(`Figure ${f.raw}: ${action}`);
            await load();
          });
          return h("div", { class: "item", dataset: { fig: key } },
            h("div", { class: "item-top" }, h("strong", { text: f.raw }), done ? badge("pass", done.action) : badge("unsourced")),
            h("div", { class: "muted small", text: `${f.location}: ${f.context.slice(0, 120)}` }),
            !done && gate ? h("div", { class: "item-actions" }, input,
              h("button", { class: "btn btn-ghost btn-sm", type: "button", text: "Link", onclick: send("link") }),
              h("button", { class: "btn btn-ghost btn-sm", type: "button", text: "Correct", onclick: send("correct") }),
              h("button", { class: "btn btn-ghost btn-sm", type: "button", text: "Confirm", onclick: send("confirm") })) : null);
        })));
      }

      if (d.claims.length) {
        const bad = d.claims.filter((c) => c.status !== "verified" || c.currency_warning);
        cards.push(h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", { text: "Citations" }), badge(bad.length ? "derived" : "verified", `${d.claims.length - d.claims.filter((c) => c.status !== "verified").length}/${d.claims.length} verified`)),
          bad.length ? bad.map((c) => h("div", { class: "item" }, h("div", { class: "item-top" }, badge(c.status), c.currency_warning ? badge("not_checked", "currency") : null),
            h("div", { class: "small", text: c.sentence.replace(/\s*\[[^\]]+\]/g, "") }),
            c.reasons.length ? h("div", { class: "muted small", text: c.reasons.join("; ") }) : null,
            c.currency_warning ? h("div", { class: "muted small", text: c.currency_warning }) : null))
            : h("p", { class: "muted small", text: "Every cited sentence is supported by the record it cites." })));
      }

      if (gate) {
        const note = h("textarea", { rows: 2, placeholder: "Note (required to request a revision)" });
        cards.push(h("section", { class: "card attention" }, h("h2", { text: "Decision" }),
          data.can_approve ? [note, h("div", { class: "actions" },
            h("button", { class: "btn btn-primary", type: "button", disabled: blockers.length > 0, title: blockers.join("; "), onclick: (e) => guarded(e.target, async () => {
              await api(`/tasks/${id}/draft/decision`, { method: "POST", json: { decision: "approve", acknowledged: data.acknowledged, note: note.value || null } });
              toast("Approved. Moving files to final/");
              setTimeout(load, 1200);
            }) }, icon("check"), "Approve"),
            h("button", { class: "btn btn-danger", type: "button", text: "Request revision", onclick: (e) => guarded(e.target, async () => {
              await api(`/tasks/${id}/draft/decision`, { method: "POST", json: { decision: "reject", note: note.value || null } });
              toast("Revision requested"); setTimeout(() => { location.href = `/t/${id}`; }, 800);
            }) })), blockers.length ? h("p", { class: "muted small", text: `Approval unlocks when: ${blockers.join("; ")}.` }) : null]
            : h("p", { class: "muted", text: "Only a user with the approver role can decide." })));
      }

      if (t.status === "completed") {
        const finals = data.deliverables.filter((x) => x.final_file_id);
        const others = shell.workspaces.filter((w) => w.id !== t.workspace);
        cards.push(h("section", { class: "card" }, h("h2", { text: "Final files" }), finals.map((x) => {
          const level = h("select", { class: "select select-sm" }, ["Unclassified", "Restricted", "Confidential"].map((l) => h("option", { text: l })));
          const reason = h("input", { class: "input", placeholder: "Reason for downgrade" });
          return h("div", { class: "item" },
            h("div", { class: "item-top" }, h("strong", { text: x.name }), labelChip(x.label)),
            h("div", { class: "item-actions" }, h("a", { class: "btn btn-ghost btn-sm", href: fileUrl(x.final_file_id) }, icon("download"), "Download"),
              others.length ? shareControl({ id: x.final_file_id, name: x.name }, others) : null),
            h("details", {}, h("summary", { class: "muted small", text: "Request a downgrade" }),
              h("div", { class: "item-actions" }, level, reason, h("button", { class: "btn btn-ghost btn-sm", type: "button", text: "Request", onclick: (e) => guarded(e.target, async () => {
                await api(`/files/${x.final_file_id}/downgrade`, { method: "POST", json: { level: level.value, reason: reason.value } });
                toast("Downgrade requested; a second authorised person must approve it");
              }) }))));
        })));
      }
      panel.replaceChildren(...cards);
    }

    await load();
    setInterval(async () => {
      if (!data) return;
      const fresh = await api(`/tasks/${id}`).catch(() => null);
      if (fresh && fresh.updated_at !== data.task.updated_at) load();
    }, 3000);
  }

  // ---------------------------------------------------------------------------------------------
  // Models page

  async function pageModels() {
    const render = async () => {
      const m = await api("/models");
      $("#registry-version").textContent = `version ${m.registry_version} · profile ${m.profile} · backend ${m.backend}`;
      const pool = m.pool;
      $("#pool-stats").replaceChildren(
        h("div", { class: "stat" }, h("b", { text: pool.tide }), h("span", { text: "Current tide" })),
        h("div", { class: "stat" }, h("b", { text: pool.tide_count }), h("span", { text: "Tides turned" })),
        h("div", { class: "stat" }, h("b", { text: pool.swap_queue }), h("span", { text: "Jobs waiting for the swap slot" })),
        h("div", { class: "stat" }, h("b", { text: `${pool.running_resident} / ${pool.running_swap}` }), h("span", { text: "Running steps (resident / swap)" })));
      $("#tide-note").textContent = `max wait ${pool.policy.tide.max_wait_s}s · batch ${pool.policy.tide.max_queue} · min resident ${pool.policy.tide.min_resident_s}s · time scale ${pool.time_scale}`;
      $("#tide").replaceChildren(...m.models.filter((x) => x.status !== "retired").map((x) => h("div", { class: `slot ${x.state}` },
        h("span", { class: "model-name", text: x.name }),
        h("span", { class: "state" }, h("span", { class: `dot ${x.state === "awake" ? "st-completed" : x.state === "asleep" ? "st-running" : ""}` }), `${x.state} · ${x.pool_profile}`),
        h("span", { class: "muted small", text: x.pool_profile === "swap" ? "weights in host RAM while asleep" : x.pool_profile === "cold" ? "not loaded" : "always loaded" }))));
      const me = shell.me || { roles: [] };
      $("#model-grid").replaceChildren(...m.models.map((x) => {
        const routes = Object.entries(x.quality);
        const admin = me.roles.includes("admin");
        return h("article", { class: `model-card${x.status === "shadow" ? " shadow" : ""}` },
          h("div", { class: "model-top" }, h("div", {}, h("div", { class: "model-name", text: x.name }), h("div", { class: "muted small", text: x.weights || "" })), badge(x.status === "active" ? "completed" : x.status === "shadow" ? "awaiting_plan" : "failed", x.status)),
          h("div", { class: "meta-row" }, x.serves.map((s) => h("span", { class: "pill", text: s })), h("span", { class: "pill", text: x.modalities.join("+") })),
          routes.length ? h("div", {}, routes.map(([r, q]) => h("div", { class: "qrow" }, h("span", { class: "muted", text: r }), h("div", { class: "qbar" }, h("i", { style: `width:${q * 100}%` })), h("span", { class: "mono small", text: q.toFixed(2) }))))
            : h("p", { class: "muted small", text: "No quality table yet. Run the shadow evaluation." }),
          h("dl", { class: "kv" },
            h("dt", { text: "Provenance" }), h("dd", { text: `${x.provenance.developer} · ${x.provenance.licence}${x.provenance.origin ? " · " + x.provenance.origin : ""}` }),
            h("dt", { text: "Context" }), h("dd", { text: `${x.max_context.toLocaleString()} tokens` }),
            h("dt", { text: "Protocol" }), h("dd", { text: x.protocol === "code_block" ? "code-block (no tool parser)" : `tools (${x.tool_parser})` }),
            h("dt", { text: "Step latency" }), h("dd", { text: `${x.latency.step_s}s${x.latency.wake_s ? ` · wake ${x.latency.wake_s}s` : ""}` }),
            h("dt", { text: "Speculative" }), h("dd", { text: x.speculative.method === "off" ? "off" : `${x.speculative.method}, k=${x.speculative.num_speculative_tokens}` }),
            h("dt", { text: "Measured speed-up" }), h("dd", { text: x.measured_speedup ? `${x.measured_speedup}×` : "n/a (no GPU)" }),
            h("dt", { text: "Tasks served" }), h("dd", { text: m.tasks_served[x.name] || 0 })),
          x.serve_argv.length ? h("details", {}, h("summary", { class: "muted small", text: "vllm serve command" }), h("pre", { class: "code", text: x.serve_argv.join(" ") })) : null,
          admin && x.status === "shadow" ? h("div", { class: "actions" },
            h("button", { class: "btn btn-ghost btn-sm", type: "button", text: "Run shadow evaluation", onclick: (e) => guarded(e.target, async () => {
              const r = await api(`/models/${x.name}/shadow-eval`, { method: "POST" });
              toast(`Measured: ${Object.entries(r.quality).map(([k, v]) => `${k} ${v}`).join(", ")}`); render();
            }) }),
            h("button", { class: "btn btn-primary btn-sm", type: "button", text: "Promote", onclick: (e) => guarded(e.target, async () => {
              await api(`/models/${x.name}/promote`, { method: "POST", json: { confirm: true } });
              toast(`${x.name} is now active`); render();
            }) })) : null);
      }));
      const cache = m.cache;
      $("#cache-table").replaceChildren(cache.length ? h("table", {},
        h("thead", {}, h("tr", {}, h("th", { text: "Partition" }), h("th", { text: "Label" }), h("th", { text: "Requests" }), h("th", { text: "Hit rate" }))),
        h("tbody", {}, cache.map((c) => h("tr", {}, h("td", { class: "mono", text: c.partition }), h("td", {}, labelChip(c.label || "Unclassified", c.label)), h("td", { class: "num", text: c.requests }), h("td", { class: "num", text: pct(c.hit_rate) })))))
        : h("div", { class: "empty", text: "No model requests yet." }));
      const th = m.routing.thresholds;
      $("#thresholds").replaceChildren(...Object.entries(th).map(([k, v]) => h("div", { class: "threshold-row" }, h("span", { class: "pill", text: k }), h("div", { class: "qbar" }, h("i", { style: `width:${v * 100}%` })), h("span", { class: "mono", text: v.toFixed(2) }))),
        h("p", { class: "muted small", text: `A classifier confidence below ${m.routing.low_confidence_below} raises the threshold one level. ${m.speculative_note}.` }));
      $("#proposed-card").hidden = !m.proposed_diff;
      $("#proposed").textContent = m.proposed_diff || "";
    };
    await render();
    setInterval(render, 5000);
  }

  // ---------------------------------------------------------------------------------------------
  // Security page

  async function pageSecurity() {
    const me = shell.me;
    const stats = async () => {
      const snap = await api("/egress");
      if (snap.status !== "ok") {
        $("#egress-stats").replaceChildren(h("div", { class: "stat bad" }, h("b", { text: "offline" }), h("span", { text: "egressd is not reachable" })));
        return;
      }
      const packets = snap.blocked_packets === null ? "n/a" : snap.blocked_packets;
      $("#egress-stats").replaceChildren(
        h("div", { class: `stat ${snap.external_connections ? "bad" : "good"}` }, h("b", { text: snap.external_connections }), h("span", { text: "External connections" })),
        h("div", { class: "stat" }, h("b", { text: packets }), h("span", { text: `Blocked packets${packets === "n/a" ? " (no nftables in dev mode)" : ""}` })),
        h("div", { class: "stat" }, h("b", { text: snap.blocked_connect_host }), h("span", { text: "Blocked connect(), host" })),
        h("div", { class: "stat" }, h("b", { text: snap.blocked_connect_sandbox }), h("span", { text: "Blocked connect(), sandbox" })),
        h("div", { class: `stat ${snap.breach ? "bad" : ""}` }, h("b", { text: snap.mode }), h("span", { text: snap.breach ? "BREACH reported" : "Monitor mode" })));
      const ev = await api("/egress/events?limit=50");
      $("#events").replaceChildren(ev.events.length ? h("table", {},
        h("thead", {}, h("tr", {}, h("th", { text: "#" }), h("th", { text: "Time" }), h("th", { text: "Origin" }), h("th", { text: "Destination" }), h("th", { text: "Source" }))),
        h("tbody", {}, ev.events.slice().reverse().map((e) => h("tr", {}, h("td", { class: "num", text: e.seq }), h("td", { text: fmtTime(e.ts) }), h("td", {}, badge(e.origin === "sandbox" ? "derived" : "running", e.origin)), h("td", { class: "mono", text: e.addr }), h("td", { class: "muted", text: e.source || "" })))))
        : h("div", { class: "empty", text: "No blocked attempts recorded yet." }));
    };
    $("#run-test").addEventListener("click", (e) => guarded(e.target, async () => {
      const res = await api("/egress/test", { method: "POST" });
      $("#test-note").textContent = `${res.pass ? "All checks passed" : "A check failed"} · mode ${res.mode}`;
      $("#test-results").replaceChildren(...res.checks.map((c) => {
        const before = c.counters_before || {};
        const after = c.counters_after || {};
        const deltas = Object.keys(after).filter((k) => typeof after[k] === "number").map((k) => `${k.replace("blocked_", "")} ${before[k] ?? "?"}→${after[k]}`);
        return h("div", { class: "check-card" },
          h("span", { class: `mark ${c.pass ? "pass" : "fail"}` }, icon(c.pass ? "check" : "cross")),
          h("div", {}, h("strong", { text: c.name.replace(/_/g, " ") }), c.simulated ? h("span", { class: "muted small", text: " (simulated in dev mode)" }) : null,
            h("div", { class: "small", text: `Expected: ${c.expected}. Observed: ${c.observed}` }),
            deltas.length ? h("div", { class: "delta", text: deltas.join(" · ") }) : null));
      }));
      await stats();
    }));
    const audit = async () => {
      const v = await api("/audit/verify");
      $("#audit-status").replaceChildren(h("div", { class: "meta-row" }, badge(v.ok ? "pass" : "failed", v.ok ? "chain intact" : `broken at entry ${v.broken_at}`),
        h("span", { text: `${v.entries} entries` }), h("span", { class: "mono small", title: v.latest_hash, text: `head ${v.latest_hash.slice(0, 16)}…` })));
      try {
        const tail = await api("/audit/tail?n=40");
        $("#audit-tail").replaceChildren(...tail.reverse().map((e) => h("li", {}, h("span", { class: "h", text: `#${e.seq}` }),
          h("span", {}, h("strong", { text: e.event.type }), " ", h("span", { class: "muted", text: Object.entries(e.event).filter(([k]) => !["type", "log", "health", "checks", "record_ids", "spec"].includes(k)).slice(0, 4).map(([k, val]) => `${k}=${typeof val === "object" ? JSON.stringify(val) : val}`).join(" ").slice(0, 140) })),
          h("span", { class: "h", text: e.hash.slice(0, 8) }))));
      } catch (err) {
        $("#audit-tail").replaceChildren(h("li", { class: "audit-note muted small", text: err.status === 403 ? `Entry details are hidden: this view ${err.message}.` : err.message }));
      }
    };
    $("#verify-audit").addEventListener("click", (e) => guarded(e.target, audit));
    const downgrades = async () => {
      const list = await api("/downgrades");
      const can = me.roles.some((r) => ["security_officer", "document_owner"].includes(r));
      $("#downgrades").replaceChildren(list.length ? list.map((d) => h("div", { class: "item" },
        h("div", { class: "item-top" }, h("strong", { text: d.file.name }), badge(d.status === "pending" ? "awaiting_action" : d.status, d.status)),
        h("div", { class: "meta-row" }, labelChip(d.before), "→", labelChip(d.after), h("span", { text: `requested by ${d.requester}` })),
        h("div", { class: "muted small", text: `Reason: ${d.reason}` }),
        d.status === "pending" && can && d.requester !== me.id ? h("div", { class: "item-actions" },
          h("button", { class: "btn btn-primary btn-sm", type: "button", text: "Approve", onclick: (e) => guarded(e.target, async () => { await api(`/downgrades/${d.id}/decision`, { method: "POST", json: { approve: true } }); toast("Downgrade approved and re-stamped"); downgrades(); }) }),
          h("button", { class: "btn btn-danger btn-sm", type: "button", text: "Reject", onclick: (e) => guarded(e.target, async () => { await api(`/downgrades/${d.id}/decision`, { method: "POST", json: { approve: false } }); downgrades(); }) }))
          : d.status === "pending" ? h("div", { class: "muted small", text: d.requester === me.id ? "Waiting for a second authorised person." : "Needs a security officer or document owner." }) : null))
        : h("div", { class: "empty", text: "No downgrade requests." }));
    };
    const drafts = async () => {
      const t = await api("/templates");
      const can = me.roles.some((r) => ["admin", "document_owner"].includes(r));
      $("#template-drafts").replaceChildren(
        h("div", { class: "side-label", text: "Active templates" }),
        h("div", { class: "chips" }, t.templates.map((x) => h("span", { class: "chip", text: `${x.name} v${x.version}` }))),
        t.drafts.length ? t.drafts.map((name) => h("div", { class: "item" }, h("div", { class: "item-top" }, h("span", { class: "mono", text: name }),
          can ? h("button", { class: "btn btn-ghost btn-sm", type: "button", text: "Approve", onclick: (e) => guarded(e.target, async () => { const r = await api(`/templates/drafts/${encodeURIComponent(name)}/approve`, { method: "POST" }); toast(`Added ${r.template}`); drafts(); }) }) : null)))
          : h("p", { class: "muted small", text: "No template drafts awaiting review." }));
    };
    $("#impact-form").addEventListener("submit", (e) => {
      e.preventDefault();
      guarded(null, async () => {
        const r = await api(`/kb/impact/${encodeURIComponent($("#impact-doc").value)}/${encodeURIComponent($("#impact-rev").value)}`);
        $("#impact").replaceChildren(h("div", { class: "markdown" },
          h("h1", { text: `${r.doc_number}: Rev ${r.previous} to Rev ${r.revision}` }),
          h("h2", { text: "Clause changes" }), h("ul", {}, r.clauses.filter((c) => c.status !== "unchanged").map((c) => h("li", { text: `${c.clause} ${c.heading}: ${c.status}${c.limits.length ? ` (${c.limits.map((l) => `${l.old} to ${l.new} ${l.unit || ""}`).join(", ")})` : ""}` }))),
          h("h2", { text: "Past notes citing changed clauses" }), h("ul", {}, r.notes.map((n) => h("li", { text: `${n.note}: ${n.title} (clause ${n.clause}, ${n.status})` }))),
          h("h2", { text: "Equipment governed" }), h("p", { text: r.equipment.join(", ") || "None" })));
      });
    });
    await Promise.all([stats(), audit(), downgrades(), drafts()]);
    setInterval(stats, 8000);
  }

  // ---------------------------------------------------------------------------------------------

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
