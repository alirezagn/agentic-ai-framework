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
| `orchestrator/context_monitor.py` | Measured context-utilization accounting |
| `orchestrator/cli.py` | Subcommands: `init`, `status`, `tasks`, `run`, `plan`, `health`, `agents`, `checkpoint`, `phase`, `waive`, `retry` |

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
| `run [--task ID] [--max-tasks N] [--max-concurrent N]` | Dispatch READY tasks (or one task); an empty graph is generated on the fly when an LLM backend is configured |
| `plan [--goal TEXT] [--force] [--max-tasks N]` | Generate the task graph from the goal via the planning agent; without an LLM backend writes the starter skeleton (or errors when the graph already has tasks); `--force` replaces an existing graph only after a successful plan |
| `health [--diagnose]` | Supervisor health check (writes `PROJECT.yaml` health block); `--diagnose` adds an LLM diagnosis when a provider is configured, rules-only otherwise |
| `phase show\|set [PHASE]` | Show the current/derived lifecycle phase, or set it explicitly (validated against the phase vocabulary; forward moves checkpoint as `cp-phase-<name>`) |
| `waive TASK --dep ID [--reason TEXT]` | Human unblock: drop one dependency edge (deadlock relief) and record it in `CHANGELOG.md` |
| `retry TASK [--reason TEXT]` | Human recovery: clear a stalled/loop-limited task's counters (`same_strategy`, `no_progress`, evidence stalls), put it back to READY when its dependencies are met, and record it in `CHANGELOG.md` |
| `agents` | List registered specialist agents |
| `checkpoint save\|list\|restore` | Checkpoint management (`--checkpoint ID`, `--notes TEXT`) |

**Exit codes:** `0` success · `1` no command · `2` usage / state error ·
`3` loop limit or STALLED/BLOCKED health · `4` `HUMAN_DECISION_REQUIRED`

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
| `docs/` | Materialized agent artifacts (`REQUIREMENTS-*.md`, `REVIEW-*.md`, …) | agents (`_materialize_artifacts`) |

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
   when `max_concurrent > 1`
3. **Finalize phase (locked):** context-utilization update → proposed-change
   recording → status finalization (`DONE`/`BLOCKED`/`FAILED`) → risk recording
   for failures → loop signals + fingerprint → DoD check → derived-state
   recompute → auto/milestone checkpoint

### Review flow

Tasks with `review: {required: true}` enter `READY_FOR_REVIEW`. Before the next
dispatch batch, `_run_pending_reviews()` routes them to `dispatch_review()`:

- reviewer is always `review_agent`, runs with `materialize=False`
- writes `docs/REVIEW-<task>.md`
- **PASS** → task `DONE`
- **PASS WITH ACTIONS** → `DONE WITH ACCEPTED LIMITATION` + follow-up tasks
- **FAIL** → task `FAILED` + follow-up tasks

### Definition of Done

`definition_of_done(task, output)` requires: DoD criteria present, all
`expected_outputs` materialized on disk, and review passed (when required).
Unmet → task `FAILED` with `DoD unmet: …`.

### Dependency gating

A task is READY only when every dependency is in a satisfied status
(`DONE`, `DONE WITH ACCEPTED LIMITATION`, `CANCELLED`). `refresh_ready_states()`
promotes `TODO`/`BLOCKED` tasks whose dependencies just became satisfied.

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
| `ORCHESTRATOR_LLM_MAX_TOKENS` | Output-token budget per completion (default `4096`; raise for data-heavy replies — truncated JSON fails validation with an explicit "looks truncated" error) |
| `ORCHESTRATOR_LLM_NUM_CTX` | Ollama context window for `num_ctx` (default `16384`; the server default of 4096 silently caps prompt+output and truncates JSON — native `/api/chat` only) |
| `ORCHESTRATOR_LLM_TIMEOUT` | Per-request timeout in seconds (default `120`; raise for slow/busy servers) |
| `ANTHROPIC_API_KEY` | Enables `anthropic` |
| `OPENROUTER_API_KEY` | Enables `openrouter` |
| `OLLAMA_BASE_URL` | Project default: `http://192.168.0.200:11434` (append `/v1` for OpenAI-compat) |
| `ORCHESTRATOR_CONTEXT_WINDOW_TOKENS` | Context window for utilization math |
| `ORCHESTRATOR_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default `INFO`; `-v` / `-q` override) |
| `CHECKPOINT_SIGNING_KEY` | Optional HMAC-SHA256 key; signed checkpoints fail `verify_checkpoint()` if files *and* metadata are rewritten |

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
- **Resume:** `resume_from_checkpoint()` restores files, reloads state, clears
  loop/oscillation history

No chat history required — the files are the memory.

---

## TESTING

```bash
python3 -m pytest -q          # full suite
python3 -m pytest test_derived_state.py -q
```

Shared fixtures live in `conftest.py` (`build_test_project`, `FakeLLMClient`,
`_task`). Tests cover dispatch/parallelism, review flow, DoD, decision gate,
risk maintenance, loop detection, milestone checkpoints, derived state and the
CLI surface. Never run `run` against a live project you care about from tests —
use a `tmp_path` copy.

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
│   ├── context_monitor.py    # utilization accounting
│   └── agents/               # BaseAgent, LLMAgent, 9 specialists
├── framework/                # architecture specs 00–10, AGENT_PROMPTS/, TEMPLATES/
├── projects/kid-robot-face/  # full example project
├── project-templates/        # copy-paste state-file templates
├── meta/                     # GETTING_STARTED, WORKFLOW, TROUBLESHOOTING
└── test_*.py                 # pytest suite
```
