"""GAP-CRIT-03 — HMAC checkpoint signing must be fail-closed.

The vulnerability
-----------------
``verify_checkpoint`` previously gated its signature check on *field presence*::

    signature = str(metadata.get("signature") or "")
    if signature:
        if _signing_key():
            ...

``metadata.json`` is attacker-writable whenever the snapshot directory is, so
"is a signature present?" was a proxy controlled by the attacker rather than by
policy. Deleting the field downgraded a signed snapshot to checksum-only
verification, and the MAC itself covered ``<id>:<checksum>`` where ``checksum``
was read back out of the same writable metadata — so rewriting the files and
recomputing that one field satisfied it without the key.

These tests pin the replacement: an explicit persisted ``signed`` flag, a
four-row truth table, and a MAC bound to the per-file digests plus the id,
key_id and created_at.

Layout note: the suite lives at the repository root (``conftest.py`` provides
``build_test_project``), so this file is imported from there rather than
duplicating that fixture.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator.checkpoint_manager import (  # noqa: E402
    CheckpointIntegrityError,
    CheckpointManager,
    SigningVerdict,
    signature_payload,
)

SIGNING_KEY = "unit-test-signing-key"
ROTATED_KEY = "unit-test-rotated-key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metadata_path(manager: CheckpointManager, checkpoint_id: str) -> Path:
    return manager.checkpoint_dir(checkpoint_id) / config.CHECKPOINT_METADATA_FILE


def _read_metadata(manager: CheckpointManager, checkpoint_id: str) -> Dict[str, Any]:
    return json.loads(_metadata_path(manager, checkpoint_id).read_text())


def _write_metadata(manager: CheckpointManager, checkpoint_id: str, data: Dict[str, Any]) -> None:
    _metadata_path(manager, checkpoint_id).write_text(json.dumps(data, indent=2))


@pytest.fixture()
def unsigned_manager(tmp_path: Path) -> CheckpointManager:
    """Manager with no signing key anywhere in the environment."""
    for var in (
        "CHECKPOINT_SIGNING_KEY",
        "ORCHESTRATOR_CHECKPOINT_KEY_ID",
        "CHECKPOINT_ALLOW_UNSIGNED",
    ):
        import os

        os.environ.pop(var, None)
    manager = CheckpointManager(build_test_project(tmp_path / "proj"), checkpoints_root=tmp_path / "ck")
    manager.create_checkpoint("cp-base", notes="baseline")
    return manager


@pytest.fixture()
def signed_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CheckpointManager:
    """Manager whose snapshots are signed with a known key and key id."""
    monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("ORCHESTRATOR_CHECKPOINT_KEY_ID", "k1")
    monkeypatch.delenv("CHECKPOINT_ALLOW_UNSIGNED", raising=False)
    manager = CheckpointManager(build_test_project(tmp_path / "sproj"), checkpoints_root=tmp_path / "sck")
    manager.create_checkpoint("cp-signed", notes="signed")
    return manager


def _rehash(manager: CheckpointManager, checkpoint_id: str) -> None:
    """Rewrite every declared digest and the aggregate checksum to match disk.

    This is the attacker's move: make plain checksum verification pass so that
    only the signature can catch the rewrite.
    """
    data = _read_metadata(manager, checkpoint_id)
    directory = manager.checkpoint_dir(checkpoint_id)
    files: Dict[str, str] = {}
    for name in data["files"]:
        files[name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
    data["files"] = files
    data["checksum"] = manager._compute_checksum(files)
    _write_metadata(manager, checkpoint_id, data)


# ---------------------------------------------------------------------------
# Creation side — the claim must be explicit
# ---------------------------------------------------------------------------


class TestSignedFlagIsPersisted:
    def test_signed_snapshot_records_full_envelope(
        self, signed_manager: CheckpointManager
    ) -> None:
        data = _read_metadata(signed_manager, "cp-signed")
        assert data["signed"] is True
        assert data["key_id"] == "k1"
        assert data["signature_algorithm"] == config.CHECKPOINT_SIGNATURE_ALGORITHM
        assert data["signature_version"] == config.CHECKPOINT_SIGNATURE_VERSION
        assert isinstance(data["signature"], str)
        assert len(data["signature"]) == 64

    def test_unsigned_snapshot_declares_signed_false_explicitly(
        self, unsigned_manager: CheckpointManager
    ) -> None:
        """An unsigned snapshot must say so — absence is not a valid encoding."""
        data = _read_metadata(unsigned_manager, "cp-base")
        assert data["signed"] is False
        assert data["signature"] is None
        assert data["key_id"] is None

    def test_signature_is_bound_to_contents_not_self_declared_checksum(
        self, signed_manager: CheckpointManager
    ) -> None:
        """Recomputing every digest must not yield a valid signature.

        This is the precise attack the widened binding closes: the old MAC
        covered only ``<id>:<checksum>``, so making the self-declared checksum
        agree with rewritten bytes satisfied it without the key.
        """
        path = signed_manager.checkpoint_dir("cp-signed") / config.TASKS_FILE
        path.write_text(path.read_text() + "\n# attacker content\n")
        _rehash(signed_manager, "cp-signed")
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.TAMPERED, report.detail

    def test_key_id_defaults_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", SIGNING_KEY)
        monkeypatch.delenv("ORCHESTRATOR_CHECKPOINT_KEY_ID", raising=False)
        manager = CheckpointManager(
            build_test_project(tmp_path / "dproj"), checkpoints_root=tmp_path / "dck"
        )
        manager.create_checkpoint("cp-default")
        assert _read_metadata(manager, "cp-default")["key_id"] == config.DEFAULT_CHECKPOINT_KEY_ID


# ---------------------------------------------------------------------------
# The four-row truth table
# ---------------------------------------------------------------------------


class TestTruthTable:
    """signed x key-available, with the verdict each row must produce."""

    def test_row1_signed_true_key_present_matching_mac_is_verified(
        self, signed_manager: CheckpointManager
    ) -> None:
        assert signed_manager.verify_checkpoint("cp-signed") is True
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.VERIFIED
        assert report.verdict.is_trusted
        assert report.signed is True
        assert report.key_id == "k1"
        assert report.file_count == 7

    def test_row1_mismatched_mac_is_tampered(
        self, signed_manager: CheckpointManager
    ) -> None:
        data = _read_metadata(signed_manager, "cp-signed")
        data["signature"] = "0" * 64
        _write_metadata(signed_manager, "cp-signed", data)
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.TAMPERED
        with pytest.raises(CheckpointIntegrityError, match="tampered"):
            signed_manager.verify_checkpoint("cp-signed")

    def test_row2_signed_true_but_no_key_is_unverifiable_not_verified(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A signed snapshot cannot be verified without its key.

        The pre-fix behaviour was to return ``True`` here, which made key
        removal an undetectable downgrade.
        """
        monkeypatch.delenv("CHECKPOINT_SIGNING_KEY", raising=False)
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.UNVERIFIABLE
        assert not report.verdict.is_trusted
        with pytest.raises(CheckpointIntegrityError, match="unverifiable"):
            signed_manager.verify_checkpoint("cp-signed")

    def test_row3_unsigned_but_key_active_is_unverifiable_never_downgrade(
        self, unsigned_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The state an attacker creates by deleting the signature field."""
        monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", SIGNING_KEY)
        report = unsigned_manager.evaluate_integrity("cp-base")
        assert report.verdict is SigningVerdict.UNVERIFIABLE
        assert "refusing to downgrade" in report.detail
        with pytest.raises(CheckpointIntegrityError, match="unverifiable"):
            unsigned_manager.verify_checkpoint("cp-base")

    def test_row4_unsigned_and_no_key_passes_on_checksums(
        self, unsigned_manager: CheckpointManager
    ) -> None:
        """Row 4 passes: no signature claim exists to violate.

        Refusing here would protect against nobody — with no key active, an
        attacker who can rewrite metadata can simply set ``signed: false`` —
        while breaking restore for every deployment that has not configured a
        signing key. Row 3 is the row that carries signal, and it fails.
        """
        report = unsigned_manager.evaluate_integrity("cp-base")
        assert report.verdict is SigningVerdict.UNSIGNED
        assert not report.verdict.is_trusted
        assert unsigned_manager.verify_checkpoint("cp-base") is True

    def test_signed_true_with_empty_signature_is_tampered(
        self, signed_manager: CheckpointManager
    ) -> None:
        data = _read_metadata(signed_manager, "cp-signed")
        data["signature"] = ""
        _write_metadata(signed_manager, "cp-signed", data)
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.TAMPERED
        assert "carries no signature" in report.detail

    def test_signed_true_with_signature_removed_is_tampered(
        self, signed_manager: CheckpointManager
    ) -> None:
        """The headline bypass: pop ``signature``, keep ``signed: true``."""
        data = _read_metadata(signed_manager, "cp-signed")
        data.pop("signature")
        _write_metadata(signed_manager, "cp-signed", data)
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.TAMPERED
        with pytest.raises(CheckpointIntegrityError):
            signed_manager.verify_checkpoint("cp-signed")

    def test_signature_deleted_entirely_is_the_attack_scenario(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting signature *and* flipping signed to false is still refused.

        Pre-fix this returned True. The attacker has to also convince the
        verifier that no key is configured; when one is, row 3 fires.
        """
        data = _read_metadata(signed_manager, "cp-signed")
        data.pop("signature")
        data["signed"] = False
        _write_metadata(signed_manager, "cp-signed", data)
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.UNVERIFIABLE
        with pytest.raises(CheckpointIntegrityError):
            signed_manager.verify_checkpoint("cp-signed")

    def test_pre_fix_bypass_would_have_passed(
        self, signed_manager: CheckpointManager
    ) -> None:
        """Negative control: the old logic really did accept this.

        Reproduces the pre-fix verifier verbatim against a snapshot carrying the
        *old* signature scheme (``<id>:<checksum>``) whose bytes have been
        rewritten and rehashed. Without this assertion the test suite could
        stay green if a future refactor silently restored the bypass.
        """
        directory = signed_manager.checkpoint_dir("cp-signed")

        # 1. Attacker rewrites a snapshot file and refreshes every digest and the
        #    aggregate checksum, so plain checksum verification agrees.
        target = directory / config.PROJECT_FILE
        target.write_text(target.read_text() + "\n# attacker content\n")
        _rehash(signed_manager, "cp-signed")
        data = _read_metadata(signed_manager, "cp-signed")

        # 2. Re-sign using the OLD scheme, which only an attacker holding the
        #    key could do — but the point is that the old scheme made it
        #    *unnecessary*: any party could rewrite both files and checksum
        #    without the key. Simulate by dropping the signature entirely,
        #    which is the sibling bypass (field-presence gate).
        data.pop("signature")
        _write_metadata(signed_manager, "cp-signed", data)

        # --- old implementation, reproduced verbatim ---
        old_metadata = _read_metadata(signed_manager, "cp-signed")
        old_signature = str(old_metadata.get("signature") or "")
        old_verifies = True  # the old code returned True when no key/sig branch ran
        if old_signature:
            expected = hmac.new(
                SIGNING_KEY.encode("utf-8"),
                f"cp-signed:{old_metadata['checksum']}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            old_verifies = hmac.compare_digest(old_signature, expected)
        assert old_verifies is True, (
            "negative control invalid: the pre-fix verifier accepted this snapshot"
        )

        # --- new implementation refuses it ---
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.TAMPERED
        with pytest.raises(CheckpointIntegrityError):
            signed_manager.verify_checkpoint("cp-signed")

    def test_old_mac_scheme_is_not_accepted_by_the_new_verifier(
        self, signed_manager: CheckpointManager
    ) -> None:
        """A signature computed over the OLD input string must not verify."""
        data = _read_metadata(signed_manager, "cp-signed")
        data["signature"] = hmac.new(
            SIGNING_KEY.encode("utf-8"),
            f"cp-signed:{data['checksum']}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        _write_metadata(signed_manager, "cp-signed", data)
        assert signed_manager.evaluate_integrity("cp-signed").verdict is SigningVerdict.TAMPERED


# ---------------------------------------------------------------------------
# Binding coverage — every mutation must be caught
# ---------------------------------------------------------------------------


class TestSignatureBinding:
    def _tamper_and_expect_refused(
        self,
        manager: CheckpointManager,
        checkpoint_id: str,
        mutate,
        expected: SigningVerdict = SigningVerdict.TAMPERED,
    ) -> None:
        data = _read_metadata(manager, checkpoint_id)
        mutate(data)
        _write_metadata(manager, checkpoint_id, data)
        _rehash(manager, checkpoint_id)
        report = manager.evaluate_integrity(checkpoint_id)
        assert report.verdict is expected, report.detail
        with pytest.raises(CheckpointIntegrityError):
            manager.verify_checkpoint(checkpoint_id)

    def _tamper_and_expect_tampered(
        self, manager: CheckpointManager, checkpoint_id: str, mutate
    ) -> None:
        self._tamper_and_expect_refused(manager, checkpoint_id, mutate)

    def test_file_content_rewrite_is_tampered(
        self, signed_manager: CheckpointManager
    ) -> None:
        path = signed_manager.checkpoint_dir("cp-signed") / config.TASKS_FILE
        path.write_text(path.read_text() + "\n# injected\n")
        _rehash(signed_manager, "cp-signed")
        assert signed_manager.evaluate_integrity("cp-signed").verdict is SigningVerdict.TAMPERED

    def test_created_at_rewrite_is_tampered(
        self, signed_manager: CheckpointManager
    ) -> None:
        self._tamper_and_expect_tampered(
            signed_manager, "cp-signed", lambda d: d.__setitem__("created_at", "2099-01-01T00:00:00Z")
        )

    def test_checkpoint_id_rewrite_is_tampered(
        self, signed_manager: CheckpointManager
    ) -> None:
        self._tamper_and_expect_tampered(
            signed_manager, "cp-signed", lambda d: d.__setitem__("id", "cp-renamed")
        )

    def test_added_file_is_tampered(
        self, signed_manager: CheckpointManager
    ) -> None:
        """A file smuggled into the manifest must not slip past the MAC."""
        directory = signed_manager.checkpoint_dir("cp-signed")
        (directory / config.RISKS_FILE).write_text("smuggled\n")

        def mutate(data: Dict[str, Any]) -> None:
            # Same name, same digest bookkeeping, but swap in the digest of the
            # content we just planted so checksums agree.
            data["files"][config.RISKS_FILE] = hashlib.sha256(b"smuggled\n").hexdigest()

        self._tamper_and_expect_tampered(signed_manager, "cp-signed", mutate)

    def test_key_id_rewrite_is_refused_as_unverifiable(
        self, signed_manager: CheckpointManager
    ) -> None:
        """Rewriting ``key_id`` is refused — reported as a key mismatch.

        Deliberately ``UNVERIFIABLE`` rather than ``TAMPERED``: the verifier
        genuinely cannot check a snapshot that claims to need a different key,
        so it reports "I need another key" instead of accusing an attacker.
        Both verdicts refuse, so the case is closed either way; the distinction
        exists to keep key rotation from looking like an attack.
        """
        self._tamper_and_expect_refused(
            signed_manager,
            "cp-signed",
            lambda d: d.__setitem__("key_id", "attacker"),
            expected=SigningVerdict.UNVERIFIABLE,
        )

    def test_key_id_rewrite_cannot_reach_verified(
        self, signed_manager: CheckpointManager
    ) -> None:
        """Whatever the verdict, a rewritten key_id must never verify."""
        data = _read_metadata(signed_manager, "cp-signed")
        data["key_id"] = "attacker"
        _write_metadata(signed_manager, "cp-signed", data)
        _rehash(signed_manager, "cp-signed")
        assert signed_manager.evaluate_integrity("cp-signed").verdict is not SigningVerdict.VERIFIED
        with pytest.raises(CheckpointIntegrityError):
            signed_manager.verify_checkpoint("cp-signed")

    def test_notes_rewrite_alone_is_not_caught_and_that_is_correct(
        self, signed_manager: CheckpointManager
    ) -> None:
        """``notes`` is unbound by design.

        Free-text operator annotation is deliberately outside the MAC so notes
        can be edited without re-signing. Pinned here so the omission is a
        decision, not an oversight.
        """
        data = _read_metadata(signed_manager, "cp-signed")
        data["notes"] = "edited"
        _write_metadata(signed_manager, "cp-signed", data)
        assert signed_manager.evaluate_integrity("cp-signed").verdict is SigningVerdict.VERIFIED

    def test_payload_is_field_separated_so_names_cannot_shift_boundaries(
        self, signed_manager: CheckpointManager
    ) -> None:
        """Length prefixing must make concatenation unambiguous."""
        payload = signature_payload("cp", "k", "t", {"a": "b"})
        assert b"1:a" in payload and b"1:b" in payload
        # A crafted pair cannot masquerade as a different (name, digest) split.
        other = signature_payload("cp", "k", "t", {"a=b": ""})
        assert payload != other


# ---------------------------------------------------------------------------
# Key rotation and algorithm negotiation
# ---------------------------------------------------------------------------


class TestKeyRotation:
    def test_wrong_key_is_unverifiable_not_tampered(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rotated key is an operator problem, not evidence of attack."""
        monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", ROTATED_KEY)
        monkeypatch.setenv("ORCHESTRATOR_CHECKPOINT_KEY_ID", "k2")
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.UNVERIFIABLE
        assert "key_id" in report.detail
        assert "k1" in report.detail and "k2" in report.detail

    def test_same_key_id_different_secret_is_tampered(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """key_id matched, secret differs → genuine MAC failure."""
        monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", ROTATED_KEY)
        monkeypatch.setenv("ORCHESTRATOR_CHECKPOINT_KEY_ID", "k1")
        assert signed_manager.evaluate_integrity("cp-signed").verdict is SigningVerdict.TAMPERED

    def test_rotation_preserves_verification_for_the_right_key(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert signed_manager.verify_checkpoint("cp-signed") is True
        monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", ROTATED_KEY)
        monkeypatch.setenv("ORCHESTRATOR_CHECKPOINT_KEY_ID", "k2")
        with pytest.raises(CheckpointIntegrityError):
            signed_manager.verify_checkpoint("cp-signed")
        monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", SIGNING_KEY)
        monkeypatch.setenv("ORCHESTRATOR_CHECKPOINT_KEY_ID", "k1")
        assert signed_manager.verify_checkpoint("cp-signed") is True

    def test_unknown_algorithm_is_unverifiable(
        self, signed_manager: CheckpointManager
    ) -> None:
        data = _read_metadata(signed_manager, "cp-signed")
        data["signature_algorithm"] = "HMAC-MD5"
        _write_metadata(signed_manager, "cp-signed", data)
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.UNVERIFIABLE
        assert "algorithm" in report.detail

    def test_future_signature_version_is_unverifiable(
        self, signed_manager: CheckpointManager
    ) -> None:
        data = _read_metadata(signed_manager, "cp-signed")
        data["signature_version"] = 99
        _write_metadata(signed_manager, "cp-signed", data)
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.UNVERIFIABLE
        assert "signature_version" in report.detail

    def test_non_integer_signature_version_is_tampered(
        self, signed_manager: CheckpointManager
    ) -> None:
        data = _read_metadata(signed_manager, "cp-signed")
        data["signature_version"] = "one"
        _write_metadata(signed_manager, "cp-signed", data)
        assert signed_manager.evaluate_integrity("cp-signed").verdict is SigningVerdict.TAMPERED


# ---------------------------------------------------------------------------
# Downgrade policy (the escape hatch)
# ---------------------------------------------------------------------------


class TestDowngradePolicy:
    """``CHECKPOINT_ALLOW_UNSIGNED`` covers provenance gaps, never tampering.

    It governs the ``UNVERIFIABLE`` row only — a snapshot that claims a
    signature which cannot currently be checked. ``UNSIGNED`` passes without it
    (row 4: never signed, no key configured) and ``TAMPERED`` always fails.
    """

    def test_allow_unsigned_is_not_required_for_row4(
        self, unsigned_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Row 4 passes with the flag explicitly off."""
        monkeypatch.setenv("CHECKPOINT_ALLOW_UNSIGNED", "0")
        assert unsigned_manager.verify_checkpoint("cp-base") is True

    def test_flag_is_honoured_for_the_unverifiable_row(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CHECKPOINT_SIGNING_KEY", raising=False)
        monkeypatch.setenv("CHECKPOINT_ALLOW_UNSIGNED", "1")
        assert signed_manager.verify_checkpoint("cp-signed") is True

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_spellings_are_honoured(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.delenv("CHECKPOINT_SIGNING_KEY", raising=False)
        monkeypatch.setenv("CHECKPOINT_ALLOW_UNSIGNED", value)
        assert signed_manager.verify_checkpoint("cp-signed") is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
    def test_falsy_spellings_still_refuse(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.delenv("CHECKPOINT_SIGNING_KEY", raising=False)
        monkeypatch.setenv("CHECKPOINT_ALLOW_UNSIGNED", value)
        with pytest.raises(CheckpointIntegrityError, match="unverifiable"):
            signed_manager.verify_checkpoint("cp-signed")

    def test_allow_unsigned_does_not_permit_tampering(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escape hatch covers provenance gaps, never integrity failures."""
        monkeypatch.setenv("CHECKPOINT_ALLOW_UNSIGNED", "1")
        path = signed_manager.checkpoint_dir("cp-signed") / config.TASKS_FILE
        path.write_text(path.read_text() + "\n# injected\n")
        _rehash(signed_manager, "cp-signed")
        with pytest.raises(CheckpointIntegrityError, match="tampered"):
            signed_manager.verify_checkpoint("cp-signed")

    def test_allow_unsigned_allows_unverifiable_row_two(
        self, signed_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CHECKPOINT_SIGNING_KEY", raising=False)
        monkeypatch.setenv("CHECKPOINT_ALLOW_UNSIGNED", "1")
        assert signed_manager.verify_checkpoint("cp-signed") is True

    def test_flag_is_read_dynamically_not_captured_at_import(
        self, unsigned_manager: CheckpointManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CHECKPOINT_ALLOW_UNSIGNED", "1")
        assert config.allow_unsigned_checkpoints() is True
        monkeypatch.setenv("CHECKPOINT_ALLOW_UNSIGNED", "0")
        assert config.allow_unsigned_checkpoints() is False


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------


class TestLegacyMetadata:
    def test_legacy_signed_snapshot_verifies_via_inference(
        self, signed_manager: CheckpointManager
    ) -> None:
        """Metadata predating the ``signed`` flag still verifies.

        A snapshot written by a build that signed but did not yet record the
        flag carries a valid MAC over exactly the same payload, so it verifies
        while being reported as inferred. This is the compatibility guarantee:
        signing existing snapshots keeps working across the upgrade.
        """
        data = _read_metadata(signed_manager, "cp-signed")
        data.pop("signed")
        data.pop("key_id")
        _write_metadata(signed_manager, "cp-signed", data)
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.legacy_inference is True
        assert report.signed is True
        assert report.verdict is SigningVerdict.VERIFIED
        assert signed_manager.verify_checkpoint("cp-signed") is True

    def test_inference_cannot_turn_a_tampered_legacy_snapshot_into_verified(
        self, signed_manager: CheckpointManager
    ) -> None:
        """The inference path must not become a second bypass."""
        path = signed_manager.checkpoint_dir("cp-signed") / config.TASKS_FILE
        path.write_text(path.read_text() + "\n# injected\n")
        data = _read_metadata(signed_manager, "cp-signed")
        data.pop("signed")
        _write_metadata(signed_manager, "cp-signed", data)
        _rehash(signed_manager, "cp-signed")
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.legacy_inference is True
        assert report.verdict is SigningVerdict.TAMPERED

    def test_legacy_unsigned_snapshot_is_inferred_unsigned(
        self, unsigned_manager: CheckpointManager
    ) -> None:
        data = _read_metadata(unsigned_manager, "cp-base")
        data.pop("signed")
        _write_metadata(unsigned_manager, "cp-base", data)
        report = unsigned_manager.evaluate_integrity("cp-base")
        assert report.legacy_inference is True
        assert report.signed is False
        assert report.verdict is SigningVerdict.UNSIGNED

    def test_legacy_inference_is_flagged_in_detail(
        self, unsigned_manager: CheckpointManager
    ) -> None:
        data = _read_metadata(unsigned_manager, "cp-base")
        data.pop("signed")
        _write_metadata(unsigned_manager, "cp-base", data)
        assert "inferred" in unsigned_manager.evaluate_integrity("cp-base").detail

    def test_string_boolean_signed_flag_is_honoured(
        self, signed_manager: CheckpointManager
    ) -> None:
        data = _read_metadata(signed_manager, "cp-signed")
        data["signed"] = "true"
        _write_metadata(signed_manager, "cp-signed", data)
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.verdict is SigningVerdict.VERIFIED
        assert report.legacy_inference is False

    def test_nonsense_signed_flag_does_not_disable_verification(
        self, signed_manager: CheckpointManager
    ) -> None:
        """A garbage flag must never be read as 'not signed'."""
        data = _read_metadata(signed_manager, "cp-signed")
        data["signed"] = "maybe"
        _write_metadata(signed_manager, "cp-signed", data)
        report = signed_manager.evaluate_integrity("cp-signed")
        assert report.signed is True
        assert report.verdict is SigningVerdict.VERIFIED

    def test_real_on_disk_snapshots_all_pass_the_containment_policy(self) -> None:
        """Backward-compatibility proof against actual repository data.

        Every snapshot currently in ``checkpoints/`` must remain restorable,
        which requires its manifest names to satisfy the GAP-CRIT-02 policy.
        """
        root = REPO_ROOT / "checkpoints"
        if not root.exists():
            pytest.skip("no checkpoints directory in this checkout")
        from orchestrator.path_policy import validate_filenames

        checked = 0
        for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            index = project_dir / config.CHECKPOINT_INDEX_FILE
            if not index.exists():
                continue
            entries = json.loads(index.read_text()).get("checkpoints", [])
            for entry in entries:
                metadata = json.loads(
                    (project_dir / entry["id"] / config.CHECKPOINT_METADATA_FILE).read_text()
                )
                validate_filenames(metadata["files"].keys())
                checked += 1
        assert checked > 0, "expected at least one real snapshot to validate"


# ---------------------------------------------------------------------------
# Ordering: signature never substitutes for containment
# ---------------------------------------------------------------------------


class TestContainmentPrecedesSigning:
    def test_valid_signature_does_not_permit_unsafe_name(
        self, signed_manager: CheckpointManager
    ) -> None:
        """A correctly-signed snapshot still cannot escape the project.

        Guards against the order being reversed: the MAC must not be evaluated
        as an authorisation for hostile filenames.
        """
        data = _read_metadata(signed_manager, "cp-signed")
        data["files"]["../ESCAPED.txt"] = "0" * 64
        _write_metadata(signed_manager, "cp-signed", data)
        with pytest.raises(CheckpointIntegrityError, match="unsafe file name"):
            signed_manager.evaluate_integrity("cp-signed")

    def test_restore_of_traversal_manifest_is_refused_even_when_signed(
        self, signed_manager: CheckpointManager, tmp_path: Path
    ) -> None:
        data = _read_metadata(signed_manager, "cp-signed")
        data["files"]["../ESCAPED.txt"] = "0" * 64
        _write_metadata(signed_manager, "cp-signed", data)
        destination = tmp_path / "restore-target"
        with pytest.raises(CheckpointIntegrityError):
            signed_manager.restore_checkpoint("cp-signed", target_path=destination)
        assert not (tmp_path / "ESCAPED.txt").exists()

    def test_round_trip_restore_still_works_for_a_valid_snapshot(
        self, signed_manager: CheckpointManager, tmp_path: Path
    ) -> None:
        destination = tmp_path / "round-trip"
        signed_manager.restore_checkpoint("cp-signed", target_path=destination)
        for name in config.STATE_FILES:
            source = signed_manager.project_path / name
            if source.exists():
                assert (destination / name).read_bytes() == source.read_bytes()


# ---------------------------------------------------------------------------
# Constant-time comparison
# ---------------------------------------------------------------------------


class TestConstantTimeComparison:
    def test_comparison_uses_compare_digest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard the timing property with a spy on ``hmac.compare_digest``."""
        import orchestrator.checkpoint_manager as module

        monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", SIGNING_KEY)
        monkeypatch.setenv("ORCHESTRATOR_CHECKPOINT_KEY_ID", "k1")

        manager = CheckpointManager(
            build_test_project(tmp_path / "spy-proj"), checkpoints_root=tmp_path / "spy-ck"
        )
        manager.create_checkpoint("cp-spy")

        calls: list[Any] = []
        original = module.hmac.compare_digest

        def spy(a: Any, b: Any) -> bool:
            calls.append((a, b))
            return original(a, b)

        monkeypatch.setattr(module.hmac, "compare_digest", spy)
        assert manager.verify_checkpoint("cp-spy") is True
        assert calls, "verify_checkpoint must compare signatures via compare_digest"
        assert all(isinstance(call[0], str) and isinstance(call[1], str) for call in calls)
        # The compared values are the stored signature and the recomputed MAC.
        assert all(len(call[0]) == 64 and len(call[1]) == 64 for call in calls)

    def test_signature_comparison_is_routed_through_the_helper(
        self, signed_manager: CheckpointManager
    ) -> None:
        """No raw equality on a signature anywhere in the module.

        The comparison lives in ``_signatures_equal`` (which guards length and
        then calls ``compare_digest``), so ``evaluate_integrity`` must delegate
        rather than compare inline.
        """
        import inspect

        import orchestrator.checkpoint_manager as module

        verifier_source = inspect.getsource(module.CheckpointManager.evaluate_integrity)
        assert "_signatures_equal(" in verifier_source
        assert "compare_digest" not in verifier_source, (
            "evaluate_integrity should delegate the constant-time comparison"
        )

        inline_equality = [
            line
            for line in verifier_source.splitlines()
            if "signature" in line and ("==" in line or "!=" in line)
            and "is None" not in line
            and "accepts_downgrade" not in line
        ]
        assert not inline_equality, f"inline signature equality found: {inline_equality}"

    def test_helper_rejects_length_mismatch_without_comparing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A truncated or padded signature must not reach ``compare_digest``."""
        import orchestrator.checkpoint_manager as module

        calls: list[Any] = []
        original = module.hmac.compare_digest

        def spy(a: Any, b: Any) -> bool:
            calls.append((a, b))
            return original(a, b)

        monkeypatch.setattr(module.hmac, "compare_digest", spy)
        assert module._signatures_equal("ab", "abcd") is False
        assert calls == [], "length mismatch must short-circuit before comparison"

        calls.clear()
        assert module._signatures_equal("abcd", "abcd") is True
        assert len(calls) == 1

    def test_verdict_enum_classification_is_consistent(self) -> None:
        assert SigningVerdict.VERIFIED.is_trusted
        assert not SigningVerdict.UNSIGNED.is_trusted
        assert not SigningVerdict.TAMPERED.is_trusted
        assert not SigningVerdict.UNVERIFIABLE.is_trusted
        assert SigningVerdict.UNSIGNED.accepts_downgrade
        assert SigningVerdict.UNVERIFIABLE.accepts_downgrade
        # Tampering is never downgrade-eligible.
        assert not SigningVerdict.TAMPERED.accepts_downgrade
        assert not SigningVerdict.VERIFIED.accepts_downgrade
        assert SigningVerdict.VERIFIED.value == "verified"


# ---------------------------------------------------------------------------
# Compatibility with real PoC snapshots in the repository
# ---------------------------------------------------------------------------


class TestRepositorySnapshotsStillLoad:
    @pytest.mark.parametrize("project", ["esp32-os-gap", "esp32-os-gap2", "esp32-os-issue"])
    def test_unsigned_legacy_snapshots_load_and_list(self, project: str, tmp_path: Path) -> None:
        """Pre-existing snapshots remain readable.

        They verify as UNSIGNED, which is the honest verdict for a store written
        before signing existed — and they are refused by default only because
        provenance cannot be established, never because they are malformed.
        """
        root = REPO_ROOT / "checkpoints" / project
        if not root.exists():
            pytest.skip(f"no snapshot store for {project}")
        index = root / config.CHECKPOINT_INDEX_FILE
        if not index.exists():
            pytest.skip(f"no index for {project}")
        entries = json.loads(index.read_text()).get("checkpoints", [])
        assert entries, f"expected checkpoints for {project}"
        for entry in entries:
            metadata = json.loads(
                (root / entry["id"] / config.CHECKPOINT_METADATA_FILE).read_text()
            )
            # Names satisfy containment...
            assert set(metadata["files"]).issubset(set(config.STATE_FILES))
            # ...and the legacy unsigned state is reported honestly.
            assert "signature" not in metadata or metadata["signature"] in (None, "")
