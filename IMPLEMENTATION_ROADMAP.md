# Implementation Roadmap — Agentic AI Framework

## Status: Specification Complete (Framework Layer)

The framework **specification** is complete (agent roles, workflows, state files, templates). Now we need to implement the **execution layer** (orchestration code).

## Three Implementation Approaches

### Approach 1: Python CLI Tool (Recommended for Production)

**Scope:** ~500-800 lines of Python

```
orchestrator/
├── cli.py                    # argparse, main entry point
├── orchestrator.py           # MasterOrchestrator class
├── supervisor.py             # SupervisorAgent (health monitoring)
├── state_manager.py          # YAML file read/write
├── checkpoint_manager.py     # Save/load checkpoints
├── agents/
│   ├── base_agent.py         # BaseAgent class (input/output)
│   ├── requirements_agent.py # Agent implementations
│   ├── research_agent.py
│   └── ... (9 total)
├── config.py                 # Config (API keys, paths, thresholds)
└── requirements.txt          # PyYAML, requests, anthropic-sdk
```

**Key Features:**
- Read/write PROJECT.yaml, TASKS.yaml, DECISIONS.md, etc.
- Call Claude API with specialized agent prompts
- Automatic context compaction at 70%
- Checkpoint before risky changes
- Loop detection (max 3 retries)
- Structured output parsing

**Estimated Time:** 5-7 days

---

### Approach 2: Claude API Wrapper (Fastest Prototype)

**Scope:** ~150-200 lines of Python

```
orchestrator.py
├── StateManager (YAML read/write)
├── PromptBuilder (framework spec + task context)
└── APIClient (Claude API calls)

cli.py
├── Main loop
├── Task selection (READY → run)
└── State updates
```

**How it works:**
```
1. Read TASKS.yaml, find READY task
2. Load framework/XX_AGENT.md (agent spec)
3. Load PROJECT_MEMORY.md (context)
4. Call Claude: "You are {Agent}. Do this: {Task}. Context: {Memory}."
5. Parse response, update TASKS.yaml
6. Commit to git
7. Next task
```

**Estimated Time:** 1-2 days

---

### Approach 3: Agentic (Full Automation)

**Scope:** Custom implementation per project

```
Use Claude's agentic capabilities (function calling, loops, tool use) to:
1. Run self-directed agents
2. Coordinate work automatically
3. Make local decisions (no human intervention until escalation)
```

**Pros:** Fully autonomous orchestration  
**Cons:** Harder to debug, more API costs, overkill for many projects

---

## Recommended Path (Week 1-4)

### Week 1: Minimal Viable Orchestrator

Build Approach 2 (Claude API wrapper):

```python
class Orchestrator:
    def __init__(self, project_path, api_key):
        self.project = load_yaml(f"{project_path}/PROJECT.yaml")
        self.tasks = load_yaml(f"{project_path}/TASKS.yaml")
        self.memory = load_yaml(f"{project_path}/PROJECT_MEMORY.md")
        self.client = Anthropic(api_key=api_key)
    
    def run_next_task(self):
        task = next((t for t in self.tasks if t['status'] == 'READY'), None)
        if not task:
            print("No ready tasks")
            return
        
        agent_spec = load_agent_spec(task['owner'])
        prompt = f"Agent: {agent_spec}\n\nTask: {task}\n\nContext: {self.memory}"
        
        response = self.client.messages.create(
            model="claude-opus-4-5",
            messages=[{"role": "user", "content": prompt}]
        )
        
        self.update_task(task['id'], response.content)
        self.commit_to_git(f"TASK-{task['id']}: complete")
```

**Test:** Run kid-robot-face requirements finalization (TASK-005)

### Week 2: State Management + Checkpoints

```python
class StateManager:
    def load_project(self, path):
        return {
            'project': yaml.load(PROJECT.yaml),
            'tasks': yaml.load(TASKS.yaml),
            'decisions': yaml.load(DECISIONS.md),
            'risks': yaml.load(RISKS.md),
            'memory': load(PROJECT_MEMORY.md)
        }
    
    def save_task(self, task_id, result):
        # Update TASKS.yaml
        # Update CURRENT_STATE.md
        # Commit to git
    
    def checkpoint(self, name):
        # Create checkpoints/ folder
        # Copy all state files
        # Git tag
```

**Test:** Save/restore checkpoint on kid-robot-face

### Week 3: Supervisor + Loop Detection

```python
class SupervisorAgent:
    def monitor_health(self, project):
        return {
            'context_usage': % (usage / limit),
            'blocked_tasks': count(status='BLOCKED'),
            'loop_detected': detect_loops(),
            'deadlock': check_circular_deps(),
            'health': 'HEALTHY' | 'WARNING' | 'STALLED'
        }
    
    def detect_loops(self):
        # Check if same task failed 3 times
        # Check if 5 cycles with no progress
        # Return HUMAN_DECISION_REQUIRED if needed
```

**Test:** Trigger loop detection, force recovery strategy

### Week 4: Production Polish

- CLI argument parsing (argparse)
- Config file support (TOML/YAML)
- Error handling + retries
- Logging
- Documentation + README
- CI/CD integration (auto-commit, GitHub Actions)

---

## What to Build First

**Priority 1: Minimal Viable Orchestrator** (Week 1)

```bash
python orchestrator.py --project projects/kid-robot-face --task TASK-005
```

Returns structured output, updates TASKS.yaml, commits to git.

**Priority 2: Checkpoint Management** (Week 2)

```bash
python orchestrator.py --checkpoint save CP-KID-ROBOT-002
python orchestrator.py --checkpoint load CP-KID-ROBOT-002
```

**Priority 3: Supervisor + Auto-Escalation** (Week 3)

```bash
python orchestrator.py --run-next-task  # Auto-detects loops, escalates
```

---

## Implementation Decisions

### State Management
- **YAML for TASKS/PROJECT/DECISIONS/RISKS** (version-controllable, human-readable)
- **Markdown for MEMORY/CURRENT_STATE** (readable + git-friendly)
- **Git commits** after each task (automatic audit trail)

### Agent Execution
- **Claude API** (can use Opus for complex decisions)
- **Structured prompts** (framework specs + task context + output format)
- **Output parsing** (expect YAML/JSON back from agents)

### Loop Detection
- **Counter per task:** attempt_count, no_progress_cycles, strategy_changes
- **Thresholds:** 3 attempts same strategy, 5 cycles no progress, max 2 different strategies
- **Action:** Stop, diagnose, escalate to human if alternatives fail

### Context Management
- **Monitor utilization** (calculate after each agent call)
- **Checkpoint at 70%** (save state + summarize + discard history)
- **Reset context** (PROJECT_MEMORY stays, old chat discarded)

---

## Testing Strategy

### Unit Tests
```python
test_state_manager.py
├── test_load_yaml()
├── test_save_yaml()
├── test_update_task()
└── test_checkpoint_restore()

test_supervisor.py
├── test_loop_detection()
├── test_context_compaction()
└── test_deadlock_detection()
```

### Integration Tests
```python
test_orchestrator.py
├── test_run_complete_workflow()  # kid-robot-face full cycle
├── test_checkpoint_recovery()
└── test_escalation_to_human()
```

### Manual Tests
1. **Init new project** → creates state files
2. **Run single task** → calls Claude, updates TASKS.yaml
3. **Checkpoint + resume** → save state, close, reload, continue
4. **Trigger loop** → run same task 3 times, verify escalation

---

## Build or Buy?

| Option | Build Time | Maintenance | Flexibility |
|--------|-----------|-------------|-------------|
| Python CLI | 1-2 weeks | Low | High |
| Claude API Wrapper | 2-3 days | Very Low | Medium |
| Third-party orchestration tool | N/A | High | Low |

**Recommendation:** Build Python CLI (Approach 1). You have the skills, it's not complex, and you control the entire system.

---

## Success Criteria

**Week 1:**
- [ ] Orchestrator.py can run TASK-005 with Claude API
- [ ] TASKS.yaml updates automatically
- [ ] Output is structured (YAML/JSON)
- [ ] Changes committed to git

**Week 2:**
- [ ] Checkpoints save/restore all state files
- [ ] Can resume from checkpoint without full context
- [ ] Git tags mark checkpoint milestones

**Week 3:**
- [ ] Loop detection working (stops after 3 retries)
- [ ] Auto-escalation when alternatives fail
- [ ] Supervisor health report shows project status

**Week 4:**
- [ ] CLI tool is polished + documented
- [ ] Can run end-to-end on kid-robot-face
- [ ] Production-ready (error handling, logging)

---

**Framework Status:** Specification Complete ✅  
**Implementation Status:** Ready to Build 🚀  
**Next Step:** Start Week 1 (Minimal Viable Orchestrator)

