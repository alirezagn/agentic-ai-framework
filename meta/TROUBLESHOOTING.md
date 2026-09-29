# TROUBLESHOOTING

| Symptom | Cause | Fix |
|---|---|---|
| `ERROR: --project is required` | flag missing | `orchestrator --project projects/<name> <cmd>` |
| `PROJECT.yaml: ...` from `status` | corrupt/missing state file | `orchestrator --project <path> status` prints `validate()` problems; restore from `checkpoint restore` |
| Tasks stay READY | unsatisfied dependencies | `orchestrator --project <path> tasks` shows deps; finish/fix predecessors |
| Dispatch refused, `LOOP LIMIT` | same_strategy / no_progress / alternatives / oscillation / repeated_output exceeded | change strategy on the task, replan, or escalate; CLI exit code 3 |
| `HUMAN_DECISION_REQUIRED` (exit 4) | pending `PROPOSED_CHANGE` in DECISIONS.md | `orch.approve_decision("DEC-NNN", approved=True/False)` or edit status |
| Task FAILED with `DoD unmet: ...` | missing outputs or failed review | materialize expected outputs / address `docs/REVIEW-<task>.md` findings |
| LLM backend unavailable | no provider configured | set `ORCHESTRATOR_LLM_PROVIDER` + API key or `OLLAMA_BASE_URL` (see `ORCHESTRATOR_GUIDE.md`) |
| Context utilisation high | long session | checkpoint + resume; compaction warns at 70%, critical at 85% |
| Tests touch my live project | wrong project path in tests | use `build_test_project(tmp_path)`; never dispatch against live projects |

Suite: `python3 -m pytest -q`.
