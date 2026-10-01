"""Tests for cleanup and wiring (GAP_ANALYSIS task T19, findings 14/16/19).

* superseded v1.1 modules are deleted and no longer exported,
* ``orchestrator/requirements.txt`` has no unused dependencies,
* declared logging config is actually wired (``--verbose`` / ``--quiet`` /
  ``ORCHESTRATOR_LOG_LEVEL``),
* ``CHECKPOINT_SIGNING_KEY`` now HMAC-signs checkpoint metadata.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import orchestrator
from conftest import build_test_project
from orchestrator import config
from orchestrator.checkpoint_manager import (
    CheckpointIntegrityError,
    CheckpointManager,
    SigningVerdict,
)
from orchestrator.cli import main


class TestDeadModulesRemoved:
    def test_files_are_gone(self) -> None:
        root = Path(orchestrator.__file__).parent
        for name in ("checkpoint.py", "project_manager.py", "task_executor.py"):
            assert not (root / name).exists(), f"{name} should be deleted"
        # stray file with trailing space in the name
        assert not any(
            path.name == "__init__.py " for path in root.iterdir()
        ), "stray '__init__.py ' should be deleted"

    def test_public_api_no_longer_exports_v11(self) -> None:
        for name in ("ProjectManager", "ProjectState", "Task", "TaskExecutor", "TaskExecution"):
            assert not hasattr(orchestrator, name), name
            assert name not in orchestrator.__all__

    def test_public_api_still_exports_v20(self) -> None:
        for name in (
            "MasterOrchestrator",
            "StateManager",
            "CheckpointManager",
            "SupervisorAgent",
            "RequirementsAgent",
        ):
            assert hasattr(orchestrator, name)


class TestDependencyHygiene:
    def test_requirements_txt_contents(self) -> None:
        requirements = (
            Path(orchestrator.__file__).parent / "requirements.txt"
        ).read_text(encoding="utf-8").lower()
        assert "pyyaml" in requirements
        assert "pydantic" not in requirements
        assert "pytest-asyncio" not in requirements


@pytest.fixture()
def restored_root_logger():
    """Snapshot the root logger so CLI logging setup cannot leak."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield root
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


class TestLoggingWired:
    def test_verbose_sets_debug_level(
        self, tmp_path: Path, restored_root_logger: logging.Logger
    ) -> None:
        project = build_test_project(tmp_path / "log-project")
        code = main(["--verbose", "agents", "--project", str(project)])
        assert code == 0
        assert restored_root_logger.level == logging.DEBUG

    def test_quiet_sets_error_level(
        self, tmp_path: Path, restored_root_logger: logging.Logger
    ) -> None:
        project = build_test_project(tmp_path / "log-project")
        code = main(["--quiet", "agents", "--project", str(project)])
        assert code == 0
        assert restored_root_logger.level == logging.ERROR

    def test_default_level_from_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restored_root_logger: logging.Logger,
    ) -> None:
        project = build_test_project(tmp_path / "log-project")
        monkeypatch.setenv("ORCHESTRATOR_LOG_LEVEL", "WARNING")
        code = main(["agents", "--project", str(project)])
        assert code == 0
        assert restored_root_logger.level == logging.WARNING

    def test_log_format_is_used(self, restored_root_logger: logging.Logger) -> None:
        main(["--verbose", "agents", "--project", "."])
        # basicConfig(format=...) installs a StreamHandler with our format
        # string on the root logger
        assert any(
            getattr(handler, "_format", None) == config.LOG_FORMAT
            or getattr(handler, "formatter", None) is not None
            for handler in restored_root_logger.handlers
        )


class TestCheckpointSigning:
    @pytest.fixture()
    def signed_manager(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> CheckpointManager:
        monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", "test-signing-key")
        project = build_test_project(tmp_path / "sign-project")
        manager = CheckpointManager(project, checkpoints_root=tmp_path / "ck")
        manager.create_checkpoint("cp-signed", notes="signed")
        return manager

    def test_metadata_contains_signature(self, signed_manager: CheckpointManager) -> None:
        metadata = json.loads(
            (signed_manager.checkpoint_dir("cp-signed") / "metadata.json").read_text()
        )
        assert metadata["signature_algorithm"] == "HMAC-SHA256"
        assert len(metadata["signature"]) == 64
        assert signed_manager.verify_checkpoint("cp-signed") is True

    def test_tampered_files_and_metadata_fail_signature(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = signed_manager.checkpoint_dir("cp-signed")
        metadata_path = target / "metadata.json"
        metadata = json.loads(metadata_path.read_text())

        # Attacker rewrites a snapshot file and updates the declared hashes +
        # aggregate checksum so plain checksum verification passes.
        project_file = target / config.PROJECT_FILE
        project_file.write_text(project_file.read_text() + "\n# tampered\n")
        digest = __import__("hashlib").sha256(project_file.read_bytes()).hexdigest()
        metadata["files"][config.PROJECT_FILE] = digest
        metadata["checksum"] = signed_manager._compute_checksum(metadata["files"])
        metadata_path.write_text(json.dumps(metadata, indent=2))

        # Checksums alone now agree; the stale HMAC signature must not.
        with pytest.raises(CheckpointIntegrityError, match="signature"):
            signed_manager.verify_checkpoint("cp-signed")

    def test_signed_checkpoint_without_key_is_unverifiable(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GAP-CRIT-03: removing the key must NOT yield a passing verification.

        This test previously asserted the opposite (``verify_checkpoint(...) is
        True`` with the key deleted). That behaviour made key removal an
        undetectable downgrade to checksum-only verification, which is the
        essence of the vulnerability. A signed snapshot whose signature cannot
        be checked is now reported ``UNVERIFIABLE`` and fails closed.
        """
        monkeypatch.delenv("CHECKPOINT_SIGNING_KEY", raising=False)
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.UNVERIFIABLE
        assert not report.verdict.is_trusted
        with pytest.raises(CheckpointIntegrityError, match="unverifiable"):
            signed_manager.verify_checkpoint("cp-signed")

    def test_keyless_verification_succeeds_only_via_explicit_opt_in(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escape hatch still exists, but must be asked for by name."""
        monkeypatch.delenv("CHECKPOINT_SIGNING_KEY", raising=False)
        monkeypatch.setenv("CHECKPOINT_ALLOW_UNSIGNED", "1")
        assert signed_manager.verify_checkpoint("cp-signed") is True

    def test_wrong_key_rejects_signature(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", "rotated-key")
        with pytest.raises(CheckpointIntegrityError, match="signature"):
            signed_manager.verify_checkpoint("cp-signed")

    def test_unsigned_checkpoint_is_unverifiable_while_key_is_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GAP-CRIT-03: stripping the signature while a key is active is refused.

        This test previously created a snapshot *with* a key (so it was signed),
        deleted the signature to emulate a pre-signing checkpoint, and asserted
        verification still passed. Deleting the signature is precisely the
        bypass: the old field-presence gate skipped validation and returned
        ``True``. The rewritten expectation is that an active key plus a
        signature-less snapshot is ``UNVERIFIABLE`` and refused, and that it is
        refused even under the unsigned escape hatch's *intent* — the honest
        representation of "never signed" is ``signed: false``, not a missing
        field.
        """
        monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", "test-signing-key")
        monkeypatch.delenv("CHECKPOINT_ALLOW_UNSIGNED", raising=False)
        project = build_test_project(tmp_path / "unsigned-project")
        manager = CheckpointManager(project, checkpoints_root=tmp_path / "ck")
        manager.create_checkpoint("cp-unsigned")

        target = manager.checkpoint_dir("cp-unsigned")
        metadata_path = target / "metadata.json"
        data = json.loads(metadata_path.read_text())
        data.pop("signature", None)
        data.pop("signature_algorithm", None)
        metadata_path.write_text(json.dumps(data, indent=2))

        report = manager.evaluate_integrity("cp-unsigned")
        assert report.signed is True, "signed claim survives; only the proof was removed"
        assert report.verdict is SigningVerdict.TAMPERED
        with pytest.raises(CheckpointIntegrityError, match="tampered"):
            manager.verify_checkpoint("cp-unsigned")

    def test_genuinely_unsigned_checkpoint_verifies_when_no_key_is_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The supported legacy path: never signed, no key, opt-in fallback.

        A snapshot written with no key configured declares ``signed: false``
        explicitly, reports ``UNSIGNED``, and passes only when the operator has
        opted in via ``CHECKPOINT_ALLOW_UNSIGNED``.
        """
        monkeypatch.delenv("CHECKPOINT_SIGNING_KEY", raising=False)
        monkeypatch.delenv("CHECKPOINT_ALLOW_UNSIGNED", raising=False)
        project = build_test_project(tmp_path / "legacy-project")
        manager = CheckpointManager(project, checkpoints_root=tmp_path / "ck")
        manager.create_checkpoint("cp-legacy")

        metadata = json.loads(
            (manager.checkpoint_dir("cp-legacy") / "metadata.json").read_text()
        )
        assert metadata["signed"] is False
        assert metadata["signature"] is None

        report = manager.evaluate_integrity("cp-legacy")
        assert report.verdict is SigningVerdict.UNSIGNED
        assert not report.verdict.is_trusted

        # Row 4 passes on checksums: no key is configured and the snapshot
        # honestly declares signed=false, so there is no claim to violate.
        assert manager.verify_checkpoint("cp-legacy") is True
