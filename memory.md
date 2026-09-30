# memory.md — session memory for agentic-ai-framework

> Last updated: 2026-09-30 (review_gaps remediation A1–E2 complete — see `review_gaps.md`)

## Where the project stands

- `GAP_ANALYSIS.md` remediation list **T1–T20 is fully complete** (milestones M1–M7); the file is now annotated as the historical baseline.
- **`review_gaps.md` (20 gaps A1–A8, B1–B7, C1–C3, D1–D3, E1–E2) is now fully remediated** — every section carries a ✅/🔶 status line; only A3 remains "mostly closed" (unnecessary-blocking heuristic approximated by the stale-block detector), A7 integration/release triggers ride `cp-phase-RELEASE`, B5 diagnosis is on-demand (`health --diagnose`), B3 keeps review/docs structural.
- New tests: `test_gap_remediation.py` (**41 tests**, one class per gap). Full suite: `python3 -m pytest -q` → **275 passed** (confirmed, 234 + 41). Test isolation: suite passes with `projects/` read-only.
- Runtime v2.0: state engine + LLM-backed specialist policy layer.
- **Pushed to GitHub** on `origin/main` (`https://github.com/alirezagn/agentic-ai-framework`): `e9d3e79` (T1–T20), `fec96b5` (memory), `7079012` (Ollama `.env` config), `f29ee79` (review_gaps.md + adoption guide), **`cb1bab3` (this session: review_gaps A1–E2 remediation, 41 new tests, docs)**.
- Docs set: `GAP_ANALYSIS.md` (historical), `review_gaps.md` (current — with remediation statuses), `ORCHESTRATOR_GUIDE.md` (v2.0 reference, CLI table incl. `phase`/`waive`/`health --diagnose`), `HOW_TO_USE.md` (walkthrough — updated this session for init scaffold, `requirement_ids`, `phase set`, compaction, 275-test count), README quick-start + Day-1 init (fixed), `framework/`, `meta/`, `project-templates/`.

## Guardrails (always keep)

- **Never dispatch the CLI against `projects/kid-robot-face/`** (a smoke run once corrupted it). Tests only *read* it; use `build_test_project(tmp_path)` copies for anything that executes.
- Its `PROJECT.yaml` / `TASKS.yaml` / `PROJECT_MEMORY.md` live-test state is **committed as-is** (lowercase `name: kid-robot-face`, TASK-002 READY, blockers set) — do not "restore" it to older HEAD content; tests depend on it.
- Run full pytest after every change: `python3 -m pytest -q`.
- YOLO mode: no approval prompts, no TODO stubs, relative paths, autonomous execution.
- LLM backend is stdlib-only; tests inject `FakeLLMClient` / `transport` — suite must stay offline-safe.

## T15–T20 — what landed most recently

- **T15 loop detection:** `state_oscillation` (status-only fingerprints via `StateManager.state_fingerprint()`, alternating A↔B over ≥4 compressed entries, checked FIRST in dispatch phase1), `repeated_output` (`last_output_hash`), `strategy_changed` → `alternatives_exhausted`; `LoopThresholds.identical_output_max_repeats=3`; `LoopLimitExceededError.loop` carried on results; CLI exit code 3.
- **T16 CLI:** `init NAME [--dest|--goal|--force]` scaffolds a project that passes `validate()`; `tasks` prints dependency graph + ready set + critical path (path may be `str` **or** `list`); executable `bin/orchestrator`; `pyproject.toml` exposes `orchestrator = "orchestrator.cli:main"`.
- **T17 derived state:** `StateManager.recompute_derived_state()` — TASKS.yaml `summary`/`parallel_groups` (pruned)/`critical_path.path` (longest chain), PROJECT.yaml `progress` % / `agents` status / `next_tasks`; writes only on change; called from `refresh_ready_states()`, dispatch phase3, and review success.
- **T18 docs:** `ORCHESTRATOR_GUIDE.md` rewritten for v2.0; created `framework/20_DEFAULT_PROJECT_START_PROMPT.md`, `framework/AGENT_PROMPTS/` (11), `framework/TEMPLATES/` (6), the 7 `project-templates/` state files, `meta/WORKFLOW.md`, `meta/TROUBLESHOOTING.md`, `agents/` + `references/` content; README quick-start uses `./bin/orchestrator init`.
- **T19 cleanup:** deleted `orchestrator/{checkpoint,project_manager,task_executor}.py` + stray `"__init__.py "`; exports removed from `orchestrator/__init__.py`; `orchestrator/requirements.txt` → `pyyaml` + `pytest` only; logging wired (`-v`/`-q`/`ORCHESTRATOR_LOG_LEVEL`, `config.LOG_FORMAT`, loggers in dispatch/supervisor/checkpoints); `CHECKPOINT_SIGNING_KEY` now HMAC-SHA256-signs checkpoint metadata and `verify_checkpoint()` enforces it.
- **T20 tests:** split `test_orchestrator_pipeline.py` → `test_state_manager.py` (+StateLoading), `test_supervisor.py`, `test_orchestrator.py`, `test_cli_commands.py` (TestCLI), `test_agents_and_review.py` (RequirementsAgent); deleted empty `test_agent.py`; added 9 structured-output parsing tests (`BaseAgent.parse_structured_output` edge cases).

## Key architecture facts (for future edits)

- Dispatch = 3 phases under `self._state_lock` (RLock): gate (locked) → `agent.run` (unlocked, thread pool when `max_concurrent>1`) → finalize (locked: context util, proposed-change, status, risks, loop signals, fingerprint, DoD, derived recompute, checkpoint).
- Phase machine: `config.PHASES`/`PHASE_OWNERS` + `StateManager.derive_phase()`; forward-only advance in `recompute_derived_state()` mirrors to nested `project["project"]["status"]` (top-level `status` does NOT exist); unknown/empty owners → IMPLEMENTATION/skip. CLI `phase show|set`.
- Task graph API: `append_task()` (auto `TASK-NNN`, validates owner/deps/status), `relax_dependency()` (CLI `waive`); dispatch phase-3 `_ingest_output_tasks()` converts planner/reviewer `proposed_tasks` output into tasks (STATE errors → warnings).
- Checkpoint triggers: milestones, `cp-phase-<name>` on phase advance (dispatch/review), `cp-risk-<id>` on failure risk (gated on `auto_checkpoint`), `cp-000-init` from `cmd_init`, compaction checkpoint. **Id priority evaluates ALL triggers** (milestones → phase) — never `or`-short-circuit the chain or milestones get skipped.
- Context accounting is **cumulative**: `_update_context_utilization` accumulates `cumulative_tokens`; `set_context_utilization` syncs `cumulative = window×pct/100`; compaction calls `compact_memory()` (head 60% + tail 25% + marker) then `reset_context_tokens()`.
- Loop response: `execution.attempts_since_change` (reset only on `strategy_changed`), `recovering` flag (set on strategy change, cleared on terminal OR when `attempts_since_change >= same_strategy_max_attempts` — clearing lives in `update_task_execution`); `classify_state` returns RECOVERY first when `recovering and loops`; `execution.last_loop` persists the latest `LoopLimitExceededError`.
- WAITING lifecycle: `PROPOSED_CHANGE` record → affected tasks `WAITING`; `approve_decision` → READY (deps ok) else BLOCKED; `get_ready_tasks` also releases `WAITING` with satisfied deps so gate refusals still surface.
- Escalations: `category` (`loop|deadlock|context|starvation|decision_conflict|stale_block`) + `blocking`; only blocking forces HUMAN_DECISION_REQUIRED; persisted as `"[category] task: reason"`.
- DoD: requirement traceability checked ONLY when task has `requirement_ids`; `acceptance_results` non-passing entries → problem; SYSTEM_RULES rule 6 requires agents to emit `acceptance_results`.
- Worker payload auto-injects `decisions_affecting_task` + `requirements` slices (`BaseAgent.relevant_context`).
- `SupervisorAgent.diagnose(use_llm=True)` + CLI `health --diagnose` — on-demand, never auto-called from `check_health()` (cost).
- Loop kinds (`config.LOOP_KINDS`): `same_strategy`, `no_progress`, `alternatives_exhausted`, `state_oscillation`, `repeated_output`, `no_new_evidence` (evidence stall, `evidence_stall_max=3`).
- Review flow: `_run_pending_reviews()` before READY dispatch; reviewer always `review_agent`, `materialize=False`, writes `docs/REVIEW-<task>.md`; PASS → DONE, PASS WITH ACTIONS → `DONE WITH ACCEPTED LIMITATION` + follow-ups, FAIL → FAILED + follow-ups.
- `PROPOSED_CHANGE` in DECISIONS.md blocks affected tasks (`_enforce_decision_gate`) until `orch.approve_decision(id, approved)`.
- Agent registry: 10 ids (`requirements|research|architecture|planning|hardware|software|firmware|test|review|documentation_agent`); `register_agent()` pins singletons (thread-safe), `agent_resolver(owner, state)` gives per-thread instances.
- LLM env: `ORCHESTRATOR_LLM_PROVIDER/MODEL`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OLLAMA_BASE_URL`, `ORCHESTRATOR_CONTEXT_WINDOW_TOKENS` (128000), `ORCHESTRATOR_LOG_LEVEL`, `CHECKPOINT_SIGNING_KEY`, `ORCHESTRATOR_CHECKPOINTS_DIR`, `MEMORY_COMPACT_MAX_CHARS` (60000).
- Shared fixtures in `conftest.py`: `build_test_project`, `test_project`, `checkpoints_root`, `FakeLLMClient`, `_task` (optional `milestone`), autouse `_redirect_default_checkpoints`.

## Known leftovers (tracked in `review_gaps.md`)

- Status: A1–A8, B1–B7, C1–C3, D1–D3, E1–E2 all remediated (see the per-section ✅/🔶 status lines). Nuances: A3 partial (stale-block detector approximates `blocked_by ⊄ task.dependencies`), A7 integration/release ride `cp-phase-RELEASE`, B5 on-demand, B3 review/docs checks remain structural.
- Hygiene: `.gitignore` now ignores `projects/*` except `projects/kid-robot-face/` (E2) and all of `checkpoints/` (E1); example scaffolds created by HOW_TO_USE/README quick starts stay untracked. An untracked `projects/my-project/` created earlier by docs examples was removed during the 2026-09-30 review.
- Repo synced on `origin/main` as of `cb1bab3`; ask before new commits beyond explicit requests.

## Project LLM backend config

- **Repo-root `.env` (gitignored, 2026-09-30):** sets `ORCHESTRATOR_LLM_PROVIDER=ollama`, `OLLAMA_BASE_URL=http://192.168.0.200:11434`, `ORCHESTRATOR_LLM_MODEL=gemma4:12b`. The CLI auto-loads it via `config.maybe_load_env_file()` (setdefault; skipped when `PYTEST_CURRENT_TEST` is set so tests stay offline). `.env.example` is committed. Verified: `main([])` outside pytest → provider `ollama`, model `gemma4:12b`.
- Caveat: as of 2026-09-30 the host `192.168.0.200` answers `/api/tags` (gemma4:12b, phi4, qwen2.5-coder:14b, gemma4:26b, deepseek-coder present) — earlier note that port 11434 was closed is stale; it is serving now.

> opencode's own config (`~/.config/opencode/`) is tooling setup, **not** project state — keep it out of this file and out of the repo docs.
