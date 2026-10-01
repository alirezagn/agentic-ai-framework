# HOW TO USE — Orchestrator Tool

Practical walkthrough for running the agentic-ai-framework orchestrator.
For architecture and API details see [`ORCHESTRATOR_GUIDE.md`](ORCHESTRATOR_GUIDE.md);
for the session bootstrap prompt see
[`framework/20_DEFAULT_PROJECT_START_PROMPT.md`](framework/20_DEFAULT_PROJECT_START_PROMPT.md).

---

## 1. Install / run

```bash
# Option A — install the `orchestrator` command
pip install -e .

# Option B — use the repo entry point (no install)
chmod +x bin/orchestrator
./bin/orchestrator --version        # orchestrator 2.0.0

# Option C — module form
python3 -m orchestrator.cli --version
```

Only dependency: `PyYAML` (see `pyproject.toml` / `orchestrator/requirements.txt`).

---

## 2. Create a project

```bash
./bin/orchestrator init my-project \
  --goal "Build a voice-reactive robot face for a 5-year-old"
```

Creates `projects/my-project/` with all state files:

```
PROJECT.yaml  TASKS.yaml  PROJECT_MEMORY.md  CURRENT_STATE.md
DECISIONS.md  RISKS.md  CHANGELOG.md  docs/
```

The scaffold passes `validate()` immediately, includes empty
`constraints` / `budget` / `resources` blocks in `PROJECT.yaml` (fill them
in — spec step "capture goal, constraints, budget, resources"), writes
the `cp-000-init` baseline checkpoint, and **generates the task graph**:
the planning agent turns `--goal` into a dependency-aware `TASKS.yaml`
when an LLM backend is configured, otherwise a deterministic 5-task
starter skeleton (requirements → architecture → implementation → test →
documentation) is written so the project is runnable immediately. Options:

| Flag | Effect |
|---|---|
| `--dest DIR` | Parent directory (default `projects/`) |
| `--goal TEXT` | One-sentence goal written into `PROJECT_MEMORY.md` and used for planning; `init`/`plan` echo it as `Goal:` and warn if it looks like a placeholder (`…`) |
| `--force` | Overwrite state files of an existing directory |
| `--no-plan` | Skip LLM planning; write the starter skeleton tasks instead |

Alternative: copy the 7 templates from `project-templates/` by hand
(see `project-templates/NEW_PROJECT_CHECKLIST.md`).

### Adopt an existing project (started without this tool)

State files *are* the memory — a project begun manually or with another
tool can be onboarded without any chat history:

```bash
# The directory already exists → --force is required. It only ADDS the
# 7 state files + docs/README.md, but OVERWRITES those exact files if
# present — back up DECISIONS.md / RISKS.md / docs/README.md first.
./bin/orchestrator init my-app --dest /path/to --force \
  --goal "One-sentence project goal"
```

Then capture reality:

1. **`PROJECT_MEMORY.md`** — goal, current status, key decisions, next
   steps (import/summarize the old tool's notes). This is what a new
   session resumes from.
2. **`CURRENT_STATE.md`** — phase, health, last updated.
3. **Copy existing artifacts into `docs/`** — the Definition of Done
   resolves `expected_outputs` under `docs/` only. The planning agent
   reads the top `docs/*.md` files as context when generating the graph.
4. **Backlog in `TASKS.yaml`** — `init --force` already generated a
   goal-derived graph (or a starter skeleton). Reconcile it with reality:
   finished work → `DONE`, current work → `READY`/`IN_PROGRESS`,
   dependencies wired. Verify read-only first:

```bash
./bin/orchestrator --project /path/to/my-app tasks    # graph + critical path
./bin/orchestrator --project /path/to/my-app status   # progress/health
./bin/orchestrator --project /path/to/my-app health   # exit 0/3/4
```

5. **Test on a copy before dispatching** — copy the directory to `/tmp`
   (or use the pytest fixture `build_test_project(tmp_path)`) and run
   there first; `run` mutates state. When re-creating a copy with
   `rsync`, exclude previous deliverables (e.g. `--exclude
   gap_analysis.md`): a stale report left in the copy can silently
   satisfy a task's `expected_outputs` DoD check and get loaded as
   agent input.
6. **Continue:**

```bash
./bin/orchestrator --project /path/to/my-app run --max-tasks 5
./bin/orchestrator --project /path/to/my-app checkpoint save cp-adapted
```

Alternative: leave the old project untouched and keep a **sidecar** state
dir under `projects/` (`init my-app --dest projects`), with
`PROJECT_MEMORY.md` pointing to where the real code lives.

---

## 3. Define work

The graph is **generated, not hand-written**: `init` seeds it (LLM plan or
starter skeleton), and you can (re)generate it any time:

```bash
./bin/orchestrator --project projects/my-project plan            # from PROJECT_MEMORY.md goal
./bin/orchestrator --project projects/my-project plan --force    # replace the graph
./bin/orchestrator --project projects/my-project plan --goal "..." --max-tasks 6
```

Without an LLM backend, `plan` writes the starter skeleton (and refuses to
touch a non-empty graph), and `run` on an empty graph prints a hint instead
of dispatching. Then **refine** the generated `TASKS.yaml` — every task
needs id, owner, status, priority, dependencies, `expected_outputs`,
`acceptance_criteria`, and optionally `review: {required: true}`,
`milestone`, and `requirement_ids` (REQ ids from `docs/REQUIREMENTS.md`;
the Definition of Done checks traceability whenever the field is present):

```yaml
tasks:
  - id: TASK-001
    title: Capture requirements
    owner: requirements_agent      # one of the 10 registered agents
    status: TODO                   # TODO -> READY -> IN_PROGRESS -> DONE
    priority: CRITICAL             # (WAITING = parked on a PROPOSED_CHANGE)
    dependencies: []
    requirement_ids: [REQ-001]
    expected_outputs: [docs/REQUIREMENTS.md]
    input_files: [docs/PRD.md, docs/ARCHITECTURE.md]
    acceptance_criteria:
      - At least 10 REQ entries with measurable criteria
    review:
      required: true
      status: NOT_STARTED
```

Tasks can also be created programmatically — `orch.state.append_task(...)`
allocates `TASK-NNN` and validates owner/dependencies — and planner/reviewer
`proposed_tasks` output is ingested automatically after dispatch.

Inspect the graph at any time:

```bash
./bin/orchestrator --project projects/my-project tasks
```

```
ID         STATUS   OWNER                    PRI       DEPS               READY
TASK-001   DONE     requirements_agent       CRITICAL  -                  yes
...
Ready now: TASK-002
Critical path: TASK-001 -> TASK-002 -> TASK-003
```

---

## 4. Run tasks

```bash
# one bounded cycle (all READY tasks, sequential)
./bin/orchestrator --project projects/my-project run --max-tasks 10

# run until it stops making progress; `|| break` exits on the first
# problem (3 = loop limit, 4 = human decision required)
for i in $(seq 1 11); do
  ./bin/orchestrator --project projects/my-project run --max-tasks 10 || break
done

# overlap independent tasks (thread pool; state writes stay locked)
./bin/orchestrator --project projects/my-project run --max-concurrent 3

# single task (REVIEW tasks route through the review flow)
./bin/orchestrator --project projects/my-project run --task TASK-002
```

When a run loop stops, its `hint:` line names the exact recovery command:

```bash
./bin/orchestrator --project projects/my-project retry TASK-003 --reason "inputs fixed"
```

What happens per task:

1. **Gate** — oscillation check → loop limits → dependencies → pending
   `PROPOSED_CHANGE` decisions → agent resolved → `IN_PROGRESS`
2. **Execute** — the owning agent runs (LLM-backed or custom `BaseAgent`)
3. **Finalize** — status written, context utilization updated, failures get a
   `RISK-NNN` entry, loop signals recorded, **Definition of Done** checked
   (criteria + artifacts in `docs/` + review), derived state recomputed,
   auto/milestone checkpoint taken

Review outcome: **PASS** → `DONE` · **PASS WITH ACTIONS** → `DONE WITH
ACCEPTED LIMITATION` + follow-up tasks · **FAIL** → `FAILED` + follow-ups.

---

## 5. Monitor health

```bash
./bin/orchestrator --project projects/my-project status    # summary + health
./bin/orchestrator --project projects/my-project health    # exit: 0 ok, 3 stalled/blocked, 4 human decision
./bin/orchestrator --project projects/my-project health --diagnose   # + LLM/rules diagnosis
./bin/orchestrator --project projects/my-project phase show          # current vs derived phase
./bin/orchestrator --project projects/my-project phase set ARCHITECTURE   # manual override (forward-only derive still guards regressions)
```

Health states: `HEALTHY`, `WARNING`, `STALLED`, `BLOCKED`, `RECOVERY`
(a new strategy is in flight), `HUMAN_DECISION_REQUIRED` (blocking
escalation — loops, deadlocks, decision conflicts). Advisory problems
(context pressure, stale blocks) stay `WARNING`. Lifecycle phase advances
automatically as work completes (requirements → architecture → … → release);
`phase show` compares the stored phase with the derived one.

Loop guards that can refuse dispatch (CLI exit code 3):

| Kind | Default trigger |
|---|---|
| `same_strategy` | 3 attempts without a strategy change |
| `no_progress` | 5 cycles with unchanged state |
| `alternatives_exhausted` | 2 alternative strategies used up |
| `state_oscillation` | project state flips READY↔BLOCKED (4 changes) |
| `repeated_output` | 3 substantially identical outputs |
| `no_new_evidence` | 3 dispatches with no new artifacts or decisions |

Recovery = fix the task's inputs, then `retry TASK-003 --reason "..."` (clears
the loop counters and puts the task back to READY), or change strategy
materially (`data.strategy_changed`), replan, or escalate to a human — never
retry identically.

---

## 6. Handle decisions and risks

An agent proposing a design change writes a `PROPOSED_CHANGE` entry into
`DECISIONS.md`; the affected non-terminal tasks park in `WAITING` until a
human responds:

```python
from orchestrator.orchestrator import MasterOrchestrator
orch = MasterOrchestrator("projects/my-project")

for dec in orch.state.pending_proposed_changes():
    orch.approve_decision(dec["id"], approved=True)    # lift the gate
    # or approved=False to keep it closed (both return WAITING tasks to READY/BLOCKED)
```

Risks: the supervisor appends `RISK-NNN` sections to `RISKS.md` whenever a
task fails (deduplicated) and snapshots the state as `cp-risk-NNN`. Add
design risks manually in the same format.

---

## 7. Checkpoints and resume

```bash
./bin/orchestrator --project projects/my-project checkpoint save cp-phase1 --notes "requirements done"
./bin/orchestrator --project projects/my-project checkpoint list
./bin/orchestrator --project projects/my-project checkpoint restore cp-phase1
```

- Checkpoints are automatic after dispatch/review (disable with
  `auto_checkpoint=False`), and milestone tasks create `cp-milestone-<slug>`.
- Automatic checkpoints also fire on lifecycle phase advances
  (`cp-phase-<name>`), on recorded failure risks (`cp-risk-NNN`), and at
  context compaction (`cp-auto-NNN`); `orchestrator init` writes the
  `cp-000-init` baseline.
- Restore verifies SHA-256 checksums first; with `CHECKPOINT_SIGNING_KEY` set
  it also verifies the HMAC signature.
- Resume needs **no chat history** — the files are the memory; loop history is
  cleared on restore.

---

## 8. Configure the LLM backend

The CLI auto-loads `./.env` at startup (setdefault semantics — real
environment variables always win; skipped while the test suite runs).
This repo is pre-configured in `.env` for the local Ollama server:

```dotenv
ORCHESTRATOR_LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://192.168.0.200:11434
ORCHESTRATOR_LLM_MODEL=gemma4:12b
```

Equivalent shell exports (override the file):

```bash
export OLLAMA_BASE_URL=http://192.168.0.200:11434   # + /v1 for OpenAI-compat mode
export ORCHESTRATOR_LLM_PROVIDER=ollama        # or anthropic / openrouter
export ORCHESTRATOR_LLM_MODEL=gemma4:12b
# export ANTHROPIC_API_KEY=...   /   OPENROUTER_API_KEY=...
export ORCHESTRATOR_CONTEXT_WINDOW_TOKENS=128000
```

Copy `.env.example` to `.env` for another machine (`.env` is gitignored).
No SDK — the client is stdlib HTTP with an injectable transport for tests.

---

## 9. Logging and exit codes

```bash
./bin/orchestrator -v --project projects/my-project run     # debug logs
./bin/orchestrator -q --project projects/my-project status  # errors only
export ORCHESTRATOR_LOG_LEVEL=WARNING                       # default INFO
```

| Exit | Meaning |
|---|---|
| 0 | success |
| 1 | no command given |
| 2 | usage / state error / missing `--project` |
| 3 | loop limit refused, or health STALLED/BLOCKED |
| 4 | `HUMAN_DECISION_REQUIRED` (pending decisions/escalations) |

---

## 10. Python API (quick slice)

```python
from orchestrator.orchestrator import MasterOrchestrator

orch = MasterOrchestrator("projects/my-project")
assert orch.state.validate() == []                 # state is sound

results = orch.run_until_stalled(max_cycles=10, max_concurrent=3)
report = orch.sync_health()                        # persists PROJECT.yaml.health
print(report.render())

orch.create_checkpoint("cp-001", notes="phase 1 done")
orch.resume_from_checkpoint("cp-001")
```

Custom agent:

```python
from orchestrator.agents.base_agent import BaseAgent, AgentOutput

class MyAgent(BaseAgent):
    AGENT_ID = "requirements_agent"
    def execute(self, payload: dict) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.completed(task_id, "done", data={...})

orch.register_agent(MyAgent(state_manager=orch.state))   # pinned singleton
```

---

## 11. Typical session loop

```bash
./bin/orchestrator init my-project --goal "..."     # once
$EDITOR projects/my-project/TASKS.yaml              # define work
# start LLM session with framework/20_DEFAULT_PROJECT_START_PROMPT.md

./bin/orchestrator --project projects/my-project run --max-concurrent 3
./bin/orchestrator --project projects/my-project health   # fix what it flags
./bin/orchestrator --project projects/my-project checkpoint save cp-auto
# resolve pending DEC-NNN decisions, then repeat until milestones complete
```

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| `error: --project is required` | pass `--project projects/<name>` before the subcommand |
| `ERROR: State file not found .../TASKS.yaml` | the workspace was wiped (`rm -rf` + rsync) without re-init — run the `init` line printed in the hint (step 2 of the PoC runbook) |
| generated tasks look unrelated to my goal | check the `Goal:` line echoed by `init`/`plan` — a copied placeholder (`…`) persists in `PROJECT_MEMORY.md` and steers the planner generic; re-run `init --force --goal "your real goal"` |
| tasks stay `READY` | `tasks` shows unsatisfied deps; finish predecessors |
| dependency deadlock (exit 4) | `waive TASK-004 --dep TASK-003 --reason "..."` to drop the edge |
| task stuck in `WAITING` | a pending `PROPOSED_CHANGE` affects it — approve/reject the decision |
| phase looks wrong | `phase show` (stored vs derived); `phase set <NAME>` to override |
| `LOOP LIMIT` (exit 3) | fix the task's inputs, then `retry TASK-003 --reason "..."` to reset its loop counters; `run` prints the exact command in its hint |
| `retry … is DONE; retry applies to active tasks only` | for finished tasks use `reopen TASK-003 --reason "..."` — never `sed` TASKS.yaml (line numbers shift when the orchestrator rewrites it); `FAILED` tasks need only `retry` (no sed) |
| task `FAILED` (agent/validation error) | read the printed error, fix the inputs or model output, then `retry TASK-005 --reason "..."` — the same hint appears in `run` output |
| memory/context grows forever | compaction folds MEMORY.md and resets utilization at the 70% threshold |
| exit 4 | pending `PROPOSED_CHANGE` → `approve_decision(...)` |
| `DoD unmet: ...` | materialize `expected_outputs` into `docs/`, fix review findings — the first rejection triggers **one automatic repair call**; if it still fails, `retry TASK-003 --reason "use data.edits on <file>"` (the reason reaches the next prompt) |
| `DoD unmet: delivered docs/X shares no line with existing Y` | the model returned prose metadata instead of editing — the auto-repair call already fed this back once; retry with a more specific `--reason` if it repeated |
| `docs/*.log` or docs "Verification Results" claim tests ran | agents cannot execute anything — claims are unverified until YOU run `ctest`; treat as claims |
| `LLM backend unavailable` | set provider env vars (§8) |
| state corrupted | `checkpoint restore cp-...` |

More: `meta/TROUBLESHOOTING.md`. Verify your install with:

```bash
python3 -m pytest -q      # 390 passed
```
