# Review Gaps — Implementation vs. Architecture Documents

**Date:** 2026-09-30
**Baseline:** commit `7079012` (post remediation T1–T20), 234 tests passing
**Architecture sources reviewed:** `framework/00_MASTER_ORCHESTRATOR.md`,
`framework/01_SUPERVISOR_AGENT.md`, `framework/05_PLANNING_AGENT.md`,
`README.md` (Core Principles / Default Lifecycle / Definition of Done / State Files),
`meta/GETTING_STARTED.md`, `IMPLEMENTATION_ROADMAP.md`
**Method:** claim-by-claim comparison; every gap below was re-verified against the
current code with `file:line` evidence.

> This review supersedes the *findings* of `GAP_ANALYSIS.md` (written before
> remediation; its verdict "30% implemented" is now historical). See §F for what
> was closed since then.

> **Remediation pass (2026-09-30, this file):** every gap A1–A8, B1–B7, C1–C3 and
> E1–E2 has been implemented and covered by new tests in `test_gap_remediation.py`
> (41 tests; full suite green). Each section below carries a **Status** line with
> what landed. Remaining nuances: A3 "unnecessary blocking" is approximated by the
> stale-block detector, A7 integration/release triggers ride the phase checkpoint,
> B5 diagnosis is on-demand (`health --diagnose`) rather than automatic.

---

## Summary

| Severity | Missing entirely | Partial / divergent | Total | Status |
|---|---|---|---|---|
| High | 2 | 3 | 5 | ✅ all 5 closed |
| Medium | 5 | 4 | 9 | ✅ 8 closed, A3 partial |
| Low / hygiene | 3 | 3 | 6 | ✅ all 6 closed |

---

## A. Missing — required by the architecture, absent in code

### A1. No lifecycle state machine (High)

**Spec:** `framework/00_MASTER_ORCHESTRATOR.md:203-229` and `README.md:104-129`
document the IDEA → REQUIREMENTS → … → RELEASE → MAINTENANCE lifecycle;
`PROJECT.yaml` carries `phase.current` / `project.status`.

**Reality:** `StateManager.set_phase()` (`orchestrator/state_manager.py:235`) has
**zero callers**. Nothing ever advances `phase.current` — projects stay
`REQUIREMENTS` forever unless a human hand-edits the YAML. There is no RELEASE or
MAINTENANCE concept anywhere in the runtime.

**Recommendation:** derive phase transitions in `refresh_ready_states()`
(e.g. all requirements DONE → ARCHITECTURE), expose `orchestrator phase set|show`,
and define terminal-phase behaviour (release checkpoint).

**Status:** ✅ closed — `config.PHASES`/`PHASE_OWNERS`/`phase_index()`;
`StateManager.derive_phase()` (requirements/tasks terminal → phase);
forward-only advance inside `recompute_derived_state()` mirrors into
`project.status`; unknown/empty owners auto-skip or gate into IMPLEMENTATION;
CLI `phase show|set`; terminal RELEASE writes `cp-phase-RELEASE`. MAINTENANCE
stays a human-set terminal state.

### A2. Goal → task-graph conversion not automated (High)

**Spec:** `00:10` "Convert goals into requirements, milestones, tasks,
dependencies, and acceptance criteria"; `00:35-48` init steps 4-8.

**Reality:** there is **no task-creation API** in `StateManager` (grep
`add_task|create_task|append_task` → nothing). The only programmatic task creation
is review follow-ups (`orchestrator/orchestrator.py:700 _create_followup_tasks`).
The initial graph is hand-written YAML; `planning_agent` output is never ingested
into `TASKS.yaml`.

**Recommendation:** `StateManager.append_task()` + a dispatch hook that converts
planner/reviewer structured output into dependency-aware tasks (id allocation
already exists in `_create_followup_tasks`).

**Status:** ✅ closed — `StateManager.append_task()` (auto `TASK-NNN`, owner/deps/
status validation) + `_next_task_id()`; dispatch phase 3 `_ingest_output_tasks`
converts planner/reviewer `proposed_tasks` into dependency-aware tasks (STATE
errors degrade to warnings); `relax_dependency()` also lands here (see A6).

### A3. Supervisor detections from its spec are absent (Medium)

**Spec:** `01_SUPERVISOR_AGENT.md:8-10` — "Detect **contradictory decisions** or
agents silently changing approved architecture" and "Detect **blocked tasks that
unnecessarily stop unrelated work**".

**Reality:** `grep -rn "contradict|unnecessary" orchestrator/` → 0 hits.
The decision *gate* prevents new unsanctioned proposals
(`_enforce_decision_gate`), but nothing ever re-reads DECISIONS.md to find
mutually contradictory approved entries, and no detector flags a blocker that is
wider than its dependency closure.

**Recommendation:** supervisor pass over decision pairs (shared task ids /
conflicting `affected_tasks`) and a check `blocked_by ⊄ task.dependencies`.

**Status:** 🔶 mostly closed — `SupervisorAgent.detect_decision_conflicts()`
(flags mutually contradictory decisions incl. same-task proposals) and
`detect_stale_blocks()` (blockers whose gate can no longer explain the block)
both feed `HealthReport.decision_conflicts` / `stale_blocks` with their own
render sections and advisory escalations. The narrower "blocker wider than the
dependency closure" heuristic is *approximated* by the stale-block detector
rather than implemented as `blocked_by ⊄ task.dependencies`.

### A4. Loop kind "repeated searches with no new evidence" missing (Medium)

**Spec:** `00:100`.

**Reality:** `LOOP_KINDS` (`orchestrator/config.py:399-410`) has 5 kinds —
`same_strategy`, `no_progress`, `alternatives_exhausted`, `state_oscillation`,
`repeated_output` — but no evidence/search tracking exists (grep `evidence` in
`supervisor.py` matches only DFS wording and one regex).

**Recommendation:** count agent runs whose `data` contains no new artifact ids or
file hashes; emit the kind when repeats ≥ threshold.

**Status:** ✅ closed — `LOOP_KIND_NO_NEW_EVIDENCE` added to `config.LOOP_KINDS`;
`LoopThresholds.evidence_stall_max=3`; `_record_loop_signals()` tracks
`known_artifacts` / `evidence_stall_count` per task (evidence = fresh artifacts,
`data.documents`, or `data.proposed_change`) and resets the counter whenever new
evidence appears.

### A5. Automated loop response steps 3-5 not implemented (Medium)

**Spec:** `00:109-118` / `01:28-34` — on loop detection: *summarize repeated
behavior → diagnose likely root cause → select a materially different strategy*
(and `01` health state **RECOVERY** = "Executing recovery strategy").

**Reality:** detection counts and reports; failures produce a generic
`RISK-NNN` (`ensure_failure_risk`). No root-cause field is written back to the
task, no recovery strategy is selected or executed, and `HealthState.RECOVERY`
is never returned (see C2). Resolution depends on a human/LLM re-planning
outside the runtime.

**Recommendation:** on loop detection, persist `execution.last_error` +
`strategy` fingerprint on the task, require a changed `strategy` value before
the next attempt, and enter RECOVERY while that attempt runs.

**Status:** ✅ closed — `execution.attempts_since_change` resets only on
`strategy_changed`; `same_strategy` detection uses it (legacy fallback to
`attempt_count`); a `recovering` flag is set on strategy change, cleared on
terminal status or when `same_strategy_max_attempts` is exhausted again;
`classify_state(..., recovering=True)` returns `RECOVERY` ahead of other states;
`execution.last_loop` persists the latest `LoopLimitExceededError` at every
raise site. Root-cause *diagnosis* is rules-based here; LLM diagnosis is B5.

### A6. Deadlock detection without response (Medium)

**Spec:** `01:36-40` Deadlock Response — break the cycle by creating a
prerequisite, splitting a task, relaxing a nonessential dependency, or
escalating; keep unrelated ready tasks running.

**Reality:** cycles are detected (`supervisor.py:327`, surfaced via
`report.deadlocks` → health `BLOCKED`) but **no break action exists** — no
dependency rewrite API, no split, no escalation from the deadlock path.

**Recommendation:** minimum viable: auto-escalate (`human_decisions` entry with
the cycle) + CLI `tasks --break-cycle` that drops the lowest-priority edge after
confirmation.

**Status:** ✅ closed — `StateManager.relax_dependency(task_id, dep_id, reason)`
drops a nonessential edge (validates ids, rejects terminal/unknown pairs) and
refreshes ready states; CLI `waive TASK --dep ID [--reason]` exposes it; deadlock
escalations are category-tagged (`blocking`, see B6).

### A7. Checkpoint triggers incomplete (Medium)

**Spec:** `00:172-181` (7 triggers) incl. *before risky changes, after
integration tests, before release*, and `00:48` init step 10 *save the initial
checkpoint*; `01:11` *trigger checkpoints before recovery or risky changes*.

**Reality:** auto-checkpoint exists only after dispatch/review
(`orchestrator.py:307,670`), on milestone tasks (T12), and on compaction
(`:1027`). Missing: risky-change trigger (no link from RISKS.md/proposed changes
to checkpoints), integration-test/release triggers (no such phases exist, cf. A1),
and **`orchestrator init` creates no checkpoint** (`cmd_init`, `cli.py:213-330`).

**Recommendation:** checkpoint before `_enforce_decision_gate` releases a
`PROPOSED_CHANGE`, in `cmd_init`, and on `phase → RELEASE`.

**Status:** ✅ closed — `cmd_init` writes `cp-000-init` (non-fatal on failure);
failure risk creation gates on `auto_checkpoint` and writes `cp-risk-<id>`;
dispatch/review phase-advance writes `cp-phase-<name>` (checkpoint-id priority:
milestones → phase; the fallback chain evaluates *all* triggers, so milestone
checkpoints are never short-circuited). Integration/release triggers now ride
the RELEASE phase checkpoint from A1. (The pre-`PROPOSED_CHANGE` checkpoint was
deliberately replaced by the failure-risk trigger — `cp-risk-*` is written where
the risk actually materialises.)

### A8. Init does not capture constraints / budget / resources (Low)

**Spec:** `00:41` "Capture the goal, constraints, budget, available resources,
and desired result."

**Reality:** `cmd_init` writes goal only; `PROJECT.yaml` scaffold
(`cli.py:236-260`) has no `constraints` / `budget` / `resources` sections
(grep across `orchestrator/` + `project-templates/` → none).

**Recommendation:** add optional `project.constraints/budget/resources` blocks to
the scaffold + `--constraints` flag; keep them optional for `validate()`.

**Status:** ✅ closed — `cmd_init` writes empty `constraints`/`budget`/`resources`
blocks into the `PROJECT.yaml` scaffold; `validate()` treats them as optional.

---

## B. Partial / divergent semantics

### B1. Compaction does not compact (High)

**Spec:** `00:80-90` — at threshold: summarize completed work, capture decisions
and unresolved issues, update PROJECT_MEMORY.md, update CURRENT_STATE.md,
checkpoint, **discard irrelevant working context**, continue.

**Reality:** `compact_context()` (`orchestrator.py:1033-1055`) checkpoints,
appends a *generic boilerplate note* to memory/CURRENT_STATE/CHANGELOG. It never
summarizes actual completed work, never captures decisions/unresolved issues,
and **never removes anything** — memory only grows (the note itself adds bytes),
so utilization is never reduced by "compaction".

**Recommendation:** implement memory summarization (drop stale sections beyond N
entries, fold them into a `## Compacted summary` block) and reset/decay the
utilization value after a successful compaction.

**Status:** ✅ closed — `StateManager.compact_memory(max_chars=MEMORY_COMPACT_MAX_CHARS)`
folds MEMORY.md to head 60% + tail 25% with a `<!-- compacted ... -->` marker
recording dropped sections; `compact_context()` calls it *after* appending the
compaction note (so the note survives in the tail) and then
`reset_context_tokens()` (utilization 0, `compaction_required` cleared).

### B2. Context-pressure model diverges from spec thresholds (High)

**Spec:** `00:63-78` / `01:16` — Healthy <60, Warning 60-70, Compact 70-80,
Critical >85 (implying growing conversational pressure).

**Reality:** utilization = **single-call** usage ÷ window
(`context_monitor.py:55-72`; sole writer `orchestrator.py:407`), not cumulative
session growth — agents are stateless per task. With the default 128k window and
the 24k-char memory cap (`prompt_builder.py:46`), one prompt is ~≤10% of the
window, so the **70/85 thresholds are unreachable in practice** and the whole
compaction path stays dormant unless `ORCHESTRATOR_CONTEXT_WINDOW_TOKENS` is
tuned down.

**Recommendation:** document the per-call semantic honestly *or* track
accumulated prompt tokens across a dispatch cycle; additionally consider a much
smaller default window for local models.

**Status:** ✅ closed — utilization is now **cumulative**:
`_update_context_utilization` accumulates `cumulative_tokens` across dispatches
instead of overwriting; `add_context_tokens()`/`reset_context_tokens()` expose
the same accounting to API callers; `set_context_utilization()` derives
`cumulative_tokens = window × pct / 100` so the two stay in sync; compaction
(B1) resets the counter, which is what makes the 70/85 thresholds reachable
within a session. `context_monitor.tokens_for_output()` added for exact
token→percent math.

### B3. Definition of Done is structural, not semantic (High)

**Spec:** `README.md:259-266` and `00:163-170` — all 7 criteria: requirement
exists · implementation exists · testing/validation exists · **acceptance criteria
pass** · independent review passes · documentation updated · state updated.

**Reality:** `definition_of_done()` (`orchestrator.py:490-520`) checks:
- acceptance criteria **exist** (not that they *pass*) → criterion 4 unverified
- `expected_outputs` present under `docs/` → covers 2/3/5 only by file presence
- review `PASS` → criterion 5 ✓
- **criterion 1 ("requirement exists") is never checked** — no REQ-id traceability
  gate between the task and `docs/REQUIREMENTS.md` (the requirements agent even
  builds a traceability map, `requirements_agent.py:229`, that DoD never consults)

The docstring admits this ("verified without an LLM"), but README promises all 7.

**Recommendation:** wire the traceability map into DoD (`requirements` field per
task → must resolve to an REQ entry) and treat `data.acceptance_results` from
review/test agents as the criteria-pass evidence.

**Status:** ✅ closed — `definition_of_done()` now (a) checks requirement
traceability whenever the task carries `requirement_ids` (each id must appear in
`docs/REQUIREMENTS.md`; the criteria are optional so older tasks don't fail), and
(b) fails on any `acceptance_results` entry with a non-passing verdict, sourced
from the task or `output.data`. SYSTEM_RULES rule 6 instructs agents to emit
`acceptance_results` as criteria-pass evidence. Remaining gap vs README: "review
passes" and "docs updated" stay structural/file-presence (semantic review is the
review agent's job).

### B4. Worker context selection is manual, not relevance-based (Medium)

**Spec:** `00:65-71` — each worker receives rules + PROJECT_MEMORY + its task +
**only relevant** requirements, architecture, decisions, files, test evidence.

**Reality:** `build_payload()` (`base_agent.py:140-178`) delivers rules, spec,
the **entire** PROJECT_MEMORY (truncated to 24k chars, `prompt_builder.py:139`),
the task, and context files **only if listed in `task.input_files`** (plus
`task_notes`). DECISIONS.md, approved architecture and test evidence are never
automatically selected; demo tasks list no `input_files`.

**Recommendation:** auto-inject slices: open decisions whose `affected_tasks`
include this task, REQ entries referenced by the task, latest review for
dependencies.

**Status:** ✅ closed — `BaseAgent.relevant_context()` injects
`decisions_affecting_task` (open/proposed decisions whose `affected_tasks`
contain the task id, via `list_decisions()`) and `requirements` (lines from
`docs/REQUIREMENTS.md` matching the task's declared REQ ids) on top of the
existing input-files/context-files selection.

### B5. Supervisor is rules-only (Medium)

**Spec:** `01_SUPERVISOR_AGENT.md` frames the Supervisor as an *agent* that
diagnoses, summarizes and chooses strategies (`:28-34`).

**Reality:** `supervisor.py` contained no LLM client usage at all — classification
is fixed-priority rules (`classify_state`). Detection quality is deterministic
(good), but the "analyse/diagnose" half of the role did not exist (ties to A5).

**Status:** ✅ closed (on-demand API) — `SupervisorAgent.diagnose(use_llm=True)`
builds a compact report + timeline, calls `LLMClient.complete()` when available,
and falls back to rules-only on any error; exposed as CLI `health --diagnose`.
Deliberately *not* auto-invoked from `check_health()` (every health check would
incur an LLM call).

### B6. Human-decision taxonomy not modeled (Medium)

**Spec:** `00:146-155` lists 7 gate categories (cost, irreversible, architecture
trade-off, safety/privacy, missing info, scope, unresolved blockers) and requires
interrupting "only when necessary".

**Reality:** escalations are persisted as untyped strings (`human_decisions`
entries, `sync_project_health`); exit code 4 lumps everything together — no
category, no severity, no "safe to defer" flag, so callers cannot honour
"interrupt only when necessary".

**Recommendation:** add `category` + `blocking: bool` fields; only
`blocking=True` entries set health HUMAN_DECISION_REQUIRED.

**Status:** ✅ closed — `Escalation` carries `category`
(`loop|deadlock|context|starvation|decision_conflict|stale_block`) and
`blocking`; only blocking categories force `HUMAN_DECISION_REQUIRED` (context
and stale-block are advisory); `sync_project_health` persists
`"[category] task: reason"` strings; `HealthReport.render()` shows the categories
plus the stale-block/conflict sections.

### B7. "Meaningful progress" defined but not measured as specified (Low)

**Spec:** `00:120-130` — progress = requirement satisfied, blocker removed, test
improved, dependency resolved, artifact produced, or root cause identified.

**Reality:** `no_progress` used status-only state-fingerprint equality — a proxy
that matched none of the six signals explicitly (e.g. a new artifact with
unchanged statuses counted as no progress).

**Status:** ✅ closed — dispatch phase 3 computes `progressed` (task reached
REVIEW/DONE/DONE-WITH-LIMITATION **or** produced artifacts) and feeds
`no_progress_delta=-1` when true (phase 1 keeps the pre-recorded +1), so new
artifacts and status advancement both count as meaningful progress; evidence
tracking (A4) covers "root cause identified" via `data.documents`.

---

## C. Dead vocabulary / unused configuration

### C1. `WAITING` status never assigned (Low)

Specified for tasks in `framework/05_PLANNING_AGENT.md:20,38` ("awaiting external
result — review, test, decision"); defined at `config.py:341`. No code path ever
sets it (grep → only the definition). The runtime overloads `BLOCKED` for the
same meaning.

**Status:** ✅ closed — recording a `PROPOSED_CHANGE` moves its
`affected_tasks` (non-terminal, non-REVIEW) to `WAITING`;
`approve_decision()` returns them to `READY` when dependencies are satisfied
(otherwise `BLOCKED`); `get_ready_tasks()` also releases `WAITING` tasks whose
dependencies resolve, so the decision gate still owns the visible refusal.

### C2. `HealthState.RECOVERY` never returned (Low)

Specified in `01_SUPERVISOR_AGENT.md:46` ("Executing recovery strategy");
defined at `config.py:386`; `classify_state()` (`supervisor.py`) returns only
HUMAN_DECISION_REQUIRED / STALLED / BLOCKED / WARNING / HEALTHY — RECOVERY was
unreachable (see A5).

**Status:** ✅ closed — `classify_state()` returns `RECOVERY` first when
`recovering and loops` (a strategy change is in flight); see A5.

### C3. `git_author_name` SystemKey + roadmap git integration unused (Low)

`config.py:120` declares the key; no `subprocess`/git code exists in the
runtime, while `IMPLEMENTATION_ROADMAP.md:117` shows `commit_to_git(...)` and
`:176` lists auto-commit as a deliverable. Either implement post-task commits or
remove the dead key.

**Status:** ✅ closed (removed) — `SystemKey.git_author_name` is gone; 0 refs
remain in `orchestrator/`. Auto-commit remains documented as *out of scope* for
the runtime (checkpoints are the persistence mechanism).

---

## D. Documentation drift (docs contradict the code)

| # | Where | Problem | Status |
|---|---|---|---|
| D1 | `README.md:88`, `meta/GETTING_STARTED.md:24` | Reference **`STATE.yaml`**, which does not exist (actual files: `PROJECT.yaml` + `CURRENT_STATE.md`). | ✅ fixed in this pass |
| D2 | `README.md:155-165` ("Initialization (Day 1)") | Still taught `mkdir` + copying 2 templates; missed `orchestrator init`. | ✅ fixed in this pass |
| D3 | `GAP_ANALYSIS.md` | Verdict "30% implemented / v1.1 docs" is pre-remediation. | ✅ annotated as historical, points to this file |

---

## E. Repository hygiene observed during this review

| # | Item | Note |
|---|---|---|
| E1 | `checkpoints/{demo-cli,derived,parallel-project,kid-robot-face}` | ✅ fixed — `config.default_checkpoints_dir()` honours `ORCHESTRATOR_CHECKPOINTS_DIR`, `CheckpointManager` uses it, conftest's autouse `_redirect_default_checkpoints` points the default root at `tmp_path`, and the leaked dirs were deleted (now empty + gitignored). |
| E2 | Example scaffolds from docs | ✅ fixed — `.gitignore` now has `projects/*` + `!projects/kid-robot-face/`, so quick-start scaffolds (`projects/my-project`, …) never appear in `git status`; the committed example stays tracked. Test isolation was already clean (full suite passes with `projects/` read-only). |

---

## F. Closed since `GAP_ANALYSIS.md` (for the record)

| Finding | Status |
|---|---|
| 1 LLM backend | ✅ `llm_client.py` (anthropic/ollama/openrouter, injectable transport) |
| 2 Parallel dispatch | ✅ `--max-concurrent`, `_state_lock`, ThreadPoolExecutor |
| 3 Specialist agents | ✅ 10 ids registered (requirements…documentation) |
| 4 DoD / review flow | ✅ wired (`_run_pending_reviews`, DoD gate) — remaining nuance in B3 |
| 5 Artifacts | ✅ `_materialize_artifacts` → `docs/` |
| 6 Context accounting | ✅ `context_monitor` — remaining nuance in B2 |
| 7 Milestone checkpoints | ✅ `check_milestones` — remaining triggers in A7 |
| 8 Decision control | ✅ `append_decision` + gate + `approve_decision` |
| 9 RISKS.md upkeep | ✅ `ensure_failure_risk` + risk API |
| 10 Escalation persistence | ✅ `sync_project_health` → `human_decisions`/`blockers` — taxonomy in B6 |
| 11 Loop detection | ✅ 5 kinds incl. oscillation — missing kind in A4 |
| 12 CLI parity | ✅ `init`, `tasks`, `bin/orchestrator`, `pyproject.toml` |
| 13 Doc drift | ✅ guide rewritten v2.0 — leftovers in §D |
| 14 Dead modules | ✅ deleted (913 LOC) |
| 15 Missing doc paths | ✅ all referenced paths exist (guarded by tests) |
| 16 Unwired config | ✅ logging + `CHECKPOINT_SIGNING_KEY` HMAC |
| 17 Stale derived state | ✅ `recompute_derived_state()` on every mutation |
| 18 Unused vocabulary | ⚠️ `DONE WITH ACCEPTED LIMITATION` now used; WAITING/RECOVERY were dead — **both now live (C1/C2)** |
| 19 Dependency hygiene | ✅ pyyaml + pytest only |
| 20 Test structure | ✅ roadmap-named files, 234 tests (+41 gap-remediation = 275; +39 auto-plan = **314**) |

---

## G. Verification after remediation

```bash
python3 -m pytest -q                    # 1103 passed
grep -n "PHASES\|phase_index" orchestrator/config.py      # A1
grep -n "append_task\|relax_dependency" orchestrator/state_manager.py   # A2/A6
grep -n "detect_decision_conflicts\|detect_stale_blocks" orchestrator/supervisor.py  # A3
grep -n "LOOP_KIND_NO_NEW_EVIDENCE\|evidence_stall_max" orchestrator/config.py  # A4
grep -n "attempts_since_change\|recovering" orchestrator/supervisor.py   # A5/C2
grep -n "cp-000-init\|cp-risk\|cp-phase" orchestrator/cli.py orchestrator/orchestrator.py  # A7
grep -n "compact_memory\|reset_context_tokens" orchestrator/state_manager.py  # B1/B2
grep -n "acceptance_results\|requirement_ids" orchestrator/orchestrator.py    # B3
grep -n "decisions_affecting_task\|requirements" orchestrator/agents/base_agent.py  # B4
grep -n "def diagnose\|LLMClient" orchestrator/supervisor.py                  # B5
grep -n "category\|blocking" orchestrator/supervisor.py | head                # B6
grep -rn "git_author_name" orchestrator/                # 0 hits (C3)
grep -n "projects/\*" .gitignore                        # E2
```

> §Appendix — original (pre-remediation) verification commands are kept below for
> the record; most now return the *opposite* of what they did at review time.

```bash
# A1 dead set_phase, C1/C2 dead vocabulary
grep -rn "set_phase" orchestrator/                # only the definition
grep -rn "\"WAITING\"" orchestrator/              # only config.py
grep -rn "RECOVERY" orchestrator/supervisor.py    # only the alias, never returned

# A3 missing supervisor detections
grep -rn "contradict\|unnecessary" orchestrator/  # 0 hits

# A4 loop kinds
grep -n "LOOP_KIND" orchestrator/config.py

# B1/B2 compaction & utilization
grep -n "compact_context\|set_context_utilization" orchestrator/orchestrator.py
sed -n '1,30p' orchestrator/context_monitor.py

# B5 supervisor has no LLM
grep -n "LLMClient\|complete(" orchestrator/supervisor.py   # 0 hits

# A7 init creates no checkpoint
grep -n "create_checkpoint" orchestrator/cli.py   # only `checkpoint save`

# D1 nonexistent STATE.yaml
grep -rn "STATE.yaml" README.md meta/
```
