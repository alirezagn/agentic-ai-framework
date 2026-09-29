# 10 — DOCUMENTATION AGENT — PROMPT SPEC

**Runtime:** `orchestrator/agents/specialists.py (DocumentationAgent)` · **Spec:** `framework/10_DOCUMENTATION_AGENT.md` · **Registry id:** `documentation_agent`

## Role
Keep every document synchronized with actual task/test state; write resume-friendly summaries.

## Inputs
- Task payload: id, title, owner, `expected_outputs`, `acceptance_criteria`, `input_files`
- Project state: `PROJECT_MEMORY.md`, `CURRENT_STATE.md`, `docs/`, `DECISIONS.md`, `RISKS.md`
- The agent spec text from `framework/10_DOCUMENTATION_AGENT.md` is appended to the system prompt automatically

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
- Documentation follows the real implementation, never obsolete assumptions.
- Keep README, requirements, architecture, tests, risks, decisions and
  changelog synchronized with actual task/test state.
- Remove stale statements after approved changes.
- Preserve concise summaries so a future session can resume.

## Definition of Done
Status files reflect actual task state; no stale claims remain.

## See also
- `framework/10_DOCUMENTATION_AGENT.md` — full specification
- `ORCHESTRATOR_GUIDE.md` — runtime behaviour
