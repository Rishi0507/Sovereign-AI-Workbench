# Architecture

This document explains how the agentic layer is put together: the components, how a task moves through them, and the rules that keep the system grounded, classified and sovereign. Section numbers such as "design 4.2" refer to [`design/SYSTEM_DESIGN.md`](design/SYSTEM_DESIGN.md).

## Contents

1. [Principles](#1-principles)
2. [Component map](#2-component-map)
3. [Task lifecycle](#3-task-lifecycle)
4. [The agent loop](#4-the-agent-loop)
5. [Routing](#5-routing)
6. [Planning](#6-planning)
7. [Evidence ledger and context compilation](#7-evidence-ledger-and-context-compilation)
8. [Documents and dual reads](#8-documents-and-dual-reads)
9. [Knowledge base and plant graph](#9-knowledge-base-and-plant-graph)
10. [Deliverables and checks](#10-deliverables-and-checks)
11. [Tidal pool](#11-tidal-pool)
12. [Host services](#12-host-services)
13. [Persistence](#13-persistence)

---

## 1. Principles

| Principle | Consequence in the code |
|---|---|
| **Models propose, code decides.** | Labels, routing constraints, plan validation, consistency checks, number provenance and approvals are deterministic Python. A model output is only accepted after it passes a JSON Schema and a semantic check. |
| **Everything is evidence.** | Tools never hand raw text to the next step. They write ledger records with an id, a label, a confidence and an anchor, and later steps refer to those ids. |
| **Data is quoted, never obeyed.** | Document text reaches a model only inside `<record>` blocks, after the instructions, and the system prompt says such content is data. |
| **Humans hold the pen.** | Plans, side-effecting actions and deliverables pass through gates. Approval of a draft is impossible while checks are open. |
| **Fail visibly.** | Invalid output is retried, then the template default runs, then the task escalates or is handed back. Every step appears in the trace with its reason. |
| **Same code path everywhere.** | The offline backend and the fake host services implement the same interfaces as vLLM and the Go daemons, and contract tests hold both sides to the same assertions. |

## 2. Component map

```mermaid
flowchart TB
    subgraph Interface
        UI["ui/<br/>pages, app.js, app.css"]
        API["api/<br/>tasks, files, system"]
        CLI["cli.py"]
    end

    subgraph Control["Control plane"]
        JOBS["jobs/queue.py<br/>worker threads, per-model slots"]
        ORCH["agent/loop.py<br/>Orchestrator"]
        GATES["agent/approvals.py<br/>AutoApprove, QueueApprove"]
        CODE["agent/code_protocol.py"]
        DEL["agent/delegate.py"]
    end

    subgraph Decisions["Deterministic decisions"]
        RT["router/<br/>rules, classifier,<br/>constraints, scorer, explain"]
        PL["planning/<br/>templates, compiler,<br/>model_tasks, promotion"]
        POL["core/labels.py<br/>PolicyEngine"]
        CHK["checks/<br/>rules, trend,<br/>provenance, citations"]
    end

    subgraph Work["Work"]
        TOOLS["tools/registry.py"]
        DOCS["documents/<br/>readers, pipeline, dual_read"]
        KB["kb/<br/>chunking, embed, bm25,<br/>store, revisions, graph"]
        REND["tools/render_*<br/>docx, xlsx, pptx"]
        SBX["tools/sandbox.py<br/>client"]
    end

    subgraph State
        LED["core/ledger.py"]
        AUD["core/audit.py"]
        DB[("SQLite<br/>var/workbench.db")]
        FS[("Workspaces<br/>var/workspaces")]
    end

    subgraph Models
        LLM["llm/<br/>heuristic, scripted,<br/>openai_compat"]
        POOL["pool/manager.py"]
        REG["registry/<br/>models, serve_cmd, shadow"]
        CTX["context/<br/>compiler, cache_salt"]
    end

    UI --> API --> JOBS --> ORCH
    CLI --> ORCH
    ORCH --> GATES
    ORCH --> RT --> REG
    ORCH --> PL
    ORCH --> CTX --> LLM
    ORCH --> POOL --> REG
    ORCH --> TOOLS
    ORCH --> CODE --> SBX
    ORCH --> DEL
    TOOLS --> DOCS
    TOOLS --> KB
    TOOLS --> CHK
    TOOLS --> REND
    TOOLS --> SBX
    TOOLS --> LED
    POL --> LED
    LED --> DB
    KB --> DB
    ORCH --> AUD
    API --> FS
```

`workbench/runtime.py` builds one `Runtime` that wires these parts together from `config/`. Tests build their own runtimes with a fixed clock, a temporary root and whichever backend or host-service fakes they need.

## 3. Task lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> routing
    routing --> planning
    planning --> awaiting_plan: plan gate
    awaiting_plan --> planning: edited
    awaiting_plan --> rejected: rejected
    awaiting_plan --> waiting_tide: model is in the swap slot
    awaiting_plan --> running: approved
    waiting_tide --> running: tide turned
    running --> awaiting_action: side-effecting tool
    awaiting_action --> running: approved or denied
    running --> rendering
    rendering --> awaiting_deliverable: draft ready
    awaiting_deliverable --> running: sent back with a note
    awaiting_deliverable --> completed: approved
    awaiting_deliverable --> rejected
    running --> handed_back: no fallback left
    running --> failed: step limit or error
    queued --> cancelled
    running --> cancelled
    completed --> [*]
    rejected --> [*]
    failed --> [*]
    handed_back --> [*]
    cancelled --> [*]
```

Every transition is written to the task state (SQLite), to the live trace shown in the UI and, for the important ones, to the audit log. Gates are persisted too: the UI reads the pending gate from the task and posts a decision to the matching endpoint.

## 4. The agent loop

`Orchestrator.run` (in `agent/loop.py`) executes the design's pseudocode (design 4.1) in four phases: route, plan, execute, deliver.

```mermaid
flowchart TD
    START([Task created]) --> ROUTE[Route: pick a model]
    ROUTE --> MATCH{Template<br/>matches?}
    MATCH -- one --> TPL[Instantiate template]
    MATCH -- several --> CHOICE[[Template choice gate]] --> TPL
    MATCH -- none --> WRITE[Model writes a plan]
    WRITE --> COMPILE{Compiler<br/>accepts?}
    COMPILE -- no --> REPAIR[Model repairs from the<br/>compiler errors] --> COMPILE
    COMPILE -- yes --> PGATE
    TPL --> PGATE[[Plan gate]]
    PGATE --> STEP

    subgraph STEP_LOOP["For each step"]
        STEP[Next step] --> KIND{Step kind}
        KIND -- tool --> DECIDE[Model decides the arguments]
        DECIDE --> VALID{Schema and<br/>semantic check}
        VALID -- invalid, retries left --> DECIDE
        VALID -- still invalid after retries --> DEFAULT[Run the template<br/>default arguments]
        VALID -- valid --> SIDE{Side effect?}
        DEFAULT --> SIDE
        SIDE -- yes --> AGATE[[Action gate]] --> RUN
        SIDE -- no --> RUN[Run the tool]
        RUN --> OK{Tool ok?}
        OK -- failed twice, default exists --> DEFAULT
        OK -- failed twice, no default --> ESC{Stronger<br/>model?}
        ESC -- yes --> TIDE[Wait for the tide,<br/>retry on that model] --> DECIDE
        ESC -- no --> HAND([Hand back])
        OK -- yes --> REC[Write ledger records]
        KIND -- model task --> MT[Structured output<br/>schema-checked] --> REC
        KIND -- code --> CODEP[Write, run in sandbox,<br/>fix from traceback] --> REC
    end

    REC --> MORE{More steps?}
    MORE -- yes --> STEP
    MORE -- no --> CHECKS[Consistency, provenance,<br/>citation checks]
    CHECKS --> DGATE[[Deliverable gate]]
    DGATE -- approve --> FINAL([Copy to final/ with marking])
    DGATE -- send back --> REVISE[Re-run from the draft<br/>step with the note] --> CHECKS
```

**Budgets.** `max_steps` caps the number of model calls per task, `max_retries` caps retries per decision, `max_code_iters` caps sandbox iterations and `max_repairs` caps plan repairs (all in `config/settings.yaml`).

**Code protocol.** A code step must answer with exactly one fenced Python block. The block is written to the job folder, run by the sandbox, and the result (exit code, tails, traceback head, new files) comes back as a `sandbox_result` record. The next attempt sees only the traceback and the previous script.

**Delegation.** A step may delegate a sub-question to a child task. The child inherits the parent's label as a floor, so a child can never produce something less classified than what the parent already read.

**Revisions.** When a reviewer sends a draft back, the note becomes a `user_input` record and the loop re-runs from the drafting step, keeping the evidence it already has.

**When a gate is skipped.** Two settings shorten the path without removing control. With `auto_start_read_only`, a plan that has no side effects and produces no file (a question) starts at once; its plan gate is recorded as decided by `system`. With `plan_approval_covers_drafts`, the person who approves a plan also approves its file-creating steps on the first pass, recorded as "approved with the plan"; a revision overwrites files and therefore asks again. The deliverable review is never skipped.

**Incomplete drafts.** If the drafting model still produces an invalid note after its retries, the renderer writes a skeleton with an explicit "incomplete" list instead of inventing content.

## 5. Routing

```mermaid
flowchart LR
    IN[Task text,<br/>attachments,<br/>workspace] --> R1

    subgraph R1["Stage 1: rules"]
        direction TB
        A1[Code file, traceback<br/>or explicit code request] --> CODE[route code]
        A2[Several files or<br/>several deliverables] --> AG[route agentic]
        A3[Single PDF] --> DOC[route document]
        A4[Single image] --> VIS[route vision]
    end

    R1 -- no rule --> C1["Classifier<br/>small model, JSON profile"]
    R1 --> P[Task profile]
    C1 --> P

    P --> S2

    subgraph S2["Stage 2: constraints"]
        direction TB
        B1[status active]
        B2[modality supported]
        B3[context fits]
        B4[tool protocol]
        B5[route allowed]
        B6[provenance policy]
        B7[pool can serve it]
    end

    S2 --> S3["Stage 3: score<br/>cheapest model whose quality<br/>meets the complexity threshold"]
    S3 --> OUT["Decision + one-line log<br/>+ JSON for the UI"]
```

A decision line looks like this:

```text
2026-09-17T04:10:23Z task=T40984344 profile={type=general mod=[text] tok≈4k lang=[en] cx=medium conf=0.75}
  candidates: qwen3-vl-8b q=0.84 cost=2.9s ✓ | gpt-oss-20b q=0.86 cost=3.5s+tide 110s ✓ | qwen2.5-coder-7b ✗ tools | granite-3.3-8b ✗ status
  threshold=0.80 → qwen3-vl-8b
```

- **Cost** is the model's step latency plus the expected wait for the tide when the model is asleep.
- **Low classifier confidence** raises the threshold by one level.
- **No candidate above the threshold:** the highest-quality candidate is chosen and the decision is marked `below_threshold`.
- **Adding a model** takes a registry entry, a `serve` block and a shadow evaluation. `workbench registry promote` then flips the entry to active with one audited change.

## 6. Planning

```mermaid
flowchart LR
    T[Template YAML<br/>or model plan JSON] --> RES[Resolve placeholders<br/>attachments, report_date,<br/>equipment_tag, step outputs]
    RES --> TYPE[Typed plan<br/>steps, inputs, output types]
    TYPE --> V1{Tools exist and<br/>arguments fit schemas}
    V1 --> V2{Inputs refer to<br/>earlier steps}
    V2 --> V3{Output types match<br/>what consumers need}
    V3 --> V4{Deliverables are<br/>produced}
    V4 --> V5{Label ceiling<br/>respected}
    V5 --> EST[Estimated time from<br/>per-step budgets]
    V1 -- error --> ERR[Compiler errors<br/>in plain language]
    V2 -- error --> ERR
    V3 -- error --> ERR
    V4 -- error --> ERR
    V5 -- error --> ERR
    ERR --> REP[plan.repair] --> TYPE
```

- **Templates** (`templates/*.yaml`) are versioned. A template matches on route, attachment types and intent keywords.
- **Placeholders** are resolved at run time. `script` and `content` arguments are kept verbatim, so code keeps its whitespace.
- **Promotion.** A successful untemplated plan can be saved as a template draft from the task page. An admin or document owner approves it on the Security page.
- **Errors** name the step and the problem, for example `step 3: input "tables" is not produced by any earlier step`, which is exactly what the repair prompt needs.

## 7. Evidence ledger and context compilation

```mermaid
flowchart TB
    subgraph Ledger["Evidence ledger (append-only)"]
        direction LR
        R1["R-T..-1<br/>user_input<br/>control"] --> R2["R-T..-2<br/>attachment"] --> R3["R-T..-3<br/>plan<br/>control"] --> R4["R-T..-4<br/>ocr_text"] --> R5["R-T..-5<br/>vlm_read"] --> R6["..."]
    end

    Ledger --> CC

    subgraph CC["Context compiler (fixed order)"]
        direction TB
        C1["1. System prompt"]
        C2["2. Tool schemas"]
        C3["3. Plan with step statuses"]
        C4["4. Records in ledger order<br/>control as text, data as quoted &lt;record&gt;<br/>old records as one-line summaries"]
        C5["5. Step instruction"]
        C1 --> C2 --> C3 --> C4 --> C5
    end

    CC --> SALT["Cache salt =<br/>HMAC(secret, task label)"]
    SALT --> M[Model request]
```

Each record carries:

| Field | Meaning |
|---|---|
| `id`, `seq`, `hash`, `prev_hash` | Identity and the hash chain (`R-<task>-<n>`). |
| `kind` | `user_input`, `plan`, `attachment`, `ocr_text`, `vlm_read`, `kb_chunk`, `graph_fact`, `sandbox_result`, `calc_result`, `check_result`, `model_output`, `tool_output`. |
| `trust` | `control` for the user's request and the approved plan; `data` for everything else. |
| `label` | Classification and compartments. |
| `anchor` | Document, revision, page and region, used for crops and citations. |
| `fields` | Typed values (quantity with unit, date, tag, party, PO, clause) with normalised forms. |
| `confidence` | `high`, `medium`, `low` or `uncertain`. |

**Cache salting.** The label-salted prefix means two tasks with different labels never share a KV-cache prefix (design 3.0.3). The evaluation confirms zero cache hits across label partitions. `CacheSimulator` models hit rates when running offline.

**Summaries.** Older records collapse to one-line summaries that change only at step boundaries, so the prompt prefix stays stable within a step. The model can pull a full record back with the `recall` tool.

## 8. Documents and dual reads

```mermaid
flowchart LR
    F[Attachment] --> KIND{Type}
    KIND -- text PDF --> PM[PyMuPDF text and tables]
    KIND -- scanned PDF / image --> OCR[OCR text<br/>sidecar in development]
    KIND -- text file --> TXT[Plain text reader]
    PM --> REC[ocr_text records per page]
    OCR --> REC
    TXT --> REC
    OCR --> REG[Critical regions:<br/>tag, dates, PO, stamp,<br/>handwriting]
    REG --> BLIND[VLM reads the crop<br/>without the OCR hint]
    BLIND --> CMP{Normalised<br/>values agree?}
    CMP -- yes --> HIGH[vlm_read, confidence high]
    CMP -- no --> ZOOM[Zoomed re-read with<br/>both candidates]
    ZOOM --> GLY{Choice equals a candidate<br/>and glyph-compatible with OCR?}
    GLY -- yes --> MED[confidence medium]
    GLY -- no --> UNC[confidence uncertain<br/>excluded from checks]
```

Classification markings found in page text (in English and Hindi) raise the attachment label. Uncertain reads are shown on the review page as "not checked" findings, never silently used.

## 9. Knowledge base and plant graph

```mermaid
flowchart TB
    subgraph Ingest
        MD[Procedures, P&IDs,<br/>past approval notes] --> CH[Chunk by heading,<br/>keep clause ids,<br/>revision, validity, ACL]
        CH --> EMB[Hashing embedder] --> VS[(Vector store)]
        CH --> BM[(BM25 index)]
        CH --> RV[Revision index<br/>in force on a date]
        AR[Asset register CSV] --> G[(Plant graph<br/>SQLite)]
        CH --> G
        HI[Inspection history] --> G
    end

    subgraph Query
        Q[Queries + as_of date] --> D[Dense search]
        Q --> S[Sparse search]
        Q --> TAGS[Tags in the query]
        TAGS --> GX[Graph expansion:<br/>tag to documents,<br/>earlier readings]
        D --> RRF[Reciprocal rank fusion]
        S --> RRF
        GX --> RRF
        RRF --> FIL[Filter: workspace, ACL group,<br/>label ceiling, revision in force]
        FIL --> RR[Lexical rerank]
        RR --> OUT[kb_chunk and graph_fact records,<br/>currency warnings]
    end
```

The graph holds tags, equipment classes, documents, clauses, vendors, purchase orders and inspections. Edges carry validity dates, so "as of" queries walk only edges that were valid then. `graph_lookup` answers structural questions in both directions: the neighbours of a tag, or the equipment a document governs through its equipment classes.

When a new revision of a procedure arrives, `workbench kb impact <doc> <rev>` lists the changed clauses and limits, the past notes that cite them and the equipment they govern.

## 10. Deliverables and checks

```mermaid
flowchart LR
    DRAFT[Model draft JSON<br/>with record citations] --> REND[Renderer<br/>docx, xlsx, pptx]
    REND --> FILE[drafts/ file<br/>marked with the task label]
    FILE --> PROV[Number provenance:<br/>every figure matched to a<br/>source or computed record]
    FILE --> CITE[Citation verification:<br/>numbers, tags and dates in the<br/>cited records, rerank support,<br/>currency of the revision]
    EXT[Extracted findings] --> CONS[Consistency rules<br/>register, calibration,<br/>limits, trend, PO, vendor]
    PROV --> REVIEW
    CITE --> REVIEW
    CONS --> REVIEW[Review page]
    REVIEW --> BLOCK{Open mismatches or<br/>unsourced figures?}
    BLOCK -- yes --> LOCK[Approve disabled,<br/>reason shown]
    BLOCK -- no --> APPROVE[Approve: final/ copy,<br/>audit entry]
```

- **Consistency rules** are YAML (`rules/consistency/`) and support lookups, aggregates, fuzzy matches and a least-squares trend projected to the next inspection date.
- **Provenance** normalises units (via pint) before comparing, within `number_tolerance`. The reviewer can correct a figure, link it to a record or confirm it, and each action is audited.
- **Spreadsheets** keep live formulas. Computed cells count as derived figures.

## 11. Tidal pool

```mermaid
sequenceDiagram
    participant J as Job queue
    participant P as Pool manager
    participant R as Resident models
    participant S as Swap-slot model

    Note over R: awake (qwen3-vl-8b, qwen2.5-coder-7b)
    Note over S: asleep, weights in host RAM
    J->>P: Step needs gpt-oss-20b
    P->>P: Queue swap job
    alt oldest waited > max_wait_s or queue >= max_queue, and residents awake >= min_resident_s
        P->>R: Let running steps finish, then sleep
        P->>S: Wake
        P-->>J: Run all queued swap jobs
        P->>S: Sleep
        P->>R: Wake
    end
```

The router adds the expected wait to a sleeping model's cost, which is why simple work stays on resident models. `HttpVLLMControl` calls vLLM's sleep and wake endpoints (development mode); `FakeVLLMControl` simulates wake times scaled by `time_scale`.

## 12. Host services

Both daemons are written in Go with the standard library only, have no third-party modules, and share `internal/udsserver` for transport, request ids, access logs and error bodies.

```mermaid
flowchart LR
    subgraph Python
        SC[SandboxdClient]
        EC[EgressdClient]
        GUARD[In-process egress guard]
    end
    subgraph sandboxd
        VAL[Validate request:<br/>job dir under the<br/>workspace root, limits]
        BACK{Backend}
        DOCK[docker run<br/>--network none,<br/>read-only, caps dropped,<br/>cpu, memory, pids]
        DEV[dev subprocess]
        FILES[New and changed files<br/>with SHA-256]
    end
    subgraph egressd
        COL[Collectors:<br/>nftables counters,<br/>conntrack, audit log]
        REP[Reports from<br/>host and sandbox]
        ST[State: counters,<br/>events ring buffer,<br/>breach flag]
        TEST[Self-test:<br/>raw IP, DNS, sandbox]
    end

    SC --> VAL --> BACK
    BACK --> DOCK --> FILES
    BACK --> DEV --> FILES
    DOCK -. blocked connects .-> REP
    GUARD -. blocked connects .-> REP
    EC --> ST
    COL --> ST
    REP --> ST
    EC --> TEST
```

In **enforced** mode `egressd` refuses to start unless the nftables table exists with an output chain whose policy is drop and no upstream nameserver is configured. Any counted external connection sets the breach flag, which the UI shows in red.

## 13. Persistence

| Store | Contents |
|---|---|
| `var/workbench.db` (SQLite, SQLAlchemy Core) | Ledger records, task states, gates, files and labels, downgrade requests, plant graph, model outcomes. |
| `var/audit.jsonl` | Hash-chained audit log; `workbench audit verify` recomputes the chain. |
| `var/workspaces/<ws>/inputs`, `drafts/<task>`, `final/<task>` | Files. Drafts and final files sit in a folder per task. Each file's label is stored in a `.label.json` sidecar and in the database. |
| `var/workspaces/_jobs/<task>/` | Sandbox job folders: the script, its inputs and whatever it wrote. |
| `var/evidence/<task>/` | Crops of critical fields for the review page. |
| `var/kb/` | Chunks (JSONL) and the vector store. |
| `var/secret.key` | HMAC key for cache salts, created on first run with owner-only permissions. |
| `run/` | Daemon sockets (or address and token files on Windows) and daemon state. |

All of `var/`, `run/`, `bin/` and `reports/` are generated and ignored by git.
