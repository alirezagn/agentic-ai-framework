"""Final critical batch — GAP-CRIT-04, GAP-CRIT-07, GAP-CRIT-09 / HIGH-13.

Three vulnerabilities with one shared property: each was *silently* permissive.

* **CRIT-04** — the review path discarded the Definition-of-Done problems it had
  just computed, so a reviewer could return PASS on a deliverable the framework
  already knew did not exist. Now the problems travel to the reviewer as a
  pre-verdict, are persisted so a later cycle can recover them, and a PASS over
  standing problems cannot reach DONE.
* **CRIT-07** — ``.env`` was resolved from ``Path.cwd()``, so running the CLI in
  any shared directory silently adopted that directory's ``OLLAMA_BASE_URL`` and
  sent every prompt there. Now strictly ``PROJECT_ROOT / ".env"``.
* **CRIT-09 / HIGH-13** — all 11 ``AGENT_PROMPTS/*.md`` files documented a
  top-level ``"documents": [{"name", "content"}]`` array that the runtime
  discards, and none mentioned the delivery keys at all. The contract now lives
  in code next to the parser that enforces it, and the files are generated from
  it so they cannot drift again.

Layout note: the suite lives at the repository root (``conftest.py`` supplies
``build_test_project``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator.agents.base_agent import BaseAgent  # noqa: E402
from orchestrator.checkpoint_manager import CheckpointManager  # noqa: E402
from orchestrator.orchestrator import (  # noqa: E402
    MasterOrchestrator,
    OrchestratorError,
    _recorded_dod_problems,
)
from orchestrator.prompt_builder import OUTPUT_FORMAT_INSTRUCTIONS, build_prompt  # noqa: E402
from orchestrator.state_manager import StateManager  # noqa: E402

PROMPT_DIR = REPO_ROOT / "framework" / "AGENT_PROMPTS"
PROMPT_FILES = sorted(PROMPT_DIR.glob("*.md"))
PROMPT_FILES = [path for path in PROMPT_FILES if path.name != "README.md"]


def _read_env_pairs(path: Path) -> List[Tuple[str, str]]:
    """Parse KEY=VALUE pairs from a .env file, for adoption assertions."""
    pairs: List[Tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        pairs.append((key.strip(), value.strip()))
    return pairs

#: Keys the runtime actually reads to deliver a file or evidence. The old
#: prompts documented a top-level ``documents`` array and none of these.
DELIVERY_KEYS = ("data.documents", "data.edits", "data.deploy", "data.acceptance_results")


# ===========================================================================
# GAP-CRIT-07 — .env confined to PROJECT_ROOT
# ===========================================================================


class TestEnvFileContainment:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """Isolate the environment, restoring it in full afterwards.

        ``monkeypatch`` only reverts the calls *it* made. These tests call
        ``load_env_file()``, which sets variables by assigning to
        ``os.environ`` directly — so monkeypatch has no record of them and the
        repository's real ``.env`` would leak into every later test. A full
        snapshot is the only reliable restore.
        """
        snapshot = dict(os.environ)
        for name in list(snapshot):
            if name.startswith(("ORCHESTRATOR_", "OLLAMA_", "ANTHROPIC_", "OPENROUTER_")):
                monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(snapshot)

    def test_default_path_is_project_root(self) -> None:
        assert config.env_file_path() == config.PROJECT_ROOT / ".env"
        assert config.env_file_path().parent == config.PROJECT_ROOT

    def test_default_path_is_never_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert config.env_file_path() != tmp_path / ".env"

    def test_cwd_env_file_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The vulnerability: a hostile directory could configure the runtime.

        The assertion is that the CWD file's *values* never appear — not that
        nothing loads, because PROJECT_ROOT/.env legitimately still does.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "ORCHESTRATOR_LLM_PROVIDER=evil-provider\n"
            "OLLAMA_BASE_URL=http://attacker.invalid:11434\n",
            encoding="utf-8",
        )
        config.load_env_file()
        assert os.environ.get("ORCHESTRATOR_LLM_PROVIDER") != "evil-provider"
        assert os.environ.get("OLLAMA_BASE_URL") != "http://attacker.invalid:11414"
        for key, value in _read_env_pairs(tmp_path / ".env"):
            assert os.environ.get(key) != value, f"{key} was adopted from the CWD .env"

    def test_explicit_path_outside_root_is_refused(self, tmp_path: Path) -> None:
        outside = tmp_path / "evil.env"
        outside.write_text("ORCHESTRATOR_LLM_PROVIDER=evil-provider\n", encoding="utf-8")
        assert config.load_env_file(outside) == 0
        assert os.environ.get("ORCHESTRATOR_LLM_PROVIDER") != "evil-provider"

    def test_project_root_env_file_is_still_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fix must not disable the feature.

        os.environ is snapshotted and restored by hand: loading the repo's own
        .env sets keys that monkeypatch never recorded, so without this the
        whole repository's configuration leaks into later tests.
        """
        inside = config.PROJECT_ROOT / ".env"
        snapshot = dict(os.environ)
        existed = inside.exists()
        original = inside.read_text() if existed else None
        probe = "ORCHESTRATOR_TEST_PROBE_KEY"
        try:
            inside.write_text(f"{probe}=inside-root\n", encoding="utf-8")
            monkeypatch.delenv(probe, raising=False)
            assert config.load_env_file() >= 1
            assert os.environ[probe] == "inside-root"
        finally:
            if original is not None:
                inside.write_text(original, encoding="utf-8")
            elif not existed:
                inside.unlink(missing_ok=True)
            os.environ.clear()
            os.environ.update(snapshot)

    def test_symlink_escape_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Containment must be checked after resolution, not before.

        A symlink inside PROJECT_ROOT pointing at an attacker-controlled file
        would defeat a check that only looked at the path string.
        """
        target = tmp_path / "evil.env"
        target.write_text("ORCHESTRATOR_LLM_PROVIDER=evil\n", encoding="utf-8")
        link = config.PROJECT_ROOT / ".env.probe-link"
        if link.exists() or link.is_symlink():
            link.unlink()
        try:
            link.symlink_to(target)
            assert config._within_project_root(link) is False
            assert config.load_env_file(link) == 0
            assert "ORCHESTRATOR_LLM_PROVIDER" not in os.environ
        finally:
            link.unlink(missing_ok=True)

    def test_containment_accepts_the_root_itself(self) -> None:
        assert config._within_project_root(config.PROJECT_ROOT) is True

    def test_containment_rejects_the_parent(self) -> None:
        assert config._within_project_root(config.PROJECT_ROOT.parent) is False

    def test_maybe_load_env_file_uses_the_same_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CWD file must not be adopted, even through the CLI entry point."""
        snapshot = dict(os.environ)
        try:
            monkeypatch.chdir(tmp_path)
            (tmp_path / ".env").write_text(
                "ORCHESTRATOR_LLM_PROVIDER=evil-provider\n", encoding="utf-8"
            )
            config.maybe_load_env_file()
            assert os.environ.get("ORCHESTRATOR_LLM_PROVIDER") != "evil-provider"
        finally:
            os.environ.clear()
            os.environ.update(snapshot)

    def test_maybe_load_env_file_still_respects_the_pytest_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "synthetic")
        assert config.maybe_load_env_file() == 0


# ===========================================================================
# GAP-CRIT-04 — DoD problems reach the reviewer
# ===========================================================================


class _CapturingReviewer(BaseAgent):
    """Records the payload it was dispatched with.

    Registers under ``review_agent`` because ``dispatch_review`` always resolves
    that key — a stub with its own id would simply never be called.
    """

    AGENT_ID = "review_agent"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.seen: List[Dict[str, Any]] = []

    def execute(self, payload: Dict[str, Any]) -> Any:
        self.seen.append(payload)
        return self.completed(
            str(payload["task"]["id"]),
            "reviewed",
            data={"review_status": "PASS", "findings": []},
        )


def _review_task(orchestrator: MasterOrchestrator) -> str:
    """Put TASK-002 into REVIEW with an unmet expected output."""
    document = orchestrator.state.load_tasks_document()
    for entry in document["tasks"]:
        if entry["id"] == "TASK-002":
            entry["status"] = "REVIEW"
            entry["review"] = {"required": True, "status": "READY"}
            # An expected output that does not exist: the DoD must reject it.
            entry["expected_outputs"] = ["docs/NEVER_DELIVERED.md"]
    orchestrator.state.save_tasks_document(document)
    return "TASK-002"


class TestReviewSeesDodProblems:
    def test_recorded_problems_are_recoverable_from_the_task(self) -> None:
        task = {"id": "TASK-002", "pending_dod_problems": ["a", "b"]}
        assert _recorded_dod_problems(task) == ["a", "b"]

    def test_absent_problems_return_empty(self) -> None:
        assert _recorded_dod_problems({"id": "TASK-002"}) == []
        assert _recorded_dod_problems({"pending_dod_problems": "not a list"}) == []
        assert _recorded_dod_problems({"pending_dod_problems": [None, "  "]}) == []

    def test_dispatch_forwards_problems_to_the_reviewer(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        task_id = _review_task(orchestrator)
        reviewer = _CapturingReviewer(state_manager=orchestrator.state)
        orchestrator.register_agent(reviewer)

        problems = ["expected output missing from the project: docs/NEVER_DELIVERED.md"]
        orchestrator.dispatch_review(task_id, dod_problems=problems)

        assert reviewer.seen, "the reviewer was never dispatched"
        payload = reviewer.seen[0]
        assert payload["pending_dod_problems"] == problems
        assert payload["task"]["pending_dod_problems"] == problems

    def test_problems_appear_in_the_rendered_prompt(self) -> None:
        """The payload alone is not enough: it has to reach the model."""
        prompt = build_prompt(
            {
                "task": {"id": "TASK-002"},
                "pending_dod_problems": [
                    "expected output missing from the project: main/x.c"
                ],
            }
        )
        assert "UNMET DEFINITION OF DONE" in prompt
        assert "expected output missing from the project: main/x.c" in prompt
        assert "A PASS verdict is invalid" in prompt

    def test_prompt_omits_the_section_when_nothing_is_pending(self) -> None:
        prompt = build_prompt({"task": {"id": "TASK-002"}, "pending_dod_problems": []})
        assert "UNMET DEFINITION OF DONE" not in prompt

    def test_dispatch_persists_problems_for_a_later_cycle(self, tmp_path: Path) -> None:
        """A review resumed next run must still see the failure."""
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        task_id = _review_task(orchestrator)
        problems = ["expected output missing from the project: docs/X.md"]
        orchestrator._persist_dod_problems(task_id, problems)
        # Reload from disk, as a fresh process would.
        reloaded = StateManager(project).get_task(task_id)
        assert _recorded_dod_problems(reloaded) == problems

    def test_pending_reviews_forwards_recorded_problems(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        task_id = _review_task(orchestrator)
        orchestrator._persist_dod_problems(task_id, ["a problem"])
        reviewer = _CapturingReviewer(state_manager=orchestrator.state)
        orchestrator.register_agent(reviewer)
        orchestrator._run_pending_reviews()
        assert reviewer.seen
        assert reviewer.seen[0]["pending_dod_problems"] == ["a problem"]

    def test_clearing_removes_the_record(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        task_id = _review_task(orchestrator)
        orchestrator._persist_dod_problems(task_id, ["a problem"])
        orchestrator._clear_dod_problems(task_id)
        assert _recorded_dod_problems(StateManager(project).get_task(task_id)) == []


class TestPassCannotOverrideAnUnmetDod:
    def test_pass_over_pending_problems_becomes_fail(self, tmp_path: Path) -> None:
        """The core hole: reviewer said PASS, DoD said no file exists."""
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        task_id = _review_task(orchestrator)
        orchestrator._persist_dod_problems(task_id, ["expected output missing"])
        status = orchestrator.complete_review(task_id, outcome="PASS")
        assert status == config.TASK_FAILED
        assert orchestrator.get_task(task_id)["status"] == config.TASK_FAILED

    def test_note_records_why_pass_was_downgraded(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        task_id = _review_task(orchestrator)
        orchestrator._persist_dod_problems(task_id, ["expected output missing"])
        orchestrator.complete_review(task_id, outcome="PASS")
        note = orchestrator.get_task(task_id)["notes"]
        assert "Definition of Done was unmet" in note
        assert "expected output missing" in note

    def test_a_clean_pass_still_completes(self, tmp_path: Path) -> None:
        """The guard must not make review useless."""
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        task = orchestrator.get_task("TASK-002")
        task["status"] = "REVIEW"
        task["review"] = {"required": True, "status": "READY"}
        task["expected_outputs"] = ["docs/TASK-002.md"]
        orchestrator.state.save_tasks_document(
            {"tasks": [t if t["id"] == "TASK-002" else t for t in orchestrator.state.load_tasks()]}
        )
        document = orchestrator.state.load_tasks_document()
        for entry in document["tasks"]:
            if entry["id"] == "TASK-002":
                entry.update(
                    {
                        "status": "REVIEW",
                        "review": {"required": True, "status": "READY"},
                        "expected_outputs": ["docs/TASK-002.md"],
                    }
                )
        orchestrator.state.save_tasks_document(document)
        (project / "docs").mkdir(exist_ok=True)
        (project / "docs" / "TASK-002.md").write_text("# done\n", encoding="utf-8")
        assert orchestrator.complete_review("TASK-002", outcome="PASS") == config.TASK_DONE

    def test_fabricated_test_task_cannot_reach_done(self, tmp_path: Path) -> None:
        """The end-to-end PoC: prose-only delivery, review required, PASS returned.

        Pre-fix this reached DONE.
        """

        class FabricatingReviewer(BaseAgent):
            AGENT_ID = "review_agent"

            def execute(self, payload: Dict[str, Any]) -> Any:
                return self.completed(
                    str(payload["task"]["id"]),
                    "looks good to me",
                    data={"review_status": "PASS", "findings": []},
                )

        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        orchestrator.register_agent(FabricatingReviewer(state_manager=orchestrator.state))
        _review_task(orchestrator)
        result = orchestrator.run_task("TASK-002")
        assert result.new_status == config.TASK_FAILED, (
            f"expected FAILED, got {result.new_status}"
        )
        assert orchestrator.get_task("TASK-002")["status"] == config.TASK_FAILED


# ===========================================================================
# GAP-CRIT-09 / HIGH-13 — prompt contracts
# ===========================================================================


class TestPromptTemplates:
    def test_all_eleven_templates_exist(self) -> None:
        assert len(PROMPT_FILES) == 11, [path.name for path in PROMPT_FILES]

    @pytest.mark.parametrize("path", PROMPT_FILES, ids=lambda p: p.name)
    def test_no_legacy_documents_array(self, path: Path) -> None:
        """The shape the runtime silently discarded."""
        text = path.read_text(encoding="utf-8")
        assert '"documents": [' not in text, f"{path.name} still documents the array shape"
        assert '"documents": [{"name"' not in text

    @pytest.mark.parametrize("path", PROMPT_FILES, ids=lambda p: p.name)
    def test_documents_is_shown_as_a_dict(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        assert "data.documents" in text, f"{path.name} does not name the documents key"
        assert '"<expected output path>": "<full file body>"' in text, (
            f"{path.name} does not show the dict shape"
        )
        assert "Never an array" in text

    @pytest.mark.parametrize("key", DELIVERY_KEYS)
    def test_every_template_documents_every_delivery_key(self, key: str) -> None:
        missing = [
            path.name
            for path in PROMPT_FILES
            if key not in path.read_text(encoding="utf-8")
        ]
        assert not missing, f"{key} missing from: {missing}"

    @pytest.mark.parametrize("path", PROMPT_FILES, ids=lambda p: p.name)
    def test_template_matches_the_runtime_contract(self, path: Path) -> None:
        """One source of truth: the file is generated from the constant.

        This is the assertion that stops the drift GAP-CRIT-09 came from — the
        docs specified a shape nothing read, and nothing compared them to the
        parser.
        """
        text = path.read_text(encoding="utf-8")
        assert OUTPUT_FORMAT_INSTRUCTIONS in text, (
            f"{path.name} no longer embeds the canonical output contract"
        )

    @pytest.mark.parametrize("path", PROMPT_FILES, ids=lambda p: p.name)
    def test_template_states_there_is_no_top_level_documents_key(self, path: Path) -> None:
        assert "no top-level `documents` key" in path.read_text(encoding="utf-8")

    @pytest.mark.parametrize("path", PROMPT_FILES, ids=lambda p: p.name)
    def test_template_warns_against_invented_results(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        assert "NOT RUN" in text, f"{path.name} does not offer the honest-failure path"

    def test_contract_names_no_top_level_documents(self) -> None:
        assert 'There is NO top-level "documents" key' in OUTPUT_FORMAT_INSTRUCTIONS

    def test_contract_documents_both_deadline_channels(self) -> None:
        assert "data.documents" in OUTPUT_FORMAT_INSTRUCTIONS
        assert "data.edits" in OUTPUT_FORMAT_INSTRUCTIONS
        assert "Never an array" in OUTPUT_FORMAT_INSTRUCTIONS

    def test_contract_documents_deploy_and_acceptance(self) -> None:
        assert "data.deploy" in OUTPUT_FORMAT_INSTRUCTIONS
        assert "data.deploy_results" in OUTPUT_FORMAT_INSTRUCTIONS
        assert "data.acceptance_results" in OUTPUT_FORMAT_INSTRUCTIONS
        assert "data.test_status" in OUTPUT_FORMAT_INSTRUCTIONS

    def test_orchestrator_and_supervisor_are_marked_non_agents(self) -> None:
        """00/01 are not registry ids; saying so prevents a future reader
        assuming their spec text reaches a model."""
        for name in ("00_ORCHESTRATOR.md", "01_SUPERVISOR.md"):
            text = (PROMPT_DIR / name).read_text(encoding="utf-8")
            assert "No registry id" in text, f"{name} still implies it is an agent"


class TestRuntimeContractReachesEveryAgent:
    def test_all_registered_agents_carry_the_new_contract(self) -> None:
        from orchestrator.agents import AGENT_REGISTRY

        assert len(AGENT_REGISTRY) == 10
        for name, agent_class in AGENT_REGISTRY.items():
            agent = agent_class.__new__(agent_class)
            agent.extra_rules = []
            rules = agent.system_rules()
            for key in ("data.documents", "data.edits", "data.deploy", "data.acceptance_results"):
                assert key in rules, f"{name} misses {key}"
            assert "NOT RUN" in rules, f"{name} misses the honest-failure path"

    def test_rendered_system_prompt_embeds_the_contract(self) -> None:
        from orchestrator.prompt_builder import render_system_prompt

        rendered = render_system_prompt("some rules", "")
        assert OUTPUT_FORMAT_INSTRUCTIONS in rendered

    def test_agent_payload_has_the_dod_problems_slot(self, tmp_path: Path) -> None:
        """GAP-CRIT-04: the field must exist even when empty, so the reviewer
        code path is stable whether or not there were problems."""
        agent = BaseAgent(project_path=tmp_path)
        payload = agent.build_payload({"id": "TASK-001", "title": "t"})
        assert payload["pending_dod_problems"] == []
        payload = agent.build_payload(
            {"id": "TASK-001", "title": "t", "pending_dod_problems": ["x"]}
        )
        assert payload["pending_dod_problems"] == ["x"]


class TestDodProblemIsNotTreatedAsAgentInput:
    def test_pending_problems_survive_append_task_sanitisation(
        self, tmp_path: Path
    ) -> None:
        """`pending_dod_problems` is review-flow state, not model input.

        It is written directly to the document, so the ingestion sanitiser must
        not be relied on for it — and a model must not be able to inject it.
        """
        state = StateManager(build_test_project(tmp_path / "p"))
        created = state.append_task(
            {"title": "t", "owner": "software_agent", "status": "DONE"}
        )
        stored = state.get_task(created["id"])
        assert "pending_dod_problems" not in stored
        assert stored["status"] == config.TASK_TODO
