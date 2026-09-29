# WORKFLOW — phase guide

Full getting-started: `meta/GETTING_STARTED.md`. Runtime behaviour:
`ORCHESTRATOR_GUIDE.md`.

## Phases

1. **REQUIREMENTS** — `requirements_agent` → `docs/REQUIREMENTS.md` (REQ-001..),
   review required, checkpoint `cp-requirements`
2. **RESEARCH** — `research_agent` (parallel with 1) → `docs/RESEARCH-*.md`
3. **ARCHITECTURE** — `architecture_agent` → `docs/ARCHITECTURE.md`;
   changes go through `DEC-NNN` decisions
4. **PLANNING** — `planning_agent` → TASKS.yaml graph with owners, DoD,
   parallel groups, milestones
5. **IMPLEMENTATION** — hardware/software agents dispatch in dependency order,
   `--max-concurrent` for independent tasks
6. **TESTING** — `test_agent` maps tests to REQ ids; failures become tasks
7. **RELEASE** — `documentation_agent` synchronizes all docs; final review;
   `cp-milestone-complete` checkpoint

## Rules of thumb
- Nothing skips its dependencies; reviews gate DONE
- Every state change leaves the files truthful (derived state recomputes itself)
- Checkpoint at phase/milestone boundaries; human decisions block dispatch until
  resolved
