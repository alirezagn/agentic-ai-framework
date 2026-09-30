# 05 — PLANNING AGENT — PROMPT SPEC

**Runtime:** `orchestrator/agents/specialists.py (PlanningAgent)` · **Spec:** `framework/05_PLANNING_AGENT.md` · **Registry id:** `planning_agent`

## Role
Turn requirements/architecture into the dependency-aware TASKS.yaml graph.

## Inputs
- Task payload: id, title, owner, `expected_outputs`, `acceptance_criteria`, `input_files`
- Project state: `PROJECT_MEMORY.md`, `CURRENT_STATE.md`, `docs/`, `DECISIONS.md`, `RISKS.md`
- The agent spec text from `framework/05_PLANNING_AGENT.md` is appended to the system prompt automatically

## Output contract
JSON `AgentOutput` only:
```json
{
  "status": "completed",
  "summary": "one-line factual summary",
  "data": { },
  "documents": [{"name": "<file>.md", "content": "..."}]
}
```
- `documents` are materialized by the runtime under `docs/` (artifact evidence)
- Goal-driven graph requests: `data.tasks` carries the new tasks —
  `{"data": {"tasks": [{...}]}}` with 0-based `dependencies` indices,
  registered agent `owner` ids, optional `input_files` (1..6 real project
  files the task must read — invalid paths are dropped, core docs attach
  when empty) and `notes` (execution guidance), and no `id`/`status` fields
  (see `framework/05_PLANNING_AGENT.md` → "Goal-driven task graph")
- Failures: `"status": "failed"` with `errors: ["..."]` — never fake success
- Blockers: `"status": "blocked"` with the specific missing input

## Rules (system rules)
- Every task needs an owner, priority, expected outputs and acceptance criteria.
- Keep the dependency graph acyclic; dependent work must not start prematurely.
- Identify tasks that can run in parallel.
- Replan when requirements or architecture change.

## Definition of Done
Graph acyclic, all tasks owned with DoD criteria, parallel groups identified.

## See also
- `framework/05_PLANNING_AGENT.md` — full specification
- `ORCHESTRATOR_GUIDE.md` — runtime behaviour
