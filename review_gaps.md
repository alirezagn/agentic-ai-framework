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

---

## Summary

| Severity | Missing entirely | Partial / divergent | Total |
|---|---|---|---|
| High | 2 | 3 | 5 |
| Medium | 5 | 4 | 9 |
| Low / hygiene | 3 | 3 | 6 |

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

### A4. Loop kind "repeated searches with no new evidence" missing (Medium)

**Spec:** `00:100`.

**Reality:** `LOOP_KINDS` (`orchestrator/config.py:399-410`) has 5 kinds —
`same_strategy`, `no_progress`, `alternatives_exhausted`, `state_oscillation`,
`repeated_output` — but no evidence/search tracking exists (grep `evidence` in
`supervisor.py` matches only DFS wording and one regex).

**Recommendation:** count agent runs whose `data` contains no new artifact ids or
file hashes; emit the kind when repeats ≥ threshold.

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

### A8. Init does not capture constraints / budget / resources (Low)

**Spec:** `00:41` "Capture the goal, constraints, budget, available resources,
and desired result."

**Reality:** `cmd_init` writes goal only; `PROJECT.yaml` scaffold
(`cli.py:236-260`) has no `constraints` / `budget` / `resources` sections
(grep across `orchestrator/` + `project-templates/` → none).

**Recommendation:** add optional `project.constraints/budget/resources` blocks to
the scaffold + `--constraints` flag; keep them optional for `validate()`.

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

### B5. Supervisor is rules-only (Medium)

**Spec:** `01_SUPERVISOR_AGENT.md` frames the Supervisor as an *agent* that
diagnoses, summarizes and chooses strategies (`:28-34`).

**Reality:** `supervisor.py` contains no LLM client usage at all (grep
`LLMClient|complete(` → 0) — classification is fixed-priority rules
(`classify_state`). Detection quality is deterministic (good), but the
"analyse/diagnose" half of the role does not exist (ties to A5).

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

### B7. "Meaningful progress" defined but not measured as specified (Low)

**Spec:** `00:120-130` — progress = requirement satisfied, blocker removed, test
improved, dependency resolved, artifact produced, or root cause identified.

**Reality:** `no_progress` uses status-only state-fingerprint equality — a proxy
that matches none of the six signals explicitly (e.g. a new artifact with
unchanged statuses counts as no progress).

---

## C. Dead vocabulary / unused configuration

### C1. `WAITING` status never assigned (Low)

Specified for tasks in `framework/05_PLANNING_AGENT.md:20,38` ("awaiting external
result — review, test, decision"); defined at `config.py:341`. No code path ever
sets it (grep → only the definition). The runtime overloads `BLOCKED` for the
same meaning.

### C2. `HealthState.RECOVERY` never returned (Low)

Specified in `01_SUPERVISOR_AGENT.md:46` ("Executing recovery strategy");
defined at `config.py:386`; `classify_state()` (`supervisor.py`) returns only
HUMAN_DECISION_REQUIRED / STALLED / BLOCKED / WARNING / HEALTHY — RECOVERY is
unreachable (see A5).

### C3. `git_author_name` SystemKey + roadmap git integration unused (Low)

`config.py:120` declares the key; no `subprocess`/git code exists in the
runtime, while `IMPLEMENTATION_ROADMAP.md:117` shows `commit_to_git(...)` and
`:176` lists auto-commit as a deliverable. Either implement post-task commits or
remove the dead key.

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
| E1 | `checkpoints/{demo-cli,derived,parallel-project,kid-robot-face}` | Test runs wrote checkpoints with the **default** root instead of the `checkpoints_root` fixture (now gitignored, but the leak remains — pass the fixture everywhere). |
| E2 | Example scaffolds from docs | Running the HOW_TO_USE/README quick start creates `projects/my-project`, which is not ignored — either ignore example scaffolds (`projects/*` with `!projects/kid-robot-face`) or document cleanup after trying the examples. Test isolation itself was verified clean: a full suite run with `projects/` read-only passes 234/234, so no test creates it. |

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
| 18 Unused vocabulary | ⚠️ `DONE WITH ACCEPTED LIMITATION` now used; **WAITING/RECOVERY remain dead** (C1/C2) |
| 19 Dependency hygiene | ✅ pyyaml + pytest only |
| 20 Test structure | ✅ roadmap-named files, 234 tests |

---

## Appendix — verification commands

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
