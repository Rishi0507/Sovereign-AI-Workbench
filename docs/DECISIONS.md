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
| D28 | Agent | Read-only questions start without a plan approval |
| D29 | Agent | Approving a plan covers its new drafts |
| D30 | Files | Drafts and final files live in a folder per task |
| D31 | UI | Inter is bundled as a local font |
| D32 | API | Tasks carry a revision counter for change detection |
| D33 | UI | Pages update in place instead of being rebuilt |
| D34 | Agent | Requests for counts add a counting step to any plan |
| D35 | Agent | Follow-ups continue one conversation |
| D36 | Agent | Small talk and vague requests get a reply, not a search |
| D37 | Files | Approved templates live with the runtime data |

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

### D28. Read-only questions start without a plan approval

**PRD:** Every plan passes a plan gate.
**Decision:** With `auto_start_read_only: true` (the default), a valid plan that has no side-effecting step and no deliverable starts immediately. The plan gate is still recorded, with `decided_by: system` and the note "read-only plan: started without a plan approval", and audited like any other decision.
**Why:** Asking a question and then having to approve "read, search, answer" made simple questions feel slow while protecting nothing: such a plan can only read what the user may already read. Plans that create files still wait for a person. Code: `workbench/agent/loop.py::_plan`.

### D29. Approving a plan covers its new drafts

**PRD:** Side-effecting steps pass an action gate.
**Decision:** With `plan_approval_covers_drafts: true` (the default), when a person approves a plan, the file-creating steps listed in it run on the first pass without a second prompt. Each is recorded as an approved action gate with the approver's id and the note "approved with the plan". During a revision, which overwrites existing drafts, the action gate appears again. Deliverable review is unchanged: nothing reaches `final/` without it.
**Why:** The plan view already lists these steps and marks them "creates a file", so the second prompt repeated a decision the user had just made. The design only requires a click for overwrites and for moving to `final/` (design 2.1), and both still need one. The API tests switch the setting off to keep exercising the explicit gate. Code: `workbench/agent/loop.py::_action_gate`.

### D30. A folder per task for drafts and final files

**Decision:** `drafts/<task id>/` and `final/<task id>/` replace the flat `drafts/` and `final/` folders.
**Why:** With flat folders, two tasks rendering `approval-note.docx` overwrote each other's drafts. Per-task folders make every first render a new file, which is also what makes D29 safe. Code: `workbench/workspace.py::write_draft`.

### D31. Inter is bundled as a local font

**Decision:** The UI ships the Latin subset of the Inter variable font (`workbench/ui/static/fonts/inter-latin.woff2`, 48 KB) with its SIL Open Font License (`OFL.txt` next to it), and falls back to the system font stack.
**Why:** The workbench cannot load web fonts from the internet, and system fonts differ widely between the Windows and Linux machines that open it. The Content-Security-Policy still allows only same-origin assets.

### D32. A revision counter for change detection

**Decision:** Every task carries `revision_no`, which increases on every save (the store takes the highest known value, so a stale copy saved later still moves it forward). The UI redraws when it changes.
**Why:** `updated_at` has one-second resolution. A task that moved through several steps within the same second looked unchanged to the page, which then stayed on "working" until it was reloaded. Code: `workbench/agent/state.py::TaskStore.save`.

### D33. Pages update in place

**Decision:** The interface keeps a small in-place updater (`fill` in `app.js`): a new render is compared with what is on screen, matching elements are patched (text, attributes, handlers), keyed rows are moved rather than recreated, and only genuinely new elements animate in. Height changes animate, tab bars and panels that own their own state are marked `data-keep`, and text the user is typing is never overwritten.
**Why:** Replacing whole sections on every poll made the page flicker, collapsed open sections and reset scroll positions. A framework would need a build step and third-party code, which the air-gapped design avoids. The browser tests check that the conversation element survives a whole task run.

### D34. Requests for counts add a counting step

**Decision:** A `document_stats` tool counts pages, words (numbers count as one word, marking lines are skipped), characters and tables in the extracted text and records each result as a citable `calc_result`. When a request asks for such counts ("and also tell me the number of words"), `planning/extras.py` inserts a "Count pages and words" step after the first reading step, including into saved templates, and feeds its result to any later answer step. The counted facts are listed with the task result.
**Why:** A saved template covers the main intent only; without this, the extra part of a combined request was silently dropped. Counting is arithmetic, so it is done by code, not guessed by a model.

### D35. Follow-ups continue one conversation

**Decision:** A follow-up is a new task whose `followup_of` points at the conversation's first task; follow-ups of follow-ups point there too. It reuses the first task's attachments and takes the conversation's highest classification as its floor. The task page shows the whole conversation, the reply box stays available whenever the last turn has finished, and the sidebar lists each conversation once.
**Why:** Moving to a new page for every follow-up lost the thread, and follow-ups previously did not inherit the classification of what had already been read.

### D36. Small talk and vague requests get a reply

**Decision:** A `chat_reply` model task answers greetings, thanks, questions about the workbench, one-word nudges such as "continue", and requests that need a document none was given for ("summarise any document"). The reply asks what is needed and offers up to four next steps, each naming real workspace files the user may read; choosing one sends it as the next message with that file attached. A request that names an input file ("summarise the vendor contract") gets that file attached automatically. The offline backend implements the reply with rules in `workbench/llm/conversation.py`; a real model receives the files and the conversation so far in `chat_reply.j2`.
**Why:** Treating every message as a search produced answers such as a random procedure sentence in reply to "hello". A reply that asks for the missing input is what a person would do.

### D37. Approved templates live with the runtime data

**Decision:** Built-in templates stay in `templates/`. Drafts saved from tasks go to `var/templates/drafts` and approved templates to `var/templates`; the library loads both.
**Why:** Promoting a plan from the interface used to write into the source tree, so a server's own templates showed up as uncommitted code changes and changed the test fixtures.

### D38. A local model writes the conversational replies

**Decision:** `WB_CHAT_MODEL` names a small instruct model on `WB_CHAT_ENDPOINT` (loopback). `ConversationalBackend`
sends it only `chat.reply` requests, with a short instruction, the kind of message it is answering, and the earlier
turns when the message depends on them. Everything else, including plans, extraction, drafting, summaries and
checks, stays with the deterministic backend. The reply text comes from the model; the suggestion buttons keep
coming from the workspace files the user may read. Questions about the outside world, such as the weather, are
answered with a plain statement that the workbench cannot know them rather than with a document search. If the
model server is unreachable, the deterministic reply is used and the interface says so.

The same model also sorts each new message before planning: one that needs none of the user's documents
(arithmetic, the time, general knowledge) is answered directly instead of being searched. A message naming plant or
document work goes to the documents without asking the model, and any router answer that does not clearly say
otherwise is treated as document work, so a wrong sort cannot silence a real question.

This is a deviation from the PRD rule "no GPU, no model weights". It is opt-in and off by default: `chat_model`
is empty in `config/settings.yaml`, the tests pin it off, no weights live in the repository, and
`python scripts/dev.py chat-model` is the only thing that downloads them, once, when a person asks for it. At run
time the model is reached on loopback only, so the offline rule still holds.

**Why:** The rules gave the same sentence to every greeting, which reads like a script rather than an assistant.
A 1.5B model on a CPU is good enough for conversation and far too weak for planning or for figures that must be
traced, so only the conversation was given to it. Code: `workbench/llm/chat_model.py`.

### D40. Hosted models can answer, paced and with the offline rules kept

**Decision:** `llm_backend: groq` sends model calls to an OpenAI-compatible hosted service described in
`config/groq.yaml`: the base URL, the environment variable holding the key, and which hosted model answers for each
name in `config/models.yaml`, so routing, the quality table and the model panel are unchanged. Calls are made one at
a time with a minimum gap between them; a refusal is waited out for the time the service asks, and the whole process
waits, since the allowance is shared. A model that refuses strict JSON schemas is asked for plain JSON with the
schema stated in the prompt, and that is remembered. The allowance each model reports is shown in the model panel.
The offline rules stay loaded: they answer when the service cannot be reached or gives up, so a task never fails
because of a rate limit. The host is on the egress allowlist, and the security page says plainly that model calls
leave the server.

**Why:** The build machine has no GPU, so the inference layer described in the design document cannot run on it and
every answer came from the deterministic rules. Standing that layer in with a hosted service exercises the agentic
layer against real model output while the registry, router, plans, checks and provenance stay unchanged, which is
what the layer is meant to be judged on. It is a deviation from "nothing leaves the premises": it is therefore
opt-in, off in the committed configuration, listed in the egress allowlist, stated on the security page, and
reversed by one setting. The target deployment uses `openai` against vLLM on loopback, where the same client code
and the same registry names apply. Code: `workbench/llm/remote.py`, `workbench/llm/select.py`.

### D39. Documents are read in place, and only decision makers are told what is waiting

**Decision:** `GET /api/files/{id}/preview` returns the readable content of a stored file: paragraphs, headings and
tables for Word files, PDFs, spreadsheets, Markdown and scripts, capped so a large document cannot fill the browser.
The library and the task page open that preview when a file is clicked, with the download beside it. The library
lists a task as waiting only for the person who can actually decide it, which is the task owner or an approver;
everyone else sees the task and its files as before, subject to clearance.

**Why:** Checking a draft meant downloading it and opening Word. The waiting list was also shown to people with no
authority to act on it, such as the internal developer account, which invited them to open approvals they cannot
give. Code: `workbench/documents/preview.py`, `workbench/api/routes_library.py`.

### D41. Classification is shown on documents, not as a banner on every page

**Decision:** The classification of a file is shown on its card, in its preview, on the review sheet and stamped
into every exported file, where it describes something concrete. The page-wide marking chip in the top bar is
removed. The account list shows people by name; their clearance governs what they may open and is not displayed as
a badge beside them.

**Why:** A banner reading CONFIDENTIAL above a conversation about the weather trains people to ignore markings,
which is the opposite of what a marking is for. Enforcement is unchanged: clearance still decides what is listed,
what can be opened and what a share or downgrade may do.

### D42. The plant graph and its drawings are part of the interface

**Decision:** `GET /api/graph` exposes the plant graph that the knowledge base already builds, and the Plant page
draws it: equipment at the centre, with the drawings, clauses, inspections, vendors and orders attached to it.
Selecting a tag narrows the view to its neighbourhood and lists what is recorded against it. A second tab shows the
P&ID sheets themselves, drawn as SVG with each tag as a clickable item that opens the same record panel. Nodes,
edges and sheets are filtered by the reading ceiling of the signed-in user, so the shape of the graph reveals
nothing above their clearance.

**Why:** An inspection engineer starts from a tag or a drawing, not from a search box. The retrieval already
understood equipment; the interface did not show it. The sheets are synthetic fixtures generated from the asset
register, and say so in their title block.

### D43. A draft can be rewritten by hand, and the machine says it cannot vouch for it

**Decision:** Any paragraph of a Word draft can be rewritten in the review page while it is still a draft. The
superscript reference markers the renderer placed are left where they are, the file is saved in place, and the
number provenance check runs again, so a figure typed in by hand with no record behind it is flagged at once.
Every edit is written to the audit log with the text before and after. An edited paragraph blocks approval until an
approver accepts it; accepting is recorded with the approver's name. Approved files cannot be edited.

The approver sees both sentences in full: the one that was there with the removed words struck through, and the
one that replaced it with the added words marked.

**Why:** Reviewers rewrite a sentence; that is normal engineering practice. What must not happen is edited text
quietly inheriting the machine's assurances. Recording the edit, re-checking the figures and requiring an approver
to own the wording keeps the traceability claim honest. Showing the whole of both sentences, rather than a clipped
summary, is what lets the approver judge the change.

### D44. Public documents sit beside the generated fixtures

**Decision:** `fixtures/public/` holds publicly available documents that this project did not write, currently a
NASA report on corrosion risk in underground piping and a scanned P&ID published by the United States Atomic
Energy Commission, both public domain. `fixtures/public/SOURCES.md` records for each file the title, author,
licence, source, retrieval date, size, checksum and any change made to it, and a hygiene test fails if a file is
present without that record or if its checksum no longer matches. They are labelled Unclassified, copied into the
demo workspace beside the generated fixtures, and the scanned sheet appears in the Drawings tab with a note that
its tags are pictures and cannot be clicked.

**Why:** The brief asks for open models and publicly available document samples. The generated fixtures stay,
because they carry the seeded faults the demo has to catch, but a reviewer should also see the workbench read a
document nobody here controls.

### D45. A hosted service is met on its own terms

**Decision:** The remote backend adapts to what a hosted service accepts: it drops the label-salted cache field,
which belongs to a local vLLM; it does not offer tools on a request that asks for JSON; and when a model refuses a
strict schema, or answers a JSON request with a tool call, the shape is stated in the prompt instead. Each of those
is remembered per model, so it is tried once. Every fall back to the offline rules is written to the audit log with
its reason and counted in `GET /api/health`.

**Why:** These refusals were silent. The pipeline kept working because the rules answered, so a summary looked
plausible while no model had been near it. A fallback is now a recorded event rather than an invisible one.
