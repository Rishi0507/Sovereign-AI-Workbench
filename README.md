# Sovereign AI Workbench

**The agentic layer of an air-gapped engineering workbench.** Engineers drop in scanned inspection reports, contracts, vendor offers and data files, and the workbench plans the work, runs the tools, checks every figure against its source, and hands back a reviewable draft: an approval note, a verified script, a contract summary, a calculation sheet or an offer comparison. Nothing leaves the premises, and every number in a deliverable can be traced back to the page it came from.

<p align="center"><img src="docs/images/home.png" width="49%" alt="Workspace home, light theme"> <img src="docs/images/home-dark.png" width="49%" alt="Workspace home, dark theme"></p>

This repository implements the **agentic layer** described in [`docs/design/SYSTEM_DESIGN.md`](docs/design/SYSTEM_DESIGN.md) section 4, following the build plan in [`docs/design/IMPLEMENTATION_PRD.md`](docs/design/IMPLEMENTATION_PRD.md). The inference servers (vLLM), the GPU and the network hardening are outside its scope; the layer talks to them through narrow, testable interfaces and ships deterministic stand-ins so the whole system runs and is tested on a laptop.

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [How a request flows](#how-a-request-flows)
- [Demo traces](#demo-traces)
- [Quick start](#quick-start)
- [Using the workbench](#using-the-workbench)
- [Command line](#command-line)
- [Configuration](#configuration)
- [Repository layout](#repository-layout)
- [Testing and quality](#testing-and-quality)
- [What is simulated](#what-is-simulated)
- [Documentation](#documentation)

---

## What it does

| Capability | How it works |
|---|---|
| **Plans before it acts** | A task is matched to a versioned YAML template, or a model writes a plan that a typed compiler validates and repairs. The engineer approves the plan before any tool runs. |
| **Chooses the model automatically** | Hard rules, a small classifier, capability constraints and a quality scorer pick one of the registered models. Every decision is logged as one readable line. |
| **Uses one GPU for several models** | A tidal pool keeps the everyday models resident and swaps the reasoning model in when a queue of work justifies it. |
| **Grounds everything in evidence** | Each extracted value, retrieved passage and computed figure becomes a hash-chained ledger record. Prompts cite records; deliverables cite records. |
| **Reads scans twice** | Critical fields are read by OCR and by a vision model independently, then reconciled. Disagreements are flagged, never guessed. |
| **Checks before it hands over** | Consistency rules, trend projection, number provenance and citation verification run on every draft. Approval stays locked until mismatches are acknowledged and unsourced figures are resolved. |
| **Runs code in a sandbox** | Generated scripts run in a network-less container managed by `sandboxd`, and the agent iterates on tracebacks. |
| **Enforces classification** | Every record and file carries a label. Tasks inherit the highest label they read, retrieval is filtered by clearance and need-to-know, and a downgrade needs two different authorised people. |
| **Proves it is sovereign** | `egressd` counts every outbound attempt from three independent sources, and the UI shows the live counters next to a one-click egress test. |

## Architecture

```mermaid
flowchart LR
    subgraph Browser["Engineer's browser"]
        UI["Workbench UI<br/>vanilla JS, no CDN"]
    end

    subgraph App["workbench (Python 3.11+)"]
        API["FastAPI<br/>/api"]
        JOBS["Job queue<br/>and scheduler"]
        ORCH["Orchestrator<br/>agent loop"]
        ROUTER["Router<br/>rules, classifier,<br/>constraints, scorer"]
        PLAN["Planning<br/>templates, compiler,<br/>repair loop"]
        CTX["Context compiler<br/>quoted records,<br/>label-salted prefix"]
        TOOLS["Tools<br/>documents, KB, checks,<br/>calculate, renderers"]
        LEDGER[("Evidence ledger<br/>hash-chained")]
        KB[("Knowledge base<br/>hybrid search +<br/>plant graph")]
        AUDIT[("Audit log<br/>hash-chained JSONL")]
        POOL["Tidal pool<br/>manager"]
    end

    subgraph Host["Host services (Go, stdlib only)"]
        SBX["sandboxd<br/>container runner"]
        EGR["egressd<br/>egress monitor"]
    end

    subgraph Models["Loopback inference"]
        VLLM["vLLM servers<br/>qwen3-vl-8b<br/>qwen2.5-coder-7b<br/>gpt-oss-20b"]
    end

    UI -->|HTTPS on the plant LAN| API
    API --> JOBS --> ORCH
    ORCH --> ROUTER
    ORCH --> PLAN
    ORCH --> CTX
    ORCH --> TOOLS
    ORCH --> POOL
    TOOLS --> LEDGER
    TOOLS --> KB
    CTX --> LEDGER
    ORCH --> AUDIT
    ORCH -->|OpenAI-compatible API| VLLM
    POOL -->|sleep / wake| VLLM
    TOOLS -->|Unix socket| SBX
    API -->|Unix socket| EGR
    SBX -.->|reports blocked connects| EGR
```

The Python application owns every control decision: labels, routing constraints, plan validation, consistency checks, number provenance and approvals are deterministic code. Models only propose. The two Go daemons are the only components that touch the container runtime and the firewall counters, and they listen on Unix sockets (loopback TCP with a token file on Windows development machines).

A deeper walk-through with more diagrams is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## How a request flows

```mermaid
sequenceDiagram
    autonumber
    actor E as Engineer
    participant UI as Workbench UI
    participant O as Orchestrator
    participant R as Router
    participant P as Planner
    participant T as Tools
    participant L as Ledger
    participant M as Model

    E->>UI: Task text and attachments
    UI->>O: POST /api/tasks
    O->>L: user_input and attachment records (labelled)
    O->>R: Task profile
    R-->>O: Model choice with one-line explanation
    O->>P: Match a template or compile a model plan
    P-->>O: Typed plan (validated, repaired if needed)
    O-->>UI: Plan gate
    E->>UI: Approve plan
    loop Every plan step
        O->>M: Decide tool arguments (records quoted, never obeyed)
        M-->>O: JSON tool call, schema-checked
        alt Side effect not covered by the plan approval
            O-->>UI: Action gate
            E->>UI: Approve action
        end
        O->>T: Run tool
        T->>L: New evidence records
    end
    O->>T: Consistency, provenance and citation checks
    O-->>UI: Draft with highlighted figures and findings
    E->>UI: Acknowledge findings, approve
    O->>L: Final deliverable, marked with the task label
```

## Demo traces

The five scenarios from the design run end to end with `workbench demo`. The table below is the actual output on a Windows laptop with the offline backend (times in seconds).

| Trace | Task | Route | Model | Plan | Checks | Label | Time |
|---|---|---|---|---|---|---|---|
| **A** | Draft an approval note from a scanned inspection report | document | qwen3-vl-8b | template `approval_note_from_scan` | 3 mismatches caught, 0 unsourced figures | Confidential | 1.26 |
| **B** | Write a script to parse pressure readings and flag anomalies | code | qwen2.5-coder-7b | compiled, 0 repairs | 2 sandbox runs (first fails with `KeyError`, fix passes) | Restricted | 0.58 |
| **C** | Summarise a 40-page vendor contract | document | qwen3-vl-8b | template `contract_summary` | map and reduce with clause citations | Confidential | 1.36 |
| **D** | Compute required pipe wall thickness | general | qwen3-vl-8b | template `calc_sheet` | units checked by `calculate` | Restricted | 0.48 |
| **E** | Compare three vendor offers and recommend one | agentic | gpt-oss-20b | compiled, 1 repair | scoring run in the sandbox | Secret, VENDOR-COMMERCIAL | 1.45 |

In every run the egress test passed and the external connection counter stayed at zero.

A task reads like a conversation: the request, the plan to approve, the steps as they finish, the issues found and the files produced.

<p align="center"><img src="docs/images/task.png" width="49%" alt="Task view, light theme"> <img src="docs/images/task-dark.png" width="49%" alt="Task view, dark theme"></p>

The review screen shows the draft as paper and underlines every figure by where it came from (a source, a calculation, or no source). The issues must be marked as reviewed before **Approve** unlocks, and each citation is checked against the record it cites.

<p align="center"><img src="docs/images/review.png" width="49%" alt="Review view, light theme"> <img src="docs/images/review-dark.png" width="49%" alt="Review view, dark theme"></p>

## Quick start

**Requirements:** Python 3.11 or newer. Go 1.22 or newer is only needed to build the host daemons. Docker is only needed for the production sandbox backend.

### 1. Install

```bash
git clone https://github.com/Rishi0507/Sovereign-AI-Workbench.git
cd Sovereign-AI-Workbench
python -m venv .venv
```

```bash
# Linux / macOS
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

```bash
pip install -e ".[dev]"
cp .env.example .env         # Windows: copy .env.example .env
```

### 2. Choose how host services run

The workbench expects the `sandboxd` and `egressd` daemons. For a first look you can use the in-process stand-ins instead by editing `.env`:

```ini
WB_SANDBOX=fake
WB_EGRESS=fake
```

To run the real daemons locally (they use a subprocess backend without Docker in development):

```bash
python scripts/dev.py go-build
workbench render-go-config --backend dev --mode dev
bin/sandboxd -config var/go-config/sandboxd.json &
bin/egressd  -config var/go-config/egressd.json &
```

### 3. Generate fixtures, seed and serve

```bash
python scripts/dev.py fixtures   # synthetic reports, contract, offers, KB, asset register; then `workbench setup`
workbench serve                   # http://127.0.0.1:8080
```

Open <http://127.0.0.1:8080> and click **Draft an approval note**. The request and the matching scanned report are filled in; press Enter, then **Start** on the plan.

### 4. Optional: let a real model write the replies

Greetings and follow-up questions can be written by a small instruct model that runs on this machine. In a
second terminal:

```bash
python scripts/dev.py chat-model   # downloads llama.cpp and Qwen2.5-1.5B-Instruct once, then serves them
```

Then set `WB_CHAT_MODEL=qwen2.5-1.5b-instruct` in `.env` and restart `workbench serve`. Plans, documents,
figures and checks stay deterministic; only the conversation goes to the model, and nothing leaves the machine.
Without this the replies come from rules, which is the default.

### 5. Run everything headless

```bash
workbench demo                   # Traces A to E with automatic approvals
workbench eval                   # writes reports/eval.md and reports/eval.json
```

`make` targets (`make setup`, `make test`, `make demo`, ...) are thin wrappers around `python scripts/dev.py <task>`, so the same commands work on Windows without `make`.

## Using the workbench

The interface is deliberately plain: a sidebar with your recent tasks, one box to type in, and a conversation for each task.

| Screen | What you do there |
|---|---|
| **Home** | Type a request, attach files with the paperclip (or upload new ones), or pick a suggestion. |
| **Plant** | The equipment map: a tag with the drawings it appears on, the clauses that govern it, its inspections, vendor and order. Click a node to see what is recorded against it. The Drawings tab shows the P&ID sheets, where every tag is clickable. |
| **Library** | Every generated file grouped by task and day, every decision in a timeline, and everything waiting on your decision. Click a file to read it in place, with the download beside it. Search across all of it and filter documents by type. |
| **Task** | A conversation: approve, edit or cancel the plan, follow progress, read answers with numbered sources, see counted facts such as page and word totals, download files, and keep asking follow-ups in the box at the bottom. **Details** opens the technical record: activity log, evidence, model choice and checks. |
| **Review** | Read the draft, click any figure or source number to see where it came from, mark issues as reviewed, fix figures without a source, then approve or request changes. Click any paragraph to rewrite it: reference markers stay, the figures are checked again, and an approver has to accept the edit before the file can be approved. A slide deck is shown slide by slide, with its speaker notes. |
| **Models** | See which models are ready, asleep or being evaluated, and how well each does per kind of work. |
| **Security** | See that nothing has left the server, run a network test, check the audit log and handle approval requests. |

The account menu (bottom left) switches the signed-in user in development and links to Models and Security. Classification travels with the document rather than the page: it is shown on every file, in the preview and on the review sheet, and stamped into each exported file. The top bar carries the workspace on the home page and is otherwise empty; a network chip appears only if data has left the server or the monitor has stopped. The interface follows the system light or dark theme, works on phone screens, and animates every change (tabs slide, panels glide, lists update in place) so nothing jumps.

<p align="center"><img src="docs/images/library.png" width="49%" alt="Library, light theme"> <img src="docs/images/library-dark.png" width="49%" alt="Library, dark theme"></p>

<p align="center"><img src="docs/images/models.png" width="49%" alt="Models, light theme"> <img src="docs/images/models-dark.png" width="49%" alt="Models, dark theme"></p>

<p align="center"><img src="docs/images/security.png" width="49%" alt="Security, light theme"> <img src="docs/images/security-dark.png" width="49%" alt="Security, dark theme"></p>

## Command line

| Command | Purpose |
|---|---|
| `workbench setup [--reset]` | Create runtime folders, seed the demo workspaces and reference data, ingest the knowledge base. |
| `workbench serve [--host --port]` | Start the API and the UI (loopback by default). |
| `workbench run "<text>" --attach inputs/x.pdf [--auto-approve]` | Run one task headless and print its trace. |
| `workbench demo [--runs N]` | Run Traces A to E and print the summary table. |
| `workbench eval [--write-registry] [--refresh]` | Run the evaluation set; optionally propose new quality values for `config/models.yaml`. |
| `workbench ingest <dir>` | Ingest markdown documents into the knowledge base and the plant graph. |
| `workbench kb stats` / `workbench kb impact <doc> <rev>` | Knowledge-base statistics and the supersession impact report. |
| `workbench registry render-serve --profile S` | Write the `vllm serve` commands for a hardware profile. |
| `workbench registry shadow <model>` / `promote <model>` | Evaluate a shadow model offline and promote it with one audited change. |
| `workbench audit verify` / `audit summary <date>` | Verify the audit hash chain and print a daily summary. |
| `workbench render-go-config` | Render the daemon JSON configuration from the YAML settings. |
| `workbench manifest-verify <manifest> <dir>` | Verify a SHA-256 manifest (supply-chain check for model weights). |

## Configuration

All configuration lives in plain files under [`config/`](config), and every top-level setting can be overridden with an environment variable `WB_<KEY>` or a local `.env` file (never committed).

| File | Contents |
|---|---|
| `config/settings.yaml` | Backend, host services, storage paths, agent limits, check tolerances, step budgets. |
| `config/models.yaml` | Model registry: capabilities, context, protocol, quality table, `serve` parameters per profile. |
| `config/pool.yaml` | Tidal pool: the swap-slot model, maximum wait, queue size that turns the tide, minimum residency. |
| `config/labels.yaml` | Classification levels, compartments, downgrade roles. |
| `config/users.yaml`, `config/workspaces.yaml` | Development directory: users, groups, clearances, workspace ceilings. |
| `config/egress_allowlist.yaml` | Loopback and LAN destinations that are allowed. |
| `templates/*.yaml` | Versioned plan templates. |
| `rules/consistency/*.yaml` | Consistency rules applied to every deliverable. |
| `schemas/*.json` | JSON Schemas for every structured model output. |

The target deployment runs the models on the premises, on the GPU described in the design document. A development
machine without that GPU can stand the inference layer in with a hosted OpenAI-compatible service: set
`WB_LLM_BACKEND=groq` and put the key in `.env` as `GROQ_API_KEY`. `config/groq.yaml` maps each entry in the model
registry to a hosted model and sets how far apart calls are spaced. The agentic layer is unchanged, so behaviour
observed this way is representative of the same layer driven by on-premises servers; the sovereignty property is
not, because model calls then leave the machine. The interface states this on the security page, and the
deterministic rules continue to answer whenever the service is unreachable or out of allowance.

The workbench needs no API keys for the offline and loopback backends. To use real models, start vLLM on loopback with the commands from `workbench registry render-serve` and set `WB_LLM_BACKEND=openai`. To keep the deterministic pipeline but let a local model write the conversational replies, set `WB_CHAT_MODEL` (and `WB_CHAT_ENDPOINT` if it is not on `127.0.0.1:8010`).

## Repository layout

```text
workbench/            Python package (the agentic layer)
  agent/              orchestrator, approval gates, code protocol, delegation
  router/             rules, classifier, constraints, scorer, explanation
  planning/           templates, typed plan compiler, repair, promotion
  context/            context compiler and label-salted cache keys
  tools/              tool registry and every tool implementation
  documents/          readers, OCR sidecars, dual-read reconciliation
  kb/                 chunking, embeddings, BM25, fusion, revisions, plant graph
  checks/             consistency engine, trend, provenance, citations
  core/               ledger, labels and policy, audit, database, normalisation
  llm/                backends (heuristic, scripted, OpenAI-compatible) and prompts
  pool/ registry/     tidal pool manager, model registry and shadow evaluation
  security/           egress guard, daemon clients, transport, manifests
  eval/               datasets, metrics, report writer
  api/ ui/            FastAPI routes and the web interface
go/                   sandboxd and egressd (standard library only)
config/ templates/ rules/ schemas/
fixtures/             evaluation sets, scripted model outputs
scripts/              fixture generator and cross-platform dev tasks
deploy/               systemd units, nftables, sinkhole, Docker and vLLM files
tests/                unit, integration, acceptance and contract tests
docs/                 architecture, API, security, deployment, decisions, design
```

## Testing and quality

```bash
python scripts/dev.py test            # pytest: unit, integration, acceptance, contract (fakes)
python scripts/dev.py lint            # ruff and strict mypy on the core packages
python scripts/dev.py go-test         # Go unit tests with coverage
python scripts/dev.py go-lint         # gofmt, go vet, no third-party modules
python scripts/dev.py go-integration  # builds both daemons and runs the contract tests against them
python -m pytest tests/ui             # optional: drives the interface in Chrome (needs Playwright)
```

- Tests run with network access blocked (`pytest-socket`); only loopback is allowed.
- Contract tests run the same assertions against the in-process fakes and, with `WB_GO_BINARIES=1`, against the real daemons.
- The routing evaluation (50 prompts) scores 100%, and the router's tricky cases are covered by unit tests.
- Go coverage: `sandbox` 92%, `egress` 93%, `pathjail` 86%.
- A hygiene test keeps the repository free of em dashes and checks that the UI script parses.

See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for the full workflow.

## What is simulated

The layer is complete, but a laptop has no GPU, no container runtime and no firewall. These parts are stand-ins, and each is clearly marked in the code and the UI:

| Stand-in | Replaces | Where |
|---|---|---|
| `HeuristicBackend` | The LLM and VLM calls, unless a real model is configured. Setting `WB_CHAT_MODEL` gives the conversational replies to a small local instruct model (`python scripts/dev.py chat-model`); plans, documents and checks stay with the rules below. It is deterministic, reads only the records it is given, and deliberately fails the first Trace E plan and the first Trace B script so the repair paths run. | `workbench/llm/heuristic.py` |
| P&ID sheets (`PID-*.svg`) | Site drawings. The sheets are generated from the asset register so the tags on them match the plant graph, and each title block says it is a synthetic drawing. A real deployment loads the site's own sheets. | `scripts/make_fixtures.py` |
| OCR sidecars (`*.ocr.json`) | PaddleOCR output for scanned fixtures. Text PDFs are read for real with PyMuPDF. | `workbench/documents/readers.py` |
| `FakeSandbox`, `FakeEgressd` | The Go daemons, when `WB_SANDBOX=fake` / `WB_EGRESS=fake`. | `workbench/tools/sandbox.py`, `workbench/security/egressd_client.py` |
| `dev` sandbox backend | Docker. Scripts run as a subprocess with a network probe; isolation is only real with the `docker` backend. | `go/internal/sandbox/dev.go` |
| Hosted models (`WB_LLM_BACKEND=groq`) | The on-premises inference layer, when no GPU is present. The same OpenAI-compatible client and the same registry names are used; only the endpoint differs. Calls are paced and fall back to the rules, and the destination is on the egress allowlist. Not part of the target deployment. | `workbench/llm/remote.py`, `config/groq.yaml` |
| Seed quality values | Measured model quality. The values in `config/models.yaml` are illustrative until `workbench eval --write-registry` runs on real models. | `config/models.yaml` |
| Development sign-in | The directory adapter. The user switcher sets an `X-User` header or cookie. | `workbench/api/deps.py` |

Two publicly available documents sit beside the generated ones so the workbench can be seen working on material
this project did not write: a NASA report on corrosion risk in underground piping, and a scanned P&ID published by
the United States Atomic Energy Commission. Both are public domain. `fixtures/public/SOURCES.md` records the
title, author, licence, source, retrieval date and checksum of each, and a test refuses a file that is not listed.

Everything else (routing, planning, the ledger, labels, checks, rendering, retrieval, the audit chain, the daemons' protocols) is the production code path.

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, the agent loop, routing, evidence, checks, retrieval and the tidal pool, with diagrams. |
| [`docs/API.md`](docs/API.md) | Every HTTP endpoint of the workbench and of the two daemons. |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Classification, need-to-know, prompt-injection containment, egress enforcement and proof. |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Installing on the air-gapped server: services, firewall, sandbox image, models. |
| [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) | Local setup, test layers, fixtures, evaluation, adding tools, templates and models. |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Every place where the implementation deviates from or refines the PRD, and why. |
| [`docs/design/`](docs/design) | The system design, the diagrams and the implementation PRD this code follows. |
