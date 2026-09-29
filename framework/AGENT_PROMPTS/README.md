# AGENT PROMPTS

Individual agent prompt specifications used by the LLM-backed specialists.
Each file documents the runtime class, inputs, JSON output contract, system
rules and Definition of Done hints. The authoritative long-form specs live in
`framework/0X_*.md`; `orchestrator/prompt_builder.py` embeds them into the
system prompt automatically.

| File | Agent | Runtime |
|---|---|---|
| [00_ORCHESTRATOR.md](00_ORCHESTRATOR.md) | `orchestrator (role, not an LLM agent)` | `orchestrator/orchestrator.py (MasterOrchestrator)` |
| [01_SUPERVISOR.md](01_SUPERVISOR.md) | `supervisor_agent (role)` | `orchestrator/supervisor.py (SupervisorAgent)` |
| [02_REQUIREMENTS.md](02_REQUIREMENTS.md) | `requirements_agent` | `orchestrator/agents/requirements_agent.py (RequirementsAgent)` |
| [03_RESEARCH.md](03_RESEARCH.md) | `research_agent` | `orchestrator/agents/specialists.py (ResearchAgent)` |
| [04_ARCHITECTURE.md](04_ARCHITECTURE.md) | `architecture_agent` | `orchestrator/agents/specialists.py (ArchitectureAgent)` |
| [05_PLANNING.md](05_PLANNING.md) | `planning_agent` | `orchestrator/agents/specialists.py (PlanningAgent)` |
| [06_HARDWARE.md](06_HARDWARE.md) | `hardware_agent` | `orchestrator/agents/specialists.py (HardwareAgent)` |
| [07_SOFTWARE_FIRMWARE.md](07_SOFTWARE_FIRMWARE.md) | `software_agent` | `orchestrator/agents/specialists.py (SoftwareAgent, id software_agent, alias firmware_agent)` |
| [08_TEST.md](08_TEST.md) | `test_agent` | `orchestrator/agents/specialists.py (TestAgent)` |
| [09_REVIEW.md](09_REVIEW.md) | `review_agent` | `orchestrator/agents/specialists.py (ReviewAgent)` |
| [10_DOCUMENTATION.md](10_DOCUMENTATION.md) | `documentation_agent` | `orchestrator/agents/specialists.py (DocumentationAgent)` |

See also: `framework/TEMPLATES/` (document templates), `framework/20_DEFAULT_PROJECT_START_PROMPT.md` (session bootstrap).
