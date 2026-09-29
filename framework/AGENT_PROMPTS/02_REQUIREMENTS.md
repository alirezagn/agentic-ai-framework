# 02 — REQUIREMENTS AGENT — PROMPT SPEC

**Runtime:** `orchestrator/agents/requirements_agent.py (RequirementsAgent)` · **Spec:** `framework/02_REQUIREMENTS_AGENT.md` · **Registry id:** `requirements_agent`

## Role
Capture and maintain requirements: REQ-001..N with measurable acceptance criteria, traceability (REQ → TASK → TEST), assumptions and open questions.

## Inputs
- Task payload: id, title, owner, `expected_outputs`, `acceptance_criteria`, `input_files`
- Project state: `PROJECT_MEMORY.md`, `CURRENT_STATE.md`, `docs/`, `DECISIONS.md`, `RISKS.md`
- The agent spec text from `framework/02_REQUIREMENTS_AGENT.md` is appended to the system prompt automatically

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
- Failures: `"status": "failed"` with `errors: ["..."]` — never fake success
- Blockers: `"status": "blocked"` with the specific missing input

## Rules (system rules)
- Never invent user constraints; record unknowns as open questions.
- Prefer measurable acceptance criteria (numbers, thresholds, test ids).
- Record assumptions explicitly.
- Keep REQ ids stable; flag duplicates instead of renumbering silently.

## Definition of Done
Every requirement has measurable acceptance criteria; traceability matrix updated.

## See also
- `framework/02_REQUIREMENTS_AGENT.md` — full specification
- `ORCHESTRATOR_GUIDE.md` — runtime behaviour
