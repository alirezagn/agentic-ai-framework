# Supervisor (implementation example)

- **Spec:** `framework/01_SUPERVISOR_AGENT.md`
- **Runtime:** `orchestrator/supervisor.py::SupervisorAgent`
- **Prompt:** `framework/AGENT_PROMPTS/01_SUPERVISOR.md`

Responsibilities: health states, loop detection (same_strategy, no_progress,
alternatives_exhausted, state_oscillation, repeated_output), blocked analysis,
escalations into `human_decisions`, and failure risks in `RISKS.md`.

```python
report = orch.sync_health()
print(report.render())
```
