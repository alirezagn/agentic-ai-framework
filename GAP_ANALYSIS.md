# Gap Analysis — Implementation vs. Architecture Documents

> **STATUS (2026-09-30): historical.** Remediation **T1–T20** addressed findings
> 1–20 (see the remediation order below; commit `e9d3e71` and later). The
> *current* architecture gaps live in **`review_gaps.md`**. This document is kept
> as the original pre-remediation baseline.

**Date:** 2026-09-30
**Scope:** `orchestrator/` runtime + `test_orchestrator_pipeline.py` compared against
`framework/00–10`, `README.md`, `ORCHESTRATOR_GUIDE.md`, `QUICK_REFERENCE.md`,
`IMPLEMENTATION_ROADMAP.md`, `meta/GETTING_STARTED.md`, `DEPLOYMENT_SUMMARY.md`,
`project-templates/NEW_PROJECT_CHECKLIST.md`.

---

## Verdict

The runtime is a solid **state engine** (YAML state management, checkpoints, loop and
deadlock detection, dependency gating, CLI) but implements roughly **30% of the documented
architecture**. The orchestration *policy* layer — LLM-backed specialist agents, parallel
execution, decision/risk control, artifact production, context accounting — is absent.
Documentation also still describes an older architecture (v1.1) that no longer matches the
code (v2.0).

| Spec area | Status |
|---|---|
| State files as source of truth | Implemented |
| Checkpoints / resume | Implemented (strongest area) |
| Loop + deadlock detection | Implemented (partial) |
| Dependency gating | Implemented |
| 10 specialist agents | **1 of 10** |
| LLM / prompt-driven agents | **Missing** |
| Parallel execution | **Missing** (core principle) |
| Decision & risk control | **Missing** |
| Artifact production (REQ→TASK→TEST) | **Missing** |
| Context utilization accounting | **Missing** |
| CLI vs. documented commands | 2 commands missing |

---

## P0 — Critical

### 1. No LLM/agent execution backend — the "autonomous agents" do not exist

**Spec:** `framework/00_MASTER_ORCHESTRATOR.md:7-33`, `IMPLEMENTATION_ROADMAP.md:21-36`,
`IMPLEMENTATION_ROADMAP.md:94-118` (Approach 2: load agent spec → build prompt → call model → parse structured output).

**Reality:** The only agent is a regex/Markdown parser
(`orchestrator/agents/requirements_agent.py`). There is no HTTP client, no prompt builder,
and no spec loading. `config.FRAMEWORK_SPECS_DIR` (`orchestrator/config.py:275`) is declared
and never used; `SystemKey` entries for `anthropic_api_key` / `ollama_base_url`
(`orchestrator/config.py:82-107`) are never consumed.

**Consequence:** `BaseAgent.extract_json_block` and `BaseAgent.parse_structured_output`
(`orchestrator/agents/base_agent.py:270-320`) are dead code with zero test coverage.

### 2. Parallel execution is not implemented

**Spec:** core principle #2 — `README.md:84`, `QUICK_REFERENCE.md:22`,
`framework/00_MASTER_ORCHESTRATOR.md:50-61`, `GETTING_STARTED.md:193-202` (Pattern 1).

**Reality:** `MasterOrchestrator.run_cycle` (`orchestrator/orchestrator.py:257-310`)
dispatches strictly sequentially. `grep -rn "Thread|concurrent|asyncio"` over
`orchestrator.py`, `supervisor.py`, `cli.py` returns nothing. The asyncio parallel engine
exists only in the **superseded** `orchestrator/task_executor.py:6,115-125`, which the CLI
never invokes.

### 3. 9 of 10 specialist agents are missing

**Spec:** `framework/03_RESEARCH_AGENT.md` … `framework/10_DOCUMENTATION_AGENT.md`, and the
default agent set in `framework/00_MASTER_ORCHESTRATOR.md:20-33`.

**Reality:** only `requirements_agent` is implemented
(`orchestrator/agents/requirements_agent.py`).

**Consequence:** the example project's `TASK-003` (`owner: architecture_agent`) cannot run —
`resolve_agent` raises `MissingAgentError` (`orchestrator/orchestrator.py:113-124`). The
15-requirement example project is effectively a one-agent system.

### 4. Independent review / Definition of Done is unenforced

**Spec:** `README.md:144-158` (7 DoD criteria), `framework/00_MASTER_ORCHESTRATOR.md:159-171`
("No major task may approve itself"), `GETTING_STARTED.md:238-245`.

**Reality:** `dispatch` checks exactly one flag, `review.required`
(`orchestrator/orchestrator.py:163-176`). Acceptance criteria, test evidence and documentation
synchronisation are never evaluated. `complete_review`
(`orchestrator/orchestrator.py:210-233`) is called **only from the test suite** — with no
review agent, nothing in production can move a `REVIEW` task to `DONE`.

### 5. Agents produce no artifacts

**Spec:** `framework/02_REQUIREMENTS_AGENT.md:48-55` (output contract: `REQUIREMENTS.md`,
traceability matrix, assumptions, open questions).

**Reality:** the requirements agent returns data in memory only; there is no `write_text` /
`save_text_file` call in `orchestrator/agents/requirements_agent.py`. `expected_outputs` in
TASKS.yaml are echoed into the summary string but never materialized, so the
REQ → TASK → TEST chain exists only in memory and is lost when the process exits.

---

## P1 — High

### 6. Context utilization is never measured

**Spec:** `framework/00_MASTER_ORCHESTRATOR.md:63-91`,
`IMPLEMENTATION_ROADMAP.md:222-226` ("monitor utilization, calculate after each agent call").

**Reality:** `StateManager.set_context_utilization` (`orchestrator/state_manager.py:186-193`)
has **0 call sites**. Compaction fires only if a human hand-edits `utilization_percent` to
≥70, so the 70%/85% safety mechanism is dormant. Tests only trigger it by writing 75 into the
fixture.

### 7. Milestone checkpoint policy missing

**Spec:** `framework/00_MASTER_ORCHESTRATOR.md:172-183` lists 7 triggers (after requirements,
after architecture lock, before risky changes, after integration tests, before release, on
compaction).

**Reality:** auto-checkpointing only happens on compaction
(`orchestrator/orchestrator.py:357-380`); everything else is manual via CLI. No
phase-transition hooks.

### 8. Decision control / "no silent changes" missing

**Spec:** `framework/00_MASTER_ORCHESTRATOR.md:132-157`,
`GETTING_STARTED.md:214-223` (Pattern 3 — `PROPOSED_CHANGE`).

**Reality:** no `append_decision` API, no DECISIONS.md write path, no guard preventing an
agent from silently changing approved architecture. `StateManager.save_decisions`
(`orchestrator/state_manager.py:474-475`) has no caller.

### 9. RISKS.md is never maintained

**Spec:** `QUICK_REFERENCE.md:66-72` assigns RISKS.md to "Supervisor + owner".

**Reality:** the supervisor reads only TASKS.yaml and PROJECT.yaml
(`orchestrator/supervisor.py`). `StateManager.save_risks`
(`orchestrator/state_manager.py:472-473`) has no caller, and no risk IDs are linked to agent
output.

### 10. Human decision gates are detected but not persisted

**Reality:** escalations are built and reported
(`orchestrator/supervisor.py:281-340`), but `StateManager.add_human_decision`
(`orchestrator/state_manager.py:225-231`) has **0 call sites**. `PROJECT.yaml` fields
`human_decisions` and `blockers` are never written, so an escalation leaves no on-disk trace
and cannot be seen by a resumed session.

### 11. Loop detection is narrower than specified

**Spec:** `framework/00_MASTER_ORCHESTRATOR.md:92-108` requires detecting repeated identical
failures, repeated identical outputs, no-progress cycles, **state oscillation**, dependency
deadlocks, repeated searches with no new evidence, and unchanged-hypothesis retries.

**Implemented:** same-strategy attempt counts and no-progress counters
(`orchestrator/supervisor.py:154-205`).

**Missing:** `LOOP_KIND_STATE_OSCILLATION` is declared
(`orchestrator/config.py:346`) and never produced. The orchestrator fingerprint
(`orchestrator/orchestrator.py:247-255`) is used only for idle-cycle counting. Additionally
`strategy_changed=True` is never passed by any caller, so the
"max 2 alternative strategies" path can never advance in production.

### 12. CLI missing documented commands and entry point

**Spec:** `ORCHESTRATOR_GUIDE.md:146-186` documents `init`, `status`, `tasks`, `run`,
`checkpoint save|list|restore` through `bin/orchestrator`.

**Implemented subcommands:** `status`, `run`, `health`, `agents`, `checkpoint`
(`orchestrator/cli.py:59-100`).

**Missing:** `init` (project scaffolding) and `tasks` (dependency-graph view). There is also
**no `bin/orchestrator`** — the documented `chmod +x bin/orchestrator` quick start cannot
work — and no `pyproject.toml`/`setup.py`, so the `orchestrator` command itself does not exist.

---

## P2 — Medium / hygiene

### 13. Documentation drift

`ORCHESTRATOR_GUIDE.md:26-144` documents the superseded v1.1 API (`ProjectManager`,
`TaskExecutor`, `orchestrator/project_manager.py`, `checkpoint.py`, `Supervisor.check_health`)
that the v2.0 runtime replaced. The guide's Quick Start is therefore wrong.

### 14. Dead/duplicated modules shipped alongside the new runtime

`orchestrator/checkpoint.py` (365 LOC), `orchestrator/project_manager.py` (315),
`orchestrator/task_executor.py` (233) — **913 LOC superseded but still exported** from
`orchestrator/__init__.py:35,51`. Two competing `CheckpointManager` classes with different
APIs. Plus a stray `orchestrator/__init__.py ` file (trailing space in the filename).

### 15. Repo paths referenced by docs that do not exist

| Referenced path | Referenced by | Reality |
|---|---|---|
| `framework/AGENT_PROMPTS/` | `README.md:22-24` | missing |
| `framework/TEMPLATES/` | `README.md:22-24`, `CONTRIBUTING.md:24` | missing |
| `framework/20_DEFAULT_PROJECT_START_PROMPT.md` | `README.md:57,75`, `DEPLOYMENT_SUMMARY.md:232` | missing |
| 7 template state files in `project-templates/` | `GETTING_STARTED.md:47-57`, `DEPLOYMENT_SUMMARY.md:63` | only `NEW_PROJECT_CHECKLIST.md` exists |
| `agents/`, `references/`, `logs/` | `README.md:26-28` | empty directories |

### 16. Declared configuration never wired

- `LOG_FORMAT` / `DEFAULT_LOG_LEVEL` (`orchestrator/config.py:393-394`) — no `import logging`
  anywhere in the package, although `IMPLEMENTATION_ROADMAP.md:174` lists logging as a
  deliverable.
- `checkpoint_signing_key` (`orchestrator/config.py:111-115`) — checkpoints are checksummed
  but never signed; the key is dead.

### 17. Stale derived state in the example project

`TASKS.yaml` `summary` / `status_breakdown` / `parallel_groups` and `PROJECT.yaml`
`progress` / `agents` / `next_tasks` are never recomputed by any new code path (only the old
`project_manager.py` wrote them). They silently go stale after every run — directly
contradicting `framework/10_DOCUMENTATION_AGENT.md:28-43` ("status files reflect actual task
state").

### 18. Unused vocabulary states

`WAITING` and `DONE WITH ACCEPTED LIMITATION` are defined
(`orchestrator/config.py:285,290`) but never assigned by any code path;
`HealthState.RECOVERY` (`orchestrator/config.py:330`) is never returned by
`SupervisorAgent.classify_state` (`orchestrator/supervisor.py:342-360`).

### 19. Dependency hygiene

`orchestrator/requirements.txt` declares `pydantic` (never imported) and `pytest-asyncio`
(only needed by the superseded `task_executor.py`).

### 20. Test-suite structure and coverage

`IMPLEMENTATION_ROADMAP.md:238-258` expects `test_state_manager.py`, `test_supervisor.py`,
`test_orchestrator.py`; there is one combined `test_orchestrator_pipeline.py` (48 tests).
Untested public API inside it: structured-output JSON parsing, oscillation detection,
decision/risk writes, and the missing `init` / `tasks` CLI paths.

---

## Recommended remediation order

1. **Agent backend** — add `llm_client.py` (Anthropic + Ollama) and `prompt_builder.py` that
   loads `framework/XX_AGENT.md` into the agent payload; make `extract_json_block` the real
   parse path (removes dead code at the same time).
2. **Remaining 8 agent nodes** — research and review are the highest leverage; review
   unblocks the Definition of Done gate.
3. **DoD + review enforcement** — make `DONE` unreachable without a passing independent
   review and satisfied acceptance criteria.
4. **Context accounting** — call `set_context_utilization` after every dispatch; this alone
   re-arms the 70%/85% compaction path.
5. **Persist escalations and blockers** — wire `add_human_decision` into `sync_health`.
6. **Parallel dispatch** in `run_cycle` behind a `--max-concurrent` flag.
7. **Artifact emission** — have agents write `REQUIREMENTS.md` and traceability matrices into
   `projects/<name>/docs/`.
8. **CLI parity** — add `init` and `tasks`, add `bin/orchestrator` and `pyproject.toml`.
9. **Decision and risk control** — `append_decision` with a `PROPOSED_CHANGE` approval gate,
   plus a supervisor path that updates RISKS.md.
10. **Cleanup** — delete the 3 superseded modules and the stray `__init__.py `, then rewrite
    `ORCHESTRATOR_GUIDE.md` for v2.0 and add the missing framework doc paths.

---

## Appendix — verification commands used

```bash
# dead public API (0 call sites)
grep -rn "add_human_decision\|set_context_utilization\|set_phase" --include=*.py orchestrator/
grep -rn "extract_json_block\|parse_structured_output" --include=*.py orchestrator/ test_orchestrator_pipeline.py

# no parallelism in the new runtime
grep -rn "Thread\|concurrent\|asyncio" orchestrator/orchestrator.py orchestrator/supervisor.py orchestrator/cli.py

# no logging, no git integration
grep -rn "import logging" --include=*.py orchestrator/
grep -rn "subprocess" --include=*.py orchestrator/

# stale derived state never written
grep -rn "next_tasks\|parallel_groups\|critical_path" --include=*.py orchestrator/orchestrator.py orchestrator/state_manager.py

# CLI surface vs documentation
grep -n "add_parser(" orchestrator/cli.py
grep -n "orchestrator init\|orchestrator --project my-project tasks" ORCHESTRATOR_GUIDE.md
```
