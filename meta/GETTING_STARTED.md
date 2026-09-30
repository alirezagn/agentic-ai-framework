# GETTING STARTED — Agentic AI Framework

## The 5-Minute Overview

This framework orchestrates complex projects using autonomous specialist agents. Instead of a single AI doing everything, you get:

1. **Master Orchestrator** — Coordinates the whole project lifecycle
2. **Specialist Agents** — Each handles one domain (requirements, research, architecture, coding, testing, etc.)
3. **State Files** — Project truth lives in YAML + Markdown, not chat history
4. **Checkpoints** — Save progress; resume without losing context
5. **Parallel Work** — Independent tasks run together; dependencies are respected

## What Problem Does This Solve?

### Before (Without Framework)
- Chat with AI to build project
- Instructions get lost in long history
- Don't know what was decided and why
- Hard to resume after a break
- Difficult to verify completion
- No clear path through complexity

### After (With Framework)
- PROJECT.yaml + CURRENT_STATE.md tell you the project status *right now*
- DECISIONS.md documents *why* you chose X over Y
- TASKS.yaml shows *exactly* what's left and dependencies
- CHECKPOINT saves recovery point; resume in 30 seconds
- DEFINITION_OF_DONE is clear and measurable
- Agent outputs are structured, not narrative

## Your First Project in 3 Steps

### Step 1: Fork or Copy This Repository

```bash
git clone <this-repo>
cd agentic-ai-framework
```

### Step 2: Create Your Project Folder

```bash
mkdir projects/my-awesome-project
cd projects/my-awesome-project
```

### Step 3: Copy the Template State Files

```bash
cp ../../project-templates/PROJECT.yaml .
cp ../../project-templates/PROJECT_MEMORY.md .
cp ../../project-templates/CURRENT_STATE.md .
cp ../../project-templates/TASKS.yaml .
cp ../../project-templates/DECISIONS.md .
cp ../../project-templates/RISKS.md .
cp ../../project-templates/CHANGELOG.md .
```

### Step 4: Fill In Your Project Details

Edit each file with your project specifics:

**PROJECT.yaml:**
```yaml
project:
  id: PROJECT-MY-AWESOME-001
  name: My Awesome Project
  version: 0.1
  status: IDEA
```

**PROJECT_MEMORY.md:**
```markdown
## Goal
A one-sentence description of what you're building.

## Key Requirements
- REQ-1: Something important
- REQ-2: Another important thing
```

## The Workflow (What Happens Next)

### Phase 1: REQUIREMENTS (1-3 days)

1. You describe your idea
2. **Requirements Agent** captures needs, constraints, acceptance criteria
3. Creates REQ-001, REQ-002, ... with measurable criteria
4. Independent review → APPROVED or rework
5. **Checkpoint saved:** You now know exactly what you're building

### Phase 2: RESEARCH (parallel with Phase 1)

1. **Research Agent** investigates unknowns:
   - What hardware exists?
   - Which APIs are suitable?
   - What standards apply?
2. Creates RESEARCH_*.md with findings, alternatives, recommendations
3. Informs ARCHITECTURE decisions

### Phase 3: ARCHITECTURE (1-2 days)

1. **Architecture Agent** designs the system:
   - Hardware block diagram
   - Software components
   - Data flow
   - Interfaces
2. Documents DECISIONS.md (why we chose X, not Y)
3. Identifies RISKS and mitigations
4. **Checkpoint saved:** Design locked

### Phase 4: PLANNING (1 day)

1. **Planning Agent** creates TASKS.yaml:
   - 20-50 tasks (depending on project size)
   - Dependencies clearly mapped
   - READY tasks identified (can run in parallel)
   - Milestones with entry/exit criteria
2. **Checkpoint saved:** Ready to build

### Phase 5-7: IMPLEMENTATION + TESTING + INTEGRATION (7-14 days)

1. **Software/Hardware Agents** implement in parallel
2. **Test Agent** verifies against acceptance criteria
3. **Review Agent** independently checks quality
4. Issues → FAILED tasks; fixes → new tasks; loop until DONE
5. **Checkpoints saved:** At each milestone

### Phase 8: RELEASE (1 day)

1. All mandatory requirements met ✓
2. All acceptance tests passing ✓
3. Independent review approved ✓
4. Documentation complete ✓
5. **Final checkpoint + tag + release notes**

## State File Reference

Every project needs these 7 files (created from templates):

| File | Purpose | Updated | By Whom |
|------|---------|---------|---------|
| **PROJECT.yaml** | Machine-readable status (version, phase, health, context %) | Continuous | Orchestrator |
| **PROJECT_MEMORY.md** | Compact long-term memory (goal, requirements, decisions, blockers) | Each milestone | Documentation Agent |
| **CURRENT_STATE.md** | Quick snapshot: where, what works, what's blocked, what's next | Each status change | Orchestrator |
| **TASKS.yaml** | All tasks with status, owner, dependencies, acceptance criteria | Continuous | Planning Agent |
| **DECISIONS.md** | Why we chose X over Y (with alternatives, impact, affected tasks) | As decisions made | Architecture Agent |
| **RISKS.md** | Risks, probability, impact, mitigation, owner, status | Continuous | Supervisor + owner |
| **CHANGELOG.md** | Version history and what changed (and why) | Each release | Documentation Agent |

## Checkpoints: Resume Without Losing Context

A checkpoint is a snapshot: PROJECT.yaml + PROJECT_MEMORY + CURRENT_STATE + TASKS + key artifacts.

**Create checkpoint before:**
- Risky changes
- Major milestone (architecture lock, integration complete)
- Context compaction (when context usage > 70%)

**Example:**
```bash
mkdir checkpoints/my-awesome-project-checkpoint-001
cp PROJECT.yaml PROJECT_MEMORY.md CURRENT_STATE.md TASKS.yaml checkpoints/my-awesome-project-checkpoint-001/
cp DECISIONS.md RISKS.md CHANGELOG.md checkpoints/my-awesome-project-checkpoint-001/
git add checkpoints/my-awesome-project-checkpoint-001
git commit -m "Checkpoint: architecture locked"
```

**Resume from checkpoint:**
1. Load latest checkpoint files
2. Read CURRENT_STATE.md (answer: where are we?)
3. Read TASKS.yaml (answer: what's READY?)
4. Read PROJECT_MEMORY.md (answer: why did we choose this?)
5. Restart only necessary agents
6. Continue

## Example: Kid-Robot-Face Project

This repo includes a **real, working example:** `projects/kid-robot-face/`

It shows:
- ✓ Complete requirements (REQ-001..REQ-015)
- ✓ Architecture design (ESP32-C3 + OLED + Ollama)
- ✓ 7 approved decisions with full rationale
- ✓ Task dependency graph (20 tasks, parallel groups)
- ✓ Risk register (7 risks, mitigations)
- ✓ Checkpoint at each phase

**Study this project to understand the framework in action.**

## Common Patterns

### Pattern 1: Blocked Task Shouldn't Stop Others

```yaml
Task A (firmware): BLOCKED (waiting for hardware BOM)
Task B (server API): READY (no dependencies) ← START THIS
Task C (tests): BLOCKED (waiting for Task B)
Task D (documentation): READY ← START THIS TOO
```

The Orchestrator keeps B and D running while A and C wait.

### Pattern 2: Loop Detection & Recovery

If an agent tries the same strategy 3 times and fails:

1. STOP (don't keep retrying)
2. Save state (important!)
3. Diagnose: "Why did all 3 attempts fail?"
4. Try materially different strategy
5. If that fails too: ESCALATE to HUMAN_DECISION_REQUIRED

### Pattern 3: Silent Architecture Change

❌ **BAD:** Agent quietly modifies circuit design without recording why
✓ **GOOD:** Agent creates PROPOSED_CHANGE in DECISIONS.md:
- Reason: "ESP32-C3 pins changed in latest datasheet"
- Alternatives: "Use older ESP32 classic" (rejected why?)
- Impact: "Affects wiring, firmware GPIO assignments"
- Affected tasks: TASK-025, TASK-030
- Risks: Compatibility with existing headers?
- Recommendation: Approve + update all references

### Pattern 4: Context Compaction

When context usage approaches 70%:

1. Summarize: "Completed X, Y, Z; still blocked on A"
2. Update PROJECT_MEMORY.md with key decisions + blockers
3. Update CURRENT_STATE.md
4. Save checkpoint
5. Discard chat history before the checkpoint
6. Continue with compact context + PROJECT_MEMORY

## Stopping Criteria (When Are You Done?)

You're DONE when:

1. **All mandatory requirements are satisfied** (or accepted limitation is documented)
2. **All acceptance tests pass** (or failures are understood + accepted)
3. **Independent review approves** (not just creator sign-off)
4. **Documentation is complete** (user guide, implementation notes, diagrams)
5. **Project files are synchronized** (no stale docs, decisions, risks)
6. **Final checkpoint created** (resumable state preserved)

## Troubleshooting

### Problem: "Where are we?"
**Solution:** Read CURRENT_STATE.md (should be < 1 minute answer)

### Problem: "What's blocked?"
**Solution:** Grep TASKS.yaml for `status: BLOCKED` and read RISKS.md

### Problem: "Why did we choose this?"
**Solution:** Look up decision ID in DECISIONS.md (should have full rationale)

### Problem: "Context is getting huge"
**Solution:** Save checkpoint, update PROJECT_MEMORY.md, discard chat history, continue with compact state

### Problem: "An agent is looping"
**Solution:** Supervisor detects after 3 failed attempts; stop, diagnose root cause, try different strategy

## Next Steps

1. **Study the example:** Open `projects/kid-robot-face/` and read all state files
2. **Run the framework:** Copy templates, create your first project, fill in PROJECT.yaml
3. **Start small:** 3-5 requirements, simple architecture, 10-15 tasks
4. **Iterate:** Build, test, review, document; rinse, repeat

---

**Framework Status:** Production Ready  
**Example Project Status:** In Progress (alpha → beta → release, on schedule)  
**Last Updated:** 2026-09-29
