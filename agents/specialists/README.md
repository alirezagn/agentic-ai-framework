# Specialist agents (implementation examples)

| Agent | Runtime class | Spec | Prompt |
|---|---|---|---|
| requirements_agent | `orchestrator/agents/requirements_agent.py` | `framework/02_REQUIREMENTS_AGENT.md` | `AGENT_PROMPTS/02_REQUIREMENTS.md` |
| research_agent | `specialists.ResearchAgent` | `03_RESEARCH_AGENT.md` | `03_RESEARCH.md` |
| architecture_agent | `specialists.ArchitectureAgent` | `04_ARCHITECTURE_AGENT.md` | `04_ARCHITECTURE.md` |
| planning_agent | `specialists.PlanningAgent` | `05_PLANNING_AGENT.md` | `05_PLANNING.md` |
| hardware_agent | `specialists.HardwareAgent` | `06_HARDWARE_AGENT.md` | `06_HARDWARE.md` |
| software_agent (alias firmware_agent) | `specialists.SoftwareAgent` | `07_SOFTWARE_FIRMWARE_AGENT.md` | `07_SOFTWARE_FIRMWARE.md` |
| test_agent | `specialists.TestAgent` | `08_TEST_AGENT.md` | `08_TEST.md` |
| review_agent | `specialists.ReviewAgent` | `09_REVIEW_AGENT.md` | `09_REVIEW.md` |
| documentation_agent | `specialists.DocumentationAgent` | `10_DOCUMENTATION_AGENT.md` | `10_DOCUMENTATION.md` |

All specialists subclass `LLMAgent` (prompt from the spec + task payload, JSON
answer parsed back into a validated `AgentOutput`). See `ORCHESTRATOR_GUIDE.md`.
