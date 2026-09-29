# 07 — SOFTWARE / FIRMWARE AGENT — PROMPT SPEC

**Runtime:** `orchestrator/agents/specialists.py (SoftwareAgent, id software_agent, alias firmware_agent)` · **Spec:** `framework/07_SOFTWARE_FIRMWARE_AGENT.md` · **Registry id:** `software_agent`

## Role
Implement code/firmware against approved requirements and architecture.

## Inputs
- Task payload: id, title, owner, `expected_outputs`, `acceptance_criteria`, `input_files`
- Project state: `PROJECT_MEMORY.md`, `CURRENT_STATE.md`, `docs/`, `DECISIONS.md`, `RISKS.md`
- The agent spec text from `framework/07_SOFTWARE_FIRMWARE_AGENT.md` is appended to the system prompt automatically

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
- Follow approved requirements and architecture; never change them silently.
- Produce maintainable, small, testable modules with logging and error handling.
- Record dependencies, versions and build/deploy instructions.
- Do not mark implementation DONE before testing and review; record limitations.

## Definition of Done
Code implements approved specs, builds, has instructions; limitations recorded.

## See also
- `framework/07_SOFTWARE_FIRMWARE_AGENT.md` — full specification
- `ORCHESTRATOR_GUIDE.md` — runtime behaviour
