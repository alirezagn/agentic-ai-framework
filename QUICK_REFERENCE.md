# Agentic AI Framework — Quick Reference

## The 3-Minute Summary

**Problem:** Managing complex AI-driven projects is hard. Chat history grows huge. Decisions disappear. Progress is unclear.

**Solution:** Use state files (PROJECT.yaml, TASKS.yaml, DECISIONS.md) as source of truth. Checkpoints for safe resumption. Specialist agents for parallel work.

**Result:** Transparent, measurable, resumable projects. No lost context. Clear decisions. Parallel execution.

---

## File Structure

```
agentic-ai-framework/
├── README.md                          ← Start here
├── LICENSE                            ← MIT (free to use)
├── DEPLOYMENT_SUMMARY.md              ← What was created
├── QUICK_REFERENCE.md                 ← This file

├── framework/                         ← Framework documentation
│   ├── 00_MASTER_ORCHESTRATOR.md      ← Main orchestration rules
│   ├── 01_SUPERVISOR_AGENT.md         ← Health monitoring
│   ├── 02-10_REQUIREMENTS...          ← Specialist agent specs
│
├── meta/
│   └── GETTING_STARTED.md             ← Comprehensive guide
│
├── projects/
│   └── kid-robot-face/                ← Real working example
│       ├── PROJECT.yaml               ← Machine-readable status
│       ├── PROJECT_MEMORY.md          ← Resumable state
│       ├── CURRENT_STATE.md           ← Where we are now
│       ├── TASKS.yaml                 ← Tasks + dependencies
│       ├── DECISIONS.md               ← Why we chose what
│       ├── RISKS.md                   ← Risks + mitigations
│       └── CHANGELOG.md               ← Version history
│
└── project-templates/
    └── NEW_PROJECT_CHECKLIST.md       ← Template for new projects
```

---

## The 10-Step Workflow

1. **Capture goal** (1-sentence)
2. **Write requirements** (REQ-001, REQ-002, ... with acceptance criteria)
3. **Research unknowns** (pick technologies, evaluate alternatives)
4. **Design architecture** (block diagram, data flow, decisions)
5. **Identify risks** (probability, impact, mitigation)
6. **Build task graph** (TASK-001..TASK-080 with dependencies)
7. **Start READY tasks** (parallel work begins)
8. **Test + review** (independent verification)
9. **Document** (update docs, capture evidence)
10. **Release** (checkpoint, tag, deliver)

**Timeline:** 20 days (with parallel work savings)

---

## State Files You Need (All 7)

| File | What It Is | Updated By |
|------|-----------|-----------|
| **PROJECT.yaml** | Machine status (phase, health, context %) | Orchestrator |
| **PROJECT_MEMORY.md** | Compact memory (goal, decisions, blockers) | Documentation Agent |
| **CURRENT_STATE.md** | Quick snapshot (1-minute answer) | Orchestrator |
| **TASKS.yaml** | Tasks + dependencies | Planning Agent |
| **DECISIONS.md** | Why we chose X (rationale, alternatives) | Architecture Agent |
| **RISKS.md** | Risks, probability, impact | Supervisor |
| **CHANGELOG.md** | Version history | Documentation Agent |

---

## How to Start Your Own Project

```bash
# 1. Copy the framework
git clone <this-repo>
cd agentic-ai-framework

# 2. Create your project folder
mkdir projects/my-project
cd projects/my-project

# 3. Copy state file templates
cp ../../project-templates/PROJECT.yaml .
cp ../../project-templates/PROJECT_MEMORY.md .
# ... copy all 7 state files

# 4. Edit files with your project details
# - PROJECT.yaml: name, ID, version
# - PROJECT_MEMORY.md: goal, constraints, resources

# 5. Commit and start work
git add .
git commit -m "Initialize project: my-project"
```

---

## Study the Example

The **kid-robot-face** project is fully initialized:
- 15 requirements (REQ-001..REQ-015)
- 7 approved decisions (DEC-001..DEC-007)
- 20 tasks with full dependency graph (TASK-001..TASK-080)
- 7 identified risks with mitigations

**To study:**
```bash
cd projects/kid-robot-face
cat PROJECT_MEMORY.md     # What are we building?
cat DECISIONS.md          # Why did we choose ESP32-C3?
cat TASKS.yaml            # What's the task graph?
cat RISKS.md              # What could go wrong?
```

---

## Key Framework Rules

1. ✅ **State files are source of truth** (not chat)
2. ✅ **Parallel work is OK** (independent tasks run together)
3. ✅ **Blocked tasks don't stop others** (dependencies are explicit)
4. ✅ **Decisions are recorded** (DECISIONS.md is the audit log)
5. ✅ **Progress is measurable** (Definition of Done is explicit)
6. ✅ **Context compaction** (at 70% usage, save checkpoint + summarize)
7. ✅ **Loop detection** (max 3 retries with same strategy)
8. ✅ **Independent review** (major work reviewed by others)
9. ✅ **Checkpointing** (save at milestones, resume from state)
10. ✅ **Human gates** (interrupt only for genuine decisions)

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Where are we?" | Read CURRENT_STATE.md (1-minute answer) |
| "What's blocked?" | Grep TASKS.yaml for `status: BLOCKED` |
| "Why did we choose this?" | Look up decision ID in DECISIONS.md |
| "Agent is looping" | Supervisor stops after 3 attempts; try different strategy |
| "Context too big" | Save checkpoint, update PROJECT_MEMORY, discard chat |
| "Test failed" | Create FAILED task; rework; run test again |

---

## Push to GitHub

```bash
# Configure remote
git remote add origin https://github.com/YOUR_USER/agentic-ai-framework.git

# Push main branch
git push -u origin main

# Tag release
git tag -a v3.0.0 -m "Release v3.0.0"
git push origin v3.0.0

# GitHub will now host your framework + projects
```

---

## What This Framework Does

✅ Organizes complex projects  
✅ Coordinates parallel work  
✅ Tracks decisions + rationale  
✅ Identifies + manages risks  
✅ Provides resumable checkpoints  
✅ Enables independent review  
✅ Measures progress objectively  
✅ Detects + breaks loops  
✅ Separates concerns (specialist agents)  
✅ Keeps state visible (not hidden in chat)  

---

## What It Doesn't Do

❌ Write code for you (agents do that)  
❌ Make decisions for you (you approve DEC-* entries)  
❌ Test automatically (Test Agent does that)  
❌ Hide complexity (framework surfaces problems)  
❌ Work without planning (TASKS.yaml is required)  

---

**Framework Version:** 3.0.0 (Production Ready)  
**Example Project:** Kid-Robot-Face (0.1.0-alpha)  
**License:** MIT (free to use, modify, share)  
**Created:** 2026-09-29

Ready to build something? 🚀
