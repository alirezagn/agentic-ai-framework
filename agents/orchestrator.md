# Orchestrator (implementation example)

- **Spec:** `framework/00_MASTER_ORCHESTRATOR.md`
- **Runtime:** `orchestrator/orchestrator.py::MasterOrchestrator`
- **Prompt:** `framework/AGENT_PROMPTS/00_ORCHESTRATOR.md`

Responsibilities: dependency-gated dispatch, review flow, Definition of Done,
decision gate, loop/oscillation gates, checkpoints (auto + milestone),
derived-state recompute, parallel execution under a state lock.

```python
from orchestrator.orchestrator import MasterOrchestrator
orch = MasterOrchestrator("projects/my-project")
orch.run_cycle(max_tasks=25, max_concurrent=3)
```
