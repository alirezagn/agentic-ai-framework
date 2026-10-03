"""Edit-session feedback must quote the file bodies the model is asked to fix.

Failure this pins — sys-usage TASK-003 (2026-10-03): the software agent's
3-turn edit session created ``src/metrics_collector.py`` in turn 1 with
``from .architecture import ...`` (no such module; the TypedDicts live only
as a code block inside docs/ARCHITECTURE.md). Every session turn rebuilds a
single fresh prompt — there is no conversation history — and the base prompt
is frozen at session start, when the expected output was still MISSING, so
turns 2..N could not see the file they were told to fix ("fix the import or
deliver the module"). The model fumbled all three turns, TASK-003 FAILED,
and the pipeline stalled in HUMAN_DECISION_REQUIRED with 4 blocked tasks.

Guarantees:

1. The Definition-of-Done feedback branch quotes the current on-disk content
   of every file the session already touched, so the next turn edits against
   authoritative text instead of guessing.
2. The apply-failure branch keeps quoting content (now for documents and
   edits_applied targets too, not only data.edits).
3. The quote is bounded (per-file cap + file-count cap) so a wide delivery
   cannot turn the turn budget into a prompt bomb.

Layout note: the suite lives at the repository root (``conftest.py`` supplies
``build_test_project`` / ``FakeLLMClient``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import FakeLLMClient, build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator.agents.llm_agent import LLMAgent  # noqa: E402

BROKEN_BODY = (
    "from .architecture import SystemMetrics\n"
    "\n"
    "\n"
    "def get_system_metrics():\n"
    "    return SystemMetrics\n"
)

IMPORT_PROBLEM = (
    "src/metrics_collector.py does `from .architecture import ...` but "
    "no module exists at that relative path — fix the import or deliver "
    "the module"
)


def _answer(task_id: str, agent_id: str, summary: str, **data: Any) -> str:
    return json.dumps(
        {
            "agent_id": agent_id,
            "task_id": task_id,
            "status": "completed",
            "summary": summary,
            "data": data,
            "errors": [],
            "warnings": [],
        }
    )


def _collect_task(outputs: List[str]) -> Dict[str, Any]:
    return {
        "id": "TASK-003",
        "title": "Backend Data Layer",
        "owner": "software_agent",
        "status": "READY",
        "priority": "HIGH",
        "dependencies": ["TASK-002"],
        "expected_outputs": outputs,
        "acceptance_criteria": ["Module retrieves system stats using psutil"],
        "input_files": [],
        "review": {"required": False, "status": "NOT_STARTED"},
    }


def _agent(project: Path, responses: List[str]):
    client = FakeLLMClient(responses)

    class Dummy(LLMAgent):
        AGENT_ID = "software_agent"
        EDIT_SESSION_TURNS = 3

    return Dummy(project_path=project, llm_client=client), client


# ===========================================================================
# The DoD branch of the session feedback carries the file bodies
# ===========================================================================


class TestDoDFeedbackCarriesFileContent:
    def test_turn2_prompt_quotes_delivered_body_and_problem(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        turn1 = _answer(
            "TASK-003",
            "software_agent",
            "Implemented the metrics collector",
            documents={"src/metrics_collector.py": BROKEN_BODY},
        )
        turn2 = _answer(
            "TASK-003",
            "software_agent",
            "Dropped the import that names a missing module",
            edits={
                "src/metrics_collector.py": {
                    "search": "from .architecture import SystemMetrics",
                    "replace": "SystemMetrics = dict",
                }
            },
        )
        agent, client = _agent(project, [turn1, turn2])

        output = agent.run(_collect_task(["src/metrics_collector.py"]))

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert len(client.calls) == 2, "turn 1 flags, turn 2 fixes"
        prompt2 = client.calls[1]["messages"][0]["content"]
        # the problem the checker found is echoed back …
        assert "no module exists at that relative path" in prompt2
        # … together with the authoritative body of the file it names,
        # which the frozen base prompt cannot contain (the file was MISSING
        # when the session started).
        assert "current content of src/metrics_collector.py" in prompt2
        assert "from .architecture import SystemMetrics" in prompt2
        # and the scripted fix really landed on disk
        fixed = (project / "src" / "metrics_collector.py").read_text(
            encoding="utf-8"
        )
        assert "SystemMetrics = dict" in fixed
        assert "from .architecture" not in fixed

    def test_failed_session_still_ends_failed_after_last_turn(
        self, tmp_path: Path
    ) -> None:
        """Content feedback does not mask an unfixable delivery: the last
        turn still fails honestly with the checker's problems attached."""
        project = build_test_project(tmp_path / "p")
        turn1 = _answer(
            "TASK-003",
            "software_agent",
            "wrote a collector that imports a missing module",
            documents={"src/metrics_collector.py": BROKEN_BODY},
        )
        # turns 2 and 3 keep the broken import alive through the edit channel,
        # so every turn's DoD verdict is the same import-contract problem.
        turn2 = _answer(
            "TASK-003",
            "software_agent",
            "touched the body, kept the import",
            edits={
                "src/metrics_collector.py": {
                    "search": "def get_system_metrics():",
                    "replace": "def get_system_metrics():  # still broken",
                }
            },
        )
        turn3 = _answer(
            "TASK-003",
            "software_agent",
            "touched the body again, kept the import",
            edits={
                "src/metrics_collector.py": {
                    "search": "# still broken",
                    "replace": "# still broken v2",
                }
            },
        )
        agent, client = _agent(project, [turn1, turn2, turn3])

        output = agent.run(_collect_task(["src/metrics_collector.py"]))

        assert output.status == config.AGENT_STATUS_FAILED
        assert any(
            "no module exists at that relative path" in err
            for err in output.errors
        ), output.errors
        assert len(client.calls) == 3


# ===========================================================================
# The session's delivery scope is cumulative across turns
# ===========================================================================


class TestSessionDeliveryScopeIsCumulative:
    """A later turn must not narrow the scope the session is judged against.

    Failure this pins (sys-usage TASK-005, rerun 3): turn 2 of the session
    delivered ``collector.py`` / ``processor.py`` / ``formatter.py`` and the
    checker flagged ``processor.py``'s undeclared ``typing_extensions``
    import; turn 3 answered with a *different* clean file only, its own
    per-turn delivery set had no problems, and the session returned COMPLETED
    with the import defect forgotten — the dispatch-level DoD re-read only
    turn 3's keys, so nothing short of the executed-verification gate could
    catch it.
    """

    BAD_IMPORT_BODY = "from typing_extensions import TypedDict\n\n\n"
    FIXED_BODY = "from typing import TypedDict\n\n\n"
    APP_BODY = "import processor\n\n\ndef main():\n    return 0\n"

    def _task(self, outputs):
        return _collect_task(outputs)

    def test_turn2_cannot_close_over_turn1s_undeclared_import(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        turn1 = _answer(
            "TASK-003",
            "software_agent",
            "Delivered the processor module",
            documents={"processor.py": self.BAD_IMPORT_BODY},
        )
        turn2 = _answer(
            "TASK-003",
            "software_agent",
            "Delivered the expected app entry point",
            documents={"src/app.py": self.APP_BODY},
        )
        turn3 = _answer(
            "TASK-003",
            "software_agent",
            "Replaced the undeclared typing_extensions import",
            edits={
                "processor.py": {
                    "search": "from typing_extensions import TypedDict",
                    "replace": "from typing import TypedDict",
                }
            },
        )
        agent, client = _agent(project, [turn1, turn2, turn3])

        output = agent.run(self._task(["src/app.py"]))

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert len(client.calls) == 3, (
            "turn 2 delivered a clean file while turn 1's undeclared import "
            "was still on disk — the session must keep asking, not close"
        )
        # the scope the completed session claims covers every turn's files,
        # so the dispatch-level DoD re-checks the whole delivery, not just
        # the last reply's keys
        touched = set(output.data.get("edits_applied") or []) | set(
            output.data.get("documents") or {}
        )
        assert "processor.py" in touched
        assert "src/app.py" in touched
        fixed = (project / "processor.py").read_text(encoding="utf-8")
        assert "typing_extensions" not in fixed

    def test_unfixed_earlier_defect_fails_the_session(
        self, tmp_path: Path
    ) -> None:
        """When later turns never repair the earlier file, the session fails
        honestly with the original problem still attached — the turn budget
        does not launder a defect out of the delivery contract."""
        project = build_test_project(tmp_path / "p")
        turn1 = _answer(
            "TASK-003",
            "software_agent",
            "Delivered the processor module",
            documents={"processor.py": self.BAD_IMPORT_BODY},
        )
        turn2 = _answer(
            "TASK-003",
            "software_agent",
            "Delivered the expected app entry point",
            documents={"src/app.py": self.APP_BODY},
        )
        turn3 = _answer(
            "TASK-003",
            "software_agent",
            "Touched only the app entry point again",
            edits={
                "src/app.py": {
                    "search": "def main():",
                    "replace": "def main():  # untouched defect",
                }
            },
        )
        agent, client = _agent(project, [turn1, turn2, turn3])

        output = agent.run(self._task(["src/app.py"]))

        assert output.status == config.AGENT_STATUS_FAILED, output.status
        assert any(
            "typing_extensions" in err for err in output.errors
        ), output.errors
        assert len(client.calls) == 3


# ===========================================================================
# The helper behind the feedback
# ===========================================================================


class TestTouchedFilesCurrentContent:
    def _agent(self, project: Path):
        return _agent(project, [])[0]

    def test_includes_documents_edits_applied_and_edits(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        (project / "src").mkdir(exist_ok=True)
        (project / "src" / "a.py").write_text("AAA = 1\n", encoding="utf-8")
        (project / "src" / "b.py").write_text("BBB = 2\n", encoding="utf-8")
        agent = self._agent(project)
        output = agent.completed(
            "TASK-001",
            "mixed channels",
            data={
                "documents": {"src/a.py": "AAA = 1\n"},
                "edits_applied": ["src/b.py"],
                "edits": {"src/a.py": {"search": "AAA", "replace": "AAA"}},
            },
        )
        text = agent._touched_files_current_content(output)
        assert "current content of src/a.py" in text
        assert "current content of src/b.py" in text
        # quoted exactly once even though a.py appears on two channels
        assert text.count("current content of src/a.py") == 1
        assert "AAA = 1" in text and "BBB = 2" in text

    def test_absolute_and_escape_paths_are_skipped(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        (project / "src").mkdir(exist_ok=True)
        (project / "src" / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")
        agent = self._agent(project)
        output = agent.completed(
            "TASK-001",
            "unsafe channels",
            data={
                "documents": {
                    "src/keep.py": "KEEP = 1\n",
                    "/etc/passwd": "root:x\n",
                    "../escape.py": "nope\n",
                },
            },
        )
        text = agent._touched_files_current_content(output)
        assert "current content of src/keep.py" in text
        assert "/etc/passwd" not in text
        assert "escape.py" not in text

    def test_missing_files_are_skipped_not_quoted(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        agent = self._agent(project)
        output = agent.completed(
            "TASK-001",
            "phantom file",
            data={"edits_applied": ["src/never_written.py"]},
        )
        assert agent._touched_files_current_content(output) == ""

    def test_file_count_is_capped_with_an_omission_note(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        (project / "src").mkdir(exist_ok=True)
        documents: Dict[str, str] = {}
        for index in range(9):
            rel = f"src/f{index}.py"
            (project / rel).write_text(f"VALUE_{index} = {index}\n", encoding="utf-8")
            documents[rel] = f"VALUE_{index} = {index}\n"
        agent = self._agent(project)
        output = agent.completed("TASK-001", "wide delivery", data={"documents": documents})

        text = agent._touched_files_current_content(output)
        assert text.count("current content of") == 8
        assert "1 more" in text and "omitted" in text


# ===========================================================================
# The apply-failure branch keeps its content quote (regression)
# ===========================================================================


class TestApplyFailureFeedbackStillQuotesContent:
    def test_wrong_search_feedback_includes_the_real_body(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        (project / "src").mkdir(exist_ok=True)
        (project / "src" / "x.py").write_text("VALUE = 1\n", encoding="utf-8")
        turn1 = _answer(
            "TASK-003",
            "software_agent",
            "blind patch",
            edits={"src/x.py": {"search": "NOPE_NOT_THERE", "replace": "X = 2"}},
        )
        turn2 = _answer(
            "TASK-003",
            "software_agent",
            "exact patch",
            edits={"src/x.py": {"search": "VALUE = 1", "replace": "VALUE = 2"}},
        )
        agent, client = _agent(project, [turn1, turn2])

        output = agent.run(_collect_task(["src/x.py"]))

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        prompt2 = client.calls[1]["messages"][0]["content"]
        assert "could not be applied" in prompt2
        assert "current content of src/x.py" in prompt2
        assert "VALUE = 1" in prompt2
        assert "NOPE_NOT_THERE" not in prompt2  # the bad guess is not authoritative
        assert (project / "src" / "x.py").read_text(encoding="utf-8").strip().endswith(
            "VALUE = 2"
        )
