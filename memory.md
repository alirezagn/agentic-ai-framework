# memory.md — session memory for agentic-ai-framework

> Last updated: 2026-10-03 (integrity batch: **suite fully green — 1213
> passed / 0 failed**, OPEN-1 closed by atomic checkpoint writes + repaired
> indexes; deploy tests hermetic under an operator's exported channel;
> `ruff check .` baseline pinned and at 0)
>
> **Authority:** this file records *verified* state, not intended state. Every
> "open" entry below was re-checked against the code before writing. If an entry
> here disagrees with `ARCHITECTURE_COMPLIANCE_AUDIT.md`, the audit is the
> finding of record and this file is the status of record; they are not the same
> document on purpose.

---

## Where the project stands

- **Runtime is v2.0.0** (`pyproject.toml`; `orchestrator/cli.py:84`; verified
  `./bin/orchestrator --version` and `python3 -m orchestrator.cli --version` →
  `orchestrator 2.0.0`). `ORCHESTRATOR_GUIDE.md` is the technical reference;
  `HOW_TO_USE.md` is the operator walkthrough.
- **Suite: 1213 collected, 1213 passed / 0 failed** — `python3 -m pytest -q`,
  197 s, 2026-10-03, verified in a **clean env and in the operator's shell
  (`ORCHESTRATOR_DEPLOY_ENABLED=1 ORCHESTRATOR_DEPLOY_ALLOWLIST=*`)**. The
  number the four docs assert is the *collected* count (1213) and is current.
  Offline-safe: an autouse fixture blocks outbound TCP while allowing loopback,
  so a stray `ANTHROPIC_API_KEY` cannot bill real API calls. `FakeDeployRunner`
  substitutes for the real runner, so no test spawns a process unless it is
  deliberately exercising one, and `tests/test_deploy_ground_truth.py` scrubs
  `ORCHESTRATOR_DEPLOY_*` for every test so an exported channel in the shell
  cannot turn a "default refuses" assertion into a real spawn.
  **Lint:** `ruff check .` → 0 errors (baseline `E4/E7/E9/F` pinned in
  `pyproject.toml`; install `python3 -m pip install -e .[dev]`). Never claim
  green from memory — re-run both.
- **Repo state:** branch `main`, **in sync with `origin/main`** after this
  batch's push (hashes in the commits list below; this batch is `69bfb72` +
  its docs commit). The only remaining tree dirt is the **pre-existing
  intentional** dirt: `projects/sys_mon/{PROJECT,TASKS,CHANGELOG,CURRENT_STATE}.yaml/.md`,
  the still-tracked `projects/sys_mon/__pycache__/test_sys_mon…pyc`, and a
  stray **0-byte file named `=`** in the repo root. Do not describe the tree as
  clean until that is resolved, and do not sweep those files into an unrelated
  commit.
- **Commits since the audit pass** (all 2026-10-02): `1ee05ae` HIGH-01..04,
  `2c55657` dependency automation, `2cd1fed` completed `sys_mon` sample,
  `69a55d1` CLI init nesting fix, `2408d42` materializer no-op edit,
  `762df40` advisory-lock fd release, `117ff10` cross-component data
  contracts, `59de074` doc count sync, `85c26b5` entrypoint + interface
  alignment, `0fbc665` UI fidelity + pre-implementation signature checks.
  Then (2026-10-02 night → 10-03): `ae8a721` truncation recovery + PEP 668
  deploys + goal-driven decomposition, `173dc75` docs (1155), `6c1feb8`
  `run_until_done.sh`, `802ce20` `run --all`, `b44ea3e` docs (1160), `e7f3fd6`
  `run --all` stop reasons, `25be3eb` static import contract (1185), `e623a70`
  docs (1185), `b678aa5` checkpoint overwrite + best-effort snapshots (1191),
  `bfafd5c` docs (1191), `bb87543` evidence-repair remedies + prose NOT RUN +
  `*` allowlist, `5b59ee8` docs (1209), `b3199b2` memory, `69bfb72` atomic
  checkpoint writes + hermetic deploy tests + lint baseline (1213).
- **An audit was performed** (`ARCHITECTURE_COMPLIANCE_AUDIT.md`, 62 findings:
  9 Critical / 18 High / 21 Medium / 14 Low). It is the finding of record; the
  status table below is the remediation state against it.

### Remediation status (verified 2026-10-02)

| Gap | Severity | State |
|---|---|---|
| CRIT-01 | Critical | **Fixed** — `data.deploy` execution channel, DoD evidence gate |
| CRIT-02 | Critical | **Fixed** — `path_policy.py`; restore path traversal refused |
| CRIT-03 | Critical | **Fixed** — explicit `signed` flag, 4-verdict integrity model |
| CRIT-04 | Critical | **Fixed** — DoD problems travel with the task into review and are persisted |
| CRIT-05 | Critical | **Fixed** — model-supplied task `status`/`execution` stripped |
| CRIT-06 | Critical | **Fixed** — cross-process locks on index + id allocation |
| CRIT-07 | Critical | **Fixed** — `.env` loading confined to `config.PROJECT_ROOT` |
| CRIT-08 | Critical | **Fixed** — base `SYSTEM_RULES` no longer shadowed by subclasses |
| CRIT-09 / HIGH-13 | Critical / High | **Fixed** — 11 prompt templates share one canonical output schema, generated from code |
| HIGH-01..04 | High | **CLOSED** — see "Remediated findings" below |
| HIGH-12, LOW-01, MED-01..03, LOW-10, HIGH-14 | High/Med/Low | **Fixed** |

**All 9 Criticals and all 18 Highs are closed.** Re-verify before claiming
that again: the table below drifted stale twice, once for a whole batch.

### Closed findings (were open at audit time)

The three items below were open at audit time and are now fixed; the prose is
kept as the record of what was wrong, not as current state.

- **CRIT-04 (fixed, `8f10641`)** — the review path took the
  `review_block.get("required")` branch *before* the DoD branch and
  `complete_review` called `definition_of_done(prospective)` with
  `preexisting=None`, so the G22 delivery guarantee was inert on review.
  `review.required: true` is the **default** (`project-templates/TASKS.yaml`),
  so this was the common path, not an edge case. Now the problems travel with
  the task, are persisted for a later cycle, and a PASS cannot reach `DONE`
  over standing problems.
- **CRIT-07 (fixed, `8f10641`)** — `config.py` resolved `./.env` from
  `Path.cwd()`, so running the CLI inside an untrusted repo redirected every
  prompt to a host that repo chose. Now strictly
  `config.PROJECT_ROOT / ".env"`.
- **CRIT-09 / HIGH-13 (fixed, `4c86c6d`)** — all **11**
  `framework/AGENT_PROMPTS/*.md` specified a top-level
  `"documents": [{"name", "content"}]` array, which the
  runtime discards (it reads `data.documents` as a dict). **0 of 11** mention
  `data.edits` or `acceptance_results` — i.e. the contract that decides whether
  a task delivers or fails was undocumented outside `base_agent.py`. The schema
  now lives in code next to the parser that enforces it and the files are
  generated from it.

### Remediated findings (closed in the final High batch)

- **HIGH-01 (closed)** — `resolve_agent(fresh=True)` built a detached instance
  from the pinned agent's class instead of returning the shared one, so parallel
  dispatches no longer share `_current_task_id` / `last_payload_chars` /
  `_delivery_snapshot`. The pinned instance is still returned on the serial
  path. `shared_agent_keys()` reports agents whose class cannot be re-instantiated
  (those are still shared, deliberately).
- **HIGH-02 (closed)** — the checkpoint trigger chain evaluates all three
  triggers and then picks one winner, in both the dispatch and review paths.
- **HIGH-03 (closed)** — the Definition-of-Done check and `repair_delivery()` run
  in a new unlocked phase 2b; every state write stays in the locked phase 3.
- **HIGH-04 (closed)** — `validate()` now also checks `phase.current`,
  unknown/blank owner, self-cycles and cycles, malformed dependencies and
  disconnected roots (advisory). It runs on the plan-ingestion path
  (`_report_graph_problems`, surfaced as dispatch warnings rather than a raise,
  because a blocker there may be a pre-existing `PROJECT.yaml` defect) and
  `orchestrator status` prints structural problems while still exiting 0.
  `graph_blockers()` is the blocking-only subset used for automation.

  Two earlier drafts of HIGH-04 were wrong and were corrected: a single
  unreferenced root task is **not** an orphan (it is how every graph starts), and
  a terminal dependency **satisfies** its dependents (`CANCELLED` is in
  `SATISFIED_DEPENDENCY_STATUSES`), so "terminal task another task waits on" is
  the normal path, not a stall.

### Open items (verified 2026-10-02 night)

### Closed this pass (were open)

- **OPEN-1 — RESOLVED (`69bfb72`).** The red
  `tests/test_hmac_verification.py::TestLegacyMetadata::test_real_on_disk_snapshots_all_pass_the_containment_policy`
  (`FileNotFoundError: checkpoints/<proj>/cp-risk-RISK-002/metadata.json`)
  had two causes and both are fixed:
  1. **Code:** `create_checkpoint` rmtree'd the old snapshot *before* copying
     the new one and `delete_checkpoint` removed the directory before the
     index row, so any failure/crash between the two left a row for a
     directory that no longer existed. Saves now stage under
     `.staging-<id>-<pid>` and swap in complete; delete clears the row first;
     four regression tests in `tests/test_checkpoint_overwrite.py` pin it.
  2. **Local state:** `checkpoints/{sys_mon_full,sys_mon_gui}/index.json` each
     had one such row; both repaired (backups in `/tmp/index-backup-*.json`).
  The test still reads gitignored `checkpoints/`, so **local `rm -rf` of a
  checkpoint directory can still turn it red** — that now means the state is
  genuinely corrupt, not that the code is wrong.
- **OPEN-2 — counts current.** The four consistency-checked docs
  (`ORCHESTRATOR_GUIDE.md`, `HOW_TO_USE.md`, `README.md`, `review_gaps.md`)
  are asserted against `pytest --collect-only` and are at **1213** (1103 →
  1160 → 1185 → 1191 → 1209 → 1213 as batches added tests), so
  `TestCountsConsistentAcrossDocs` is green. Still stale:
  `ARCHITECTURE_COMPLIANCE_AUDIT.md:8` says "has since grown to 963 tests"
  (findings-of-record doc, deliberately untouched).

### Known documentation drift

- `memory.md` previously claimed 314 tests and "uncommitted work" while the
  suite was at 401+; that is the class of rot that made this file unreliable.
  Test counts are now **derived from `pytest --collect-only`** and asserted across
  all four user-facing docs (`TestCountsConsistentAcrossDocs`), so a stale number
  fails the suite rather than being discovered by a reader.
- `README.md` was materially false until this pass: v1.0/Production Ready,
  `REQ-001..REQ-020` (actual 015), `30+ tasks` (actual 4), a `docs/` +
  `implementation/` example tree that does not exist, and a phantom
  `Failure Risk` health state. All corrected.
- Still stale elsewhere: `framework/AGENT_PROMPTS/`, `framework/TEMPLATES/`
  (7 of 16 architecture sections; `DRAFT` is not a valid requirement status),
  `meta/GETTING_STARTED.md` (teaches a manual copy path and a checkpoint layout
  `CheckpointManager` cannot read), `IMPLEMENTATION_ROADMAP.md` /
  `QUICK_REFERENCE.md` / `DEPLOYMENT_SUMMARY.md` / `GITHUB_PUSH_INSTRUCTIONS.md`
  (pre-implementation, instruct commands that cannot succeed), and
  `AGENT_PROMPTS/00-01` (claim `framework/00`/`01` are auto-appended; they are
  not — those ids are unreachable).

---

## Dependency management (never regress)

- Agents introducing a non-stdlib import must **declare** it in
  `requirements.txt` at the project root and **install** it via a `data.deploy`
  `pip install -r requirements.txt` entry placed *before* any test step. Single
  source of truth: `prompt_builder.DEPENDENCY_AUTOMATION_INSTRUCTIONS`, surfaced
  through `base_agent.AUTHORING_CONTRACT` and the `02`/`07`/`08` templates.
- The refusal path is load-bearing: `pip` is **not** allowlisted by default, so
  the contract must keep saying "report `NOT RUN`, never claim an install".
  `tests/test_dependency_automation.py` asserts the channel stays closed.
- `projects/sys_mon` is the regression: it imported `psutil` with no
  declaration, so collection died and **the whole suite was interrupted**
  (0 tests ran). Its module now imports `psutil` lazily and
  `test_sys_mon.py` installs via `setUpModule`.
- Root `conftest.py` sets `collect_ignore_glob = ["*/docs/*"]`: the materializer
  mirrors deliverables into `docs/`, and collecting that mirror collided on
  basename and broke collection.

## Guardrails (always keep)

- **Never dispatch the CLI against `projects/kid-robot-face/`** (a smoke run once
  corrupted it). Tests only *read* it; use `build_test_project(tmp_path)` copies
  for anything that executes.
- Its `PROJECT.yaml` / `TASKS.yaml` / `PROJECT_MEMORY.md` live-test state is
  **committed as-is** — do not "restore" it to older HEAD content; tests depend
  on it.
- Run full pytest after every change: `python3 -m pytest -q` (~200 s, 1213 tests),
  and `ruff check .` (0 errors, baseline pinned in `pyproject.toml`).
- **Never claim a green suite from memory.** Re-run it. The on-disk snapshot
  test reads gitignored `checkpoints/`, so "it passed earlier today" is not
  evidence — deleting a checkpoint directory by hand will redden it (OPEN-1,
  now closed, showed why).
- YOLO mode: no approval prompts, no TODO stubs, relative paths, autonomous execution.
- LLM backend is stdlib-only; tests inject `FakeLLMClient` / `transport`.
  **The suite must stay offline-safe** — do not add a test that dials out.
- `checkpoints/` and `projects/*` are gitignored (except `kid-robot-face/`).
  Probes that construct `MasterOrchestrator` with a default root will write
  there; pass `checkpoints_root=` explicitly.
- **Standing rule (user, 2026-09-30):** after every batch, update git
  (commit + push) and keep docs in sync — no need to re-ask first.

---

## Key architecture facts (for future edits)

- **Dispatch** = 3 phases under `self._state_lock` (RLock): gate (locked) →
  `agent.run` **and** `data.deploy` (both unlocked) → finalize (locked).
  The deploy runner is deliberately in phase 2 so a 5-minute build cannot stall
  other workers' state turns.
- **Task status is derived, never supplied** (`GAP-CRIT-05`):
  `derive_initial_status(deps)` → `TODO` with no deps, `BLOCKED` with any.
  `refresh_ready_states()` owns `TODO/BLOCKED → READY`, so the transition lives
  in one place. `append_task` strips `status`/`execution`/`review.status` and
  refuses a spec that would *join* a dependency cycle (scoped deliberately: an
  unrelated pre-existing cycle must not block new work).
- **Cycle detection** is `StateManager.find_dependency_cycles()` — iterative
  three-colour DFS (a plan can be thousands of tasks deep; the recursive form
  raised `RecursionError`). It is called from `validate()` and `append_task`.
- **Execution / evidence (`data.deploy`)** — off by default. Needs *both*
  `ORCHESTRATOR_DEPLOY_ENABLED=1` and a non-empty
  `ORCHESTRATOR_DEPLOY_ALLOWLIST` (basenames; enabling alone stays inert).
  `orchestrator/deploy_runner.py` is the **only** module that spawns a process,
  which is what makes it auditable via `grep -rn subprocess orchestrator/`.
  `shell=False` + list `argv`; forced project-root cwd; scrubbed child env (API
  and signing keys are **not** visible to the child); bounded output (tail kept,
  truncation flagged) and timeout; `executed: false` is a first-class outcome.
  Transcripts → `docs/evidence/<task>/NN-<command>.json`.
- **DoD evidence rule** — fires when the output declares `data.deploy`, *or* the
  summary/artifacts claim execution (`config.claims_execution`), *or* an expected
  output looks like a build/test artifact. Then either an `executed: true`
  record must exist (real exit code authoritative, incl. non-zero), or the agent
  must set `data.test_status = "NOT RUN"`. Otherwise `FAILED`, with the note
  carrying the *missing evidence*, never the claim. This is what makes
  "I could not run this" a legitimate outcome and "42/42 tests passed" a
  rejected one.
- **State IO is locked and atomic.** `atomic_dump_yaml()` serializes *before*
  touching disk (a failed dump leaves the file byte-identical), writes to a temp
  file, `fsync`s, `os.replace`s, then `fsync`s the parent directory. Locks are
  taken on a **sibling `.lock` file**, not the target — `os.replace` swaps the
  inode, so a lock on the target is released by the very rename it was meant to
  cover. `_file_lock` is **reentrant** (`RLock` + depth counter); a plain `Lock`
  self-deadlocked `append_decision`. Lock files persist by design and are not
  litter.
- **Read-modify-write needs the whole cycle under one lock** (`_document_lock`).
  `DEC-NNN` / `RISK-NNN` / `cp-auto-NNN` allocation is only collision-free
  because read → allocate → write happen together; `cp-auto` also *reserves* the
  number in the index, or the allocation is still advisory. Append-only logs
  (`CURRENT_STATE.md`, `CHANGELOG.md`) use `O_APPEND` under the lock, not
  read-concatenate-rewrite.
- **Derived state parses `TASKS.yaml` once** per `recompute_derived_state()`
  (was 4×; ~11× faster at 400 tasks). `get_ready_tasks(tasks=…)`,
  `status_breakdown(tasks=…)` and `_tasks_from_document()` exist so a caller
  that already parsed can reuse it. Pass a preloaded list, not a re-read.
- **Checkpoint integrity** — `evaluate_integrity()` returns one of
  `VERIFIED` / `UNSIGNED` / `TAMPERED` / `UNVERIFIABLE`, driven by the persisted
  `signed` flag and *never* by field presence (that inversion was CRIT-03). The
  MAC binds `checkpoint_id`, `key_id`, `created_at` and per-file digests.
  `CHECKPOINT_ALLOW_UNSIGNED=1` downgrades only `UNVERIFIABLE`.
  `resume_from_checkpoint` silently drops `registered_agents` — re-register
  custom agents after a restore.
- **Context accounting is cumulative**, reset only at compaction. A compaction
  is a *state-changing* event and is now recorded in `CHANGELOG.md`,
  `CURRENT_STATE.md` and a queryable index row
  (`checkpoints.compaction_events()`) — all written *before* the memory fold, so
  the trail survives its own side effects.
- **Telemetry is off by default** (`ORCHESTRATOR_TELEMETRY_DIR` / `_FILE`). When
  on, one-line JSON with a stable field set; credentials are redacted by key
  name *and* value shape, recursively. `main()` returns 2 (not 1) for an
  unhandled error and 130 for Ctrl-C.
- **Phase machine** is forward-only; `INTEGRATION`/`VALIDATION`/`RELEASE` are
  unreachable in the derived path (empty `PHASE_OWNERS`), and a
  documentation/review-only endgame **regresses the phase to `REQUIREMENTS`**
  because those owners are in no tuple.
- **Agent registry**: 10 ids. `GLOBAL_SYSTEM_RULES` is read only by
  `system_rules()` and is therefore unreachable by a subclass that replaces
  `SYSTEM_RULES` (all 10 do) — that separation is load-bearing, do not merge the
  two blocks. `firmware_agent` is a deliberate second registration of
  `SoftwareAgent`.
- **Prompt-contract batch (post-audit)** — `ENTRYPOINT_CONTRACT` and
  `INTERFACE_ALIGNMENT_CONTRACT` (`orchestrator/agents/base_agent.py:398`,
  `:426`) are appended to every agent's rules (`base_agent.py:543`), so the
  entrypoint obligation is stated once in source and inherited everywhere.
  `DATA_CONTRACT_SPEC` and `UI_FIDELITY_SPEC` (`orchestrator/prompt_builder.py`)
  are injected only when `agent_id in DATA_CONTRACT_SPEC_AGENTS` /
  `UI_FIDELITY_SPEC_AGENTS` — both `frozenset({"software_agent"})` — so
  "which agents get which spec" is one frozenset, not a copy per site.
  Enforcement: `tests/test_agent_contract_enforcement.py`,
  `tests/test_ui_contract_enforcement.py`,
  `tests/test_data_contract_automation.py`.
- **Advisory-lock fds are released** (`state_manager.release_file_locks()`,
  `:449`): cached descriptors from `_file_lock` accumulated across the suite
  until the fd limit was hit. Lock files still persist on disk by design.
- **Checkpoint id kinds**: `cp-000-init`, `cp-phase-<name>`, `cp-milestone-<slug>`,
  `cp-risk-<id>`, `cp-auto-<NNN>`. Lowercase phase names (`cp-phase-release`).
- **Architecture-redesign batch (2026-10-02 night, three features):**
  1. **Truncation recovery** — `orchestrator/agents/llm_agent.py`
     `recover_truncated_payload()` + `_recover_truncated_output()`, wired into
     `LLMAgent.execute` and `_execute_edit_session` *before* the legacy
     `_repair_truncated_output`. It rescans the raw text for `{` starts /
     fenced JSON, reassembles a cut JSON document with a stack scanner, and
     either (a) returns a complete reassembled payload (status kept, warning
     `"recovered from a truncated payload (local chunked parse)"`), or (b)
     returns `status="blocked"` with partial file bodies in
     `data.edit_buffers` — buffers are **never auto-applied**, because
     `BaseAgent.run()` only materializes edits when `status == COMPLETED`, or
     (c) yields nothing so the old repair path runs unchanged (the fenced
     `TRUNCATED` fixture in `test_agents_and_review.py` still returns `None`
     after 2 client calls). Order matters: local recovery first preserves the
     truncation-repair tests.
  2. **PEP 668 deploys** — `orchestrator/deploy_runner.py` gained
     `is_pep668_managed()` / `pip_targets_running_environment()` /
     `pip_requirements_satisfied()` and a `STATUS_SKIPPED` record kind. In
     `run_one`: skip first (externally-managed interpreter + target under
     `sys.prefix` + bare-name/`==` specs all provably satisfied + no
     unskipable option such as `-U`/`-e`/`--target`) → else append
     `--break-system-packages` once. Skipped records are `executed: false`,
     `exit_code: null`, and `_evidence_problems` accepts an all-skip result
     only when the task claims nothing and emits no test-like outputs — a
     skip can never back an execution claim.
  3. **Goal-driven decomposition** — new `orchestrator/auto_plan.py`
     (stdlib-only). `should_decompose()` scans the goal + implementation-task
     title for `GUI_SIGNALS` (tkinter/qt/canvas/dashboard/…) or
     `MULTI_MODULE_SIGNALS` (module/plugin/…), GUI wins ties; "web", "app",
     "widget" deliberately do **not** trigger. `expand_implementation_stages()`
     replaces exactly one `software_agent` task with
     `Backend Data Layer → UI Canvas Components → Application Launcher`
     (or `Module Interface Layer` for multi-module), chains deps
     (`requirements → data → middle → launcher → original dependents`),
     renumbers `TASK-NNN` and remaps dependencies, keeps the original
     `expected_outputs`/acceptance criteria on the launcher, mirrors the
     replaced task's output style (bare `IMPLEMENTATION.md` stays bare), and is
     idempotent. Contract text `PLANNING_RULES` is appended to
     `MasterOrchestrator.build_plan`; `seed_starter_tasks` expands specs and
     joins every later task to `implementation_join_index()` (last
     `software_agent`, fallback index 2), so starter seeds grow 4 → 7 for a
     GUI goal. Expansion may exceed `max_tasks` by up to 2 — atomicity wins.
  4. **`orchestrator run --all`** (later same session) — one `run` is one
     *wave* (tasks unblocked by it dispatch on the next call), so "run
     everything" is a loop. `_run_all()` in `orchestrator/cli.py` is that loop:
     success is decided from `summary_counts()` having no non-terminal status
     (**not** from `phase.current`, which is stored and forward-only and can
     read RELEASE with work still on the graph), it stops on
     `HUMAN_DECISION_REQUIRED` (exit 4), on BLOCKED/STALLED health, on a loop
     limit, and on "no READY and not finished" (exit 3), and it refuses
     `--all --task` (exit 2). Exit codes match a single `run`, so wrappers
      cannot tell the difference. `scripts/run_until_done.sh` is now a thin
      wrapper (validate path, `exec … run --all`) — the loop lives in one place.
  5. **Static import contract** (2026-10-03) — new stdlib-only
     `orchestrator/import_contract.py`: `import_contract_problems(project,
     delivered)` parses the project's Python with `ast` (no subprocess) and
     reports, for files this task delivered **or** that import a module this
     task delivered: an intra-project `from X import Y` where `X` does not
     define `Y`, an `import a.b` whose module is missing from the project, a
     relative import with no module at that path, and a delivered file that
     does not parse. Scope is deliberately symmetric with responsibility — a
     file is judged only when the task touched it or it consumes a touched
     producer — so pre-existing breakage never fails an unrelated task;
     third-party/stdlib imports are skipped by first-component lookup;
     `from x import *` and modules defining `__getattr__` (PEP 562) are never
     guessed at; `if TYPE_CHECKING:`/`try`-wrapped definitions count as defined.
     Wired into `delivery_problems()` (before the expected-outputs loop), so
     both the edit session and `definition_of_done` see it and the one
     auto-repair round feeds the message back. `INTERFACE_ALIGNMENT_CONTRACT`
     now says imports are statically verified. Why: a consumer in
     `sys_mon_full` imported `get_metrics_snapshot` while the producer defines
     `get_system_metrics`, and prompt text alone ("read the producer first")
     did not stop it — the app was broken at the operator's first launch with
     the task already DONE. Message names the producer file and the names it
     really defines. Tests: `tests/test_import_contract.py` (25).
  6. **Checkpoint collisions never fail a dispatch** (2026-10-03, user-reported
     live stall) — `orchestrator create_checkpoint` now saves with
     `overwrite=True`: an existing directory for the same id is replaced
     (source validated *first*, so a failed overwrite cannot destroy the old
     snapshot) instead of raising. `CheckpointManager.create_checkpoint` keeps
     `overwrite: bool = False` as its default, so direct/CLI callers that mean
     "id must be new" still get `CheckpointExistsError`. The real stall: a
     fresh plan rewrote `RISKS.md`, the next failing task re-allocated
     `RISK-001`, and the stale `checkpoints/<proj>/cp-risk-RISK-001` directory
     made `create_checkpoint` raise *inside dispatch finalize* — the task
     failed with "Checkpoint … already exists" instead of its own error and
     `run --all` stopped with 6/7 DONE. Additionally every finalize-path
     snapshot (`cp-risk-*`, `cp-phase-*`, `cp-milestone-*`, `cp-auto-*`) runs
     through `MasterOrchestrator._safe_auto_checkpoint()`, which logs and
     returns `None` on error: a snapshot may be lost, the dispatch may not.
     Tests: `tests/test_checkpoint_overwrite.py` (10), including the exact
     reproduced sequence (failing dispatch → RISKS.md reset → re-dispatch
     keeps the task's own `connection refused` error and one refreshed index
     row).
  7. **Evidence rejections are now answerable** (2026-10-03, live failure on
     `sys_mon_full` TASK-006) — the single hardcoded `DOD_REPAIR_NOTE` claimed
     *every* DoD rejection was "valid JSON but did not deliver real file
     content" and ordered file edits, so for an evidence rejection
     (`report test_status=NOT RUN`) the model re-delivered files, never set
     the field, and a legitimate NOT RUN task failed twice.
     `repair_remedies(problems)` (`orchestrator/agents/llm_agent.py`) picks
     the remedy block from the problem markers: evidence → the exact
     `{"data": {"test_status": "NOT RUN"}}` JSON and "do not re-deliver";
     import → "patch the consumer with data.edits"; content → the original
     file guidance; unclassified → generic. Second, independent escape:
     `_reports_not_run()` (`orchestrator/orchestrator.py`) accepts `NOT RUN`
     stated in the **summary** when the same output does not also claim
     execution — a fabricated "NOT RUN" only loses information, while a
     fabricated pass still needs a ground-truth record. **Verified end to
     end on `sys_mon_full`:** `retry TASK-006` then `run --all` → TASK-006
     `READY -> DONE` on the first attempt (model reported `NOT RUN` in the
     summary *and* `data.test_status`), project 7/7 DONE, health HEALTHY,
     exit 0.
  8. **`ORCHESTRATOR_DEPLOY_ALLOWLIST=*` means any executable** — it used to
     be compared as the literal basename `*`, so an operator who enabled the
     channel with `*` got "enabled" plus a refusal for *every* command
     (`pip`, `python` refusals in a live run). `_resolve_executable` now
     treats a `*` entry as allow-all; `shell=False`, project cwd, scrubbed
     env, timeout and transcript still apply, and the project-directory
     confinement for relative paths is unchanged.
  9. **Operator project `projects/sys_mon_full` repaired by hand** (not
     framework): `src/gui_controller.py` renamed its guessed
     `get_metrics_snapshot` → `get_system_metrics` (the drift the import
     contract now blocks at DoD), the CPU card denominator was
     `disk_total` instead of `100.0` (the bar rendered ~0.1%), a
     `ttk.Progressbar(bootcolor=…)` option that does not exist crashed the
     window at construction (now a per-card `ttk.Style`), and
     `tests/test_monitor.py` patched/asserted `…get_metrics_snapshot` and
     `cpu_card_label`, names the app never had. Verified: their suite 4/4
     green and `python3 main.py` runs the window for 6 s without an
     exception (`timeout 6`, exit 124). Entry point is **`main.py`** —
     `python3 src/gui_controller.py` can never work with a relative import.
  10. **Snapshots are atomic; the suite is hermetic under an exported deploy
      channel** (2026-10-03, `69bfb72`) — two failures from a real operator
      shell (`ORCHESTRATOR_DEPLOY_ENABLED=1 ORCHESTRATOR_DEPLOY_ALLOWLIST=*`):
      (a) `tests/test_deploy_ground_truth.py` only scrubbed `ORCHESTRATOR_DEPLOY_*`
      inside `TestDeployPolicyDefaults`, so "the shipped default refuses
      everything" inherited the shell export and spawned a real `ctest`
      during pytest — the autouse fixture is now module-level; (b)
      `create_checkpoint` rmtree'd the old snapshot before copying the new
      one and `delete_checkpoint` removed the directory before the index row,
      so a failure in between left a row for a directory that was gone
      (OPEN-1: `cp-risk-RISK-002` in `checkpoints/{sys_mon_full,sys_mon_gui}`).
      Saves now stage under `.staging-<id>-<pid>` and swap in complete,
      delete clears the row first, both local indexes repaired (backups in
      `/tmp`), and the containment test skips documented `reserved` id
      placeholders. Also fixed while there: `KeyboardInterrupt` in
      `orchestrator/cli.py` referenced an unbound `exc` (every Ctrl-C raised
      `NameError` instead of exiting 130), `auto_plan.__all__` exported a
      phantom `EDIT`, and the dead `base_agent.restore_checkpoint` stub went.
  11. **Lint baseline pinned** (2026-10-03, `69bfb72`) — `pyproject.toml`
      now carries `[tool.ruff] select = ["E4", "E7", "E9", "F"]` (ruff's
      classic pyflakes/PEP 8 error set) plus a `dev` extra (`pip install -e
      .[dev]`) and `per-file-ignores` for the root `test_*.py` sys.path
      bootstrap. `ruff check .` → 0 after fixing 51 findings (26 unused
      imports, 10 unused locals, 3 undefined names, `__all__` export,
      E402 ordering). Style rules (UP/I/RUF) are deliberately *not* in the
      baseline.


---

## Test layout

Root-level `test_*.py` (T1–T20 + gap-remediation + parallel + supervisor +
derived-state + docs-consistency) plus `tests/` for the security and regression
suites added during remediation:

| File | Covers |
|---|---|
| `tests/test_hmac_verification.py` | CRIT-03 — signing truth table, tampering, key rotation; includes the real-on-disk snapshot containment test (green since the staging fix, OPEN-1 closed) |
| `tests/test_deploy_ground_truth.py` | CRIT-01 — runner refusals, live execution, DoD evidence (+ the `*` allowlist: any executable, disabled channel still inert, traversal still confined); module-level fixture scrubs `ORCHESTRATOR_DEPLOY_*` so an exported shell channel cannot leak in |
| `tests/test_task_status_injection.py` | CRIT-05 — status/execution injection |
| `tests/test_state_io_and_contracts.py` | atomic writes, single-parse derived state, deploy contract (its "HIGH-01..04" labels are the *state I/O* batch, not audit IDs) |
| `tests/test_concurrent_appends.py` | CRIT-06 — concurrent append + id allocation |
| `tests/test_medium_gaps.py` | MED-01/02/03, HIGH-14, LOW-10 — telemetry, cycles, compaction, README, counts |
| `tests/test_final_critical_gaps.py` | CRIT-04/07/09 — review DoD threading, `.env` containment, prompt schema |
| `tests/test_agent_contract_enforcement.py` | ENTRYPOINT + INTERFACE_ALIGNMENT contracts, per-agent DATA_CONTRACT_SPEC injection |
| `tests/test_ui_contract_enforcement.py` | UI/application contract — entrypoint, schema match, signature verification, visual fidelity |
| `tests/test_data_contract_automation.py` | cross-component data contract — typed schemas, boundary conversion, entrypoint, error paths |
| `tests/test_materializer.py` | edits materialization — full write when nothing to patch |
| `tests/test_state_io_and_contracts.py` | also: lock-descriptor hygiene (`release_file_locks`) |
| `tests/test_cli_init.py` | init destination resolution, name validation, traversal refusal |
| `tests/test_dependency_automation.py` | dependency contract — declaration, install ordering, honest refusal, `sys_mon` regression |
| `tests/test_final_high_gaps.py` | audit HIGH-01..04 — pinned-agent isolation, checkpoint trigger evaluation, off-lock DoD repair, deep validation + reachability |
| `tests/test_architecture_redesign.py` | 52 tests — truncation/chunked-JSON recovery, PEP 668 `--break-system-packages` + satisfied-install skip, skip-vs-DoD evidence, decomposition scope/chains/starter seeding |
| `tests/test_import_contract.py` | 25 tests — static import/DoD enforcement: drift on consumer *and* producer side, scope limits, resolver edges, parse failures, DoD wiring, prompt promise |
| `tests/test_checkpoint_overwrite.py` | 10 tests — `cp-risk-*` collision replaces instead of raising; strict default kept; staged `.staging-<id>-<pid>` swap keeps the old snapshot when the new one fails (empty source / copy error); delete clears the row first; crash leftovers cleaned; dispatch keeps its own error and best-effort snapshotting |
| `tests/test_repair_remedies.py` | 15 tests — problem-aware repair note (evidence/import/content/generic + what the model receives), prose `NOT RUN` accepted, claim still refused, dispatch rejection → repair → DONE |

Shared fixtures in `conftest.py`: `build_test_project`, `test_project`,
`checkpoints_root`, `FakeLLMClient`, `_task`, `FakeDeployRunner` (+ the
`fake_deploy_runner` / `disabled_deploy_runner` / `failing_deploy_runner`
variants), and two autouse fixtures: `_redirect_default_checkpoints` and
`block_external_network`.

---

## Docs map (what to trust for what)

| Need | Read |
|---|---|
| Runtime behaviour, env vars, DoD, integrity model | `ORCHESTRATOR_GUIDE.md` |
| Operator walkthrough, troubleshooting table | `HOW_TO_USE.md` |
| Findings of record (62 gaps) | `ARCHITECTURE_COMPLIANCE_AUDIT.md` |
| Env defaults + how to generate keys | `.env.example` |
| Findings-vs-status for A1–E2 (older pass) | `review_gaps.md` |
| Framework spec (agent contracts) | `framework/00–10` — **see drift note above** |
| Runbook for a PoC against a real project | `HOW_TO_USE.md` "Test on a copy" |

---

> opencode's own config (`~/.config/opencode/`) is tooling setup, **not** project
> state — keep it out of this file and out of the repo docs.
