# Agentic AI Framework

A comprehensive orchestration system for managing complex technical projects using autonomous specialist agents, dependency-aware scheduling, and resilient state management.

## What Is This?

This framework provides:

1. **Master Orchestrator** — Coordinates the complete project lifecycle from idea to release
2. **Specialist Agents** — Requirements, Research, Architecture, Planning, Hardware, Software, Test, Review, Documentation
3. **State-Driven Execution** — Projects resume from saved state, not conversation history
4. **Dependency Awareness** — Parallel execution of independent tasks; blocks only tasks with genuine dependencies
5. **Resilience** — Loop detection, recovery strategies, checkpoints, and human decision gates
6. **Measurable Progress** — Definition of Done, traceability, independent review, and evidence-based completion

## Repository Structure

```
agentic-ai-framework/
├── bin/orchestrator          # Executable CLI entry point
├── pyproject.toml            # Package metadata (`orchestrator` script)
├── orchestrator/             # Python runtime (state, dispatch, agents, LLM)
├── ORCHESTRATOR_GUIDE.md     # Runtime implementation guide (v2.0)
├── framework/              # Core framework documentation and specs
│   ├── 00_MASTER_ORCHESTRATOR.md
│   ├── 01_SUPERVISOR_AGENT.md
│   ├── AGENT_PROMPTS/      # Individual agent specifications
│   └── TEMPLATES/          # All project document templates
├── agents/                 # Agent implementation examples
│   ├── orchestrator.md
│   ├── supervisor.md
│   └── specialists/        # Requirements, Research, Architecture, etc.
├── projects/               # Live project demonstrations
│   └── kid-robot-face/     # Real working example project
│       ├── PROJECT.yaml
│       ├── PROJECT_MEMORY.md
│       ├── CURRENT_STATE.md
│       ├── TASKS.yaml
│       ├── DECISIONS.md
│       ├── RISKS.md
│       ├── CHANGELOG.md
│       ├── docs/           # Requirements, architecture, plans, tests
│       └── implementation/ # Code, firmware, schematics
├── project-templates/      # Copy-paste state-file templates
│   ├── PROJECT.yaml  PROJECT_MEMORY.md  CURRENT_STATE.md  TASKS.yaml
│   ├── DECISIONS.md  RISKS.md  CHANGELOG.md
│   └── NEW_PROJECT_CHECKLIST.md
├── meta/                   # Framework implementation guide
│   ├── GETTING_STARTED.md
│   ├── WORKFLOW.md
│   └── TROUBLESHOOTING.md
├── checkpoints/            # Saved recovery points
└── references/             # External tools, research, standards
```

## Quick Start

### Start a New Project Using This Framework

1. Scaffold all state files: `./bin/orchestrator init my-project`
   (or copy the 7 files from `project-templates/`)
2. Update `PROJECT.yaml` / `PROJECT_MEMORY.md` with your project details
3. Start a session with the Master Orchestrator using `framework/20_DEFAULT_PROJECT_START_PROMPT.md`
4. Orchestrator creates requirements, architecture, tasks, and starts parallel work

### Study the Framework With a Real Example

Examine `projects/kid-robot-face/` — a voice-reactive ESP32 robot face project fully initialized with:
- Complete requirements (REQ-001 through REQ-020)
- Architecture design (ESP32 + OLED, Ollama/Gemma integration)
- Dependency-aware task graph (30+ tasks, parallel-ready)
- Risk register and decision log
- Test plan and acceptance criteria
- Checkpoints at key milestones

### Key Framework Documents

Start here:
- **[00_MASTER_ORCHESTRATOR](framework/00_MASTER_ORCHESTRATOR.md)** — How orchestration works
- **[01_SUPERVISOR_AGENT](framework/01_SUPERVISOR_AGENT.md)** — Health monitoring and loop detection
- **[20_DEFAULT_PROJECT_START_PROMPT](framework/20_DEFAULT_PROJECT_START_PROMPT.md)** — The canonical startup prompt

Then read the specialist agents:
- Requirements, Research, Architecture, Planning, Hardware, Software, Test, Review, Documentation

## Core Principles

1. **Project Files Are Source of Truth** — STATE.yaml, requirements, architecture, tasks drive the work. Chat is temporary context.

2. **Parallel Execution** — Independent tasks run together. Blocked tasks don't stop unrelated work.

3. **Dependency Awareness** — Tasks start only when predecessors complete or are waived.

4. **Meaningful Progress** — No loops without changed strategy. Max 3 attempts at same approach; max 5 cycles with no progress.

5. **Independent Review** — Major tasks reviewed by agents who didn't create them. Definition of Done is measurable.

6. **Resilience** — Checkpoints at milestones. Resume from saved state. Compaction at 70% context. Escalate only genuine decisions.

7. **No Silent Changes** — Architecture changes recorded as PROPOSED_CHANGE with reason, alternatives, impact, risks.

8. **Interrupt Only When Necessary** — Human decides: purchases, irreversible actions, major architecture trade-offs, safety concerns, unresolved blockers.

## Default Lifecycle

```
IDEA
  ↓
REQUIREMENTS (+ RESEARCH in parallel)
  ↓
ARCHITECTURE (+ RESEARCH continuation)
  ↓
PLANNING (dependency graph, parallel tasks)
  ↓
IMPLEMENTATION (hardware, software, firmware in parallel)
  ↓
INTEGRATION (modules combine)
  ↓
TESTING (unit, integration, system, acceptance)
  ↓
VALIDATION (against acceptance criteria)
  ↓
RELEASE (checkpoint, delivery)
  ↓
MAINTENANCE
```

Research, testing, documentation, and risk management run continuously where useful.

## State Files (Every Project Needs These)

- **PROJECT.yaml** — Machine-readable project controller (status, phase, health, context usage)
- **PROJECT_MEMORY.md** — Compact long-term memory for resume without full history
- **CURRENT_STATE.md** — Current snapshot: where we are, what works, what's blocked, what's next
- **TASKS.yaml** — Dependency-aware task list with status, owner, inputs, outputs
- **DECISIONS.md** — Important decisions, reason, alternatives, impact, status
- **RISKS.md** — Identified risks, probability, impact, mitigation, owner, status
- **CHANGELOG.md** — Version history, what changed, why, who affected

## Document Templates (Create As Needed)

- Requirements Specification
- Architecture Design
- Project Plan & Milestones
- Research Reports
- Implementation Guide
- Test Plan & Test Cases
- Bill of Materials (BOM)
- Release Notes
- User Guide

## Running a Project

### Initialization (Day 1)

```bash
# Create project folder
mkdir projects/my-project

# Copy state file templates
cp project-templates/PROJECT.yaml projects/my-project/
cp project-templates/PROJECT_MEMORY.md projects/my-project/

# Update with your project details
# Run Master Orchestrator with default start prompt
```

### Execution (Ongoing)

1. Orchestrator reads TASKS.yaml, identifies READY tasks
2. Agents receive task + PROJECT_MEMORY.md + relevant docs
3. Agents complete work, return structured output
4. Supervisor monitors for loops, deadlocks, context pressure
5. Independent review checks major outputs
6. Documentation updated, state files refreshed
7. Parallel READY tasks start immediately

### Checkpointing (Before Risky Changes)

```bash
# Create checkpoint with:
# - PROJECT.yaml snapshot
# - PROJECT_MEMORY snapshot
# - CURRENT_STATE snapshot
# - TASKS snapshot
# - Key decisions and risks
# - Validated artifacts
# - Recovery notes
cp -r projects/my-project checkpoints/my-project-checkpoint-001
```

### Recovery (After Interruption)

1. Load latest checkpoint
2. Verify files haven't diverged unexpectedly
3. Reconstruct READY/BLOCKED task state
4. Restart required agents
5. Re-run only validation needed after checkpoint
6. Continue with compact context

## Context Management

| Utilization | Status | Action |
|-------------|--------|--------|
| < 60% | Healthy | Continue normally |
| 60–70% | Healthy | Monitor |
| 70–80% | Warning | Begin compaction prep |
| 80–85% | Critical | Compact immediately |
| > 85% | Failure Risk | Compact + save checkpoint |

**Compaction procedure:**
1. Summarize completed work
2. Capture important decisions
3. Update PROJECT_MEMORY.md
4. Update CURRENT_STATE.md
5. Save checkpoint
6. Discard irrelevant history
7. Continue with compact context

## Loop Detection & Recovery

**Limits (built-in stall protection):**
- Same failing strategy: max 3 attempts
- No meaningful progress: max 5 cycles
- Alternative strategies: max 2 materially different approaches

**When a loop is detected:**
1. Stop current strategy
2. Preserve state
3. Summarize failure
4. Diagnose root cause
5. Choose materially different approach
6. Retry within limits
7. If alternatives fail → create HUMAN_DECISION_REQUIRED

**Meaningful progress** means at least one of:
- A requirement satisfied
- A blocker removed
- A test improved
- A dependency resolved
- A valid artifact produced
- A root cause identified and addressed

Rewording, repeating searches, or rerunning failed commands without new hypothesis = NOT progress.

## Human Decision Gates

Interrupt only for:
- Significant purchase or cost
- Irreversible/destructive action
- Major architecture trade-off
- Safety/privacy/external publishing concern
- Essential missing information
- Major scope change
- Unresolved blocker after recovery fails

**Continue independent work** while one decision is pending. Don't stall everything.

## Definition of Done

A task is DONE only when ALL applicable criteria pass:

1. ✓ Requirement exists
2. ✓ Implementation exists
3. ✓ Testing/validation exists
4. ✓ Acceptance criteria pass
5. ✓ Independent review passes
6. ✓ Documentation updated
7. ✓ Project state updated

Possible completion states:
- `DONE` — All criteria met
- `DONE WITH ACCEPTED LIMITATION` — Criteria met except approved limitation
- `BLOCKED` — Waiting on dependency or decision
- `FAILED` — Unresolvable failure after recovery attempts
- `CANCELLED` — Formally cancelled

An agent must NOT mark its own significant work DONE solely because it generated code, text, or design.

## Framework vs. Project Files

```
/framework/              ← Instructions for agents, templates, specs
/projects/my-project/   ← Your actual project (state, requirements, code, docs)
```

The framework is re-usable. Each project owns its state, requirements, implementation, and decisions.

## Example Projects

Currently in this repo:
- **projects/kid-robot-face/** — Voice-reactive ESP32 robot face with Ollama/Whisper/TTS integration for a 5-year-old

More examples to follow.

## Support & Contributing

This framework is designed to be:
- **Forkable** — Copy the whole repo and start your project
- **Extensible** — Add specialist agents as needed
- **Verifiable** — State files prove project health
- **Resumable** — Always know where you are and what's next

See [meta/GETTING_STARTED.md](meta/GETTING_STARTED.md) for detailed walkthroughs.

---

**Last Updated:** 2026-09-29  
**Framework Version:** 1.0  
**Status:** Production Ready
