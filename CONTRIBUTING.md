# Contributing to Agentic AI Framework

## What This Project Is

This is a framework for orchestrating complex technical projects using autonomous AI agents. It's designed to be:

- **Forkable:** Copy the repo and start your own project
- **Extensible:** Add specialist agents as needed
- **Teachable:** Study working examples to understand the patterns
- **Transparent:** All state/decisions/risks in project files, not hidden in chat

## How to Contribute

### Adding New Projects

1. Copy `project-templates/` to `projects/my-project/`
2. Fill in PROJECT.yaml with your project details
3. Initialize state files (requirements, architecture, tasks, etc.)
4. When project reaches 1.0.0 release status, create a pull request

### Improving the Framework

1. **Agent Specs:** Suggest improvements to `framework/0X_AGENT.md`
2. **Templates:** Add new document templates to `framework/TEMPLATES/`
3. **Documentation:** Update `meta/GETTING_STARTED.md` or create new guides
4. **Examples:** Create working example projects in `projects/`

### Bug Reports

If you find issues with:
- State file structure
- Agent responsibility definitions
- Checkpoint/recovery procedures
- Loop detection thresholds

Please open an issue with:
- What you expected
- What actually happened
- Reproduction steps (if applicable)
- Which framework document/agent was involved

## Project Status

- **Framework:** Production Ready (v1.0)
- **Example (Kid-Robot-Face):** In Progress (alpha → beta → release)

## Code of Conduct

This is a professional framework for serious project work. Contributions should be:

- **Clear:** Documentation and examples should be understandable
- **Honest:** Framework should surface problems, not hide them
- **Tested:** State the framework has been used successfully; include evidence
- **Respectful:** Acknowledge other approaches; explain trade-offs

## Philosophy

This framework exists because:

1. Chat history grows unwieldy
2. Decisions disappear in conversation noise
3. Context management is hard
4. Parallel work needs coordination
5. Progress should be measurable, not narrative

If your contribution makes it easier for teams to:
- Resume from checkpoints
- Coordinate parallel work
- Track decisions + rationale
- Measure progress objectively
- Recover from failures

...then it's aligned with the project's purpose.

---

**Questions?** Open an issue or start a discussion.

**Ready to contribute?** Fork, create a feature branch, and submit a pull request.

Thank you for improving the framework!
