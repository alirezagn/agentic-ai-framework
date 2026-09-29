# memory.md — session memory for agentic-ai-framework

> Last updated: 2026-09-30 (end of remediation session, T1–T20 complete)

## Where the project stands

- `GAP_ANALYSIS.md` remediation list **T1–T20 is fully complete** (milestones M1–M7).
- Test suite: **`python3 -m pytest -q` → 227 passed, 0 failed** (14 test files, ~3.8k lines).
- Runtime v2.0: state engine + LLM-backed specialist policy layer (~30% → full coverage of the documented architecture).
- Nothing has been committed; work tree holds all changes (see `git status`).

## Guardrails (always keep)

- **Never dispatch the CLI against `projects/kid-robot-face/`** (a smoke run once corrupted it). Tests only *read* it; use `build_test_project(tmp_path)` copies for anything that executes.
- Its `PROJECT.yaml` / `TASKS.yaml` / `PROJECT_MEMORY.md` show as modified vs HEAD — pre-existing working-tree drift the live tests depend on; do not "restore" to HEAD.
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
- Review flow: `_run_pending_reviews()` before READY dispatch; reviewer always `review_agent`, `materialize=False`, writes `docs/REVIEW-<task>.md`; PASS → DONE, PASS WITH ACTIONS → `DONE WITH ACCEPTED LIMITATION` + follow-ups, FAIL → FAILED + follow-ups.
- `PROPOSED_CHANGE` in DECISIONS.md blocks affected tasks (`_enforce_decision_gate`) until `orch.approve_decision(id, approved)`.
- Loop kinds (`config.LOOP_KINDS`): `same_strategy`, `no_progress`, `alternatives_exhausted`, `state_oscillation`, `repeated_output`.
- Agent registry: 10 ids (`requirements|research|architecture|planning|hardware|software|firmware|test|review|documentation_agent`); `register_agent()` pins singletons (thread-safe), `agent_resolver(owner, state)` gives per-thread instances.
- LLM env: `ORCHESTRATOR_LLM_PROVIDER/MODEL`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OLLAMA_BASE_URL`, `ORCHESTRATOR_CONTEXT_WINDOW_TOKENS` (128000), `ORCHESTRATOR_LOG_LEVEL`, `CHECKPOINT_SIGNING_KEY`.
- Shared fixtures in `conftest.py`: `build_test_project`, `test_project`, `checkpoints_root`, `FakeLLMClient`, `_task` (optional `milestone`).

## Known leftovers (not in T1–T20 scope)

- `GAP_ANALYSIS.md` finding 18: `WAITING` status and `HealthState.RECOVERY` still unused vocabulary (decision-gated tasks intentionally stay `READY`).
- `git_author_name` SystemKey declared but no git integration exists.
- Commit/push never done — ask user before committing.

## External config done earlier

- opencode: `~/.config/opencode/opencode.jsonc` → `ollama/phi4`, `ollama/qwen2.5-coder:14b`, baseURL `http://192.168.0.200:11434/v1`.
