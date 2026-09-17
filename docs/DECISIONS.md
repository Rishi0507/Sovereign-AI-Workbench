# Decisions

The implementation follows [`design/IMPLEMENTATION_PRD.md`](design/IMPLEMENTATION_PRD.md). The PRD asks that every deviation, and every choice made where the specification was ambiguous, be written down here. Each entry states the decision, the reason, and where it lives in the code.

## Index

| # | Area | Decision |
|---|---|---|
| D1 | Repository | The root README describes the project; the design document lives in `docs/design/` |
| D2 | Dependencies | Two small runtime dependencies beyond the PRD list |
| D3 | Runtime | Python 3.12 for development and type checking |
| D4 | Ledger | Two extra record kinds: `attachment` and `tool_output` |
| D5 | Router | Rule order and an extra rejection reason |
| D6 | Transport | Loopback TCP with a token file on Windows |
| D7 | API | All routes under `/api` |
| D8 | Sandbox | `FakeSandbox` runs a subprocess |
| D9 | Sandbox | The in-sandbox probe refuses loopback too |
| D10 | Documents | Plain text attachments become `ocr_text` records |
| D11 | Planning | `required` arguments are relaxed at compile time |
| D12 | Planning | Question plans have no deliverable |
| D13 | Agent | Incomplete drafts render as a marked skeleton |
| D14 | Agent | Approval waits live in memory |
| D15 | Jobs | The scheduler lives in `jobs/queue.py` |
| D16 | Evaluation | `force_model` override for shadow and code evaluation |
| D17 | Registry | `serve` parameters per profile in `models.yaml` |
| D18 | Registry | Seed quality values are illustrative |
| D19 | Offline backend | Deliberate first failures, grounded answers |
| D20 | Knowledge base | Deduplication keys stored in record fields |
| D21 | Knowledge base | `graph_lookup` also answers "what does this document govern" |
| D22 | UI | Content-hashed static assets |
| D23 | Security | Development authentication |
| D24 | Deployment | `sandboxd` runs as the application user |
| D25 | Deployment | The application runs under systemd, not compose |
| D26 | Adapters | No optional GPU adapters are shipped |
| D27 | Tooling | Wider strict typing, conditional race detector |

---

### D1. The root README describes the project

**PRD:** `README.md` at the root is a copy of the design document.
**Decision:** The root `README.md` introduces this implementation (what it does, how to run it, how it is tested). The design document is kept verbatim as [`design/SYSTEM_DESIGN.md`](design/SYSTEM_DESIGN.md), next to the diagrams and the PRD. References such as "README section 4.2" in code comments and in the PRD point to that file.
**Why:** A repository landing page has to explain the code that is in it; a 1,100-line design document is the wrong first page.

### D2. Two extra runtime dependencies

**Decision:** `pyyaml` (configuration, templates and rules are YAML) and `python-multipart` (FastAPI needs it for file uploads) are runtime dependencies. `jinja2` is used for both the UI and the prompt templates, as the PRD's UI line implies.
**Why:** Both are required by features the PRD specifies; neither performs network I/O.

### D3. Python 3.12

**Decision:** The package supports Python 3.11 and newer (`requires-python = ">=3.11"`); development, tests and `mypy` (`python_version = "3.12"`) use 3.12.
**Why:** 3.12 was the interpreter available on the development machine. No 3.12-only syntax is used.

### D4. Extra record kinds

**Decision:** `RecordKind` adds `attachment` (one record per attached file: name, pages, label, hash) and reserves `tool_output` for generic tool results that are neither evidence nor computation.
**Why:** The high-water mark must include an attachment's label from the moment it is attached, before any page is read. `attachment` counts as a source kind for provenance and labels. Code: `workbench/core/models.py`.

### D5. Router rule order and rejection reason

**Decision:** Stage 1 rules run in this order: code attachment, traceback, explicit code request, several attachments or several deliverables (`agentic`), then a single image (`vision`) or a single PDF (`document`). Stage 2 rejects a model whose `serves` list does not contain the route with the reason `route`, in addition to the PRD's `modality | context | tools | provenance | pool | status`.
**Why:** Trace E attaches four PDFs; checking "single PDF" first would never see the multi-file case. Without a `route` constraint a coder model could be chosen for a contract summary purely on cost. Code: `workbench/router/rules.py`, `workbench/router/constraints.py`.

### D6. Loopback TCP with a token on Windows

**PRD:** Python and Go talk only over Unix domain sockets, never TCP.
**Decision:** On POSIX the daemons use Unix sockets with mode `0660`, exactly as specified. On Windows, where CPython has no `AF_UNIX`, `service_transport: auto` makes the daemons listen on an ephemeral `127.0.0.1` port, write the address to `run/<name>.addr` and a random token to `run/<name>.token`, and require `Authorization: Bearer <token>` on every request.
**Why:** Development happens on Windows. The token keeps other local processes out, and production servers (Linux) never use this path. Code: `go/internal/udsserver`, `workbench/security/transport.py`.

### D7. API under `/api`

**Decision:** Every JSON route from PRD section 4.17 is served under the `/api` prefix; UI pages are served at `/`, `/t/{id}`, `/t/{id}/review`, `/models` and `/security`.
**Why:** It keeps page routes and data routes from colliding and makes the reverse proxy rules simple. Code: `workbench/api/app.py`.

### D8. `FakeSandbox` runs a subprocess

**PRD:** The fake sandbox runs in-process.
**Decision:** `FakeSandbox` runs the script in a child Python process through the same `probe/run.py` entry point that `sandboxd` uses, and returns the same result shape.
**Why:** Running agent code inside the application process would let a script crash or modify the server. A subprocess keeps the probe, the timeout and the file snapshot identical to the real daemon. Isolation is still probe-only, which is why production uses `sandboxd` with Docker. Code: `workbench/tools/sandbox.py`.

### D9. The sandbox probe refuses loopback

**Decision:** Inside the sandbox every IP `connect()` and every name lookup is logged and refused, including loopback. Only `AF_UNIX` passes.
**Why:** A script has no legitimate reason to reach local services (the model servers and the daemons run on the host). With the `dev` backend there is no network namespace, so refusing loopback is what prevents a script from calling a local model server or the workbench API.

### D10. Plain text attachments become `ocr_text` records

**Decision:** Markdown, CSV and other text attachments are read by the same `read_document` tool and produce `ocr_text` records through the `text` reader engine.
**Why:** Downstream consumers (extraction, answering, provenance) already understand `ocr_text`; a separate kind would duplicate every consumer. The record summary names the engine, so the origin stays visible. Code: `workbench/documents/readers.py`.

### D11. `required` arguments are relaxed at compile time

**Decision:** When the compiler validates a step's default arguments against the tool schema, missing `required` properties are allowed; they are checked again, strictly, when the step runs with its resolved arguments.
**Why:** Some arguments only exist at run time (for example the `script` of `run_python`, written by the model in an earlier step). Code: `workbench/planning/compiler.py`.

### D12. Question plans have no deliverable

**Decision:** A plan for a question ("Which clause covers warranty?") ends with an `answer_question` model task and declares no file deliverable. The answer is shown in the task view with its citations, and the task label is the high-water mark of what was read.
**Why:** Forcing a `.docx` for a one-line answer adds an approval step with no value. Follow-up questions use the same path.

### D13. Incomplete drafts render as a marked skeleton

**Decision:** If the drafting model still returns an invalid note after its retries, the step is marked `incomplete` and the renderer writes a skeleton note (title, subject, empty sections) with an "Incomplete steps" section that tells the reviewer what to complete by hand. The checks and the review page run as usual.
**Why:** The PRD requires a visible failure rather than invented content, and the reviewer can still send the task back with a note.

### D14. Approval waits live in memory

**Decision:** `QueueApprove` blocks the job thread on an in-memory event until a decision arrives through the API. Gates themselves are persisted in the task state, but a waiting thread is not: when the server restarts, jobs that were running are marked failed with the reason "server restarted while the job was running", and the task has to be started again.
**Why:** A persistent continuation mechanism is out of scope for the single-server target. Code: `workbench/agent/approvals.py`.

### D15. The scheduler lives in `jobs/queue.py`

**PRD layout:** `jobs/queue.py` and `jobs/scheduler.py`.
**Decision:** The queue, the worker threads and the per-model concurrency slots are one module.
**Why:** The scheduler is a few dozen lines that share the queue's lock; splitting them would only add an import cycle.

### D16. Evaluation-only model override

**Decision:** Task metadata may contain `force_model`, which replaces the router's choice and appends "(evaluation override)" to the decision line. The API does not accept this key (`ALLOWED_META`); only the evaluation code sets it.
**Why:** Shadow evaluation and per-model code evaluation must run a specific model through the unchanged agent loop. Code: `workbench/agent/loop.py`, `workbench/eval/metrics.py`.

### D17. `serve` parameters per profile

**Decision:** Each registry entry in `config/models.yaml` has a `serve` block keyed by hardware profile (port, GPU memory fraction, context length). `workbench registry render-serve` turns these into `deploy/vllm_commands.sh`, which also exports `VLLM_SERVER_DEV_MODE=1` because vLLM only exposes the sleep and wake endpoints in that mode.
**Why:** One source of truth for what runs where; the generated script is never edited by hand.

### D18. Seed quality values are illustrative

**Decision:** The quality tables in `config/models.yaml` are plausible seed values, marked as such in the file. They drive routing until `workbench eval --write-registry` measures real models and proposes replacements.
**Why:** No GPU was available to measure them. The router, the scorer and the proposal workflow are exercised end to end with these values.

### D19. Offline backend behaviour

**Decision:** `HeuristicBackend` implements every model purpose deterministically from the records it is given. As the PRD requires, it fails naively on purpose twice: the first Trace E plan assumes a full read also yields tables (the compiler rejects it and the repair fixes it), and the first Trace B script guesses a column name (the sandbox raises `KeyError`, and the fix reads the real header). Question answering ranks sentences and table rows with inverse-frequency term weights, demotes boilerplate repeated across passages, prefers statements about a named equipment tag and sections whose heading matches, and answers structural questions from the plant graph.
**Why:** The UI suggestions and the evaluation should produce answers a reviewer can check, not placeholders. Code: `workbench/llm/heuristic.py`.

### D20. Deduplication keys in record fields

**Decision:** `search_kb` and `graph_lookup` store a `source_key` text field on each record and reuse an existing record when the same passage or fact is retrieved again in the same task. Citation checks ignore that field.
**Why:** Repeated searches would otherwise fill the ledger and the prompt with duplicates, and the key must survive restarts, so it lives in the persisted record. Code: `workbench/tools/search_kb.py`.

### D21. `graph_lookup` by document

**Decision:** `graph_lookup` accepts either `tag` (neighbours of a tag, as specified) or `doc` (the equipment governed by the revision of that document in force, through its equipment classes). Both respect the retrieval ceiling.
**Why:** "Which pumps are governed by SOP-MECH-014?" is a structural question that text search cannot answer reliably, and the graph already holds the `governed_by` edges used by the impact report. Code: `workbench/kb/retrieve.py::governed_equipment`.

### D22. Content-hashed static assets

**Decision:** The UI references `app.css` and `app.js` with a `?v=<hash of the file contents>` query.
**Why:** Browsers kept serving an old script after an upgrade; a content hash makes every change visible immediately without cache headers that would need tuning per proxy. Code: `workbench/ui/pages.py`.

### D23. Development authentication

**Decision:** Until the directory adapter is installed, the user comes from the `X-User` header or the `wb_user` cookie set by the user switcher, and the UI labels this as "dev". nginx clears `X-User` from client requests.
**Why:** The PRD specifies `users.yaml` as the development stand-in for the directory; the switcher makes the role-based flows (approver, security officer, document owner) demonstrable. Code: `workbench/api/deps.py`, `deploy/nginx.conf`.

### D24. `sandboxd` runs as the application user

**PRD:** `workbench-sandboxd.service` runs as `wb-sandbox`, and job folders must be owned by "the workbench user".
**Decision:** `sandboxd` checks that a job folder is owned by the user it runs as, and the unit runs it as `wb-app` (the application user, which creates the job folders), with the `docker` group added to that unit only.
**Why:** With two different users the ownership check would refuse every job. Running the daemon as the folder owner keeps the check strict while the web process itself never holds Docker access. Code: `go/internal/pathjail`, `deploy/systemd/workbench-sandboxd.service`.

### D25. The application runs under systemd

**Decision:** `deploy/systemd/workbench.service` runs the application from a virtual environment. `deploy/compose.yaml` runs only the model servers, the router classifier and nginx (all on the host network, bound to loopback except nginx on 443).
**Why:** The daemons are already systemd services; running the application the same way avoids a container image with access to the Docker group or the host sockets. The reference compose file previously listed a Qdrant service, which was removed because the default vector store is the on-disk `SimpleVectorStore`.

### D26. No optional GPU adapters

**PRD:** Optional adapters for `torch`, `vllm`, `transformers` or `paddleocr` may live under `workbench/adapters/optional/`.
**Decision:** None are shipped. Real models are reached through `OpenAICompatBackend` (vLLM's server API). OCR in development comes from `.ocr.json` sidecars; a PaddleOCR reader would implement the same `Reader` interface in `workbench/documents/readers.py`. `QdrantStore` exists as an optional, lazily imported vector store.
**Why:** None of them can be exercised or tested without a GPU; untested adapters would be dead code.

### D27. Typing and Go race detection

**Decision:** `mypy --strict` covers `core`, `planning`, `checks` and `router` (the PRD asks only for `core`). `go test -race` runs where cgo and a C compiler exist; on Windows without a C toolchain the task prints a note and runs without `-race`.
**Why:** The deterministic decision code benefits most from strict types. The race detector requires cgo.
