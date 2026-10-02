"""Static import enforcement: a guessed interface must fail the delivery.

``sys_mon_gui``-class failure this exists to stop: the consumer was written as
``from src.metrics_collector import get_metrics_snapshot`` while the producer
exports ``get_system_metrics``. The file parses, the prompt contract said
"read the producer first", and nothing ever imported the code — so the
mistake reached the operator's terminal while the task was already DONE.

The checker is pure ``ast`` work (:mod:`orchestrator.import_contract`), wired
into :func:`delivery_problems` so the DoD and the one automatic repair round
see it. Scope is deliberately symmetric with responsibility: a file is judged
only when this task delivered it, or when it imports a module this task
delivered.

Layout note: the suite lives at the repository root (``conftest.py`` supplies
``build_test_project``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator.agents.base_agent import (  # noqa: E402
    AgentOutput,
    delivery_problems,
)
from orchestrator.import_contract import import_contract_problems  # noqa: E402

PRODUCER = (
    "import logging\n"
    "import psutil\n"
    "\n"
    "logger = logging.getLogger(__name__)\n"
    "\n"
    "def get_system_metrics():\n"
    "    return {\"cpu_load\": 1.0}\n"
    "\n"
    "class MetricSnapshot(dict):\n"
    "    pass\n"
)

CONSUMER_OK = (
    "from .metrics_collector import get_system_metrics\n"
    "\n"
    "def refresh():\n"
    "    return get_system_metrics()\n"
)

CONSUMER_DRIFTED = (
    "from .metrics_collector import get_metrics_snapshot\n"
    "\n"
    "def refresh():\n"
    "    return get_metrics_snapshot()\n"
)


def _project(root: Path, files: Dict[str, str]) -> Path:
    project = root / "p"
    project.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return project


def _output(delivered: Dict[str, str]) -> AgentOutput:
    return AgentOutput(
        agent_id="software_agent",
        task_id="TASK-001",
        status=config.AGENT_STATUS_COMPLETED,
        summary="delivered",
        data={"documents": delivered},
    )


# ===========================================================================
# The failure itself
# ===========================================================================


class TestInterfaceDriftIsCaught:
    def test_guessed_producer_name_is_reported(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": PRODUCER,
                "src/gui_controller.py": CONSUMER_DRIFTED,
            },
        )
        problems = import_contract_problems(project, ["src/gui_controller.py"])
        assert len(problems) == 1
        message = problems[0]
        assert "src/gui_controller.py" in message
        assert "get_metrics_snapshot" in message
        assert "src/metrics_collector.py" in message
        # The repair round must be able to act: the real name is named.
        assert "get_system_metrics" in message

    def test_producer_side_drift_is_reported(self, tmp_path: Path) -> None:
        """The task changed the producer; an older consumer now imports a
        name that no longer exists. The consumer was not delivered, but the
        breakage is this task's."""
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": PRODUCER,
                "src/gui_controller.py": "from .metrics_collector import old_api\n\n"
                "def refresh():\n    return old_api()\n",
            },
        )
        problems = import_contract_problems(project, ["src/metrics_collector.py"])
        assert problems, "a producer that drops a still-imported name must fail"
        assert "old_api" in problems[0]

    def test_correct_import_passes(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": PRODUCER,
                "src/gui_controller.py": CONSUMER_OK,
            },
        )
        assert import_contract_problems(project, ["src/gui_controller.py"]) == []

    def test_multiple_missing_names_are_listed_together(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": PRODUCER,
                "src/gui_controller.py": "from .metrics_collector import get_a, get_b\n",
            },
        )
        problems = import_contract_problems(project, ["src/gui_controller.py"])
        assert len(problems) == 1
        assert "get_a, get_b" in problems[0]
        assert "them" in problems[0]

    def test_absolute_intra_project_import_is_checked(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": PRODUCER,
                "main.py": "from src.metrics_collector import get_metrics_snapshot\n",
            },
        )
        problems = import_contract_problems(project, ["main.py"])
        assert len(problems) == 1
        assert "get_metrics_snapshot" in problems[0]


# ===========================================================================
# Scope: only this task's own breakage
# ===========================================================================


class TestScopeIsLimitedToTheTask:
    def test_pre_existing_breakage_is_not_this_tasks_fault(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": PRODUCER,
                "src/gui_controller.py": CONSUMER_DRIFTED,
            },
        )
        # This task delivered something else entirely (a doc).
        assert import_contract_problems(project, ["docs/REPORT.md"]) == []

    def test_third_party_imports_are_never_checked(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/gui_controller.py": "import psutil\n"
                "from tkinter import ttk\n"
                "import requests.sessions\n"
                "from numpy import ndarray\n",
            },
        )
        assert import_contract_problems(project, ["src/gui_controller.py"]) == []

    def test_docs_mirrors_are_not_application_code(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": PRODUCER,
                "docs/gui_controller.py": CONSUMER_DRIFTED,
            },
        )
        assert import_contract_problems(project, ["docs/gui_controller.py"]) == []

    def test_empty_delivery_checks_nothing(self, tmp_path: Path) -> None:
        project = _project(tmp_path, {"main.py": "from nope import missing\n"})
        assert import_contract_problems(project, []) == []


# ===========================================================================
# Resolver edges
# ===========================================================================


class TestResolver:
    def test_sibling_package_import_without_init(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": PRODUCER,
                "main.py": "from src import metrics_collector\n\n"
                "def go():\n    return metrics_collector.get_system_metrics()\n",
            },
        )
        assert import_contract_problems(project, ["main.py"]) == []

    def test_from_package_import_submodule_name(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/__init__.py": "",
                "src/metrics_collector.py": PRODUCER,
                "main.py": "from src import metrics_collector\n",
            },
        )
        assert import_contract_problems(project, ["main.py"]) == []

    def test_missing_relative_module_is_reported(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {"src/gui_controller.py": "from .nonexistent import thing\n"},
        )
        problems = import_contract_problems(project, ["src/gui_controller.py"])
        assert len(problems) == 1
        assert "nonexistent" in problems[0]

    def test_missing_project_module_import_is_reported(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/__init__.py": "",
                "main.py": "import src.missing_module\nimport psutil\n",
            },
        )
        problems = import_contract_problems(project, ["main.py"])
        assert len(problems) == 1
        assert "src.missing_module" in problems[0]

    def test_star_import_is_not_guessed_at(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {"src/metrics_collector.py": PRODUCER, "src/x.py": "from .metrics_collector import *\n"},
        )
        assert import_contract_problems(project, ["src/x.py"]) == []

    def test_module_getattr_makes_any_name_legal(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": PRODUCER + "\n\ndef __getattr__(name):\n    raise AttributeError(name)\n",
                "src/x.py": "from .metrics_collector import whatever_you_like\n",
            },
        )
        assert import_contract_problems(project, ["src/x.py"]) == []

    def test_type_checking_guard_still_defines_the_name(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": "from __future__ import annotations\n"
                "from typing import TYPE_CHECKING\n\n"
                "if TYPE_CHECKING:\n"
                "    from dataclasses import dataclass\n\n"
                "    class Snapshot:\n"
                "        pass\n"
                "def get_system_metrics():\n    return {}\n",
                "src/x.py": "from .metrics_collector import Snapshot\n",
            },
        )
        assert import_contract_problems(project, ["src/x.py"]) == []

    def test_assignment_names_count_as_defined(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": "A, B = 1, 2\nC: int = 3\n",
                "src/x.py": "from .metrics_collector import A, C\n",
            },
        )
        assert import_contract_problems(project, ["src/x.py"]) == []

    def test_conditional_definition_counts(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": "try:\n    import ujson as json_mod\n"
                "except ImportError:\n    json_mod = None\n",
                "src/x.py": "from .metrics_collector import json_mod\n",
            },
        )
        assert import_contract_problems(project, ["src/x.py"]) == []


# ===========================================================================
# The file itself must be importable
# ===========================================================================


class TestDeliveredFileMustParse:
    def test_syntax_error_in_a_delivered_file_is_reported(self, tmp_path: Path) -> None:
        project = _project(tmp_path, {"main.py": "def broken(:\n    pass\n"})
        problems = import_contract_problems(project, ["main.py"])
        assert len(problems) == 1
        assert "does not parse" in problems[0]
        assert "main.py" in problems[0]

    def test_syntax_error_elsewhere_is_not_this_tasks_fault(
        self, tmp_path: Path
    ) -> None:
        project = _project(
            tmp_path,
            {"broken.py": "def broken(:\n", "ok.py": "X = 1\n"},
        )
        assert import_contract_problems(project, ["ok.py"]) == []


# ===========================================================================
# Wiring: DoD and edit sessions see the problems
# ===========================================================================


class TestDeliveryIntegration:
    def test_delivery_problems_reports_drift(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": PRODUCER,
                "src/gui_controller.py": CONSUMER_DRIFTED,
                "docs/gui_controller.py": CONSUMER_DRIFTED,
            },
        )
        problems = delivery_problems(
            project,
            {"expected_outputs": ["src/gui_controller.py"]},
            _output({"src/gui_controller.py": CONSUMER_DRIFTED}),
            preexisting=["src/metrics_collector.py"],
        )
        assert problems, problems
        assert all("get_metrics_snapshot" in problem for problem in problems)

    def test_delivery_problems_accepts_a_consistent_delivery(
        self, tmp_path: Path
    ) -> None:
        project = _project(
            tmp_path,
            {
                "src/metrics_collector.py": PRODUCER,
                "src/gui_controller.py": CONSUMER_OK,
                "docs/gui_controller.py": CONSUMER_OK,
            },
        )
        problems = delivery_problems(
            project,
            {"expected_outputs": ["src/gui_controller.py"]},
            _output({"src/gui_controller.py": CONSUMER_OK}),
            preexisting=["src/metrics_collector.py"],
        )
        assert problems == [], problems

    def test_definition_of_done_blocks_on_drift(self, tmp_path: Path) -> None:
        from orchestrator.orchestrator import MasterOrchestrator

        project = build_test_project(tmp_path / "p")
        src = project / "src"
        src.mkdir(exist_ok=True)
        (src / "metrics_collector.py").write_text(PRODUCER, encoding="utf-8")
        (src / "gui_controller.py").write_text(CONSUMER_DRIFTED, encoding="utf-8")

        orch = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        output = orch.resolve_agent("software_agent").completed(
            "TASK-001",
            "wired the controller to the collector",
            data={"documents": {"src/gui_controller.py": CONSUMER_DRIFTED}},
        )
        problems = orch.definition_of_done(
            {
                "id": "TASK-001",
                "acceptance_criteria": ["the GUI reads live metrics"],
                "expected_outputs": [],
                "review": {"required": False, "status": "NOT_STARTED"},
            },
            output,
            deploy_records=[],
        )
        assert any("get_metrics_snapshot" in problem for problem in problems), problems

    def test_definition_of_done_passes_a_consistent_delivery(
        self, tmp_path: Path
    ) -> None:
        from orchestrator.orchestrator import MasterOrchestrator

        project = build_test_project(tmp_path / "p")
        src = project / "src"
        src.mkdir(exist_ok=True)
        (src / "metrics_collector.py").write_text(PRODUCER, encoding="utf-8")
        (src / "gui_controller.py").write_text(CONSUMER_OK, encoding="utf-8")

        orch = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        output = orch.resolve_agent("software_agent").completed(
            "TASK-001",
            "wired the controller to the collector",
            data={"documents": {"src/gui_controller.py": CONSUMER_OK}},
        )
        problems = orch.definition_of_done(
            {
                "id": "TASK-001",
                "acceptance_criteria": ["the GUI reads live metrics"],
                "expected_outputs": [],
                "review": {"required": False, "status": "NOT_STARTED"},
            },
            output,
            deploy_records=[],
        )
        assert problems == [], problems


# ===========================================================================
# The prompt promises what the checker enforces
# ===========================================================================


class TestPromptPromisesTheCheck:
    def test_interface_contract_tells_the_model_imports_are_verified(self) -> None:
        from orchestrator.agents.base_agent import INTERFACE_ALIGNMENT_CONTRACT

        assert "statically verified" in INTERFACE_ALIGNMENT_CONTRACT
        assert "Definition of Done" in INTERFACE_ALIGNMENT_CONTRACT
