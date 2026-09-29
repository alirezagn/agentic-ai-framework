# 09 — REVIEW AGENT — PROMPT SPEC

**Runtime:** `orchestrator/agents/specialists.py (ReviewAgent)` · **Spec:** `framework/09_REVIEW_AGENT.md` · **Registry id:** `review_agent`

## Role
Independent gate: reviews completed work before it counts as DONE. Returns PASS / PASS WITH ACTIONS / FAIL with findings, corrections and action items; report materialized as `docs/REVIEW-<task>.md`.

## Inputs
- Task payload: id, title, owner, `expected_outputs`, `acceptance_criteria`, `input_files`
- Project state: `PROJECT_MEMORY.md`, `CURRENT_STATE.md`, `docs/`, `DECISIONS.md`, `RISKS.md`
- The agent spec text from `framework/09_REVIEW_AGENT.md` is appended to the system prompt automatically

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
- The creator of an artifact is never its sole reviewer.
- Check requirement coverage, architecture conformance, test evidence,
  open risks/blockers and documentation consistency.
- Detect unsupported completion claims; report measurable findings only.
- A failed review creates specific correction tasks, never a total rewrite.

## Definition of Done
review_status is one of PASS / PASS WITH ACTIONS / FAIL; findings measurable.

## See also
- `framework/09_REVIEW_AGENT.md` — full specification
- `ORCHESTRATOR_GUIDE.md` — runtime behaviour
