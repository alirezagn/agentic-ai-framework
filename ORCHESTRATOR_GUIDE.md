# ORCHESTRATOR RUNTIME — IMPLEMENTATION GUIDE

**Version:** 2.0.0
**Status:** STABLE (state engine + LLM-backed agent policy layer)
**Created:** 2026-09-29 · **Rewritten:** 2026-09-30

---

## OVERVIEW

The Agentic AI Orchestrator is a Python runtime that executes the framework
specification (`framework/00–10`). Project state lives in plain files
(`PROJECT.yaml`, `TASKS.yaml`, `PROJECT_MEMORY.md`, …); the orchestrator turns
that state into decisions: which agent runs next, when a task may start, when a
review is required, when a checkpoint must be taken, and when to stop and hand
control back to a human.

**Key features:**

- State files as the single source of truth (atomic writes, `validate()`)
- Dependency-gated dispatch with READY → IN_PROGRESS → DONE/BLOCKED transitions
- Parallel execution (`--max-concurrent N`) serialized through a state lock
- LLM-backed specialist agents with DoD checks and mandatory review flow
- An opt-in, allowlisted **execution channel** (`data.deploy`) that produces
  real ground-truth evidence, so a verification result can be checked rather
  than believed — see [Evidence and execution](#evidence-and-execution)
- Loop detection: `same_strategy`, `no_progress`, `alternatives_exhausted`,
  `state_oscillation`, `repeated_output`, `no_new_evidence`
- Decision control (`PROPOSED_CHANGE` gate) and risk maintenance (`RISKS.md`)
- Milestone + automatic checkpoints, resume without chat history
- Supervisor health states: `HEALTHY`, `WARNING`, `STALLED`, `BLOCKED`,
  `RECOVERY`, `HUMAN_DECISION_REQUIRED`
- Derived-state recompute so status files always reflect actual task state

---

## ARCHITECTURE — CORE MODULES

| Module | Purpose |
|---|---|
| `orchestrator/state_manager.py` | Read/validate/update all project state files atomically; derived-state recompute |
| `orchestrator/orchestrator.py` | `MasterOrchestrator` — dispatch, review flow, DoD, checkpoints, loop gates |
| `orchestrator/supervisor.py` | `SupervisorAgent` — health checks, loop detection, escalations, RISKS.md |
| `orchestrator/checkpoint_manager.py` | Checkpoint create/list/restore with checksums |
| `orchestrator/llm_client.py` | Stdlib HTTP client for Anthropic / Ollama / OpenRouter |
| `orchestrator/prompt_builder.py` | Renders system prompts from `framework/*.md` specs |
| `orchestrator/agents/` | `BaseAgent`, `LLMAgent`, 9 specialists + `ReviewAgent` |
| `orchestrator/deploy_runner.py` | **The only process-spawning module** — allowlisted, scrubbed, bounded execution for `data.deploy` |
| `orchestrator/path_policy.py` | Shared path-containment policy used by checkpoint restore and agent delivery |
| `orchestrator/context_monitor.py` | Measured context-utilization accounting |
| `orchestrator/cli.py` | Subcommands: `init`, `status`, `tasks`, `run`, `plan`, `health`, `agents`, `checkpoint`, `phase`, `waive`, `retry`, `reopen` |

> **Note:** `project_manager.py`, `task_executor.py` and `checkpoint.py` were
> the v1.1 API and have been removed. Use `MasterOrchestrator` /
> `StateManager` / `CheckpointManager` instead.

---

## QUICK START

```bash
# 1. Install (or run in place)
pip install -e .            # exposes the `orchestrator` command
# — or use the repo entry point directly:
chmod +x bin/orchestrator

# 2. Scaffold a new project (state files + auto-generated task graph)
./bin/orchestrator init my-project --goal "One-sentence project goal"

# 3. Inspect it
./bin/orchestrator --project projects/my-project status
./bin/orchestrator --project projects/my-project tasks

# 4. Execute READY tasks (bounded, sequential by default)
./bin/orchestrator --project projects/my-project run --max-tasks 10

# 5. Overlap independent agents
./bin/orchestrator --project projects/my-project run --max-concurrent 3

# 6. Checkpoint
./bin/orchestrator --project projects/my-project checkpoint save cp-phase1 --notes "phase 1 done"
```

Bootstrap a conversation-driven run with
[`framework/20_DEFAULT_PROJECT_START_PROMPT.md`](framework/20_DEFAULT_PROJECT_START_PROMPT.md).

---

## CLI REFERENCE

```
orchestrator [--project PATH] [--version] <command>
```

| Command | Description |
|---|---|
| `init NAME [--dest DIR] [--goal TEXT] [--force] [--no-plan]` | Scaffold a valid project under `projects/` (default); writes (or refreshes, on re-init/`--force`) the `cp-000-init` baseline checkpoint, empty `constraints`/`budget`/`resources` blocks, and generates the task graph (LLM planning agent when a backend is configured, deterministic starter skeleton otherwise; `--no-plan` forces the skeleton) |
| `status` | Print project/task summary + health recommendations |
| `tasks` | Dependency-graph table (id, status, owner, deps, readiness) + critical path |
| `run [--task ID] [--max-tasks N] [--max-concurrent N] [--all]` | Dispatch READY tasks (or one task); an empty graph is generated on the fly when an LLM backend is configured. One call is one **wave** — tasks unblocked by it are dispatched on the next call. `--all` is that loop built in: it keeps dispatching waves until every task is terminal (exit `0`), stops on a loop limit/blocked/stalled state or when nothing can reach READY (exit `3`), and hands back on `HUMAN_DECISION_REQUIRED` (exit `4`). `--all` and `--task` are mutually exclusive |
| `plan [--goal TEXT] [--force] [--max-tasks N]` | Generate the task graph from the goal via the planning agent; without an LLM backend writes the starter skeleton (or errors when the graph already has tasks); `--force` replaces an existing graph only after a successful plan |
| `health [--diagnose]` | Supervisor health check (writes `PROJECT.yaml` health block); `--diagnose` adds an LLM diagnosis when a provider is configured, rules-only otherwise |
| `phase show\|set [PHASE]` | Show the current/derived lifecycle phase, or set it explicitly (validated against the phase vocabulary; forward moves checkpoint as `cp-phase-<name>`) |
| `waive TASK --dep ID [--reason TEXT]` | Human unblock: drop one dependency edge (deadlock relief) and record it in `CHANGELOG.md` |
| `retry TASK [--reason TEXT]` | Human recovery: clear a failed/stalled/loop-limited task's counters (`same_strategy`, `no_progress`, evidence stalls), put it back to READY when its dependencies are met, and record it in `CHANGELOG.md`. The reason (or, absent one, the previous failure) is stored as `execution.retry_reason` and surfaced to the next attempt as `recovery_feedback_from_previous_attempt` in the prompt — without it the model is blind to why earlier attempts failed |
| `reopen TASK --reason TEXT` | Conscious overturn of a **terminal** task (`DONE`, `DONE WITH ACCEPTED LIMITATION`, `CANCELLED`, `WAIVED`) back to READY — the alternative to hand-editing TASKS.yaml. `--reason` is required and feeds the next prompt; refused for non-terminal statuses (use `retry`) |
| `agents` | List registered specialist agents |
| `checkpoint save\|list\|restore` | Checkpoint management (`--checkpoint ID`, `--notes TEXT`) |

**Exit codes:** `0` success · `1` no command · `2` usage / state error ·
`3` loop limit or STALLED/BLOCKED health · `4` `HUMAN_DECISION_REQUIRED`

### Goal-driven plan expansion (`orchestrator.auto_plan`)

`init`/`plan` (LLM plan *and* starter skeleton) run the generated graph through
`expand_implementation_stages()` before it is written. When the goal or the
implementation task's title looks like a **GUI**
(`gui`, `tkinter`, `pyqt`/`qt`, `canvas`, `dashboard`, `desktop`, `frontend`,
`webui`, `graphical`, `ui`, …) or a **multi-module** build (`module`, `package`,
`plugin`, `microservice`, `monorepo`, …), the single `software_agent`
implementation task is replaced by the matching stage chain:

- GUI → `Backend Data Layer → UI Canvas Components → Application Launcher`
- multi-module → `Backend Data Layer → Module Interface Layer → Application Launcher`

The chain is wired as `requirements → data layer → middle stage → launcher →
original dependents` (the test task now waits on the launcher), `TASK-NNN` ids
are renumbered and every dependency remapped, and the launcher inherits the
replaced task's `expected_outputs` and acceptance criteria — so a deliverable
never disappears. Expansion is **idempotent** (already-expanded graphs are left
alone), keeps the replaced task's output style (a bare `IMPLEMENTATION.md`
stays bare rather than gaining a `docs/` prefix), and may exceed
`--max-tasks` by up to two tasks: not splitting the implementation would be a
plan the framework cannot execute correctly. Goals that mention neither
signal are untouched. Detection is deliberately conservative — the words
`web`, `app` and `widget` alone do not trigger it.

### Lifecycle phases

`PROJECT.yaml.phase.current` is derived forward-only from the task graph
(`StateManager.derive_phase`): the first phase in `REQUIREMENTS → RESEARCH →
ARCHITECTURE → PLANNING → IMPLEMENTATION → INTEGRATION → TESTING → VALIDATION →
RELEASE` that owns a non-terminal task wins; phases with no matching tasks are
auto-skipped (`MAINTENANCE` is manual only). Derived advances checkpoint as
`cp-phase-<name>`; explicit `phase set` validates against `config.PHASES`.

---

## PYTHON API

### Dispatch and run loops

```python
from orchestrator.orchestrator import MasterOrchestrator

orch = MasterOrchestrator("projects/my-project")

problems = orch.state.validate()          # [] == structurally sound

results = orch.run_cycle(max_tasks=25, max_concurrent=3)
for r in results:
    print(r.task_id, r.previous_status, "->", r.new_status, r.output.summary)

# run cycles until no progress (bounded by no-progress threshold + max_cycles)
cycles = orch.run_until_stalled(max_cycles=10, max_concurrent=3)

# single task (routes REVIEW tasks through the review flow)
result = orch.run_task("TASK-002")
```

### Custom agents

```python
from orchestrator.agents.base_agent import BaseAgent, AgentOutput

class MyAgent(BaseAgent):
    AGENT_ID = "requirements_agent"

    def execute(self, payload: dict) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.completed(task_id, "requirements captured",
                              data={"requirements": ["REQ-001"]})

orch.register_agent(MyAgent(state_manager=orch.state))

# or resolve per-owner automatically (used by the default registry):
def resolver(owner: str, state_manager):
    return MyAgent(state_manager=state_manager)

orch = MasterOrchestrator("projects/my-project", agent_resolver=resolver)
```

`register_agent()` pins a singleton (safe for `--max-concurrent > 1`);
`resolve_agent(owner, fresh=True)` gives per-thread instances.

### Health, decisions, checkpoints

```python
report = orch.sync_health()          # recompute + persist health block
print(report.render())
print(report.state)                  # HEALTHY | WARNING | STALLED | ...

pending = orch.state.pending_proposed_changes()
for dec in pending:
    orch.approve_decision(dec["id"], approved=True)   # or approved=False

orch.create_checkpoint("cp-001", notes="requirements approved")
orch.resume_from_checkpoint("cp-001")   # restores files, clears loop history

print(orch.checkpoints.print_checkpoint_list())
```

---

## STATE FILES

| File | Purpose | Maintained by |
|---|---|---|
| `PROJECT.yaml` | Phase, health, context, progress, agents, `next_tasks`, blockers, pending human decisions | orchestrator (derived recompute), supervisor |
| `TASKS.yaml` | Task graph, execution counters, review status, `summary`, `critical_path` | orchestrator (derived recompute) |
| `PROJECT_MEMORY.md` | Compact resumable state (goal, status, decisions, next steps) | you + documentation agent |
| `CURRENT_STATE.md` | Quick human snapshot | you |
| `DECISIONS.md` | `### DEC-NNN` entries incl. `PROPOSED_CHANGE` gates | `append_decision` / `approve_decision` |
| `RISKS.md` | `### RISK-NNN` register (status, probability, impact) | supervisor (`ensure_failure_risk`, `append_risk`) |
| `CHANGELOG.md` | State-file history | every state write |
| `docs/` | Materialized agent artifacts (`REVIEW-<task>.md`, …) | agents (`_materialize_artifacts`) |
| `docs/evidence/<task>/` | Execution transcripts (`NN-<command>.json`) for anything run via `data.deploy` | `DeployRunner.write_evidence` |

**Authoring (G16):** agents deliver real file changes through their payload —
`data.documents["<path>"]` carries the full content of a new/short file, and
`data.edits["<path>"] = {"search", "replace"}` patches an existing file in
place (the post-edit body is mirrored into `docs/<name>` for the DoD check).
A JSON summary without that content only produces a wrapper document — every
agent's `system_rules()` carries the `AUTHORING_CONTRACT` spelling this out,
and invalid edits (path escape, missing/ambiguous search) fail the task with a
precise error instead of silently delivering a stub. Key-shape variants are
normalized before any check runs (`normalize_delivery_data` at
`AgentOutput.from_dict`): literal dotted keys (`{"data.documents": …}`) and
list-form records (`documents: [{path, content}]`) fold into the canonical
channels, so a delivery in either shape writes the real file instead of
degrading into a wrapper — the `sys-usage` regression where a dotted-key
delivery passed Done with the expected output missing.

**Edit session (G20):** `software_agent` does not deliver in one giant JSON
reply. `EDIT_SESSION_TURNS = 3` switches `execute()` into a bounded
**multi-turn edit session**: each turn asks only for the next change set
(small JSON — no truncation), the deterministic applier patches the real
files, and `delivery_problems()` verifies the **workspace** (mirror exists,
real file shares content, `data.documents` cannot stand in for an existing
file). Apply errors and remaining DoD problems are fed back as the next
turn's feedback; after the last failing turn the output returns `failed`
with those problems. Successful change sets are recorded in
`data.edits_applied` and consumed so `run()` never re-applies them.
Verification is shared: `definition_of_done` delegates its delivery checks to
the same `delivery_problems()` helper (`orchestrator/agents/base_agent.py`),
so the session and the DoD can never disagree.

**Truncation recovery:** a reply cut mid-stream (token budget, dropped
connection) no longer fails the task by default. Before the legacy one-shot
repair, `LLMAgent` runs a local recovery pass
(`recover_truncated_payload`, `orchestrator/agents/llm_agent.py`): it re-finds
the JSON in the raw text (fenced ```json blocks first, then brace-matched
candidates), reassembles a document whose tail was cut, and resolves the cut
against the file it was editing. Three ordered outcomes: a fully reassembled
payload is used as-is (the model's status is kept and a warning
`recovered from a truncated payload (local chunked parse)` is attached); a cut
**inside a file body** returns `status=blocked` with the partial body in
`data.edit_buffers` so nothing is applied — edits materialize only on
`completed`, a partial body is never written to disk; and when there is nothing
to salvage the pass yields nothing and the legacy `_repair_truncated_output`
runs unchanged (one re-ask, and its `TRUNCATED` contract — no JSON at all
returns `failed` — is untouched). Raise `ORCHESTRATOR_LLM_MAX_TOKENS` for
data-heavy replies; recovery is a safety net, not a token budget.

**Delivery manifest (G22):** the agent never guesses the delivery channel.
At payload build, `relevant_context()` adds a `delivery_manifest` entry
classifying every `expected_outputs` path — `EXISTS` (update with
`data.edits` only), `MISSING` (create with `data.documents`), or a `docs/`
deliverable — and the same **task-start snapshot**
(`preexisting_expected()`) is threaded through the edit session,
artifact materialization and every dispatch-time DoD check. Consequences:
`data.documents` actually **creates a missing expected file at its real
project path** (previously it only wrote the `docs/` mirror, so no channel
could create a new source file at all), it never touches files that
pre-existed (those belong to `data.edits`), the rendered wrapper fallback
is never written to a real file, and the DoD rejects content delivered only
to `docs/` when the real file is missing (`expected output missing from the
project`). Files the task itself created mid-session are therefore not
mistaken for pre-existing project files — redelivery inside the session
converges instead of looping. Two more facts reach the model up front: every existing expected output is inlined into the payload as `expected_output:<path>` (budgeted 32K per file / 40K total, rendered without middle-truncation) so `data.edits` search snippets are quoted, never guessed; and when an apply still fails, the edit-session feedback includes each target file's current body (`current content of <path> (authoritative)`).

**Derived-state recompute** (`StateManager.recompute_derived_state`) runs after
every status mutation and on `refresh_ready_states()`:

- `TASKS.yaml`: `summary.total_tasks`, `summary.status_breakdown`,
  `parallel_groups` pruned to live tasks, `critical_path.path` (longest chain)
- `PROJECT.yaml`: `progress` per workstream %, `agents` activity per owner,
  `next_tasks` = currently READY ids

Planning-only fields (durations, estimates, group metadata) are preserved; files
are only written when a derived value actually changed.

---

## TASK EXECUTION FLOW

### Dispatch phases (per task, under `_state_lock` where noted)

1. **Gate phase (locked):** oscillation gate → loop-limit check → dependency
   check → `PROPOSED_CHANGE` decision gate → resolve agent → `IN_PROGRESS` →
   `record_attempt`
2. **Execution phase (unlocked):** `agent.run(task)` — runs in a worker thread
   when `max_concurrent > 1` — then any `data.deploy` invocations, which also
   run here. Both are deliberately off-lock: a build or test suite can take
   minutes, and holding `_state_lock` across one would stall every other
   worker's state turn.
3. **Finalize phase (locked):** context-utilization update → proposed-change
   recording → status finalization (`DONE`/`BLOCKED`/`FAILED`) → risk recording
   for failures → loop signals + fingerprint → DoD check (including the
   execution-evidence check) → derived-state recompute → auto/milestone
   checkpoint

### Review flow

Tasks with `review: {required: true}` enter `READY_FOR_REVIEW`. Before the next
dispatch batch, `_run_pending_reviews()` routes them to `dispatch_review()`:

- reviewer is always `review_agent`, runs with `materialize=False`
- writes `docs/REVIEW-<task>.md`
- **PASS** → task `DONE`
- **PASS WITH ACTIONS** → `DONE WITH ACCEPTED LIMITATION` + follow-up tasks
- **FAIL** → task `FAILED` + follow-up tasks

### Definition of Done

`definition_of_done(task, output, deploy_records=…)` requires:

- measurable acceptance criteria — a missing/empty `acceptance_criteria` list
  is **not** a failure: the DoD falls back to owner-scoped default criteria
  (`_fallback_acceptance_criteria`, e.g. "Tests map to requirements and pass"
  for `test_agent`, `Complete task analysis and produce structured markdown
  outputs` otherwise), and dispatch phase 3 persists those defaults into
  `TASKS.yaml` (`StateManager.set_acceptance_criteria`), so a reviewer or a
  resumed session sees measurable criteria instead of a planning omission
- all `expected_outputs` materialized on disk
- **content plausibility** — when an expected output already exists in the
  project, its `docs/` mirror must share at least one meaningful line with the
  real file (summary/prose JSON wrappers rejected: deliver via `data.edits` or
  full file content)
- **static import contract** — every file the task delivered is parsed and its
  intra-project imports resolved (`orchestrator/import_contract.py`, via
  `delivery_problems()`): a name the producer module does not define, an import
  of a project module that does not exist, or a file that does not parse all
  block delivery. Checked in both directions — the consumer side, and a
  producer that dropped a still-imported name — and only against files this
  task touched, so pre-existing breakage never fails an unrelated task. Pure
  `ast` (no subprocess), so the suite stays hermetic, and the message names
  the producer file plus the names it really defines, which is exactly what the
  repair round feeds back to the model.
- **ground truth for any claimed execution** — see below
- review passed when required

Unmet → task `FAILED` with `DoD unmet: …`. Before failing cold, dispatch makes
**one auto-repair round**: the DoD problems are sent back to the agent
(`repair_delivery`), which may re-materialize a real delivery; LLM agents
re-ask the model, deterministic agents skip. The note is **problem-aware**
(`repair_remedies`, `orchestrator/agents/llm_agent.py`): an evidence rejection
gets the exact JSON that sets `data.test_status`, an import rejection gets
"patch the consumer with `data.edits`", a content rejection gets the original
file-content guidance — one hardcoded "it did not deliver real file content"
note used to answer *every* rejection, so a model told to fix evidence with
file edits re-delivered files, never set the field, and a legitimate `NOT RUN`
task failed. The DoD rejection is stored as the task note (not the claiming
summary) so the next attempt sees honest context.

A rejected turn's analysis is not lost either: before the failure is
recorded, `BaseAgent.harvest_findings()` appends `data.findings` and
`data.analysis` to `docs/findings/<task-id>.md` (de-duplicated across
retries) and mirrors up to five `data.risks` entries into `RISKS.md` as
`<task-id>: <risk title>` — a schema-rejected research turn keeps its
findings on disk.

#### Why an unevidenced claim becomes `NOT RUN`

The execution evidence check exists because the framework previously had no way
to tell an invented result from a real one. An agent asked for test outcomes it
could not obtain would state them anyway, nothing could verify the statement, and
a fabricated "42/42 tests passed" reached `DONE` and was persisted into
`TASKS.yaml`.

The check fires on **any one** of three triggers:

1. the output declares `data.deploy` — the agent asked to execute;
2. the summary or any artifact *claims* an executed verification
   (`config.claims_execution`: "42/42", "tests passed", "build succeeded",
   "flashed", "ctest", "coverage", …);
3. an expected output looks like a build/test artifact (`*.log`, `*.bin`,
   `test_*`, `test_results*`, …).

When it fires, one of two things must be true:

- **A record with `executed: true` exists.** The real exit code is then
  authoritative. A non-zero exit blocks completion and routes to repair — a
  genuine failure is ground truth, and losing it is exactly what a fabricated
  report accomplishes. A declared `expect` that disagrees with the real exit
  code also blocks.
- **The agent reports `data.test_status = "NOT RUN"`.** An honest negative is
  accepted and the task proceeds on its other merits. "I could not run this" is a
  legitimate outcome; inventing a pass is not. A `NOT RUN` stated in the
  **summary** instead of the field is accepted too (`_reports_not_run`) — the
  substance is the same honest negative, and a model that will not move it into
  the field would otherwise turn a legitimate outcome into a failed task. It is
  honoured only when the same output does not *also* claim execution, so a
  contradictory "NOT RUN … 42/42 passed" still needs ground truth.

With neither, the task is `FAILED` and the note states the missing evidence —
never the claim. The asymmetry is deliberate: refusing an unevidenced claim is
always correct, whereas forcing `NOT RUN` on a task that legitimately had
nothing to run would be noise. An ordinary documentation task triggers nothing
here.

### Dependency gating

A task is READY only when every dependency is in a satisfied status
(`DONE`, `DONE WITH ACCEPTED LIMITATION`, `CANCELLED`, `WAIVED` — the last
one set only by the supervisor's validation circuit-breaker).
`refresh_ready_states()` promotes `TODO`/`BLOCKED` tasks whose dependencies
just became satisfied.

---

## PARALLEL EXECUTION

```bash
orchestrator --project projects/my-project run --max-concurrent 4
```

- READY tasks are collected in graph order, then dispatched on a thread pool
  (batch bounded by `--max-tasks`)
- `agent.run()` executes off-lock; all state read-modify-writes are serialized
  by `orchestrator.state._state_lock` (an `RLock`)
- agents handed in via `register_agent()` are pinned singletons; use
  `agent_resolver` for per-thread instances
- failures do not consume the `max_tasks` quota in the sequential path

---

## LOOP DETECTION & RECOVERY

Supervisor thresholds (`config.LoopThresholds`, overridable via
`SupervisorAgent(thresholds=...)` or `loop_thresholds({...})`):

| Kind | Trigger | Default |
|---|---|---|
| `same_strategy` | `attempts_since_change` (attempts since the last `strategy_changed`) | 3 |
| `no_progress` | `no_progress_cycles` (state fingerprint unchanged) | 5 cycles |
| `alternatives_exhausted` | `strategy_changes` without success | 2 |
| `state_oscillation` | project state flips A↔B (status-only fingerprints) | 4 changes, 2 states |
| `repeated_output` | substantially identical outputs (`last_output_hash`) | 3 repeats |
| `no_new_evidence` | dispatches that produced no new artifacts, decisions or requirement updates (`evidence_stall_count`) | 3 dispatches |

When a loop is exceeded:

1. Dispatch refuses the task (`LoopLimitExceededError`, CLI exit code 3)
2. State is preserved; the detection is recorded on the result/`loop` field
3. Recovery: fix the inputs, then `orchestrator retry <TASK-ID>` to reset the
   counters (equivalent to a human-approved strategy change), or change strategy
   (`data.strategy_changed`), escalate to a human, or rework the task graph

`state_oscillation` is checked **first** in the dispatch gate and applies to the
whole project (task id `PROJECT`); the fingerprint history is cleared on
checkpoint restore.

### Validation circuit-breaker (auto-waive)

Not every rejection deserves a human. Supervisor failures are classified
(`supervisor.is_validation_failure`):

- **validation-class** — `DoD unmet: …` and schema/shape violations
  (`Output summary must not be empty`, `Output data must be a dictionary`,
  unparseable JSON, mismatched agent/task id): counted in
  `execution.validation_failure_streak`.
- **code/runtime-class** — tracebacks, `connection refused`/backend
  failures, exit codes: always **reset** the streak and keep the normal
  retry/escalation path (a crashing agent is never silently waived).

After `validation_waive_after` (default **2**) consecutive validation
failures — on the second rejection itself — the supervisor flips the task
to `WAIVED`: a terminal status that satisfies dependencies, announced as
`WARNING: <task-id> auto-waived after N consecutive Definition-of-Done/
schema rejections …` in CURRENT_STATE.md, followed by an automatic
`refresh_ready_states()` so dependents dispatch in the same wave. Because a
waived task is terminal, it never trips `same_strategy` and cannot park the
project in `HUMAN_DECISION_REQUIRED` over a purely structural rejection.
`sync_project_health()` also sweeps for struck-out FAILED tasks before every
health computation (idempotent safety net for any path that records a
streak). `orchestrator retry` and `reopen` reset the streak; `reopen
TASK-00X --reason "…"` is the conscious human path back to READY.

Threshold: `LoopThresholds.validation_waive_after` (YAML key
`validation_waive_after`, override via `loop_thresholds({...})`).

---

## Evidence and execution

The framework can run a build, a test or a tool on the agent's behalf, and can
then **prove** it. This is the only ground truth in the system: the runner — not
the model — stamps `executed` and the exit code.

**Disabled by default.** The runtime is offline by design; nothing executes
unless both `ORCHESTRATOR_DEPLOY_ENABLED=1` and a non-empty allowlist are set.

### Requesting execution

An agent proposes *what* to run via `data.deploy`. It never decides *whether* it
ran.

```json
{
  "status": "completed",
  "summary": "Built and ran the unit tests",
  "data": {
    "deploy": [
      {
        "command": "ctest",
        "args": ["--test-dir", "build", "--output-on-failure"],
        "cwd": "build",
        "expect": "PASS",
        "rationale": "verify the refactor did not break anything"
      }
    ],
    "test_status": "PASS"
  }
}
```

| Field | Type | Meaning |
|---|---|---|
| `command` | string | Required. An allowlisted executable, or a project-relative script (`./scripts/verify.sh`) confined to the project. Rejected *before* spawning if not permitted. |
| `args` | list of strings | Optional. Passed as a list — never a shell string, so `;`, `&&`, `>` and pipes are inert text. |
| `cwd` | string | Optional, project-relative. Forced to the project root when omitted; a path escaping the project is refused. |
| `expect` | `"PASS"` \| `"FAIL"` | Optional. The agent's prediction, **checked against the real exit code**; a mismatch blocks completion just as a failure does. |
| `rationale` | string | Optional, free text. Recorded in the transcript. |

A single object or a shell-like string is also accepted and normalised, but the
list form is what agents are instructed to emit.

### Results

Every invocation returns a record, attached to `output.data["deploy_results"]`
so the agent, the DoD and any later reviewer read the same evidence:

```json
{
  "command": "ctest", "args": ["--test-dir", "build"],
  "exit_code": 0,
  "stdout_tail": "100% tests passed, 0 tests failed out of 42",
  "stderr_tail": "", "stdout_truncated": false, "stderr_truncated": false,
  "duration_ms": 8432,
  "executed": true, "status": "executed",
  "declared_expect": "PASS", "expect_matched": true,
  "artifact_path": "docs/evidence/TASK-004/00-ctest.json"
}
```

`executed` is the field that matters, and only `DeployRunner` sets it.

| `status` | Meaning |
|---|---|
| `executed` | The process really ran. `exit_code` is authoritative, including when non-zero. |
| `refused` | Not run: the channel is off, the executable is not allowlisted, or the request was malformed. `reason` says which. |
| `timeout` | Not run: the child exceeded the timeout and was terminated. |
| `spawn_failed` | Not run: the process could not be started. |

`executed: false` is a first-class, reportable outcome — which is what makes
`NOT RUN` an honest, checkable state rather than a silent gap.

### Evidence transcripts

Each executed invocation is persisted as JSON under
`docs/evidence/<task-id>/NN-<command>.json`, containing the record plus a
rendered `transcript` and a `recorded_at` stamp. Paths are flattened to a
single filename, so a project-relative command cannot produce nested evidence
paths. Evidence is written best-effort: a failure to write it is logged and
never fails a dispatch.

### Security properties

`orchestrator/deploy_runner.py` is the **only** module in the package that
spawns a process, so its properties are auditable with a single grep
(`grep -rn subprocess orchestrator/`). Each exists because it was a way to
abuse the capability:

- **`shell=False` always**, with a list `argv` — no argument is re-parsed as
  shell syntax.
- **Allowlist before spawn.** An executable not on the list is refused before
  the process is created. Comparison is on the basename, which also means the
  allowlist trusts `PATH` — allowlisting `gcc` permits whatever `gcc` resolves
  to.
- **Forced working directory** at the project root; `cwd` overrides are
  containment-checked.
- **Scrubbed child environment** — only `PATH`, `HOME`, locale and a few
  similar variables are passed, and proxies are dropped. `ANTHROPIC_API_KEY`,
  `OPENROUTER_API_KEY` and `CHECKPOINT_SIGNING_KEY` are **not** readable by any
  spawned build script.
- **Bounded output and time** — a per-stream byte cap keeping the *tail* (where
  build failures are legible), with truncation reported rather than silent, and
  a hard timeout.
- **Fail closed.** There is no "warn and run anyway" path: a disabled channel
  or empty allowlist means nothing executes.

```python
records = orch.deploy_runner.run_payload([{"command": "ctest", "expect": "PASS"}])
print(records[0].executed, records[0].exit_code)
```

---

## HEALTH MONITORING

```python
report = orch.sync_health()     # persists PROJECT.yaml.health + blockers
print(report.render())
```

| State | Meaning |
|---|---|
| `HEALTHY` | all systems normal |
| `WARNING` | recoverable issue (advisory escalation, context pressure, stale state) |
| `STALLED` | no progress across cycles |
| `BLOCKED` | blocked tasks and no independent ready work |
| `RECOVERY` | a strategy change is in flight while loop signals are still present |
| `HUMAN_DECISION_REQUIRED` | a **blocking** escalation (loop/deadlock/starvation/decision conflict) |

Checks: context utilization (compaction 70% / critical 85%), failed tasks,
no-progress tasks, circular dependencies, blocked-task analysis
(`analyze_blocked` counts `TODO`/`READY` tasks with unsatisfied deps), stale
blocks (`detect_stale_blocks` — BLOCKED but every dependency satisfied),
pending human decisions, and `detect_decision_conflicts` (two pending
`PROPOSED_CHANGE`s affecting the same task). Escalations carry a `category`
(`loop`/`deadlock`/`context`/`starvation`/`decision_conflict`/`stale_block`)
and a `blocking` flag; only blocking ones force `HUMAN_DECISION_REQUIRED`
(others classify as `WARNING`). Failed tasks get a `RISK-NNN` entry in
`RISKS.md` (checkpointed as `cp-risk-NNN`) so risks never silently disappear.

`SupervisorAgent.diagnose()` explains the state and the next action — LLM when
a provider is available, deterministic rules-only fallback otherwise (also
available via `orchestrator health --diagnose`).

---

## DECISION CONTROL

```python
# an agent's output.data["proposed_change"] is persisted automatically:
#   - DEC-NNN entry with status PROPOSED_CHANGE in DECISIONS.md
#   - a human_decision block in PROJECT.yaml
#   - output warning "proposed change ... awaiting human approval"

pending = orch.state.pending_proposed_changes()
orch.approve_decision("DEC-001", approved=True)   # gate lifted
orch.approve_decision("DEC-002", approved=False)  # gate stays closed
```

While a `PROPOSED_CHANGE` affects a task, its non-terminal tasks move to
`WAITING` (visible in `tasks` output); dispatch still attempts them so the
gate refusal surfaces in results — the task is never marked FAILED. On
approval/rejection the gate opens and `WAITING` tasks return to `READY`
(or `BLOCKED` if a dependency is unsatisfied). `REVIEW` tasks keep their
status (the review queue keys off it).

---

## CONTEXT ACCOUNTING

`context_monitor.py` converts each dispatch into tokens (provider `usage`
when reported, else `payload_chars / 4`) and **accumulates** them into
`PROJECT.yaml.context.cumulative_tokens` against
`ORCHESTRATOR_CONTEXT_WINDOW_TOKENS` (default 128000); the utilisation percent
is derived from that session total — it never resets except at a compaction
event, which zeroes the budget. Thresholds come from `COMPACTION_THRESHOLDS`
(compaction 70%, critical 85%) and feed supervisor health warnings. When the
budget is exhausted, `compact_context()` checkpoints, appends a memory note,
folds an oversized `PROJECT_MEMORY.md` (head + tail kept, middle replaced by a
drop marker — `MEMORY_COMPACT_MAX_CHARS`, default 60000 chars) and resets the
token budget.

---

## LLM CONFIGURATION

Stdlib-only client — no third-party SDK. Environment variables (the CLI
also auto-loads `./.env`, e.g. the repo's Ollama preset in `.env.example`):

| Variable | Purpose |
|---|---|
| `ORCHESTRATOR_LLM_PROVIDER` | `anthropic` \| `ollama` \| `openrouter` (auto-detected from keys) |
| `ORCHESTRATOR_LLM_MODEL` | Model id (project default: `gemma4:12b`) |
| `ORCHESTRATOR_LLM_MAX_TOKENS` | Output-token budget per completion (default `4096`; raise for data-heavy replies — a cut reply is first run through local truncation recovery, and only what that cannot salvage fails validation with an explicit "looks truncated" error) |
| `ORCHESTRATOR_LLM_NUM_CTX` | Ollama context window for `num_ctx` (default `16384`; the server default of 4096 silently caps prompt+output and truncates JSON — native `/api/chat` only) |
| `ORCHESTRATOR_LLM_TIMEOUT` | Per-request timeout in seconds (default `120`; raise for slow/busy servers) |
| `ANTHROPIC_API_KEY` | Enables `anthropic` |
| `OPENROUTER_API_KEY` | Enables `openrouter` |
| `OLLAMA_BASE_URL` | Project default: `http://192.168.0.200:11434` (append `/v1` for OpenAI-compat) |
| `ORCHESTRATOR_CONTEXT_WINDOW_TOKENS` | Context window for utilization math |
| `ORCHESTRATOR_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default `INFO`; `-v` / `-q` override) |
| `CHECKPOINT_SIGNING_KEY` | HMAC-SHA256 key. Setting it **enables signing**: new snapshots record `signed: true` plus a signature bound to contents, id, key id and timestamp. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`. Without it, snapshots are written `signed: false` and verify on checksums only |
| `ORCHESTRATOR_CHECKPOINT_KEY_ID` | Identifies the signing key (default `default`). Bound into the signature, so a rotated secret reports `UNVERIFIABLE` rather than `TAMPERED` |
| `CHECKPOINT_ALLOW_UNSIGNED` | `1` lets a snapshot whose signature cannot currently be checked fall back to checksums with a warning. Never allows a tampered snapshot to pass |
| `ORCHESTRATOR_DEPLOY_ENABLED` | `1` enables the execution channel (`data.deploy`). **Default `0`** — the runtime is offline by design |
| `ORCHESTRATOR_DEPLOY_ALLOWLIST` | Comma-separated executable **basenames** permitted to be spawned (e.g. `ctest,cmake,python3`). Entries are reduced to a basename, so a path cannot smuggle a different binary in. A literal `*` entry means **any executable** (the sandbox — `shell=False`, project cwd, scrubbed env, timeout, transcript — still applies). Empty means nothing can run |
| `ORCHESTRATOR_DEPLOY_TIMEOUT` | Per-invocation wall-clock seconds (default `300`; accepted range 1–3600) |
| `ORCHESTRATOR_DEPLOY_MAX_OUTPUT` | Byte cap per captured stream (default `65536`; minimum 1024). The **tail** is kept, and truncation is reported explicitly |
| `ORCHESTRATOR_DEPLOY_EVIDENCE_DIR` | Where transcripts are written, project-relative (default `docs/evidence`) |

Both deploy variables are required: setting `ORCHESTRATOR_DEPLOY_ENABLED=1` with
an empty allowlist stays inert, because a flag that appears live and does
nothing is worse than one that is off. See [Evidence and execution](#evidence-and-execution).

### Dependency installation is an operator decision

Agents that introduce a third-party import are instructed to (1) declare it in
`requirements.txt` at the project root and (2) request
`pip install -r requirements.txt` through `data.deploy` **before** any test step
(`orchestrator.prompt_builder.DEPENDENCY_AUTOMATION_INSTRUCTIONS`). Declaring is
a file the Definition of Done can verify; installing is an execution request.

Because the channel is closed by default, `pip` is normally refused. That is the
intended behaviour, not a bug — the agent then keeps `requirements.txt` as its
deliverable, reports `data.test_status = "NOT RUN"` and names the packages it
could not install. To let the install actually run:

```bash
export ORCHESTRATOR_DEPLOY_ENABLED=1
export ORCHESTRATOR_DEPLOY_ALLOWLIST=python3,pip,pytest
```

Allowlisting `pip` lets an agent install arbitrary packages from an index, which
is a real supply-chain decision — scope it to the projects that need it rather
than adding it globally. On a PEP 668 "externally managed" interpreter the
runner no longer fails by design: before spawning pip it appends
`--break-system-packages` when the invocation targets *that same* environment
(`pip_targets_running_environment` — a `--target`/`--root`/`--prefix` pip, or a
different interpreter, is left alone), and it **skips** the install entirely
when the interpreter is externally managed and every requirement is already
provably satisfied (bare names and exact `==` pins resolvable through
`importlib.metadata`; `--upgrade`, `--force-reinstall`, `-e` and friends always
run). A skip is recorded as `status: skipped`, `executed: false`, no exit code.
It is not ground truth: the DoD accepts an all-skipped result **only** when the
output claims nothing and names no test-like expected output (a genuine
"nothing was left to install"), while a summary that claims tests passed over
skipped records alone is still refused — the agent must report
`test_status: NOT RUN` or really run the command. Prefer a project virtualenv
for anything that must really install.

See [Snapshot integrity](#snapshot-integrity) for the four verdicts
(`VERIFIED` / `UNSIGNED` / `TAMPERED` / `UNVERIFIABLE`) and how they are decided.

```python
from orchestrator.llm_client import LLMClient, is_available
print(is_available())
result = LLMClient().complete("system…", "user…")
```

`LLMClient(transport=...)` accepts an injectable transport for tests.

---

## CHECKPOINTING

```
checkpoints/
  index.json
  cp-001-phase1/
    PROJECT.yaml, TASKS.yaml, PROJECT_MEMORY.md, … (state snapshot)
    metadata.json          # id, timestamp, notes, checksum
```

- **Automatic:** after dispatch/review when `auto_checkpoint=True`
- **Milestone:** `cp-milestone-<slug>` once every task of a milestone is
  terminal; `cp-milestone-complete` when the whole graph is terminal
- **Manual:** `orchestrator checkpoint save cp-001 --notes "…"`
- **Id reuse replaces, it never aborts:** the orchestrator saves with
  `overwrite=True`, so an existing directory for the same id is refreshed
  instead of raising. These are bookkeeping snapshots: a fresh plan rewrites
  `RISKS.md` and re-allocates `RISK-001` while the old `cp-risk-RISK-001`
  directory is still on disk, and that collision used to fail the task with an
  error *about the checkpoint*, hiding the task's own error and stopping
  `run --all`. `CheckpointManager.create_checkpoint` stays strict unless
  `overwrite=True` is passed explicitly.
- **A snapshot is staged, then swapped in:** `create_checkpoint` builds the
  new directory under a `.staging-<id>-<pid>` sibling and renames it over the
  old one only after the copies and `metadata.json` are complete;
  `delete_checkpoint` removes the **index row first**, then the directory.
  The index therefore only ever names finished directories. The previous
  order (rmtree the old directory, *then* copy) destroyed the existing
  snapshot on any later failure — empty source, failed copy, a crash in
  between — which is how real `checkpoints/*/index.json` files grew rows for
  directories that no longer existed: `list_checkpoints`/`has_checkpoint`
  kept reporting the ghost and every reader of `metadata.json` died with
  `FileNotFoundError`. A failure now costs only the staging directory, which
  is cleaned up on the way out (a leftover from a killed process is removed
  by the next save of that id), and the containment test in
  `tests/test_hmac_verification.py` keeps the whole set honest.
- **Auto-checkpoints are best-effort:** every snapshot in the dispatch
  finalize path (`cp-risk-*`, `cp-phase-*`, `cp-milestone-*`, `cp-auto-*`)
  runs through `_safe_auto_checkpoint()`, which logs and returns `None` on
  error — the task's work is already persisted, so an I/O failure may cost
  the snapshot, never the dispatch.
- **Resume:** `resume_from_checkpoint()` restores files, reloads state, clears
  loop/oscillation history

No chat history required — the files are the memory.

### Snapshot integrity

Verification runs three ordered stages, and each can end it:

1. **Containment** — every filename in `metadata.json` must be a safe relative
   path *and* a member of `config.STATE_FILES`. A manifest naming
   `../escape.txt` or `/etc/passwd` is refused before any file is opened, so a
   valid signature can never authorise a write outside the project.
2. **Contents** — per-file SHA-256 recomputed from disk, then the aggregate
   checksum.
3. **Signature policy** — evaluated from the persisted `signed` flag, *never*
   from the presence of the `signature` field (which is itself writable by
   whoever can write the snapshot).

`metadata.json` records the claim explicitly, so "unsigned" cannot be reached by
deleting a field:

| Field | Meaning |
|---|---|
| `signed` | `true`/`false` — whether this snapshot claims a signature |
| `key_id` | which signing key it was signed with (`ORCHESTRATOR_CHECKPOINT_KEY_ID`) |
| `signature` | HMAC-SHA256 over `checkpoint_id`, `key_id`, `created_at` and every `name=digest` pair |
| `signature_algorithm`, `signature_version` | scheme identifiers, so an unknown scheme is reported rather than guessed at |

`evaluate_integrity()` returns one of four verdicts:

| Verdict | Condition | `verify_checkpoint()` |
|---|---|---|
| `VERIFIED` | claims signed, key available, MAC matches the recomputed payload | passes |
| `UNSIGNED` | `signed=false` and no key configured | passes on checksums |
| `TAMPERED` | signature claim exists but does not hold — wrong MAC, missing/empty signature under `signed: true`, rewritten contents | **always fails** |
| `UNVERIFIABLE` | signature claim exists but cannot be checked — no key configured, or the recorded `key_id` is not the active one | fails unless `CHECKPOINT_ALLOW_UNSIGNED=1` |

Two consequences worth stating plainly. First, `UNSIGNED` passing is deliberate:
with no key configured there is no signature claim to violate, and refusing would
break restore for any deployment that has not set a key while protecting against
nobody. The row that carries signal is `UNVERIFIABLE` — a claim that cannot be
checked. Second, the MAC binds the per-file digests, so rewriting both the files
and the self-declared checksums no longer satisfies it.

`CHECKPOINT_ALLOW_UNSIGNED=1` downgrades `UNVERIFIABLE` to a warning for
snapshot stores you cannot re-sign. It never permits a `TAMPERED` snapshot.

```python
report = orch.checkpoints.evaluate_integrity("cp-001")
print(report.verdict.value, report.detail, report.key_id)
```

---

## TESTING

```bash
python3 -m pytest -q          # full suite — 1238 passed
python3 -m pytest test_derived_state.py -q
python3 -m pytest tests/ -q   # security/regression suites
ruff check .                  # lint — 0 errors (baseline pinned in pyproject.toml)
```

Shared fixtures live in `conftest.py` (`build_test_project`, `FakeLLMClient`,
`_task`, `FakeDeployRunner` plus the `fake_deploy_runner` / `disabled_deploy_runner`
/ `failing_deploy_runner` variants). Tests cover dispatch/parallelism, review
flow, DoD, decision gate, risk maintenance, loop detection, milestone
checkpoints, derived state, concurrent state IO, and the CLI surface.

The suite is **hermetic**: an autouse fixture blocks outbound TCP while
allowing loopback, so a test can never reach a real provider even if
`ANTHROPIC_API_KEY` is exported in the developer's shell. The execution channel
is off by default and `FakeDeployRunner` substitutes for the real runner, so no
test spawns a process unless it is deliberately exercising one. Never run `run`
against a live project you care about from tests — use a `tmp_path` copy.

---

## TROUBLESHOOTING

```bash
# Project won't load
python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" projects/my-project/PROJECT.yaml
orchestrator --project projects/my-project status     # prints validate() problems

# Tasks stuck in READY
orchestrator --project projects/my-project tasks      # shows unsatisfied deps

# Dispatch refused with LOOP LIMIT
orchestrator --project projects/my-project health     # inspect detections

# Pending human decisions block dispatch
orchestrator --project projects/my-project status     # lists human_decisions
# resolve via approve_decision(...) or edit DECISIONS.md status
```

See also `meta/TROUBLESHOOTING.md`.

---

## FILE LOCATIONS

```
agentic-ai-framework/
├── bin/orchestrator          # executable CLI entry point
├── pyproject.toml            # package metadata + `orchestrator` script
├── orchestrator/
│   ├── cli.py                # subcommands
│   ├── orchestrator.py       # MasterOrchestrator (dispatch/review/DoD)
│   ├── state_manager.py      # state files + derived recompute
│   ├── supervisor.py         # health, loops, risks, escalations
│   ├── checkpoint_manager.py # checkpoints
│   ├── llm_client.py         # stdlib LLM HTTP client
│   ├── prompt_builder.py     # prompts from framework/*.md
│   ├── deploy_runner.py      # allowlisted execution chokepoint (data.deploy)
│   ├── path_policy.py        # shared path-containment policy
│   ├── context_monitor.py    # utilization accounting
│   └── agents/               # BaseAgent, LLMAgent, 9 specialists
├── framework/                # architecture specs 00–10, AGENT_PROMPTS/, TEMPLATES/
├── projects/kid-robot-face/  # full example project
├── project-templates/        # copy-paste state-file templates
├── meta/                     # GETTING_STARTED, WORKFLOW, TROUBLESHOOTING
├── test_*.py                 # pytest suite (root)
└── tests/                    # security + regression suites
```
