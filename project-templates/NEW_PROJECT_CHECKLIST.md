# New Project Checklist

Use this checklist when starting a new project with the Agentic AI Framework.

## Pre-Launch (Day 0)

- [ ] Create project folder: `projects/my-project/`
- [ ] Copy template state files from `project-templates/`
- [ ] Create `docs/` subfolder for detailed documents
- [ ] Initialize `docs/README.md` with project summary
- [ ] Update PROJECT.yaml with project ID, name, version
- [ ] Write 1-sentence goal in PROJECT_MEMORY.md
- [ ] Create initial CURRENT_STATE.md snapshot
- [ ] Git add + commit: "Initialize project: my-project"

## Phase 1: REQUIREMENTS (Days 1-3)

- [ ] Answer the 5 key questions:
  - [ ] Who are the users?
  - [ ] What problem does this solve?
  - [ ] What are hard constraints? (budget, time, hardware, platform, language, safety)
  - [ ] What is out of scope?
  - [ ] How will we know it's done?
- [ ] Create REQUIREMENTS.md with REQ-001, REQ-002, ... (at least 10)
- [ ] Each requirement has acceptance criteria (measurable, not vague)
- [ ] Identify assumptions (don't invent constraints)
- [ ] Create traceability matrix (REQ → TASK → TEST)
- [ ] Save checkpoint: CP-PROJECT-001-REQUIREMENTS
- [ ] Update PROJECT.yaml: `phase: REQUIREMENTS`, `status: APPROVED`
- [ ] Git commit: "Complete requirements phase"

## Phase 2: RESEARCH (Days 1-3, parallel with Phase 1)

- [ ] Identify unknowns: technologies, products, standards, APIs, components
- [ ] Create RESEARCH.md for each major question
- [ ] Prefer primary/official sources (datasheets, API docs, standards)
- [ ] Evaluate alternatives (at least 2-3 options per decision)
- [ ] Record confidence level (HIGH, MEDIUM, LOW)
- [ ] Stop searching when no new evidence appears (avoid loops)
- [ ] Update DECISIONS.md with tech choices (DEC-001, DEC-002, ...)

## Phase 3: ARCHITECTURE (Days 4-5)

- [ ] Create ARCHITECTURE.md with 16 sections (see template)
- [ ] Draw block diagram (hardware/software subsystems)
- [ ] Document data flow (how information moves)
- [ ] Define interfaces (APIs, protocols, hardware connections)
- [ ] List assumptions + constraints
- [ ] Identify 5-10 key architectural risks
- [ ] Create DECISIONS.md entries for major design choices
- [ ] Have independent review before approval (not creator alone)
- [ ] Save checkpoint: CP-PROJECT-002-ARCHITECTURE
- [ ] Update PROJECT.yaml: `phase: ARCHITECTURE`, `status: APPROVED`
- [ ] Git commit: "Lock architecture; decisions approved"

## Phase 4: PLANNING (Day 6)

- [ ] Break project into 20-50 tasks (TASK-001, TASK-002, ...)
- [ ] For each task:
  - [ ] Assign owner (which agent)
  - [ ] List dependencies (TASK-001 must complete before TASK-005)
  - [ ] Define expected outputs (what gets created?)
  - [ ] Set acceptance criteria (how do we know it's done?)
  - [ ] Estimate priority (CRITICAL, HIGH, MEDIUM, LOW)
- [ ] Build dependency graph (predecessor/successor relationships)
- [ ] Identify READY tasks (no dependencies; can start immediately)
- [ ] Group tasks into parallel work groups (GROUP-1, GROUP-2, ...)
- [ ] Calculate critical path (longest chain of dependencies)
- [ ] Estimate timeline (est. days per task)
- [ ] Save checkpoint: CP-PROJECT-003-PLAN
- [ ] Update PROJECT.yaml: `phase: PLANNING`, `status: READY`
- [ ] Git commit: "Build task dependency graph; ready to implement"

## Phase 5-7: IMPLEMENTATION + TESTING (Days 7-20)

- [ ] Start all READY tasks in parallel
- [ ] Agents work independently; coordinate via PROJECT_MEMORY.md
- [ ] Each agent returns structured output (not narrative chat)
- [ ] Independent review for major work (not creator alone)
- [ ] Failed reviews → explicit rework tasks (don't just reject)
- [ ] Update CURRENT_STATE.md after each status change
- [ ] Update TASKS.yaml: mark completed tasks DONE, identify newly READY tasks
- [ ] Run unit tests, integration tests, acceptance tests
- [ ] Document test evidence (logs, screenshots, data)
- [ ] Monitor RISKS.md; update mitigation status as work progresses
- [ ] Save checkpoint every 3-4 days or after major milestone
- [ ] Git commit frequently: "TASK-010 complete; TASK-015 ready"

## Pre-Release (Days 18-20)

- [ ] Verify all mandatory requirements satisfied (or accepted limitation documented)
- [ ] All acceptance tests passing (or failures understood + accepted)
- [ ] Independent review approved (not just implementer)
- [ ] Documentation complete:
  - [ ] USER_GUIDE.md (for end users, not engineers)
  - [ ] IMPLEMENTATION.md (how to build/deploy)
  - [ ] TROUBLESHOOTING.md (known issues + solutions)
  - [ ] Diagrams up-to-date (block diagram, data flow, pinout, etc.)
- [ ] Create RELEASE.md with:
  - [ ] What's included (features, requirements met)
  - [ ] What's known not to work (accepted limitations)
  - [ ] Setup instructions
  - [ ] Installation/deployment notes
  - [ ] Rollback procedure
- [ ] Save final checkpoint: CP-PROJECT-RELEASE
- [ ] Git tag: `v1.0.0` with release notes
- [ ] Update CHANGELOG.md with release summary

## Post-Release

- [ ] Deliver to user (code, docs, hardware if applicable)
- [ ] Update PROJECT.yaml: `status: RELEASED`, `phase: MAINTENANCE`
- [ ] Document lessons learned (wiki / project README)
- [ ] Plan next iteration (v1.1, v2.0, etc.)
- [ ] Archive project or keep in maintenance mode

---

**Typical Timeline:** 20 days (research + requirements + architecture + implementation + testing + release)  
**Parallel Work Savings:** 5-10 days (compared to strictly sequential)  
**Team Size:** 1 orchestrator + 5-8 specialist agents (can be same AI taking on roles)

See GETTING_STARTED.md for detailed workflow explanation.
