# Architecture Compliance & Gap Analysis Report

**System:** Agentic AI Orchestrator Runtime v2.0.0
**Repository:** `/media/alireza/microos/projects/agentic-ai-framework`
**Baseline commit:** `29b920c` (== `origin/main`, tree clean)
**Specification set audited:** `ORCHESTRATOR_GUIDE.md` (v2.0, 521 ln), `framework/00–10` + `framework/AGENT_PROMPTS/*` + `framework/TEMPLATES/*`, `framework/20_DEFAULT_PROJECT_START_PROMPT.md`, `review_gaps.md`, `HOW_TO_USE.md`, `README.md`, `meta/*`, `project-templates/*`, `GAP_ANALYSIS.md`, `IMPLEMENTATION_ROADMAP.md`, `QUICK_REFERENCE.md`, `DEPLOYMENT_SUMMARY.md`, `memory.md`
**Implementation audited:** `orchestrator/` (12 modules, 7 586 LOC incl. `agents/`), `conftest.py`, 18 `test_*.py` files, `bin/orchestrator`, `pyproject.toml`, `.env`/`.env.example`, `.gitignore`
**Verification (at time of writing):** `python3 -m pytest -q` → **721 passed** in 173 s, offline, `git status` clean after run (no writes to `projects/kid-robot-face/`). *The suite has since grown to 721 tests as the Critical/High gaps were remediated; the counts below are the audit-time baseline and the finding IDs are unchanged.*

---

## Executive Summary

The runtime's **internal engineering quality is high and its core state machine is faithful to the specification.** Of the 26 behavioural claims in `ORCHESTRATOR_GUIDE.md` that were checked line-by-line against code, 10 verify exactly (dispatch 3-phase locking boundaries, dependency gating, derived-state recompute, review flow transitions, DoD content plausibility, escalation taxonomy, cumulative context accounting, payload injection, parallel mechanics), and the remaining 16 fail only in specific, nameable ways. `yaml.safe_load` is used everywhere, `hmac.compare_digest` is used correctly, the state-write path is genuinely atomic per file, delivery is single-sourced through one helper (`delivery_problems`), the tests are fast, hermetic, and the example-project read-only guardrail is honoured. This is not a codebase in distress.

It is, however, a codebase whose **trust boundary is unearned**. Four independent defects each allow work to be marked `DONE` with no real deliverable: the designed `data.deploy` execution channel was never built, so nothing can ever run a build, a test, or a flash; the review-required path (the *default* path — 3 of 4 tasks in the shipped example set `review.required: true`) discards the G22 delivery guarantee computed moments earlier; the base agent's `SYSTEM_RULES` block is shadowed by every subclass, so the mandate to emit `data.acceptance_results` — the only acceptance-evidence gate — never reaches a model; and dispatch-time task ingestion honours a model-supplied `status`, letting a planner emit `{"status": "DONE"}` and self-approve. A fabricated reply was demonstrated end-to-end becoming `DONE`, with `"Executed ctest: 42/42 tests passed, coverage 94%"` persisted verbatim into `TASKS.yaml`. The pipeline has no ground-truth channel, so its quality signal is entirely model self-report.

Security posture is **partially broken in ways that matter**. `restore_checkpoint()` joins attacker-controlled filenames from `metadata.json` onto the destination with no validation — an arbitrary file write outside the project, reachable from the documented CLI. The HMAC checkpoint signature is skipped whenever the `signature` key is absent or empty, i.e. exactly when an attacker needs it skipped. `./.env` from the current directory is auto-loaded silently before argument parsing and can redirect every prompt to an arbitrary endpoint. Write paths for agent delivery are, by contrast, correctly contained.

**Concurrency and durability are the weakest structural dimension.** There is no file lock anywhere: `dispatch_review`, `complete_review`, `_create_followup_tasks`, `approve_decision`, and the whole CLI mutate state outside any lock; `DEC-NNN`/`RISK-NNN` id allocation is a read-max-plus-one race; `build_plan --force` truncates `TASKS.yaml` to `[]` *before* the replacement plan is known to be ingestible, so eight per-task failures destroy the prior graph and then raise. Meanwhile the documented serialization point, `orchestrator.state._state_lock`, does not exist — the lock is on `MasterOrchestrator`. Performance is quadratic by construction: no caching layer, full YAML parse+dump per mutation, four redundant parses per `recompute_derived_state()` — measured 5.99 s for a single `update_task_status` at 400 tasks, of which 6.0 s is `yaml.safe_load`/`safe_dump`.

Documentation fidelity is **the widest gap and the cheapest to fix.** One contract — `AUTHORING_CONTRACT`, which decides whether a task delivers or fails — is documented in exactly one place (`base_agent.py:333-359`) and is absent from all 11 `framework/AGENT_PROMPTS/*` files and 8 of 11 `framework/0X` specs. Those 11 files document an output shape the runtime silently discards (top-level `"documents": [...]` array; the code reads `data.documents` as a dict). `README.md`, `DEPLOYMENT_SUMMARY.md`, `QUICK_REFERENCE.md` and `CONTRIBUTING.md` declare "Framework v1.0 / Production Ready" against a runtime that is 2.0.0 with 401 tests; `IMPLEMENTATION_ROADMAP.md` still says "Ready to Build 🚀" and prescribes `anthropic-sdk`, `requests`, `claude-opus-4-5`, `python orchestrator.py --run-next-task`, git auto-commit and TOML config — none of which exist. `review_gaps.md` and `memory.md` both claim 314 tests; the truth is 401. **Zero tests guard any of this**: `test_docs_consistency.py` asserts 20 paths exist and that the guide avoids v1.1 symbols, but asserts no version string, no test count, no status table, and no artifact filename — every stale count in this report would have been caught by four assertions.

**Bottom line:** the state engine deserves its "STABLE" label; the *trust engine* does not. The single highest-leverage change is closing the verification loop (`data.deploy` + mandatory evidence), because GAP-CRIT-01 through GAP-CRIT-04 all reduce to "nothing can check the truth." Second is making the review path and the base `SYSTEM_RULES` actually deliver what the specification says they deliver.

---

## Categorized Discrepancies

### 1. Critical Severity

- **Gap ID:** GAP-CRIT-01
- **Component / Path:** `orchestrator/orchestrator.py`, `orchestrator/agents/specialists.py:154-160`, `orchestrator/agents/base_agent.py:357-358`
- **Expected (Doc):** `memory.md:81` — "The final mile (`idf.py build` → `idf.py -p /dev/ttyACM0 flash`) requires the designed-but-**NOT-implemented** `data.deploy` channel (agent emits allowlisted build/flash commands → orchestrator executes → output feeds back to the agent)." `HOW_TO_USE.md:401` repeats the admission. `framework/08_TEST_AGENT.md:50-51` requires "Test statuses are `NOT RUN` unless the payload contains real execution output"; `base_agent.py:357-358` "Never invent executed results".
- **Actual (Code):** No execution channel exists anywhere. `grep` for `subprocess|os.system|shell=True|eval(|exec(` across `orchestrator/` and `bin/` returns one hit — a comment at `config.py:167`. No `data.deploy` key is read by any module. `TestAgent.SYSTEM_RULES` (`specialists.py:154-160`) never mentions execution or the `NOT RUN` rule. Demonstrated end-to-end: a pure-prose reply (no `documents`, no `edits`, no execution) → `status: DONE`, with the string `Executed ctest: 42/42 tests passed in 10m 12s, coverage 94%, flashed ESP32 OK` persisted verbatim into `TASKS.yaml` `notes`, and `docs/TEST_REPORT.md` materialized as an empty rendered wrapper. Downstream `review_agent` sees only that wrapper.
- **Impact:** The pipeline's entire quality signal is model self-report, with zero enforcement. Every G18/G19 note in `memory.md` ("fabricated `docs/test_results.log` + '10-minute UI run' claims rode along"; "attempt-3 broken C passed structural DoD") is a symptom of this one gap. Nothing the framework delivers can be distinguished from something a model invented. This is the root cause of the user's user-observed end state (2026-10-01): the connected ESP32 shows no change because nothing was ever built or flashed.

- **Gap ID:** GAP-CRIT-02
- **Component / Path:** `orchestrator/checkpoint_manager.py:412-424` (restore), `:197-199` (`checkpoint_dir` id sanitisation), `:359-365` (`verify_checkpoint`)
- **Expected (Doc):** `checkpoint_manager.py:404-406` — restore is "fully isolated". `HOW_TO_USE.md:281-282` — "Restore verifies SHA-256 checksums first".
- **Actual (Code):** `restore_checkpoint()` iterates `checkpoint.files` — keys taken verbatim from the checkpoint's own `metadata.json` — and joins them onto the destination with no validation:
  ```python
  for name in checkpoint.files:
      source_file = checkpoint.directory / name
      target_file = destination / name
      shutil.copy2(source_file, target_file)
  ```
  `checkpoint_dir()` sanitises only the *id* (`:197-199`), never the *filenames*. `verify_checkpoint()` reads the same unsanitised keys, so one `../` passes verification (the source path resolves and exists) and then lands outside the project. Demonstrated: metadata key `../ESCAPED_WRITE.txt` → `verify_checkpoint() -> True` → file written outside the project directory. An absolute key additionally yields a read primitive.
- **Impact:** Arbitrary file write at any path the process can write, reachable from the documented CLI (`orchestrator checkpoint restore --checkpoint ID` → `cli.py:948-956` → `orchestrator.py:1784`). Defeats the doc's own isolation guarantee. Precondition is write access to `checkpoints/<project>/<id>/metadata.json` — i.e. anyone who can tamper with or drop a snapshot into a shared workspace.

- **Gap ID:** GAP-CRIT-03
- **Component / Path:** `orchestrator/checkpoint_manager.py:378-389`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:430` — "signed checkpoints fail `verify_checkpoint()` if files *and* metadata are rewritten". `checkpoint_manager.py:19-21` — "verification then fails on any post-hoc rewrite of files *and* metadata (checksums alone are self-declared)". `HOW_TO_USE.md:281-282` — "with `CHECKPOINT_SIGNING_KEY` set it also verifies the HMAC signature".
- **Actual (Code):** Verification is conditional on the attacker-controlled field being *present* **and** a key being configured:
  ```python
  signature = str(metadata.get("signature") or "")
  if signature:                       # absent/empty == skip entirely
      signing_key = _signing_key()
      if signing_key:                 # no key == skip entirely
          ...
  ```
  The MAC covers `<id>:<checksum>` (`:67-69`) where `checksum` is itself read from metadata (`:384`) — so an attacker rewrites the files, recomputes `files[name]` + `checksum`, and deletes the `signature` key. Demonstrated with `CHECKPOINT_SIGNING_KEY=supersecret`: `verify_checkpoint() -> True` after tampering with the signature removed, and again with `signature: ""`.
- **Impact:** The HMAC's entire value is making "rewrite files + metadata" unforgeable; a field-presence check hands that back. An integrity control documented as tamper-evident is tamper-*silent*. (The constant-time comparison itself is correct.)

- **Gap ID:** GAP-CRIT-04
- **Component / Path:** `orchestrator/orchestrator.py:281-288` (review-required short-circuit), `:1252` + `:1223-1229` (`complete_review` signature), `orchestrator/agents/base_agent.py:163-167`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:228-234` — "the DoD rejects content delivered only to `docs/` when the real file is missing (`expected output missing from the project`)". `:287-288` — "The DoD rejection is stored as the task note (not the claiming summary) so the next attempt sees honest context".
- **Actual (Code):** `orchestrator.py:281-287` takes the `review_block.get("required")` branch **before** the `elif dod_problems:` at `:288`, discarding the computed problems. The re-check in `complete_review` (`:1252`) calls `definition_of_done(prospective)` with `output=None` **and** `preexisting=None`, and the missing-real-file rule is gated on `preexisting_set is not None` (`base_agent.py:163-167`) — so it is inert. Demonstrated with the real `TestAgent` + real `ReviewAgent`: `expected_outputs: ["tests/test_display.c"]`, no file ever created, `docs/` directory absent → `FINAL: DONE`, `review: PASS`, `note: Approved.` The DoD rejection is also never recorded: `note` reads `Rewrote driver; flashed; 8/8 pass` (the claiming summary), `error: None`.
- **Impact:** Review-required tasks bypass the entire G22 delivery guarantee. `project-templates/TASKS.yaml:15-17` ships `review.required: true` and 3 of 4 tasks in `projects/kid-robot-face/TASKS.yaml` have it — this is the **default** path, not an edge case. The existing regression test (`test_agents_and_review.py:1461-1488`) calls `definition_of_done` directly and therefore cannot see it.

- **Gap ID:** GAP-CRIT-05
- **Component / Path:** `orchestrator/orchestrator.py:401-419` (`_ingest_output_tasks`), `:353` (dispatch call site), `orchestrator/state_manager.py:754-782` (`append_task`)
- **Expected (Doc):** `framework/05_PLANNING_AGENT.md:107-110` — "Do **not** include `id`, `status` or `execution` fields — ids are assigned `TASK-NNN` by the runtime and **statuses are derived**"; "3..8 tasks … invalid specs are skipped with a warning". `ORCHESTRATOR_GUIDE.md:96` — "`--force` replaces an existing graph **only after a successful plan**".
- **Actual (Code):** The whitelist (`_normalize_plan_specs`, `orchestrator.py:642-652`) exists only on the `build_plan` path. `dispatch` calls `_ingest_output_tasks` **directly** at `:353`, and `StateManager.append_task` accepts from the spec: `status` (`:754-760`) → a model can inject `{"status": "DONE"}` and self-approve a task with no work; and `execution` merged verbatim (`:774-782`) → `{"attempt_count": 999}` immediately trips `same_strategy` and permanently loop-gates the task. There is no cap on task count and no length cap on `title`/`notes`/`expected_outputs` — compare `_create_followup_tasks` which caps at 10 items / 160 chars (`:1413`, `:1430`).
- **Impact:** A model can mark its own task DONE and can permanently brick a task, from any dispatch, with no human in the loop. The "statuses are derived" guarantee holds for `orchestrator plan` and is absent for every normal dispatch.

- **Gap ID:** GAP-CRIT-06
- **Component / Path:** `orchestrator/orchestrator.py:561-570` (`build_plan` truncate-then-ingest), `:1288-1389` (`dispatch_review`), `:1409-1455` (`_create_followup_tasks`), `orchestrator/state_manager.py:1078-1079`, `:1236-1237`, `orchestrator/checkpoint_manager.py:180-195`
- **Expected (Doc):** `orchestrator.py:441-443` (the code's own docstring) — "An existing graph is only replaced after the agent produced a usable plan, so a failed or unavailable LLM never destroys existing tasks." `ORCHESTRATOR_GUIDE.md:306-307` — "all state read-modify-writes are serialized by … an `RLock`".
- **Actual (Code):** Two independent defects.
  (a) *Data loss:* `build_plan` truncates first, ingests second:
  ```python
  561  with self._state_lock:
  562      if existing:
  564          document["tasks"] = []
  565          self.state.save_tasks_document(document)
  568      created = self._ingest_output_tasks(output)
  569  if not created:
  570      raise StateError("no tasks could be ingested from planning output")
  ```
  The "successful plan" gate is spec-normalisation only; every per-task `append_task` failure inside `_ingest_output_tasks` is downgraded to a warning (`:415-419`). If all 8 fail, the prior graph is already destroyed at `:565` and `:570` then raises.
  (b) *No cross-process serialization:* there is **no file lock anywhere in the codebase**. `atomic_write_text` (`state_manager.py:84-104`) makes each file replace atomic, but nothing serialises a read-modify-write across processes. `dispatch_review` (the whole method), `complete_review`, `_create_followup_tasks` and `approve_decision` mutate state with no lock; `_create_followup_tasks` is the worst case (`load_tasks_document()` at `:1416` … `save_tasks_document()` at `:1450`, a lost-update window over every task in the graph). `DEC-NNN`/`RISK-NNN` ids are `max(existing)+1` (`:1078-1079`, `:1236-1237`), so two concurrent appenders both compute `DEC-004` and the second `os.replace` silently wins. `checkpoint_manager._load_index`/`_save_index` has the identical unlocked race on `index.json`, which then makes `check_milestones`' dedupe set (`orchestrator.py:1705`) wrong and re-creates an existing `cp-milestone-*`.
- **Impact:** Task-graph data loss on `plan --force`, silent loss of decisions/risks/checkpoint index entries under concurrent access. `memory.md:62` already records the mitigation as "One runner process at a time against a project" — an operational workaround standing in for a missing durability mechanism.

- **Gap ID:** GAP-CRIT-07
- **Component / Path:** `orchestrator/config.py:138`, `orchestrator/cli.py:963`, `orchestrator/llm_client.py` (endpoint consumption)
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:416` — "the CLI also auto-loads `./.env`". `HOW_TO_USE.md:290-291` — "auto-loads `./.env` at startup (setdefault semantics … skipped while the test suite runs)".
- **Actual (Code):** `env_path = Path.cwd() / ".env"` and `maybe_load_env_file()` runs **before** `parse_args` (`cli.py:963`), with the return value discarded — no notice, no allowlist, no scheme check. Because the backend endpoint comes from that file (`OLLAMA_BASE_URL`, `ORCHESTRATOR_LLM_PROVIDER`, `ANTHROPIC_BASE_URL`), running `orchestrator` inside an untrusted or freshly cloned repo silently redirects every prompt — `PROJECT_MEMORY.md`, `DECISIONS.md`, inline expected-output bodies up to 32K/file (`base_agent.py:418-444`), and `_plan_source_index()`'s list of real source paths — to a host chosen by that repo's `.env`. Demonstrated live: a probe server received two `POST /api/chat` requests of ~19.7 KB. There is **no redaction layer anywhere** (`grep -rniE "redact|scrub|mask" orchestrator/` → 0 hits).
- **Impact:** Prompt exfiltration to an attacker-chosen endpoint, and SSRF, from merely `cd`-ing into a directory. The single act that makes the tool convenient (auto-config) is the act that makes it unsafe.

- **Gap ID:** GAP-CRIT-08
- **Component / Path:** `orchestrator/agents/base_agent.py:382-386` vs `orchestrator/agents/specialists.py:34,52,70,105,134,154,174,282` and `orchestrator/agents/requirements_agent.py:58`
- **Expected (Doc):** `base_agent.py:382-386` is designed so that `[self.SYSTEM_RULES, OFFLINE_EXECUTION_RULE, AUTHORING_CONTRACT]` reaches every agent — verified by probe: `CONTRACT=True OFFLINE=True` for all 10 registry ids.
- **Actual (Code):** The probe confirms the composition works, and simultaneously that it delivers nothing. Every subclass **replaces** `SYSTEM_RULES` rather than extending it, so the six base rules never reach any model. Probe across all 10 ids: `base-rules=False  rule6=False`. The lost rules include **rule 6**, `data.acceptance_results = [{name, status, detail}]`, which `orchestrator.py:1205-1220` uses as the *only* acceptance-evidence gate; rule 4 "Report measurable results only; vague claims are rejected"; and rule 1 "Project files are the source of truth". `extra_rules` (`base_agent.py:375,384`) is the intended extension point and is never used by any agent.
- **Impact:** The acceptance-evidence DoD branch is dead for every real agent: nothing instructs a model to emit the field, so the check only fires when a *task* happens to carry one by hand. A documented control that no code path can reach from the model side.

- **Gap ID:** GAP-CRIT-09
- **Component / Path:** `framework/AGENT_PROMPTS/*.md` (all 11 output-contract blocks), `orchestrator/agents/base_agent.py:252-260`, `:786`
- **Expected (Doc):** `framework/AGENT_PROMPTS/02_REQUIREMENTS.md:15-22` and its 10 siblings specify a top-level `"documents": [{"name": "<file>.md", "content": "..."}]` array as the delivery channel, plus `status`/`summary`/`data` as the contract.
- **Actual (Code):** Nothing reads a top-level `documents` key. `AgentOutput` has 9 fields (`agent_id, task_id, status, summary, data, artifacts, errors, warnings, produced_at`); only 2 are documented. Materialization reads `data.documents` as a **dict** `path → body` (`base_agent.py:786`). A model following these 11 documents emits a shape the runtime silently discards. Conversely, the fields the code *does* consume — `data.edits`, `data.documents`, `data.edits_applied`, `data.acceptance_results`, `data.review_status`, `data.proposed_change`, `data.strategy_changed` — have **0 occurrences** across all 11 files (`grep -rn "data.edits\|data.documents\|acceptance_results" framework/AGENT_PROMPTS/` → 0 hits), as do `OFFLINE_EXECUTION_RULE` and `AUTHORING_CONTRACT`.
- **Impact:** The prompt specifications — the natural reference for anyone debugging agent behaviour, and the closest thing to a contract for the model — are wrong in shape and silent on substance. Combined with GAP-CRIT-08 this is the highest-leverage drift cluster in the repository: ~15 findings, one root cause.

---

### 2. High Severity

- **Gap ID:** GAP-HIGH-01
- **Component / Path:** `orchestrator/orchestrator.py:150`, `:141-164`; `orchestrator/agents/base_agent.py:592-596`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:163-164` — "`register_agent()` pins a singleton (**safe for `--max-concurrent > 1`**)"; `:308` — under "PARALLEL EXECUTION".
- **Actual (Code):** `orchestrator.py:150`: `if key in self.registered_agents and (not fresh or key in self._pinned_agents): return self.registered_agents[key]` — a pinned key is returned **even when `fresh=True`**. The method's own docstring (`:143-147`) says the opposite ("its execution scratch state is not thread-safe"). `run_cycle` passes `fresh=True` for parallel batches (`:1615`) and still gets the shared instance. `run()` writes per-task scratch onto the instance: `_current_task_id` (`base_agent.py:592`), `last_payload_chars` (`:593`, read at `orchestrator.py:1030` for token accounting → wrong tokens attributed to the wrong task), `_delivery_snapshot` (`:596`, the DoD's `preexisting` set). Demonstrated cross-thread scratch corruption.
- **Impact:** Under `--max-concurrent > 1`, two tasks owned by one pinned agent interleave scratch state: `validate_output:934-938` can reject a correct output as belonging to another task, and `delivery_problems` can read another task's delivery snapshot. The documented safety guarantee is false.

- **Gap ID:** GAP-HIGH-02
- **Component / Path:** `orchestrator/orchestrator.py:377-381`, `:1377-1381`
- **Expected (Doc):** `review_gaps.md:180-182` — "the fallback chain evaluates *all* triggers, so **milestone checkpoints are never short-circuited**"; `ORCHESTRATOR_GUIDE.md:453-454` — "`cp-milestone-complete` when the whole graph is terminal".
- **Actual (Code):** ```checkpoint_id = (self._maybe_auto_checkpoint(after) or self.check_milestones() or phase_checkpoint)``` — `or` short-circuits. The comment at `:367-368` ("every check must run — no short-circuit") is true only of the *phase* check, which is hoisted to `:369`.
- **Impact:** On the dispatch that both triggers compaction and completes the last task of a milestone (or of the whole graph), `check_milestones()` never runs. If that was the final dispatch, `cp-milestone-complete` is never written — precisely the moment the snapshot matters most.

- **Gap ID:** GAP-HIGH-03
- **Component / Path:** `orchestrator/orchestrator.py:253`, `:269`; `orchestrator/agents/llm_agent.py:319-327`
- **Expected (Doc):** `orchestrator.py:199-201` (its own docstring) — "**Execution phase (unlocked):** `agent.run(task)`"; "only the (slow) agent execution runs outside the lock". `ORCHESTRATOR_GUIDE.md:258-259` repeats it.
- **Actual (Code):** The finalize lock opens at `:253`; at `:269`, still inside it, `agent.repair_delivery(task, output, dod_problems)` runs a full `build_payload` → `build_prompt` → `client().complete()` network round trip (`ORCHESTRATOR_LLM_TIMEOUT` default 120 s).
- **Impact:** With `--max-concurrent 4`, three workers block on `_state_lock` for one repair round trip — twice per dispatch at worst (dispatch + edit session). The stated off-lock invariant is broken on the slowest path.

- **Gap ID:** GAP-HIGH-04
- **Component / Path:** `orchestrator/state_manager.py:1412-1445` (`validate`); `orchestrator/cli.py:491`, `:577-586`; `orchestrator/orchestrator.py:1805-1819`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:20` — "State files as the single source of truth (atomic writes, `validate()`)"; `:128` — "`problems = orch.state.validate()   # [] == structurally sound`"; `:483` — "`orchestrator … status  # prints validate() problems`".
- **Actual (Code):** `validate()` checks only: PROJECT/TASKS load, entries are mappings, `id` present, `id` unique, `status ∈ TASK_STATUSES`, `dependencies` resolve. **Not** checked: `phase.current ∈ config.PHASES` (a garbage phase makes `phase_index` return 0 at `config.py:426-431`, silently disabling forward-only comparison in `_checkpoint_phase_advance:913` and pinning the phase forever); dependency cycles (only `supervisor.detect_circular_dependencies`, a separate `health` call); self-dependencies; unknown `owner` (which `append_task:748-752` *does* check); non-list `dependencies` (silently treated as empty by `dependency_ids:341-342`, so a typo'd dependency **disappears** and the task becomes dispatchable). It is called from exactly one place — `cli.py:491` in `cmd_init`. `cmd_status`, `cmd_tasks`, `cmd_run` and `cmd_plan` never call it, so the troubleshooting section's recommended command does not do what it says.
- **Impact:** Corrupted state is invisible on the command documented to reveal it; a typo'd dependency silently converts a blocked task into a dispatchable one.

- **Gap ID:** GAP-HIGH-05
- **Component / Path:** `orchestrator/orchestrator.py:1288-1389`, `:1364`, `:352-359`; `orchestrator/state_manager.py:662`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:263` — the finalize phase includes "loop signals + fingerprint". `config.LOOP_KINDS` advertises `repeated_output` and `no_new_evidence` as gates.
- **Actual (Code):** `dispatch` records loop signals at `:352`; `dispatch_review` never calls `_record_loop_signals`. It calls only `update_task_execution(task_id, no_progress_delta=-1)` at `:1364`, with `reset_error` defaulting to `False`. So (a) a task that only churns through review attempts never accumulates `repeated_output_count` or `evidence_stall_count`, making `repeated_output` and `no_new_evidence` unreachable for review-only loops; (b) `execution.last_error` from the last failed review survives a PASS and is injected into the *next* task's prompt as `recovery_feedback_from_previous_attempt` (`base_agent.py:403-406`) as if it were the current failure; (c) `last_loop` is never cleared on the review path.
- **Impact:** Two of six documented loop kinds are dead on the review path — which is the default path. Stale failure text from a previous task is injected as if current, actively misinforming the next agent.

- **Gap ID:** GAP-HIGH-06
- **Component / Path:** `orchestrator/orchestrator.py:1223-1271` (`complete_review`), `:1252`, `:1259-1267`
- **Expected (Doc):** `review_gaps.md:277-280` — the DoD "fails on any `acceptance_results` entry with a non-passing verdict, sourced from the task or **`output.data`**".
- **Actual (Code):** `complete_review` has no `output` parameter; `:1252` calls `definition_of_done(prospective)` with `output=None`, so `:1206-1209` can only read `task.get("acceptance_results")`. `dispatch_review:1352` never passes the output. Separately, `:1259-1267` writes `entry["status"] = status` **directly into the document**, bypassing `update_task_status` (`state_manager.py:620-653`) — so the terminal-status `recovering = False` reset (`:641-644`) never happens on the review path, and the status is written without `config.TASK_STATUSES` validation.
- **Impact:** A reviewer returning `{"review_status": "PASS", "acceptance_results": [{"name": "x", "status": "FAIL"}]}` produces `TASK_DONE` — the documented gate is unreachable for the only agent whose verdict carries evidence.

- **Gap ID:** GAP-HIGH-07
- **Component / Path:** `orchestrator/agents/llm_agent.py:245`, `:253`; `orchestrator/agents/base_agent.py:587-590`, `:611-613`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:270` — "reviewer … runs with `materialize=False`". `base_agent.py:587-590` documents the flag as suppressing artifact emission.
- **Actual (Code):** `run()` honours it (`:611,613`); `_execute_edit_session` calls `self._apply_edits` and `self._materialize_artifacts` **inside** `execute()` (`:245,253`), where the flag does not exist. Demonstrated: `materialize=False` → `status: completed`, and the real project file `x.c` was written with the new content anyway.
- **Impact:** A contract the code documents but does not honour. Latent today (only `ReviewAgent` receives `materialize=False`, and it has `EDIT_SESSION_TURNS=0`), but any agent with sessions enabled inherits the ability to patch real project files during a non-materializing pass.

- **Gap ID:** GAP-HIGH-08
- **Component / Path:** `orchestrator/agents/base_agent.py:729`, `:758`, `:768`, `:775` (`_apply_edits` loop)
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:200-201` and `base_agent.py:706-712` describe atomic application of a change set.
- **Actual (Code):** Only the *per-file* write is atomic. The loop `return`s on the first error, leaving earlier files already patched. Demonstrated: three edits where the second fails → `status: failed`, error `edits['bad.c'][0] search matched 0 time(s)`, and `ok.c` **already patched on disk** with `int ok(void){return 111;}`, while `docs/ok.c` was never written (`run()` skips materialization on failure).
- **Impact:** A rejected change set leaves the source tree half-patched with no `docs/` mirror describing it, and the task FAILED — so the next attempt sees a file no artifact accounts for.

- **Gap ID:** GAP-HIGH-09
- **Component / Path:** `orchestrator/agents/base_agent.py:800-801`, `:452-484`, `:149`, `:151-156`; `orchestrator/state_manager.py:771`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:222-236` — the delivery manifest classifies each `expected_outputs` path as `EXISTS` / `MISSING` / `docs/`, and `data.documents` creates the real path for `MISSING`.
- **Actual (Code):** Two defects from `filename = Path(name).name`. (a) A `docs/`-nested declared output such as `docs/nested/x.md` is flattened to `docs/x.md`, while `_delivery_manifest:460` classifies it a docs deliverable, so `delivery_problems:149` `continue`s — demonstrated: materialization reports success, the declared path does not exist, DoD reports **zero** problems. (b) Same-basename outputs across directories collide last-writer-wins, and the surviving `docs/` mirror then contradicts the real file, producing a phantom `shares no line` DoD failure. (c) `expected_outputs` is never path-validated anywhere: `append_task` takes the list verbatim (`state_manager.py:771`) and `_normalize_plan_specs` keeps model paths as-is (contrast `input_files`, filtered by `_validated_input_files:850-879`). A planner can inject `docs/../../etc/evil.c` or `/etc/evil2.c`; `relevant_context` then inlines arbitrary readable file bodies into the prompt — demonstrated reading `/etc/passwd`. (d) `delivery_problems:151-156` treats an *escaping* path as "no problem" rather than a violation. (e) `_delivery_manifest:468` calls `target.is_file()` unguarded, so an unreadable absolute path raises an uncaught `PermissionError` that aborts `run()`.
- **Impact:** Silent delivery failure for a class of paths; an arbitrary-file-read exfiltration primitive into whatever LLM backend is configured; a malformed task can abort the run instead of degrading.

- **Gap ID:** GAP-HIGH-10
- **Component / Path:** `orchestrator/agents/requirements_agent.py:53`, `:338-350`; `orchestrator/config.py:413`; `orchestrator/state_manager.py:808`
- **Expected (Doc):** `framework/02_REQUIREMENTS_AGENT.md:4` — "Turn a project idea into precise, testable requirements"; `:50-51` — return `REQUIREMENTS.md` with `REQ-*` entries, a traceability matrix, assumptions, open questions.
- **Actual (Code):** `RequirementsAgent` subclasses `BaseAgent`, **not** `LLMAgent`, and `execute()` never calls a model. It regex-parses whatever context it is given and never emits `data.documents`, so its delivery is always the rendered wrapper. Demonstrated on the shipped starter task: `status: DONE`, `summary: Parsed 0 requirements`, `docs/REQUIREMENTS.md` first line `# REQUIREMENTS.md`, and **no `REQUIREMENTS.md` at the project root**. `definition_of_done` only verifies criteria *exist* (`orchestrator.py:1174-1177`), never that they are met, so "At least 10 REQ entries with measurable acceptance criteria" passes vacuously.
- **Impact:** The agent that owns the `REQUIREMENTS` phase is structurally incapable of authoring requirements. `memory.md:74` records this ("deterministic parser — structurally incapable of authoring"), yet it remains the registered owner and DoD's traceability gate depends on a `REQUIREMENTS.md` it can never write.

- **Gap ID:** GAP-HIGH-11
- **Component / Path:** `orchestrator/orchestrator.py:987-1015` (`_detect_oscillation`), `:1521-1534` (`state_fingerprint`)
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:324` — "`state_oscillation` | project state flips A↔B (status-only fingerprints) | **4 changes, 2 states**"; `:336-338` — "`state_oscillation` is checked **first** in the dispatch gate".
- **Actual (Code):** `orchestrator.py:1000`: `if len(compressed) < 4 or len(set(compressed)) != 2: return None` — the two-state condition is over the **whole** 12-entry rolling history, not the last 4. Once a project has recorded 3+ distinct status fingerprints (i.e. after any normal sequence of ≥3 dispatches that changed state), `state_oscillation` is permanently unreachable until older entries slide out of the buffer — which cannot happen, because each new distinct state adds to the set. Secondary: `flips = len(compressed) - 1` at `:1009` is computed over the whole history while `count`/`threshold` describe the last 4, so `detail` misreports; and `state_fingerprint` (`:1531`) hashes `review.status`, so a review transition alone registers as a state change despite the "status-only" description.
- **Impact:** The loop kind documented as the *first* gate is effectively dead in a real session. Its threshold is also hard-coded (`:998`, `:1015`) and absent from `config.LoopThresholds`, contradicting `config.py:11-13` — "Nothing in this package should hard-code a threshold".

- **Gap ID:** GAP-HIGH-12
- **Component / Path:** `orchestrator/state_manager.py:470-558` (`recompute_derived_state`), `:84-104` (`atomic_write_text`); measured
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:246-247` — "files are only written when a derived value actually changed" (implying change detection is the design).
- **Actual (Code):** No caching layer; every mutation re-reads and re-writes whole documents, and `recompute_derived_state` re-parses `TASKS.yaml` **four times** (`:470`, `:472`, via `status_breakdown():475`, via `get_ready_tasks():558`). Measured `update_task_status` cost by graph size: 25 tasks → 206 ms; 100 → 679 ms; 200 → 1 620 ms; 400 → ~3 100 ms — clean O(n) per mutation ⇒ **O(n²) per run**; 100 sequential mutations at n=400 did not finish inside 120 s. cProfile of one mutation at n=400: **5.99 s**, of which `yaml.safe_load` = 3.72 s and `safe_dump` = 2.27 s. Every parse is a separate `os.replace` durability event; there is no batch or transactional write. Additionally `dependencies_satisfied` rebuilds the whole `by_id` index on every call (`:345-354`), called per task → O(n²) inside an O(n²) caller; `validate()` rebuilds `seen_ids` by full list copy per iteration (`:1436-1438`) and its declared type `Iterable[str]` (`:1427`) is false.
- **Impact:** A 400-task generated graph (`plan --max-tasks N`) is unusable. Quadratic I/O and no transactional grouping are the reason every mutator costs ~4 durability events.

- **Gap ID:** GAP-HIGH-13
- **Component / Path:** `framework/AGENT_PROMPTS/*` (11 files), `framework/00_MASTER_ORCHESTRATOR.md`, `framework/05_PLANNING_AGENT.md`, `framework/09_REVIEW_AGENT.md`, `project-templates/TASKS.yaml:54-58`
- **Expected (Doc):** These files present themselves as the authoritative per-agent prompt specification and task schema.
- **Actual (Code):** Systemic drift, verified by probe and grep:
  - `framework/05:47-51` and `project-templates/TASKS.yaml:54-58` document 4 `execution:` fields; the live schema has **10** — the 6 undocumented ones (`attempts_since_change`, `recovering`, `evidence_stall_count`, `repeated_output_count`, `last_output_hash`, `retry_reason`, `last_loop`) are precisely those that gate dispatch.
  - `framework/05:38` lists 8–9 task states; `config.TASK_STATUSES` has 10. `DONE WITH ACCEPTED LIMITATION` — the actual outcome of `PASS WITH ACTIONS` — appears in neither list; `CANCELLED` is missing too.
  - `framework/05:58-65` promises the planner delivers parallel work groups, milestones with exit criteria, and a risk list; `_normalize_plan_specs` whitelists none of them (`orchestrator.py:643-652`) and `parallel_groups` is only *pruned*, never computed (`state_manager.py:484-501`).
  - `framework/09:41` says `REVIEW_REPORT.md`; the code writes `docs/REVIEW-<task>.md` (`orchestrator.py:1394`) and `AGENT_PROMPTS/09:6` correctly says the latter — the two framework docs contradict each other.
  - `framework/00:105-107` and `framework/01:16-22` state 3 loop limits; `LoopThresholds` has 6.
  - `AGENT_PROMPTS/00:11` and `01:11` claim `framework/00` and `framework/01` are "appended to the system prompt automatically". False: `load_agent_spec` is only called from `LLMAgent.spec_text()` with `self.AGENT_ID`, and no agent has `AGENT_ID == "supervisor_agent"` or `"master_orchestrator"` — those keys in `prompt_builder.AGENT_SPEC_FILES:27-28` are never read. `SupervisorAgent.diagnose()` uses a hardcoded 4-line system prompt (`supervisor.py:816-821`), and `MasterOrchestrator` never builds a prompt.
  - `AGENT_PROMPTS/09:27-32` omits the very rule that fixes the G12 failure ("Output contract: `data.review_status` MUST be exactly 'PASS' / 'PASS WITH ACTIONS' / 'FAIL'") — `specialists.py` carries it, the doc does not.
  - `AGENT_PROMPTS/06:27-31` omits both hardware anti-refusal rules added to fix G10 (the incident's post-mortem artifact still omits the fix).
  - `AGENT_PROMPTS/05:27-37` relegates the entire JSON task-graph contract to a prose bullet, while `PlanningAgent.SYSTEM_RULES:82-84` carries the full machine-facing version.
- **Impact:** Roughly 15 findings from one root cause: the contract that governs model behaviour is specified in one place and contradicted in eleven. Anyone diagnosing agent behaviour from the prompt docs will reach the wrong conclusion.

- **Gap ID:** GAP-HIGH-14
- **Component / Path:** `README.md:309-310`, `:69-71`, `:33-43`, `:104-126`; `DEPLOYMENT_SUMMARY.md:5`, `:37-45`, `:90-100`, `:203`, `:245-257`; `QUICK_REFERENCE.md:13-42`, `:107-110`, `:151-165`; `CONTRIBUTING.md:44`; `GAP_ANALYSIS.md:25-37`, `:170-176`; `IMPLEMENTATION_ROADMAP.md` (whole file)
- **Expected (Doc):** These are the user-facing entry points (`README.md:23`, `:27-32`, `:52-53` link `framework/`, `agents/`, `references/`, `checkpoints/`).
- **Actual (Code):** Verified against the tree:
  - **Version contradiction, 4-to-1.** `README.md:309` "Framework Version: **1.0** / Status: Production Ready", `DEPLOYMENT_SUMMARY.md:254`, `QUICK_REFERENCE.md:194`, `CONTRIBUTING.md:44` — against `ORCHESTRATOR_GUIDE.md:3-4` ("2.0.0 / STABLE") and the repo's own `pyproject.toml:7`.
  - **README's example-project claims are false:** `REQ-001..REQ-020` (actually `REQ-015`), "30+ tasks" (4), "Test plan and acceptance criteria" (no `docs/` at all), "Checkpoints at key milestones" (no `checkpoints/kid-robot-face/`). The tree at `:33-43` lists `projects/kid-robot-face/docs/` and `implementation/` — neither exists.
  - **`IMPLEMENTATION_ROADMAP.md` is dead.** "Ready to Build 🚀", a file layout that doesn't exist, `anthropic-sdk`/`requests` (runtime is stdlib-only, PyYAML), `claude-opus-4-5`, `python orchestrator.py --run-next-task` (real CLI is 12 subcommands under `orchestrator`/`bin/orchestrator`), git auto-commit (`subprocess` → 0 hits), TOML/YAML config (no loader), and four weeks of unchecked success criteria that are all satisfied today.
  - **`DEPLOYMENT_SUMMARY.md` is dead.** Wrong path `/home/claude/…`; "Production-tested (used on real projects like kid-robot-face)" for a project `memory.md:17` forbids dispatching against; invented `TEST-001…TEST-060`, `TASK-001…TASK-080`, `GROUP-1…GROUP-8`, `CP-KID-ROBOT-001-INIT` (all 0 hits); file statistics off by ~10×; "Next Step: build CLI tooling" for the CLI that exists.
  - **`QUICK_REFERENCE.md` is dead.** Tree predates the runtime (no `orchestrator/`, no `bin/`, no tests); claims `project-templates/` holds 1 file (8); instructs a `cp` of files its own tree says are absent; "20 tasks (TASK-001..TASK-080)" is self-contradictory and wrong (4); its git instructions would create a duplicate remote and fail on the existing `v1.0.0` tag (at `88276d7`, 24 commits behind HEAD).
  - **`GAP_ANALYSIS.md` is self-labelled historical (`:3-6`) but its tables are unmarked and present-tense** — "LLM/prompt-driven agents: **Missing**", "Parallel execution: **Missing**", "Decision & risk control: **Missing**", "there is no `bin/orchestrator`", "913 LOC superseded but still exported", "`requirements.txt` declares `pydantic`". All false; every cited `file:line` is stale by 100+ lines.
  - `GITHUB_PUSH_INSTRUCTIONS.md` — wrong path, "Git history (4 commits)", re-tagging an existing `v1.0.0`, "Next Step: start building Week 1 orchestrator".
- **Impact:** A new reader is actively misinformed about the most fundamental facts: what version this is, what the runtime does, and whether the example project works. The dead files instruct commands that cannot succeed.

- **Gap ID:** GAP-HIGH-15
- **Component / Path:** `orchestrator/orchestrator.py:1119-1128`, `:1078-1083`; `orchestrator/state_manager.py:558`, `:364-369`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:388-393` — "its **non-terminal** tasks move to `WAITING` … the task is never marked FAILED". `:244` — "`next_tasks` = **currently READY** ids".
- **Actual (Code):** `_set_affected_waiting` excludes only `TERMINAL_TASK_STATUSES`, `TASK_REVIEW` and `TASK_WAITING`, so `IN_PROGRESS`, `FAILED`, `TODO`, `READY`, `BLOCKED` are all clobbered to `WAITING`. Consequences: (a) under `--max-concurrent > 1`, task B's `proposed_change` (recorded at `:255` in B's phase 3) flips task A to `WAITING` **while A's worker thread is still executing**; A's phase 3 then writes `DONE`/`FAILED` and the gate is never re-evaluated — a gated task completes; (b) a `FAILED` task's status is erased, hiding the failure from `analyze_blocked` (`supervisor.py:443` keys on `TASK_FAILED`) and from `ensure_failure_risk`. Separately, `next_tasks` is built from `get_ready_tasks()`, which includes `TASK_WAITING` (`:367`), so gate-parked tasks are published as "READY". And `affected_tasks` is filtered to non-empty strings but never validated against real ids (`:1078-1083`), so a model inventing `affected_tasks: ["ARCHITECTURE"]` produces a `PROPOSED_CHANGE` that gates **no** task yet still counts for `detect_decision_conflicts`, able to force `HUMAN_DECISION_REQUIRED`/exit 4 for nothing.
- **Impact:** The decision gate — a core safety control — has three holes: it leaks under parallel execution, it erases failure evidence, and it can be weaponised by a hallucinated id into blocking escalation.

- **Gap ID:** GAP-HIGH-16
- **Component / Path:** `conftest.py`, `orchestrator/config.py:169-170`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:473-474` — tests are offline; `memory.md:21` — "LLM backend is stdlib-only … the suite must stay offline-safe".
- **Actual (Code):** There is **no network guard** — no socket/`urlopen` block anywhere in `conftest.py`. Offline safety rests solely on the `PYTEST_CURRENT_TEST` check in `maybe_load_env_file()` (a plain, user-controllable env var, absent during collection) and on the developer's shell being clean. Demonstrated real egress: with `OLLAMA_BASE_URL` pointed at a local probe, `test_cli_commands.py::TestBinEntrypoint::test_bin_orchestrator_init_roundtrip` produced **two live `POST /api/chat` requests of ~19.7 KB**. It passed only because `cmd_init` catches `Exception` and degrades to the starter skeleton (`cli.py:444-449`). Export `ANTHROPIC_API_KEY` in your shell and the suite silently bills real calls, each bounded only by `ORCHESTRATOR_LLM_TIMEOUT` (default 120 s).
- **Impact:** The hermeticity the docs promise is unenforced and has already been demonstrated to leak. A CI environment with an inherited key would incur unbounded, untested spend.

- **Gap ID:** GAP-HIGH-17
- **Component / Path:** `project-templates/PROJECT.yaml`, `project-templates/TASKS.yaml:63-66`, `project-templates/NEW_PROJECT_CHECKLIST.md:29,53,58,67,71`; `orchestrator/cli.py:371`, `:388-390`, `:403-405`
- **Expected (Doc):** `meta/GETTING_STARTED.md:40-57` and `:139` teach "copy the 7 template state files" as a primary setup path; `review_gaps.md` (A8 status) claims `constraints`/`budget`/`resources` blocks "are part of the scaffold".
- **Actual (Code):** Two hand-diverged scaffolds for the same files. `project-templates/PROJECT.yaml` has **6** `progress` keys and **no** `constraints`/`budget`/`resources`; `cmd_init:371` writes **2** progress keys, `:388-390` writes all three blocks. `project-templates/TASKS.yaml` writes `summary: {total_tasks, status_breakdown}`; `cmd_init:405` writes `summary: {total: 0}` — a different key that `recompute_derived_state` then orphans and replaces. `project-templates/` has no `docs/` at all, and `cmd_init:430-433` creates `docs/README.md`; a copied-template project therefore **can never pass DoD**, because every `expected_outputs` mirror must exist under `docs/`. `NEW_PROJECT_CHECKLIST.md:29,53,71` teaches hand-writing `project.status: APPROVED`/`READY`, which `recompute_derived_state` (`state_manager.py:570-572`) and `set_phase` (`:275-276`) silently overwrite with a phase name. `:28,52,70,105` name checkpoints `CP-PROJECT-001-REQUIREMENTS` etc. that no code path can create. `:58,24` demand "20–50 tasks" and "at least 10" requirements against a runtime whose planner guidance is **3–8 tasks** (`specialists.py:92`, `framework/05:109`).
- **Impact:** The documented manual setup path produces a project that cannot satisfy DoD and teaches three writes the runtime silently reverts.

- **Gap ID:** GAP-HIGH-18
- **Component / Path:** `memory.md:9`, `:10`, `:12`, `:13`, `:88`, `:36`; `review_gaps.md:437`, `:444`, `:76`, `:56`
- **Expected (Doc):** `memory.md` is the session-memory-of-record and directs the next agent's behaviour, including the standing git rule at `:88`.
- **Actual (Code):** Verified: `git status -sb` → clean, **0 ahead**; `origin/main` == HEAD == `29b920c`. Both halves of `:12` are wrong ("Pushed … through `5dc41fc`"; "The G1–G8 work above is not yet committed — ask before committing"). `:10` claims G1–G8 "uncommitted" — they shipped 24 commits ago. `:9` and `:13` claim a 314-test suite; the truth is 401 (`test_auto_plan.py` collects 46, not 39). `:88` says "synced as of `5dc41fc`". `:36` propagates the wrong ingestion key (`proposed_tasks` — no such key anywhere in code). Header `:3` says "Last updated 2026-09-30" while `:76-82` carry 2026-10-01 entries. `:84` points leftovers at `review_gaps.md`, which contains no entry for the `data.deploy` gap, the unrun TASK-006, or the missing last mile — those live only here.
- **Impact:** The file that exists to keep future sessions accurate actively misleads them: on git state, on test counts, on a contract key, and on where the open problems are recorded. The "ask before committing" instruction is moot, and the standing commit rule at `:88` is being followed — which is precisely why the "as of `5dc41fc`" note is now wrong.

---

### 3. Medium Severity

- **Gap ID:** GAP-MED-01
- **Component / Path:** `orchestrator/llm_client.py:144`, `:152`, `:158`; `orchestrator/cli.py:962-995`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:105-106` — "`2` usage / **state error**". `HOW_TO_USE.md:327` repeats it.
- **Actual (Code):** `main()` handles `StateFileMissingError`, `(StateError, OrchestratorError, MissingAgentError, CheckpointError)`, `FileNotFoundError` — and nothing else. **No `except Exception`**, no `KeyboardInterrupt` guard. Demonstrated: `PermissionError` on `TASKS.yaml` → full traceback and **exit 1**. `LLMClient.__init__` parses `int(env_max)`, `int(env_ctx)`, `float(env_timeout)` with no guard (contrast `context_monitor.py:28-34`, which does), so a malformed `ORCHESTRATOR_LLM_TIMEOUT` raises `ValueError` that `LLMAgent.execute` (which catches only `LLMError`, `:142`, `:207`) drops into `BaseAgent.run`'s blanket handler and reports as "Agent 'X' raised ValueError" instead of a configuration error. `atomic_write_text` lets `OSError` propagate and `save_yaml_file` converts only `yaml.YAMLError`, while every catch site is narrower than `StateError`.
- **Impact:** Filesystem conditions (ENOSPC, EACCES, EISDIR) surface as interpreter tracebacks with exit code 1 — the code the guide reserves for "no command" — leaving partial state with no rollback. Separately, exit code 1 is overloaded onto validation failure at `cli.py:491-495`, so a caller cannot distinguish "no command" from "your project is structurally broken".

- **Gap ID:** GAP-MED-02
- **Component / Path:** `orchestrator/cli.py:637-641`, `:667-672` vs `:734-737`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:105-106` — "`3` loop limit **or STALLED/BLOCKED health**".
- **Actual (Code):** `cmd_run` returns 3/4 only in the `if not results:` branch (`:637-641`). On the results path (`:667-672`) it computes the report and escalates only `HUMAN_DECISION_REQUIRED` → 4. `cmd_health` (`:734-737`) handles it correctly.
- **Impact:** A run that dispatched work and ended `STALLED` or `BLOCKED` exits **0**. Any script or CI gate relying on the documented exit-code contract silently reports success.

- **Gap ID:** GAP-MED-03
- **Component / Path:** `orchestrator/state_manager.py:200-208`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:405-407` — cumulative tokens "**never resets except at a compaction event, which zeroes the budget**".
- **Actual (Code):** `set_context_utilization()` computes `cumulative_tokens = round(context_window_tokens() * pct / 100.0)` — a public setter that *decreases* the accumulator to match an externally supplied percentage.
- **Impact:** Any API caller of `set_context_utilization(10)` discards the session's token history and can postpone compaction indefinitely. `review_gaps.md:247-249` describes this as "so the two stay in sync"; it is a lossy override, not a derivation.

- **Gap ID:** GAP-MED-04
- **Component / Path:** `orchestrator/orchestrator.py:181-191`, `:210-214`
- **Expected (Doc):** `review_gaps.md:141-142` — "`execution.last_loop` persists the latest `LoopLimitExceededError` **at every raise site**".
- **Actual (Code):** `orchestrator.py:183-185` returns early when `loop.task_id != task_id`, with the comment "PROJECT-level detections (oscillation) are not task state". The oscillation raise at `:210-214` therefore persists nothing; oscillation state lives only in the in-memory `self._fingerprint_history` (`:117`), which `resume_from_checkpoint:1789` clears.
- **Impact:** A project that oscillates, is stopped, and is resumed has **no persisted trace** that the gate ever fired — the audit trail is memory-only.

- **Gap ID:** GAP-MED-05
- **Component / Path:** `orchestrator/agents/base_agent.py:490-499`, `:521-532`; `orchestrator/state_manager.py:1036-1047`, `:1042`
- **Expected (Doc):** `review_gaps.md:299-301` — decisions injected into the payload are the "**open/proposed**" ones affecting the task.
- **Actual (Code):** (a) A bare `except Exception: return ""` with no log silently drops all decision context on any parse failure. (b) `list_decisions()` hard-codes `"affected_tasks": []` for table-format rows (`:1042`), so any decision recorded in table form is invisible to `_decisions_affecting` forever. (c) The filter at `base_agent.py:497-499` tests only `task_id in entry["affected_tasks"]` — **no status filter** — so REJECTED and APPROVED decisions are injected too. (d) `_requirements_for` scans every line of `docs/REQUIREMENTS.md` once per declared id: O(ids × lines) per dispatch.
- **Impact:** An agent can be handed resolved decisions as live constraints, or handed none at all depending on which formatting path wrote them.

- **Gap ID:** GAP-MED-06
- **Component / Path:** `orchestrator/orchestrator.py:1200-1203`
- **Expected (Doc):** `review_gaps.md:275-277` — "each id must appear in `docs/REQUIREMENTS.md`".
- **Actual (Code):** `if req_id and req_id not in body` where `body` is the entire file (`:1192`) — a raw substring search, unanchored to a `REQ` heading, with no status check. `REQ-01` is satisfied by `REQ-010`; a requirement listed under "Rejected" satisfies traceability. `append_task` never validates `requirement_ids`, and `_normalize_plan_specs:642-652` **strips** the field, so `build_plan` silently loses any `requirement_ids` a model emits while `HOW_TO_USE.md:130` advertises it as a normal task field.
- **Impact:** The traceability gate is satisfied by any substring coincidence, and the planner cannot set the field at all.

- **Gap ID:** GAP-MED-07
- **Component / Path:** `orchestrator/llm_client.py:89-101`, `:350-362`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:424` promises a timeout; no retry policy is documented anywhere.
- **Actual (Code):** (a) `except urllib.error.HTTPError as exc: return int(exc.code), exc.read()` — `HTTPError` **is** the response, and the `with urlopen(...)` at `:94` never entered, so nothing closes it: every 4xx/5xx leaks a socket. (b) `ssl.SSLError`, `http.client.RemoteDisconnected`/`IncompleteRead` and `ConnectionResetError` are `OSError` subclasses that escape as `OSError`, not `LLMClientError`, defeating every `except LLMError` degradation path — demonstrated `UNHANDLED: ConnectionResetError`. (c) The ollama retry at `:350-362` tests `b'"think"' in body` against the **request** body, which always contains `"think": False` when the field was sent, so the condition collapses to `status not in 2xx`: every non-2xx triggers an immediate second request with no backoff, jitter, cap, or `Retry-After` handling. Demonstrated: one `init` against a 500 produced 2 POSTs of ~19.7 KB. Combined with `EDIT_SESSION_TURNS = 3` and the G17/G19 repair rounds, a persistently failing endpoint multiplies load deterministically. (d) `response.read()` has no size cap.
- **Impact:** Socket leak per failed request, retry amplification with no backoff, and unhandled transport exception types that reach the user as tracebacks.

- **Gap ID:** GAP-MED-08
- **Component / Path:** `orchestrator/supervisor.py:898-901`, `:513-514`, `:582`; `orchestrator/state_manager.py:293-298`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:345-346` — `sync_health` "persists PROJECT.yaml.health + blockers".
- **Actual (Code):** `human_decisions` is appended, never replaced: `for escalation in report.escalations: add_human_decision(f"[{category}] {task_id}: {reason}")`. The dedupe at `state_manager.py:295` (`if decision_text not in decisions`) is defeated by interpolation — the loop reason embeds counts (`(3/3 attempts)`, `supervisor.py:513-514`) and the context reason embeds `utilization_percent` (`:582`), so both strings mutate every cycle and each sync appends a new entry. Each append is a full `load_project()` + `save_project()`. `cmd_run` calls `sync_health()` **twice** (`cli.py:630`, `:667`), each `check_health` also parsing `DECISIONS.md` and `RISKS.md` per failed task. Compare `set_blockers` (`:301-306`), which *replaces* — the right model.
- **Impact:** `PROJECT.yaml.human_decisions` grows monotonically with redundant entries, each costing a full file rewrite, per cycle.

- **Gap ID:** GAP-MED-09
- **Component / Path:** `orchestrator/state_manager.py:973-981`, `:1328-1336`, `:1338-1345`, `:940-965`; `orchestrator/orchestrator.py:974-977`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:407-409` describes compaction of `PROJECT_MEMORY.md` only.
- **Actual (Code):** `append_current_state`, `append_changelog` and `append_memory_section` each read the whole file and rewrite it — called 3–5 times per dispatch (`orchestrator.py:307-309`, `:336-338`, `:422-424`, `:297-299`, plus `create_checkpoint:1775`). `compact_memory` is the only compaction routine and targets `PROJECT_MEMORY.md` alone, so `CURRENT_STATE.md` and `CHANGELOG.md` grow without limit. Separately, `known_artifacts` (`:974-977`) is a monotonically growing list rewritten into `TASKS.yaml` on every dispatch and never pruned; task `notes` are not truncated (`state_manager.py:646`, contrast the 500-char `ERROR_SNIPPET_LENGTH` on `last_error` at `:649`) and are injected into every subsequent prompt.
- **Impact:** O(n²) I/O over a session plus unbounded growth in three files that no routine prunes.

- **Gap ID:** GAP-MED-10
- **Component / Path:** `orchestrator/orchestrator.py:1742-1777`, `:1787`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:404-409`, `:177-178` — "Resume: `resume_from_checkpoint()` restores files, reloads state, clears loop/oscillation history".
- **Actual (Code):** (a) `compact_context` performs seven separate durability events across four files in a fixed order with no rollback. (b) `create_checkpoint` calls `state.record_checkpoint` **before** `checkpoints.create_checkpoint`, so a failed snapshot leaves `PROJECT.yaml.last_checkpoint` pointing at a checkpoint that does not exist; and `CheckpointExistsError` is a `StateError`, so `_dispatch_safe:1574` converts a checkpoint-id collision into a **synthesized failed task result** that the CLI reports as a task failure with a `retry` hint (`cli.py:656-665`) — a misleading diagnosis. (c) `create_checkpoint` raises on directory existence (`:215-216`) but `compact_context:1746` checks only the *index*, so after a lost index write the next compaction collides. (d) `resume_from_checkpoint` sets `self.registered_agents = {}` (`:1787`) but not `_pinned_agents`, and **silently discards every agent registered via the documented `register_agent()`/`agent_resolver` pattern** — after a restore, a custom agent owner raises `MissingAgentError` (`:167-169`). The doc says nothing about this.
- **Impact:** A restore invalidates the documented custom-agent extension point, and checkpoint-id collisions are reported to the user as task failures.

- **Gap ID:** GAP-MED-11
- **Component / Path:** `orchestrator/orchestrator.py:1634-1671`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:134` — "run cycles until no progress (bounded by no-progress threshold + max_cycles)".
- **Actual (Code):** `_track_progress` compares `fingerprint()` (`:1510-1519`), which includes `attempt_count` and `no_progress_cycles`; `record_attempt` (`:237`, `supervisor.py:944-951`) increments `attempt_count` on **every** gate-passing dispatch, and `dispatch_review:1364` decrements `no_progress_cycles`. So any cycle in which a task actually ran always changes the fingerprint and `_idle_cycles` resets to 0; the threshold can only advance on a cycle where every dispatch was refused **pre-execution**. The bound therefore measures "consecutive all-refused cycles", not "no progress". Additionally `:1640` never forwards `max_tasks`, so the per-cycle bound is the hard-coded default 25 (`:1593`) with no way to change it; `:1646` calls `sync_project_health()` with no lock; and `:1638` reads the **global** `config.LOOP_THRESHOLDS`, so a `SupervisorAgent(thresholds=…)` override is silently ignored — the same global-vs-instance split affects `orchestrator.py:1468-1469` and `:1552-1554`, where synthesised loop diagnostics report `count == threshold == 3` by construction.
- **Impact:** The documented loop bound does not bound what it claims to; documented threshold overrides are ignored in three places.

- **Gap ID:** GAP-MED-12
- **Component / Path:** `orchestrator/state_manager.py:428-454`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:110-114` — "the first phase … that owns a non-terminal task wins; phases with no matching tasks are auto-skipped".
- **Actual (Code):** `PHASE_OWNERS[INTEGRATION]`, `[VALIDATION]`, `[RELEASE]` are **empty** (`config.py:418-421`) and are skipped at `:441-442`, so `derive_phase` can never return them — nor `MAINTENANCE` (manual-only, matching the doc). `documentation_agent` and `review_agent` are in **no** owner tuple, so they are folded into IMPLEMENTATION as `unknown_owners` (`:435-445`); `:453-454` then returns `PHASE_REQUIREMENTS` when only unknown-owner tasks remain — but that branch is unreachable dead code, since `:428-429` already returns RELEASE when everything is terminal, and any non-terminal unknown-owner task returned IMPLEMENTATION at `:452`. Net effect: a graph whose only remaining work is documentation/review **regresses the phase to REQUIREMENTS**, contradicting `meta/WORKFLOW.md:18` ("RELEASE — `documentation_agent` synchronizes all docs").
- **Impact:** Three lifecycle phases are permanently unreachable in the derived path (so `framework/00:205-227`'s diagram is partly fiction), and a documentation-only endgame silently rewinds the phase — which also drives `cp-phase-<name>` checkpoint naming.

- **Gap ID:** GAP-MED-13
- **Component / Path:** `orchestrator/agents/base_agent.py:207`
- **Expected (Doc):** `memory.md:57` — `_repair_json_candidate()` "repairs invalid backslash escapes (`docs\config.md`, broken `\uXXXX`) and bare control chars (raw newlines) inside strings — gemma4:12b emits trailing `\` line-continuations in markdown tables inside JSON."
- **Actual (Code):** `_INVALID_JSON_ESCAPE.sub(r"\\\\", candidate)` runs **before** the first `json.loads`, and the pattern `\\(?![\\"/bfnrt]|u[0-9a-fA-F]{4})` also matches the *second* backslash of a valid `\\` pair, doubling it. Demonstrated: `{"summary":"col1 | col2 \\| col3\nrow2","data":{}}` → `loads` fails on the control char (which the control-char pass handles correctly in isolation) → repair returns `None`. Character-level proof: `"a\\b"` becomes `"a\\\b"`, unparseable.
- **Impact:** Any reply containing a legitimately escaped backslash (Windows paths, regex, LaTeX, markdown tables) **plus** a raw newline becomes unrecoverable — the salvage mechanism is defeated by the exact model quirk it was written for.

- **Gap ID:** GAP-MED-14
- **Component / Path:** `orchestrator/agents/llm_agent.py:211-215`, `:247-252`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:215-216` — "Successful change sets are recorded in `data.edits_applied` and consumed so `run()` never re-applies them."
- **Actual (Code):** `_execute_edit_session` rebinds `output` per turn, so only the **last** turn's change set reaches `edits_applied`. Demonstrated: turn 1 edits `a.c`, turn 2 edits `b.c` only → the loop re-asks for `a.c` and burns turns; when the model re-touches `a.c` it passes but `edits_applied` reflects turn 2 only.
- **Impact:** Multi-turn sessions can loop on already-delivered files, and the recorded provenance of earlier patches is lost. The guide understates a per-turn record as a per-session one.

- **Gap ID:** GAP-MED-15
- **Component / Path:** `orchestrator/state_manager.py:498-502`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:242-243` — "`parallel_groups` pruned to live tasks … **Planning-only fields (durations, estimates, group metadata) are preserved**".
- **Actual (Code):** A group whose every member is gone is dropped wholesale, together with its `name`, `milestone`, `strategy` and other planning metadata. The "preserved" clause only survives for groups retaining ≥1 member.
- **Impact:** Planner-authored group metadata is destroyed as the graph drains — the opposite of what the doc promises.

- **Gap ID:** GAP-MED-16
- **Component / Path:** `orchestrator/cli.py:356`, `:442`, `:476-489`, `:685-687`, `:709-710`; `orchestrator/orchestrator.py:453-457`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:92` — `init … [--goal TEXT]` generates the task graph from the goal.
- **Actual (Code):** `cli.py:356` substitutes `goal = "TBD — define the one-sentence project goal."`, and `build_plan` is called at `:442` **before** `PROJECT_MEMORY.md` is written at `:476-489`. So `init` without `--goal` spends a full LLM planning call on the literal `TBD` string and persists a 3–8 task graph generated from it, while `build_plan`'s memory fallback (`:453-457`) can never fire from `cmd_init`. If the process dies between `:442` and `:476`, the project has a graph and no goal. Separately `cmd_plan` extracts and *prints* the `## Goal` section (`:685-687`) but passes the **raw** `args.goal` (`:709-710`); `build_plan`'s own fallback uses the **entire** `PROJECT_MEMORY.md` (`load_memory().strip()`), feeding the planner the goal plus Status/Decisions/Next-Steps boilerplate.
- **Impact:** A placeholder goal is silently materialised into a persisted task graph. (Mitigated in practice only by the G15 placeholder warning at `cmd_init`/`cmd_plan`, which warns but does not substitute.)

- **Gap ID:** GAP-MED-17
- **Component / Path:** `orchestrator/agents/specialists.py:206`, `:256-257`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:272-274` — the review verdict vocabulary is exactly `PASS` / `PASS WITH ACTIONS` / `FAIL`.
- **Actual (Code):** `candidates.append(output.status)` appends the agent's *own* completion status, and `_normalize_review_outcome("FAILED")` → `REVIEW_FAIL` (`:256-257`). A review reply that omits `review_status` and carries `"status": "failed"` gets `data["review_status"] = "FAIL"` written into the output at `:219`. Today's `dispatch_review` takes the `output.status != COMPLETED` branch (`orchestrator.py:1319-1330`) so no verdict lands — but the fabricated value is present in `output.data` and in any `to_dict()` serialization, and `output.status == "completed"` normalises to `None` only by accident.
- **Impact:** The G12 fallback chain includes the agent's completion status as a verdict candidate; the guard against it landing is incidental rather than designed, and the value persists in serialized output.

- **Gap ID:** GAP-MED-18
- **Component / Path:** `orchestrator/orchestrator.py:1614`, `:1618-1620`, `:1818`; `orchestrator/cli.py:593`; `orchestrator/config.py:185`, `:523`, `:341-343`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:298-311` — "READY tasks are collected in graph order, then dispatched on a thread pool (batch bounded by `--max-tasks`)".
- **Actual (Code):** (a) `--max-concurrent` has no upper clamp (`cli.py:593` `max(1, int(...))`) and is passed straight to `ThreadPoolExecutor(max_workers=max_concurrent)` (`:1614`); `--max-concurrent 5000` spawns 5000 OS threads, all futures submitted at once. (b) `:1618-1620` discards the `_raised` flag (`result, _raised = future.result()`), so in parallel mode a pre-execution refusal is indistinguishable from an execution failure. (c) `:1818` `sorted(self.registered_agents)` iterates the live dict while workers mutate it under the lock at `:156` — `RuntimeError: dictionary changed size during iteration` is reachable when `status()` runs concurrently with a parallel `run_cycle`. (d) Dead config in the same pattern as `review_gaps` C3: `LoopThresholds.max_retries` (`config.py:185`, serialized in `as_dict()`, read by **no** runtime code), `MAX_CONSECUTIVE_IDLE_CYCLES` (`:523`, 0 readers), `DEFAULT_LOGS_DIR`/`DEFAULT_AGENTS_DIR`/`DEFAULT_ORCHESTRATOR_DIR` (`:341-343`, 0 readers).
- **Impact:** Unbounded resource consumption from a documented flag; tunable-looking config that has no effect; a reachable concurrency crash in the status path.

- **Gap ID:** GAP-MED-19
- **Component / Path:** `orchestrator/cli.py:49-58`; `orchestrator/state_manager.py` (no `import logging`); `logs/README.md`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:429` — "`ORCHESTRATOR_LOG_LEVEL` … (`-v`/`-q` override)". `logs/README.md` — "Runtime logs (CLI runs, dispatch output)".
- **Actual (Code):** `logging.basicConfig(..., force=True)` configures **stderr only** — there is no `FileHandler` or `RotatingFileHandler` anywhere (`grep -rn "FileHandler|RotatingFile" orchestrator/` → 0 hits), so nothing has ever written to `logs/`; it contains only `README.md`. Modules with **zero** log statements despite being the interesting ones: `state_manager.py` (no `import logging` at all — every state mutation is invisible), `llm_client.py`, `context_monitor.py`, `prompt_builder.py`, `base_agent.py`, and `cli.py` itself (`logger` defined at `:46`, never used). There is **no telemetry of any kind** (`grep -rniE "telemetry|metric|prometheus|structlog|observab"` across `*.md`/`*.py` → 0 hits) — no machine-readable run record exists anywhere. An invalid level silently degrades: `getattr(logging, name.upper(), logging.INFO)`.
- **Impact:** The GUIDE's logging claim is true for exactly three modules (dispatch, supervisor, checkpoints). There is no audit trail of state mutations and no metrics surface — notable for a framework whose value proposition is auditability.

- **Gap ID:** GAP-MED-20
- **Component / Path:** `orchestrator/agents/base_agent.py:616-627`; `orchestrator/cli.py:656-658`; `orchestrator/state_manager.py:649`; `orchestrator/supervisor.py:863-871`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:204-205` — "invalid edits … fail the task with a **precise error** instead of silently delivering a stub".
- **Actual (Code):** `BaseAgent.run`'s blanket handler returns `errors=[str(exc), traceback.format_exc(limit=8)]`; `cmd_run` prints `result.output.errors[:3]`, `error_text = "; ".join(output.errors)` truncates it to 500 chars into `execution.last_error`, and `ensure_failure_risk(..., error_text)` persists it into `RISKS.md`. Upstream error bodies (up to 300 B) also flow into `last_error` and `RISKS.md` via `llm_client.py:108-109, 271-273, 365-367, 401-403`.
- **Impact:** Python stack frames and provider-controlled bytes are permanently recorded in the project's durable risk register and re-injected into later agents' prompts.

- **Gap ID:** GAP-MED-21
- **Component / Path:** `orchestrator/state_manager.py:1078-1081`, `:1239-1240` (correct) vs `:973-981`, `:1328-1336`; `orchestrator/supervisor.py:856-862`
- **Expected (Doc):** `ORCHESTRATOR_GUIDE.md:193-195` — `DECISIONS.md` and `RISKS.md` are structured registers the decision gate parses.
- **Actual (Code):** `append_decision`/`append_risk` correctly apply `one_line()` (`:1081-1082`, `:1239-1240`), so header forgery into the gate parser is **not** possible — verified clean. But `append_current_state` (`:973-981`) and `append_changelog` (`:1328-1336`) write their argument verbatim, and callers pass model- and CLI-controlled text (`orchestrator.py:293` `f"{task_id} rejected by Definition of Done: {error_text}"`; `cli.py:849-852`). Also `ensure_failure_risk` dedupes purely on the title regex `\bTask {id}\b`, so only the **first** failure's error is ever recorded; later distinct root causes for the same task are silently dropped.
- **Impact:** Impact is limited to misleading humans and agents in `CURRENT_STATE.md`/`CHANGELOG.md` (Low on its own, escalated here because these files feed later prompts), plus systematic loss of all but the first failure cause per task in `RISKS.md`.

---

### 4. Low Severity

- **Gap ID:** GAP-LOW-01
- **Component / Path:** `orchestrator/state_manager.py:93-97`, `:89`
- **Expected (Doc):** `state_manager.py:3-6` — "Every write goes through an atomic replace (write to a temporary file in the same directory, fsync, then `os.replace`) so **a crash can never leave a half-written** YAML or Markdown file behind."
- **Actual (Code):** `os.fsync(handle.fileno())` then `os.replace(tmp_path, path)` — no `os.fsync` on the parent directory fd. The `.tmp` prefix (`:89`) is also not excluded from `CheckpointManager._read_phase`/`_read_version` globs, so a crashed write leaves `.{name}.*.tmp` litter in the project root that nothing ever cleans up.
- **Impact:** On power loss the rename itself is not guaranteed durable, so a file may revert. The doc's absolute claim is not satisfied.

- **Gap ID:** GAP-LOW-02
- **Component / Path:** `orchestrator/orchestrator.py:109`, `:499`; `orchestrator/checkpoint_manager.py:173-174`; `orchestrator/state_manager.py:88-97`
- **Expected (Doc):** Implied by the state engine's contract.
- **Actual (Code):** `CheckpointManager.__init__` calls `mkdir(parents=True, exist_ok=True)` on two roots, so constructing `MasterOrchestrator(project)` — including from a read-only `status` call — creates directories. `cmd_init:499` constructs a *second* `MasterOrchestrator` for the same project. Separately, `tempfile.mkstemp` + `os.replace` means every state file is created mode `0600` (verified `-rw------- PROJECT.yaml`), an undocumented side effect.
- **Impact:** Read-only commands mutate the filesystem; a scaffolded project is unreadable to any other user/group (CI artifact sharing, web UI) with no documentation.

- **Gap ID:** GAP-LOW-03
- **Component / Path:** `.env` (mode 664); `CHECKPOINT_SIGNING_KEY` commented out in `.env` and `.env.example`
- **Expected (Doc):** `.env.example:19-21,26` — the file holds `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` / `CHECKPOINT_SIGNING_KEY`.
- **Actual (Code):** `stat -c '%a' .env` → 664 (group/world readable). `CHECKPOINT_SIGNING_KEY` is commented out in both files; the init log confirms `checkpoint 'cp-000-init' created (7 files, signed=False)`.
- **Impact:** On a shared host any local user can read the keys; and checkpoint signing — the control GAP-CRIT-03 depends on — is off in the shipped configuration. Gitignore is correct (`.env` untracked, only `.env.example` committed).

- **Gap ID:** GAP-LOW-04
- **Component / Path:** `.env:6`, `.env.example:7`; `ORCHESTRATOR_GUIDE.md:427`
- **Expected (Doc):** `OLLAMA_BASE_URL` — "Project default: `http://192.168.0.200:11434` (append `/v1` for OpenAI-compat)".
- **Actual (Code):** The shipped preset is **cleartext HTTP to a LAN host**. TLS verification is otherwise correct (`urllib.request.urlopen` default SSL context; no `CERT_NONE`, no `verify=False` anywhere), and API keys are never logged and DEBUG does not dump payloads — both verified clean.
- **Impact:** Prompts and any secrets in state files traverse the LAN unencrypted, with no risk note in the docs.

- **Gap ID:** GAP-LOW-05
- **Component / Path:** `ORCHESTRATOR_GUIDE.md:433-435`
- **Expected (Doc):** `from orchestrator.llm_client import LLMClient, is_available` then `print(is_available())`.
- **Actual (Code):** `is_available` is a `@staticmethod` on `LLMClient` (`llm_client.py:185`), not a module-level name. Verified: `ImportError: cannot import name 'is_available'`. `memory.md` and `HOW_TO_USE.md` use the correct `LLMClient.is_available()` form.
- **Impact:** The documented Python snippet does not run.

- **Gap ID:** GAP-LOW-06
- **Component / Path:** `ORCHESTRATOR_GUIDE.md:44`, `:515`; `orchestrator/agents/specialists.py`; `orchestrator/prompt_builder.py:26-39`
- **Expected (Doc):** "`orchestrator/agents/` — `BaseAgent`, `LLMAgent`, **9 specialists + `ReviewAgent`**"; and the 12-file FILE LOCATIONS tree.
- **Actual (Code):** 9 agent classes exist (8 in `specialists.py`, which *includes* `ReviewAgent`, plus `RequirementsAgent`), yielding 10 registry ids (`firmware_agent` is a second `@register_agent` on `SoftwareAgent`, `specialists.py:125-126`). "9 specialists + ReviewAgent" implies 10 classes. `config.py` and the four `agents/*.py` files are absent from the file-locations tree. Separately, `prompt_builder.py:26-39` duplicates the agent-id list already encoded in the `@register_agent` decorators, and `PlanningAgent.SYSTEM_RULES:82-84` hardcodes all 10 ids a third time — adding an agent requires editing three unrelated places with nothing enforcing agreement.
- **Impact:** Cosmetic miscount in the primary reference, plus a three-place id duplication with no consistency check.

- **Gap ID:** GAP-LOW-07
- **Component / Path:** `ORCHESTRATOR_GUIDE.md:444-450`; `orchestrator/checkpoint_manager.py:165-167`
- **Expected (Doc):** `checkpoints/index.json` and `checkpoints/cp-001-phase1/`.
- **Actual (Code):** `CheckpointManager.__init__` inserts a project-name level: `checkpoints/<project>/index.json`. On disk: `checkpoints/esp32-os-gap2/...`.
- **Impact:** The documented tree omits a directory level, so the tree in the guide cannot be used to locate a checkpoint manually.

- **Gap ID:** GAP-LOW-08
- **Component / Path:** `ORCHESTRATOR_GUIDE.md:196`; `orchestrator/agents/base_agent.py:800`
- **Expected (Doc):** "`docs/` | Materialized agent artifacts (`REQUIREMENTS-*.md`, `REVIEW-*.md`, …)".
- **Actual (Code):** Materialization uses `Path(name).name`, so the name is the expected output's basename. Only `REVIEW-<task_id>.md` (`orchestrator.py:1394`) is code-suffixed; no path ever emits `REQUIREMENTS-<something>.md`.
- **Impact:** An invented naming convention in the primary reference.

- **Gap ID:** GAP-LOW-09
- **Component / Path:** `meta/TROUBLESHOOTING.md:8`; `agents/supervisor.md:22-23`; `framework/AGENT_PROMPTS/01_SUPERVISOR.md:6`; `framework/00:75-78`; `README.md:203-209`; `README.md:104-126`; `meta/GETTING_STARTED.md:69`, `:107`
- **Expected (Doc):** Loop kinds and context bands as guidance for operators.
- **Actual (Code):** Three documents list **5** loop kinds; `config.LOOP_KINDS` has **6** (all three omit `no_new_evidence`) — only `ORCHESTRATOR_GUIDE.md:24-25` and `HOW_TO_USE.md:230-237` are correct. Context bands disagree three ways: `framework/00:75-78` (4 bands, skips 80–85), `README.md:203-209` (invents a "Failure Risk" state that exists in no code path), `config.py:245-254` (5 bands incl. `COMPACT_HIGH` — the only correct one). `README.md:104-126` and `meta/GETTING_STARTED.md:69`, `:107` list `IDEA` as a lifecycle phase; `config.PHASES` starts at `REQUIREMENTS`.
- **Impact:** Operator guidance disagrees with the runtime in three independent places; the phantom `IDEA` phase and `Failure Risk` state have no code.

- **Gap ID:** GAP-LOW-10
- **Component / Path:** `HOW_TO_USE.md:80-81`, `:153`; `review_gaps.md:76`, `:437`, `:444`; `memory.md:36`
- **Expected (Doc):** "The Definition of Done resolves `expected_outputs` under `docs/` **only**" (`HOW_TO_USE.md:80-81`); "planner/reviewer `proposed_tasks` output is ingested" (`:153`).
- **Actual (Code):** Stale since G22 — `delivery_problems()` (`base_agent.py:97-197`) additionally rejects delivery when the real project file is missing. And no code path reads a `proposed_tasks` key (`grep -rn proposed_tasks orchestrator/` → 0 hits); `_ingest_output_tasks` reads **`data.tasks`** (`orchestrator.py:405`). The same key error appears in three files against one code site, with `memory.md` the probable origin. Test counts: `review_gaps.md:437,444` and `memory.md:9,13` claim 314; the truth is **401** (verified), with `test_auto_plan.py` at 46, not 39. Only `HOW_TO_USE.md:409` is correct.
- **Impact:** HOW_TO_USE is the best-maintained doc yet carries two contract errors, and the count divergence that `memory.md` propagates is the reason three files disagree. `test_docs_consistency.py` asserts no test count, no version string, no status table, and no artifact filename — four assertions would have prevented all of it.

- **Gap ID:** GAP-LOW-11
- **Component / Path:** `agents/specialists/README.md:44`; `framework/TEMPLATES/REQUIREMENTS.md:14`, `:9`; `framework/TEMPLATES/ARCHITECTURE.md:1-22`; `framework/TEMPLATES/REVIEW.md:1`; `framework/TEMPLATES/README.md:6-13`
- **Expected (Doc):** Template set aligned with the agents and artifacts the runtime produces.
- **Actual (Code):** (a) "**All** specialists subclass `LLMAgent`" is false — `RequirementsAgent(BaseAgent)` is deterministic, as the same table's first row implies. (b) `TEMPLATES/REQUIREMENTS.md:14` documents `**Status:** DRAFT | APPROVED`; `config.REQUIREMENT_STATUSES` is `(APPROVED, PROPOSED, REJECTED)` — `DRAFT` is invalid anywhere and `REJECTED` is missing, and `RequirementsAgent.validate_requirements:207-208` warns on unknown status, so a user following the template gets a spurious warning and cannot express rejection. The template also omits `Type:`, which `framework/02:21` requires and `requirements_agent.py:203-204` validates. (c) `TEMPLATES/ARCHITECTURE.md` delivers 7 sections where `framework/04:16-31` mandates **16** — and `NEW_PROJECT_CHECKLIST.md:44` points at it as the 16-section template. (d) `TEMPLATES/REVIEW.md:1` is `# REVIEW — <task id>` while the runtime always writes `docs/REVIEW-<task_id>.md`; three docs, three names for one artifact (`REVIEW_REPORT.md`, `# REVIEW —`, `REVIEW-<task>.md`). (e) Three of the five artifacts the starter skeleton declares have no template at all (`IMPLEMENTATION.md`, `TEST_REPORT.md`, `RELEASE_NOTES.md` — `state_manager.py:839,850,859`), and `hardware_agent` — which defines a 14-field BOM inline — is omitted from the template mapping entirely.
- **Impact:** The templates a user copies carry a status vocabulary the code rejects, omit a validated field, under-deliver half the mandated architecture sections, and cover 3 of the 5 artifacts a fresh `init` produces.

- **Gap ID:** GAP-LOW-12
- **Component / Path:** `meta/GETTING_STARTED.md:160-167`, `:151-153`, `:40-57`, `:179-189`, `:273-275`; `meta/WORKFLOW.md:9`, `:18`; `contributing.md` / `CONTRIBUTING.md:44`
- **Expected (Doc):** A secondary onboarding path for users who do not use `init`.
- **Actual (Code):** (a) `:160-167` instructs `mkdir checkpoints/… && cp <files> && git add && git commit` — a layout `CheckpointManager` cannot read (wrong depth, no `index.json`, no `metadata.json`, no checksum), so following it yields checkpoints invisible to `checkpoint list|restore`. (b) `:151-153` says a checkpoint is "PROJECT.yaml + … + key artifacts"; `create_checkpoint` snapshots all 7 state files and **not** `docs/`. (c) `:40-57` teaches copying the 7 templates as *the* setup path, which — per GAP-HIGH-17 — produces a project that cannot pass DoD (no `docs/`) and diverges from `init`'s schema. (d) `:179-189` advertises `projects/kid-robot-face/` as "a real, working example … ✓ Test plan … ✓ Checkpoint at each phase" — it has no `docs/`, 4 tasks (not 20), and no per-phase checkpoints. (e) `:273-275` still says "Framework Status: Production Ready", predating the v2.0 runtime. (f) `meta/WORKFLOW.md:9` names the phase-1 checkpoint `cp-requirements`; the code emits `cp-phase-requirements`. `CONTRIBUTING.md` never mentions running the 401-test suite or `bin/orchestrator`.
- **Impact:** The alternate onboarding path produces projects and checkpoints the runtime cannot use, and contradicts `HOW_TO_USE.md` on the primary path.

- **Gap ID:** GAP-LOW-13
- **Component / Path:** `orchestrator/state_manager.py:84-104` (no transaction); `orchestrator/orchestrator.py:436-451`, `:1233-1256`; `orchestrator/checkpoint_manager.py:437-449`, `:451-495`; `orchestrator/config.py:328-335`; `orchestrator/llm_client.py:253`; `orchestrator/orchestrator.py:1779-1790`, `:130-138`; `orchestrator/state_manager.py:1274-1314`
- **Expected (Doc):** — (undocumented capabilities).
- **Actual (Code):** `ORCHESTRATOR_CHECKPOINTS_DIR` (`config.py:328-335`) is absent from the GUIDE env table, `HOW_TO_USE.md` §8 and `.env.example`; `memory.md:49` is currently its only documentation. `ANTHROPIC_BASE_URL` (`llm_client.py:253`) appears in **no** doc. `resume_from_checkpoint(id, isolated=True)` — a restore-to-scratch-dir safety valve — is undocumented and would be the natural mitigation for several Criticals. `create_backup()`/`list_backups()`, `delete_checkpoint()` (the `init --force` idempotency mechanism, `cli.py:503`, destructive), `update_risk_status(..., "REALIZED")`, `register_agent_class()`, `compact_memory(max_chars)` and `REPAIR_NOTE`/`DOD_REPAIR_NOTE` are all undocumented. Conversely `ORCHESTRATOR_GUIDE.md:196` and `.env.example` describe `requirements.txt` as a single source while `pyproject.toml` declares dependencies independently.
- **Impact:** Several of the framework's most safety-relevant affordances — isolated restore, backup, truncation repair — are discoverable only by reading source.

- **Gap ID:** GAP-LOW-14
- **Component / Path:** `orchestrator/config.py:144-158`; `orchestrator/state_manager.py:645-650`
- **Expected (Doc):** — (hygiene).
- **Actual (Code):** `load_env_file` is a naive parser: no inline-comment stripping (`KEY=v # x` yields the literal `v # x`), no multiline values, no key-name validation. Task `notes` are stored untruncated while `execution.last_error` is capped at `ERROR_SNIPPET_LENGTH = 500`, and notes are injected into every subsequent prompt as `task_notes` (`base_agent.py:398-400`) — an unbounded, model-influenced, per-prompt payload.
- **Impact:** Minor parsing surprises and unbounded prompt growth over long task histories.

---

## Recommended Resolution Priority Matrix

| Gap ID | Severity | Category | Target File(s) | Remediation Complexity |
| :--- | :--- | :--- | :--- | :--- |
| GAP-CRIT-01 | Critical | Design / NFR | `orchestrator/orchestrator.py`, `orchestrator/agents/specialists.py`, `orchestrator/agents/base_agent.py` | High |
| GAP-CRIT-02 | Critical | Security | `orchestrator/checkpoint_manager.py` | Low |
| GAP-CRIT-03 | Critical | Security | `orchestrator/checkpoint_manager.py` | Low |
| GAP-CRIT-04 | Critical | Design | `orchestrator/orchestrator.py`, `orchestrator/agents/base_agent.py` | Med |
| GAP-CRIT-05 | Critical | Security / Design | `orchestrator/orchestrator.py`, `orchestrator/state_manager.py` | Low |
| GAP-CRIT-06 | Critical | Design / Data-Loss | `orchestrator/orchestrator.py`, `orchestrator/state_manager.py`, `orchestrator/checkpoint_manager.py` | High |
| GAP-CRIT-07 | Critical | Security | `orchestrator/config.py`, `orchestrator/cli.py` | Med |
| GAP-CRIT-08 | Critical | Design | `orchestrator/agents/base_agent.py`, `orchestrator/agents/specialists.py`, `orchestrator/agents/requirements_agent.py` | Low |
| GAP-CRIT-09 | Critical | Design / Docs | `framework/AGENT_PROMPTS/*` (11 files), `orchestrator/agents/base_agent.py` | Med |
| GAP-HIGH-01 | High | Design / NFR | `orchestrator/orchestrator.py`, `orchestrator/agents/base_agent.py` | Med |
| GAP-HIGH-02 | High | Design | `orchestrator/orchestrator.py` | Low |
| GAP-HIGH-03 | High | NFR / Design | `orchestrator/orchestrator.py`, `orchestrator/agents/llm_agent.py` | Med |
| GAP-HIGH-04 | High | Design / NFR | `orchestrator/state_manager.py`, `orchestrator/cli.py` | Med |
| GAP-HIGH-05 | High | Design | `orchestrator/orchestrator.py`, `orchestrator/state_manager.py` | Low |
| GAP-HIGH-06 | High | Design | `orchestrator/orchestrator.py` | Med |
| GAP-HIGH-07 | High | Design | `orchestrator/agents/llm_agent.py`, `orchestrator/agents/base_agent.py` | Low |
| GAP-HIGH-08 | High | Design | `orchestrator/agents/base_agent.py` | Med |
| GAP-HIGH-09 | High | Security / Design | `orchestrator/agents/base_agent.py`, `orchestrator/state_manager.py` | Med |
| GAP-HIGH-10 | High | Design | `orchestrator/agents/requirements_agent.py`, `orchestrator/config.py` | High |
| GAP-HIGH-11 | High | Design | `orchestrator/orchestrator.py`, `orchestrator/config.py` | Low |
| GAP-HIGH-12 | High | NFR | `orchestrator/state_manager.py` | High |
| GAP-HIGH-13 | High | Docs | `framework/AGENT_PROMPTS/*`, `framework/00`, `framework/05`, `framework/09` | Med |
| GAP-HIGH-14 | High | Docs | `README.md`, `IMPLEMENTATION_ROADMAP.md`, `DEPLOYMENT_SUMMARY.md`, `QUICK_REFERENCE.md`, `CONTRIBUTING.md`, `GAP_ANALYSIS.md` | Low |
| GAP-HIGH-15 | High | Design | `orchestrator/orchestrator.py`, `orchestrator/state_manager.py` | Med |
| GAP-HIGH-16 | High | NFR | `conftest.py`, `orchestrator/config.py` | Low |
| GAP-HIGH-17 | High | Design / Docs | `project-templates/*`, `orchestrator/cli.py` | Med |
| GAP-HIGH-18 | High | Docs | `memory.md`, `review_gaps.md`, `HOW_TO_USE.md` | Low |
| GAP-MED-01 | Medium | NFR | `orchestrator/cli.py`, `orchestrator/llm_client.py` | Low |
| GAP-MED-02 | Medium | Design | `orchestrator/cli.py` | Low |
| GAP-MED-03 | Medium | Design | `orchestrator/state_manager.py` | Low |
| GAP-MED-04 | Medium | Design | `orchestrator/orchestrator.py` | Low |
| GAP-MED-05 | Medium | Design | `orchestrator/agents/base_agent.py`, `orchestrator/state_manager.py` | Med |
| GAP-MED-06 | Medium | Design | `orchestrator/orchestrator.py`, `orchestrator/state_manager.py` | Med |
| GAP-MED-07 | Medium | NFR / Security | `orchestrator/llm_client.py` | Med |
| GAP-MED-08 | Medium | NFR | `orchestrator/supervisor.py`, `orchestrator/state_manager.py` | Low |
| GAP-MED-09 | Medium | NFR | `orchestrator/state_manager.py`, `orchestrator/orchestrator.py` | Med |
| GAP-MED-10 | Medium | Design | `orchestrator/orchestrator.py`, `orchestrator/checkpoint_manager.py` | Med |
| GAP-MED-11 | Medium | Design | `orchestrator/orchestrator.py` | Med |
| GAP-MED-12 | Medium | Design | `orchestrator/state_manager.py`, `orchestrator/config.py` | Med |
| GAP-MED-13 | Medium | Design | `orchestrator/agents/base_agent.py` | Low |
| GAP-MED-14 | Medium | Design | `orchestrator/agents/llm_agent.py` | Med |
| GAP-MED-15 | Medium | Design | `orchestrator/state_manager.py` | Low |
| GAP-MED-16 | Medium | Design | `orchestrator/cli.py`, `orchestrator/orchestrator.py` | Low |
| GAP-MED-17 | Medium | Design | `orchestrator/agents/specialists.py` | Low |
| GAP-MED-18 | Medium | NFR | `orchestrator/cli.py`, `orchestrator/orchestrator.py`, `orchestrator/config.py` | Low |
| GAP-MED-19 | Medium | NFR | `orchestrator/cli.py`, `orchestrator/state_manager.py`, `logs/README.md` | Med |
| GAP-MED-20 | Medium | NFR | `orchestrator/agents/base_agent.py`, `orchestrator/cli.py`, `orchestrator/supervisor.py` | Low |
| GAP-MED-21 | Medium | Design | `orchestrator/state_manager.py`, `orchestrator/supervisor.py` | Low |
| GAP-LOW-01 | Low | Design | `orchestrator/state_manager.py` | Low |
| GAP-LOW-02 | Low | Design | `orchestrator/checkpoint_manager.py`, `orchestrator/state_manager.py`, `orchestrator/cli.py` | Low |
| GAP-LOW-03 | Low | Security | `.env`, `.env.example` | Low |
| GAP-LOW-04 | Low | Security | `.env`, `.env.example`, `ORCHESTRATOR_GUIDE.md` | Low |
| GAP-LOW-05 | Low | Docs | `ORCHESTRATOR_GUIDE.md` | Low |
| GAP-LOW-06 | Low | Docs | `ORCHESTRATOR_GUIDE.md`, `orchestrator/prompt_builder.py` | Low |
| GAP-LOW-07 | Low | Docs | `ORCHESTRATOR_GUIDE.md`, `orchestrator/checkpoint_manager.py` | Low |
| GAP-LOW-08 | Low | Docs | `ORCHESTRATOR_GUIDE.md` | Low |
| GAP-LOW-09 | Low | Docs | `meta/TROUBLESHOOTING.md`, `agents/supervisor.md`, `framework/AGENT_PROMPTS/01`, `framework/00`, `README.md` | Low |
| GAP-LOW-10 | Low | Docs | `HOW_TO_USE.md`, `review_gaps.md`, `memory.md` | Low |
| GAP-LOW-11 | Low | Docs | `framework/TEMPLATES/*`, `agents/specialists/README.md` | Med |
| GAP-LOW-12 | Low | Docs | `meta/GETTING_STARTED.md`, `meta/WORKFLOW.md`, `CONTRIBUTING.md` | Low |
| GAP-LOW-13 | Low | Docs | `ORCHESTRATOR_GUIDE.md`, `.env.example`, `pyproject.toml` | Low |
| GAP-LOW-14 | Low | Design | `orchestrator/config.py`, `orchestrator/state_manager.py` | Low |

### Suggested sequencing

1. **Security sweep (Low complexity, immediate):** GAP-CRIT-02, GAP-CRIT-03, GAP-CRIT-05, GAP-HIGH-16, GAP-LOW-03. Five small, independent edits; three close arbitrary-write / integrity-bypass / self-approval holes and one closes real egress from the test suite.
2. **Trust loop (High complexity, the real work):** GAP-CRIT-01 (`data.deploy`) plus GAP-CRIT-04, GAP-CRIT-08, GAP-HIGH-06, GAP-HIGH-10 — the review path, the base `SYSTEM_RULES`, the acceptance-evidence gate and the requirements author. Sequenced after step 1 because the edits overlap in `orchestrator.py` and `base_agent.py`.
3. **Durability (High complexity):** GAP-CRIT-06 (cross-process locking + `build_plan` ordering), GAP-HIGH-08, GAP-MED-10. Fixes data loss, which currently has no mechanism behind it but an operational workaround (`memory.md:62`).
4. **Contract convergence (Med/Low, cheapest per unit of risk removed):** GAP-CRIT-09 + GAP-HIGH-13 together — regenerate the 11 prompt specs from `system_rules()` so the contract exists in exactly one place. Then GAP-HIGH-14, GAP-HIGH-18, GAP-LOW-10, GAP-LOW-12 (docs), and GAP-HIGH-17 (templates).
5. **Guards (Low complexity, prevents regression of everything above):** add four assertions to `test_docs_consistency.py` (version string, test count, status table, artifact filenames) and an autouse network-blocking fixture. Both are the highest leverage-per-line changes in the repository.

---

*Report generated 2026-10-01. No fix code was written, in accordance with the audit brief. Findings were verified against the working tree at `29b920c`; 62 findings across 4 severities (9 Critical, 18 High, 21 Medium, 14 Low), each reproduced by direct code inspection or an executed probe unless marked as read-only analysis.*
