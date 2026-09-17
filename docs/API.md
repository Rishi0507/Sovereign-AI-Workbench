# API Reference

The workbench exposes a JSON API under `/api`, served by FastAPI on `127.0.0.1:8080` (TLS for the plant LAN is terminated by nginx, see [DEPLOYMENT.md](DEPLOYMENT.md)). The UI uses nothing else, so everything the UI can do can be scripted. The two host daemons expose small HTTP APIs of their own over Unix sockets.

Interactive OpenAPI documentation is available at `http://127.0.0.1:8080/api/docs` while the server runs.

## Contents

- [Conventions](#conventions)
- [Tasks](#tasks)
- [Evidence and checks](#evidence-and-checks)
- [Drafts and approval](#drafts-and-approval)
- [Files and workspaces](#files-and-workspaces)
- [Classification downgrades](#classification-downgrades)
- [Models, jobs and templates](#models-jobs-and-templates)
- [Security and audit](#security-and-audit)
- [Knowledge base](#knowledge-base)
- [sandboxd](#sandboxd)
- [egressd](#egressd)
- [Scripting example](#scripting-example)

---

## Conventions

| Topic | Rule |
|---|---|
| **Identity** | Development builds read the user from the `X-User` header or the `wb_user` cookie. Production replaces `workbench/api/deps.py::current_user` with the directory adapter; nothing else changes. A missing user returns `401`. |
| **Access** | Every task and file endpoint checks the workspace ACL and the user's clearance against the object's label. Objects above your clearance return `403` with the reason. |
| **Roles** | `engineer`, `buyer`, `approver`, `security_officer`, `document_owner`, `admin`, `developer` (see `config/users.yaml`). |
| **Errors** | `{"detail": "<plain-language reason>"}` with `400`, `401`, `403`, `404`, `409` (wrong state, for example approving a plan that has errors) or `422` (invalid body). |
| **Headers** | Responses carry a strict `Content-Security-Policy`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. The CSP forbids framing, inline script and any third-party origin. |
| **Labels** | Labels are returned as display strings such as `Confidential` or `Secret · VENDOR-COMMERCIAL`, and accepted as `{"level": "...", "compartments": [...]}`. |

## Tasks

### `POST /api/tasks`

Create a task and queue it.

```json
{
  "workspace": "plant-a",
  "text": "Draft an approval note for this inspection report",
  "attachments": ["inputs/inspection_P108B.pdf"],
  "meta": {"report_date": "2026-03-12"}
}
```

`meta` accepts only `report_date`, `equipment_tag` and `templates_disabled` (plan without templates). When `attachments` is empty, input files the text names ("summarise the vendor contract") are attached, provided the caller may read them. Returns `201` with the task view. A read-only question (no side effects, no file) starts without a plan gate when `auto_start_read_only` is on.

### `GET /api/tasks`

Tasks you can see, newest first. Query `workspace` to filter.

### `GET /api/tasks/{task_id}`

The full task view used by the task page:

| Field | Meaning |
|---|---|
| `status` | One of `queued`, `routing`, `planning`, `awaiting_plan`, `waiting_tide`, `running`, `awaiting_action`, `rendering`, `awaiting_deliverable`, `completed`, `failed`, `handed_back`, `rejected`, `cancelled`. |
| `label` | Current high-water label of the task. |
| `route` | Router decision: profile, candidates with reasons, chosen model, log line. |
| `plan` | Typed plan with step statuses, compiler errors, repair history and time estimate. |
| `pending_gate` | The gate waiting for a person: `{id, kind, payload}` where `kind` is `plan`, `action`, `template_choice` or `deliverable`. |
| `trace` | Live trace rows: kind, step, tool or model purpose, summary, records, latency. |
| `checks` | Consistency findings after review. |
| `deliverables` | Draft and final files with provenance and citation summaries. |

### `POST /api/tasks/{task_id}/plan/decision`

Owner or approver only.

```json
{"decision": "approve"}
{"decision": "edit", "plan": {"steps": [ ... ], "deliverables": [{"type": "xlsx"}]}}
{"decision": "reject", "note": "not needed"}
```

An edited plan is recompiled and the gate opens again. Approving a plan with compiler errors returns `409`.

### `POST /api/tasks/{task_id}/template/decision`

Answer a template-choice gate: `{"template": "approval_note_from_scan"}`.

### `POST /api/tasks/{task_id}/actions/{gate_id}/decision`

Approve or deny a side-effecting step: `{"approve": true, "note": "optional"}`. A denied action is recorded and the loop continues without it. With `plan_approval_covers_drafts` (the default), this gate only appears for steps the plan approval does not cover, such as re-rendering a file during a revision; covered steps show up in `gates` with `decided_by` set to the plan approver and the note "approved with the plan".

### `POST /api/tasks/{task_id}/followup`

Continue a conversation: `{"text": "How many words are in it?", "attachments": ["inputs/x.pdf"]}` (`attachments` is optional). Returns the new task (`201`). A follow-up always joins the conversation's first task (even when posted on a later follow-up), reuses its attachments unless others are given, and starts at the highest classification seen so far in the conversation. The first task's view lists the conversation under `followups`, oldest first; each follow-up carries `followup_of`.

### `GET /api/routing/{task_id}`

The routing decision as JSON, including the one-line explanation.

## Evidence and checks

| Endpoint | Returns |
|---|---|
| `GET /api/tasks/{task_id}/ledger` | All ledger records of the task in order, with labels and hashes. |
| `GET /api/records/{record_id}` | One record with its typed fields, anchor and full body. |
| `GET /api/tasks/{task_id}/evidence/{name}` | A crop image of a critical field. |
| `GET /api/tasks/{task_id}/checks` | Consistency findings with status (`pass`, `mismatch`, `not_checked`), detail and the records involved. |
| `POST /api/tasks/{task_id}/checks/{record_id}/acknowledge` | Approver only. Marks a mismatch as read; audited. |

## Drafts and approval

### `GET /api/tasks/{task_id}/draft`

The review model of the current draft: rendered paragraphs and tables with figure spans (`sourced`, `derived`, `unsourced`), citation checks with reasons, consistency findings and the list of approval blockers.

### `POST /api/tasks/{task_id}/draft/figures/{figure_id}`

Approver only. Resolve an unsourced figure:

```json
{"action": "link", "record_id": "R-T114F968A-9"}
{"action": "correct", "value": "5.6 mm", "note": "typo in the draft"}
{"action": "confirm", "note": "stated by the inspector on site"}
```

`correct` edits the figure in the `.docx` itself. Every action is audited.

### `POST /api/tasks/{task_id}/draft/decision`

Approver only.

```json
{"decision": "approve", "acknowledged": ["R-T114F968A-26", "R-T114F968A-27"]}
{"decision": "reject", "note": "Add the vendor's corrective action."}
```

Approval returns `409` with the blockers while mismatches are unacknowledged or figures are unsourced. A rejection needs a note; the first two rejections send the task back for a revision, the third rejects it.

## Files and workspaces

| Endpoint | Purpose |
|---|---|
| `GET /api/workspaces` | Workspaces you can access, with ceilings and your effective access. |
| `GET /api/workspaces/{ws}/files?area=inputs` | Files in `inputs`, `drafts` or `final` that your clearance allows. |
| `POST /api/workspaces/{ws}/files` | Multipart upload: `file`, optional `level` and `compartments` (comma-separated). Markings found in the file raise the label; a label above the workspace ceiling is refused. |
| `GET /api/files/{file_id}` | File metadata and label. |
| `GET /api/files/{file_id}/download` | The file, if your clearance allows it. |
| `POST /api/files/{file_id}/share` | Copy a file into another workspace: `{"workspace": "proc"}`. Refused if the label is above that workspace's ceiling. |

## Classification downgrades

| Endpoint | Purpose |
|---|---|
| `POST /api/files/{file_id}/downgrade` | Request a lower label: `{"level": "Restricted", "compartments": [], "reason": "..."}`. |
| `GET /api/downgrades` | Requests you can see. |
| `POST /api/downgrades/{request_id}/decision` | Security officer or document owner, and not the requester: `{"approve": true, "note": "..."}`. An approved downgrade re-stamps the file. |

## Models, jobs and templates

| Endpoint | Purpose |
|---|---|
| `GET /api/models` | Registry entries, quality tables, pool states, `serve` commands and registry version. |
| `POST /api/models/{name}/shadow-eval` | Admin only. Evaluate a shadow entry offline. |
| `POST /api/models/{name}/promote` | Admin only. Promote a shadow entry to active. |
| `GET /api/jobs` | Running and queued jobs in your workspaces. |
| `DELETE /api/jobs/{job_id}` | Cancel a job you own. |
| `GET /api/templates` | Active templates and drafts awaiting review. |
| `POST /api/templates/drafts` | Save a finished task's plan as a template draft (stored in `var/templates/drafts`): `{"task_id": "...", "name": "optional"}`. |
| `POST /api/templates/drafts/{name}/approve` | Admin or document owner. Adds the template to `var/templates`, next to the built-in ones. |

## Security and audit

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | `ok` or `degraded`, with the state of `sandboxd`, `egressd` and the in-process egress guard. |
| `GET /api/me`, `GET /api/users` | The current user; the development user list. |
| `GET /api/egress` | Counters: external connections, blocked packets, blocked connects from the host and the sandbox, monitor mode, breach flag. |
| `POST /api/egress/test` | Run the three-part egress test (host raw IP, host DNS, sandbox socket). |
| `GET /api/egress/events?since=&limit=` | Recent blocked attempts. |
| `GET /api/audit/verify` | Recompute the audit hash chain: `{ok, entries, latest_hash, broken_at}`. |
| `GET /api/audit/tail?n=40` | Security officer, admin or document owner. The latest audit entries. |

## Library

### `GET /api/library?workspace=`

Every task the caller may see, newest first, with what it produced and what was decided:

```json
{"tasks": [{
  "id": "T4E7E556B", "text": "Draft an approval note for this inspection report",
  "workspace": "plant-a", "workspace_title": "Plant A · Mechanical integrity", "user": "engineer1",
  "status": "awaiting_deliverable", "created_at": "...", "updated_at": "...", "label_display": "Confidential",
  "files": [{"id": "F...", "name": "approval-note.docx", "path": "drafts/T4E7E556B/approval-note.docx",
             "final": false, "status": "draft", "size": 39512, "label_display": "Confidential"}],
  "decisions": [{"kind": "plan", "title": "Plan", "status": "approved", "by": "engineer1", "at": "...", "note": null},
                {"kind": "action", "title": "Create the Word document", "status": "approved",
                 "by": "engineer1", "note": "approved with the plan"}],
  "waiting_for": "deliverable"
}]}
```

Tasks in workspaces the caller cannot access, or above the caller's clearance, are left out. The Library page combines this with `GET /api/downgrades`.

## Knowledge base

| Endpoint | Purpose |
|---|---|
| `GET /api/kb/stats` | Chunk, document, graph node and edge counts. |
| `GET /api/kb/impact/{doc_number}/{revision}` | Document owner, admin or security officer. Supersession impact: changed clauses and limits, notes that cite them, governed equipment, and a Markdown report. |

---

## sandboxd

Listens on `run/sandboxd.sock` (`/run/workbench/sandboxd.sock` in production, mode `0660`; the systemd units grant access through group membership). On Windows development machines it listens on an ephemeral loopback port written to `run/sandboxd.addr`, and every request must carry `Authorization: Bearer <run/sandboxd.token>`. Every response carries an `X-Request-Id`.

| Endpoint | Purpose |
|---|---|
| `GET /v1/health` | `{status, version, backend, ...}` |
| `POST /v1/run` | Run a script and wait for it. |
| `GET /v1/runs/{id}` | The stored result of a run. |
| `POST /v1/runs/{id}/kill` | Stop a running script. |

`POST /v1/run` body:

```json
{
  "run_id": "run-1",
  "job_dir": "/var/lib/workbench/workspaces/_jobs/T1",
  "script": "script.py",
  "image": "workbench-sandbox:py311",
  "runtime": "runc",
  "timeout_s": 60,
  "cpus": 2,
  "memory_mb": 1024,
  "pids": 128,
  "label": "Restricted"
}
```

The job folder must resolve inside the configured workspace root (symlinks are refused) and belong to the service user. The result:

```json
{
  "run_id": "run-1",
  "backend": "docker",
  "exit_code": 0,
  "timed_out": false,
  "oom_killed": false,
  "duration_ms": 812,
  "stdout_tail": "...",
  "stderr_tail": "",
  "traceback_head": [],
  "new_files": [{"path": "anomalies.csv", "size": 1432, "sha256": "..."}],
  "changed_files": [],
  "net_attempts": [],
  "argv": ["docker", "run", "--rm", "--network", "none", "..."]
}
```

## egressd

Listens on `run/egressd.sock` (same Windows rules as above).

| Endpoint | Purpose |
|---|---|
| `GET /v1/health` | `{status, version, mode, uptime_s}` |
| `GET /v1/snapshot` | `{mode, external_connections, blocked_packets, blocked_connect_host, blocked_connect_sandbox, collectors, since, breach}`. `blocked_packets` is `null` when nftables is not available (dev mode). |
| `GET /v1/events?since=<seq>&limit=<n>` | Events from the ring buffer. |
| `POST /v1/report` | A blocked connect seen by the host guard or the sandbox probe: `{origin: "host" or "sandbox", addr, port, pid, run_id, ts, source}`. Unknown fields are rejected. |
| `POST /v1/test` | Run the self-test and persist the state. |

---

## Scripting example

```python
import time
import httpx

api = httpx.Client(base_url="http://127.0.0.1:8080/api", headers={"X-User": "engineer1"})
task = api.post("/tasks", json={
    "workspace": "plant-a",
    "text": "Draft an approval note for this inspection report",
    "attachments": ["inputs/inspection_P108B.pdf"],
}).json()

while True:
    t = api.get(f"/tasks/{task['id']}").json()
    gate = t["pending_gate"]
    if t["status"] in {"completed", "failed", "handed_back", "rejected", "cancelled"}:
        break
    if gate and gate["kind"] == "plan":
        api.post(f"/tasks/{t['id']}/plan/decision", json={"decision": "approve"})
    elif gate and gate["kind"] == "action":
        api.post(f"/tasks/{t['id']}/actions/{gate['id']}/decision", json={"approve": True})
    elif gate and gate["kind"] == "deliverable":
        acks = [c["record_id"] for c in t["checks"] if c["status"] == "mismatch"]
        api.post(f"/tasks/{t['id']}/draft/decision", json={"decision": "approve", "acknowledged": acks})
    time.sleep(0.5)

print(t["status"], [d["final_file_id"] for d in t["deliverables"]])
```
