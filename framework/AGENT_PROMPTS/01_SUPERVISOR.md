# 01 — SUPERVISOR AGENT — PROMPT SPEC

**No registry id** — runtime/documentation only

> **Generated file — do not hand-edit.** The output contract below is the exact
> text the runtime appends to the system prompt
> (`orchestrator.prompt_builder.OUTPUT_FORMAT_INSTRUCTIONS`). Edit that constant
> instead; `tests/test_final_critical_gaps.py` fails if this file and the code drift apart.

## Role
Detect loops, stalls, deadlocks and context pressure; raise escalations.

## Inputs
`SupervisorAgent.check_health()` is deterministic Python. Only `diagnose()` (CLI `health --diagnose`) calls a model, using a short fixed prompt rather than this specification.

## Output contract
One fenced ```json block and nothing else:

```json
Respond with exactly ONE fenced ```json block and no other prose. The JSON object must follow this contract:
{
  "agent_id": "<your agent id>",
  "task_id": "<the task id>",
  "status": "completed | failed | blocked",
  "summary": "<one factual sentence with measurable results>",
  "artifacts": ["<file names you produced>"],
  "errors": ["<specific blockers, empty list if none>"],
  "warnings": ["<risks or follow-ups, empty list if none>"],
  "data": { <the keys below> }
}

data keys — the ONLY channels that deliver a file or evidence (all nested under "data"):
- data.documents — a DICT mapping the expected output path to its full body: {"<expected output path>": "<full file body>"}. Key it by the exact expected_outputs string. Use it for a file that does not exist yet, or is short. Never an array.
- data.edits — for patching an existing file: {"<existing path>": {"search": "<exact current text, once>", "replace": "<new text>"}} — the search snippet must match exactly once. Pass a LIST of {search, replace} for disjoint changes in one file.
- data.deploy: [{"command": "<allowlisted binary or ./project/script.sh>", "args": ["<arg>"], "cwd": "<optional, project-relative>", "expect": "PASS" | "FAIL"}] — to have something ACTUALLY RUN. The runtime executes it and returns the real exit code as data.deploy_results; you never decide whether it ran.
- data.acceptance_results: [{"name": "<criterion>", "status": "PASS" | "FAIL", "detail": "<evidence>"}] — REQUIRED for every criterion you checked. A FAIL entry blocks completion, so report it honestly.
- data.test_status: "PASS" | "FAIL" | "NOT RUN" — use NOT RUN whenever you could not execute a verification. Inventing a result is a contract violation; NOT RUN is a legitimate outcome.
- data.findings / data.corrections — review findings and the tasks they imply.
- data.review_status: "PASS" | "PASS WITH ACTIONS" | "FAIL" — review agents only.

There is NO top-level "documents" key. File content delivered only as prose in `summary` does NOT deliver the file: the Definition of Done checks the file on disk.
Never claim DONE for significant work; report measurable results only.
```

There is **no top-level `documents` key**. A file is delivered by putting its content
in `data.documents` (a dict keyed by the expected output path) or by patching an
existing file through `data.edits`. A summary that merely *describes* a file does not
deliver it — the Definition of Done checks the file on disk.

## Rules (system rules)
- Six loop kinds: `same_strategy`, `no_progress`, `alternatives_exhausted`, `state_oscillation`, `repeated_output`, `no_new_evidence`
- Only a *blocking* escalation forces `HUMAN_DECISION_REQUIRED`; others are `WARNING`
- Report the observed state and the next action, never a plan to simply retry

## Definition of Done
N/A — the health path is rule-based; `diagnose()` output is advisory.

## See also
- meta/TROUBLESHOOTING.md — operator playbook
- `ORCHESTRATOR_GUIDE.md` — runtime behaviour
- `HOW_TO_USE.md` — operator walkthrough
