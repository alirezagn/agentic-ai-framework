"""CheckpointManager — isolated project snapshots and directory backups.

Layout (relative to the repository root)::

    checkpoints/
      <project-name>/
        index.json                     # list of all checkpoints
        <checkpoint-id>/
          PROJECT.yaml
          TASKS.yaml
          PROJECT_MEMORY.md
          ...
          metadata.json                # ids, timestamps, per-file sha256
        backups/
          <backup-id>/                 # full copy of the project directory

Every snapshot file is checksummed with SHA-256. Restoring verifies the
checksums first, so a corrupted or tampered checkpoint can never overwrite
live project state. When ``CHECKPOINT_SIGNING_KEY`` is set, metadata is also
HMAC-SHA256-signed over ``<id>:<checksum>``; verification then fails on any
post-hoc rewrite of files *and* metadata (checksums alone are self-declared).
Restores can also target a different directory, which keeps the live project
untouched (isolation).

Containment policy
------------------
``metadata.json`` is attacker-writable whenever the snapshot directory is, and
it is the sole authority on *which files exist*. Containment is therefore
enforced deny-by-default on both the read and the write side, via
:mod:`orchestrator.path_policy`:

* every declared filename must be a safe relative path **and** a member of
  ``config.STATE_FILES`` — so ``../escape.txt``, ``/etc/passwd`` and
  ``docs/x.md`` are all refused, not merely neutralised;
* every source and destination is additionally resolved and asserted to sit
  inside its root, which also defeats symlink escapes;
* validation is **pure and runs before any I/O**, so a hostile manifest is
  refused without creating a directory or overwriting a single file — the
  previous implementation created the destination first and then copied
  whatever names the manifest declared.

Snapshot and backup ids are *rejected* rather than sanitised. The former
``replace("/", "_").replace("..", "_")`` behaviour turned a traversal attempt
into a silent no-op, hiding both the attempt and the bug that permitted it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import config
from .path_policy import (
    PathPolicyError,
    resolve_inside,
    validate_filenames,
    validate_snapshot_id,
    validate_state_file_name,
)
from .state_manager import StateError, atomic_write_text, utc_now_iso

logger = logging.getLogger(__name__)


class CheckpointError(StateError):
    """Base error for checkpoint operations."""


class CheckpointNotFoundError(CheckpointError):
    """The requested checkpoint id is not in the index."""


class CheckpointExistsError(CheckpointError):
    """A checkpoint with the same id already exists."""


class CheckpointIntegrityError(CheckpointError):
    """Stored checksums or signature do not match the checkpoint contents."""


class SigningVerdict(str, Enum):
    """Outcome of evaluating a snapshot against the signing policy.

    Modelled as an explicit state rather than a boolean because the security
    decision needs to distinguish *why* verification passed or failed:

    ``VERIFIED``
        Snapshot claims to be signed, a key is available, and the MAC matches
        the recomputed payload.
    ``UNSIGNED``
        Snapshot makes no signature claim. Verified on checksums alone. Only
        reachable when the snapshot genuinely never had a key, never by
        removing a field from a signed snapshot.
    ``TAMPERED``
        A signature claim exists and does not hold: wrong MAC, missing or
        empty signature under ``signed: true``, mutated contents, or a
        rewritten binding field.
    ``UNVERIFIABLE``
        A signature claim exists but cannot be checked right now — no key is
        configured, or the recorded ``key_id`` is not the active one. Distinct
        from ``TAMPERED`` because the bytes may well be fine; the operator
        simply needs the right key.
    """

    VERIFIED = "verified"
    UNSIGNED = "unsigned"
    TAMPERED = "tampered"
    UNVERIFIABLE = "unverifiable"

    @property
    def is_trusted(self) -> bool:
        """True when the snapshot's provenance is positively established."""
        return self is SigningVerdict.VERIFIED

    @property
    def accepts_downgrade(self) -> bool:
        """True when this verdict may fall back to checksum-only verification."""
        return self in (SigningVerdict.UNSIGNED, SigningVerdict.UNVERIFIABLE)


@dataclass(frozen=True)
class IntegrityReport:
    """Full result of a verification pass, for CLI display and logging."""

    checkpoint_id: str
    verdict: SigningVerdict
    detail: str
    signed: bool
    key_id: str
    legacy_inference: bool = False
    file_count: int = 0


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _signing_key() -> Optional[str]:
    return config.SYSTEM_KEYS.resolve("checkpoint_signing_key")


def _signing_key_id() -> str:
    """Identifier for the active signing key.

    Recorded inside every signature so a rotated key can be told apart from a
    tampered one: a mismatch on ``key_id`` means "this snapshot needs a
    different key", not "someone rewrote the bytes".
    """
    configured = config.SYSTEM_KEYS.resolve("checkpoint_signing_key_id")
    if configured and configured.strip():
        return configured.strip()
    return config.DEFAULT_CHECKPOINT_KEY_ID


def signature_payload(
    checkpoint_id: str,
    key_id: str,
    created_at: str,
    files: Dict[str, str],
) -> bytes:
    """Build the canonical byte string a checkpoint HMAC is computed over.

    Every field that must not be attacker-controlled is bound in:

    ``checkpoint_id``
        stops a valid snapshot's signature being replayed under another id.
    ``key_id``
        distinguishes key rotation from tampering.
    ``created_at``
        stops a snapshot's signature being reused after the metadata is
        rewritten with a new timestamp.
    ``files`` (sorted ``name=digest`` pairs)
        binds the *contents*, not merely the self-declared aggregate checksum.
        The old scheme signed ``<id>:<checksum>`` where ``checksum`` was read
        back out of the same attacker-writable metadata, so rewriting the files
        and recomputing that field satisfied the MAC without the key. Binding
        the per-file digests — and the file *names*, which are what a restore
        actually writes — removes that freedom.

    Names and digests are length-prefixed rather than joined with a delimiter,
    so no crafted filename or digest can shift the field boundaries and forge a
    collision.
    """
    parts: List[bytes] = [
        b"checkpoint-signature",
        str(config.CHECKPOINT_SIGNATURE_VERSION).encode("utf-8"),
        str(config.CHECKPOINT_SIGNATURE_ALGORITHM).encode("utf-8"),
        _length_prefixed(checkpoint_id),
        _length_prefixed(key_id),
        _length_prefixed(created_at),
    ]
    for name in sorted(files):
        parts.append(_length_prefixed(name))
        parts.append(_length_prefixed(files[name]))
    return b"\n".join(parts)


def _length_prefixed(value: str) -> bytes:
    raw = value.encode("utf-8")
    return b"%d:%s" % (len(raw), raw)


def _signature(key: str, payload: bytes) -> str:
    return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _signatures_equal(provided: str, expected: str) -> bool:
    """Constant-time comparison of two hex digests.

    ``hmac.compare_digest`` is only constant-time for equal-length ``str``;
    it short-circuits on a length mismatch. Both operands here are hex digests
    whose length is public, so the shortcut leaks nothing — but the length is
    nonetheless checked first so a malformed or truncated stored signature
    cannot reach a comparison at all.
    """
    if len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ignore_for_backup(directory: str, names: List[str]) -> List[str]:
    ignored: List[str] = []
    for name in names:
        if name in ("__pycache__", ".pytest_cache", ".mypy_cache", ".git"):
            ignored.append(name)
        elif name.endswith(".pyc"):
            ignored.append(name)
    return ignored


class Checkpoint:
    """In-memory representation of a saved checkpoint."""

    def __init__(
        self,
        checkpoint_id: str,
        project_name: str,
        created_at: str,
        phase: str,
        version: str,
        notes: str,
        files: Dict[str, str],
        checksum: str,
        directory: Path,
    ) -> None:
        self.checkpoint_id = checkpoint_id
        self.project_name = project_name
        self.created_at = created_at
        self.phase = phase
        self.version = version
        self.notes = notes
        self.files = dict(files)
        self.checksum = checksum
        self.directory = Path(directory)

    @property
    def file_names(self) -> List[str]:
        return sorted(self.files)

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "id": self.checkpoint_id,
            "project": self.project_name,
            "created_at": self.created_at,
            "phase": self.phase,
            "version": self.version,
            "notes": self.notes,
            "files": dict(self.files),
            "checksum": self.checksum,
            "checksum_algorithm": config.CHECKSUM_ALGORITHM,
        }

    @classmethod
    def from_metadata(cls, metadata: Dict[str, Any], directory: Path) -> "Checkpoint":
        return cls(
            checkpoint_id=str(metadata.get("id", "")),
            project_name=str(metadata.get("project", "")),
            created_at=str(metadata.get("created_at", "")),
            phase=str(metadata.get("phase", "")),
            version=str(metadata.get("version", "")),
            notes=str(metadata.get("notes", "")),
            files=dict(metadata.get("files") or {}),
            checksum=str(metadata.get("checksum", "")),
            directory=Path(directory),
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"Checkpoint(id={self.checkpoint_id!r}, created_at={self.created_at!r}, "
            f"files={len(self.files)}, checksum={self.checksum[:12]}...)"
        )


class CheckpointManager:
    """Create, list, verify, restore and export project checkpoints."""

    def __init__(
        self,
        project_path: str | Path,
        checkpoints_root: Optional[str | Path] = None,
    ) -> None:
        self.project_path = Path(project_path).expanduser().resolve()
        if not self.project_path.exists():
            raise CheckpointError(f"Project path does not exist: {project_path}")
        project_name = self.project_path.name
        if checkpoints_root is None:
            root = Path(config.default_checkpoints_dir()).resolve() / project_name
        else:
            root = Path(checkpoints_root).expanduser().resolve()
        self.checkpoints_root = root
        self.project_name = project_name
        self.index_path = self.checkpoints_root / config.CHECKPOINT_INDEX_FILE
        self.backups_root = self.checkpoints_root / config.CHECKPOINT_BACKUP_DIR
        self.checkpoints_root.mkdir(parents=True, exist_ok=True)
        self.backups_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Index handling
    # ------------------------------------------------------------------

    def _load_index(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            return {"project": self.project_name, "checkpoints": []}
        try:
            with open(self.index_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"Corrupted checkpoint index at {self.index_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise CheckpointError(f"Checkpoint index must be a JSON object: {self.index_path}")
        data.setdefault("project", self.project_name)
        data.setdefault("checkpoints", [])
        return data

    def _save_index(self, index: Dict[str, Any]) -> None:
        atomic_write_text(self.index_path, json.dumps(index, indent=2, sort_keys=False) + "\n")

    def checkpoint_dir(self, checkpoint_id: str) -> Path:
        """Directory holding ``checkpoint_id``.

        The id becomes a single path segment, so it is validated rather than
        rewritten. The previous ``replace("/", "_").replace("..", "_")``
        sanitiser turned a traversal attempt into a silent no-op; rejecting it
        keeps the attempt visible in the logs and in the error the operator
        sees.
        """
        safe_id = validate_snapshot_id(checkpoint_id, context="checkpoint id")
        return self.checkpoints_root / safe_id

    def _validate_manifest_files(self, checkpoint_id: str, names: Iterable[str]) -> List[str]:
        """Deny-by-default validation of every filename a manifest declares.

        Pure — performs no I/O — so it can run before anything is created.
        Every name must be both a safe relative path *and* a member of
        ``config.STATE_FILES``. A traversal string cannot be a member of that
        fixed tuple, which closes the vulnerability structurally instead of by
        pattern matching.

        Raises :class:`CheckpointIntegrityError` rather than letting the policy
        error escape, so every checkpoint failure surfaces through one
        ``CheckpointError`` type for the CLI.
        """
        try:
            return validate_filenames(names, context="checkpoint file name")
        except PathPolicyError as exc:
            raise CheckpointIntegrityError(
                f"Checkpoint '{checkpoint_id}' declares an unsafe file name: {exc}"
            ) from exc

    def _resolve_snapshot_file(self, checkpoint_id: str, directory: Path, name: str) -> Path:
        """Resolve one manifest entry to a real path inside the snapshot directory."""
        try:
            return resolve_inside(directory, name, context="checkpoint file name")
        except PathPolicyError as exc:
            raise CheckpointIntegrityError(
                f"Checkpoint '{checkpoint_id}' file '{name}' is not contained in its snapshot "
                f"directory: {exc}"
            ) from exc

    def _resolve_restore_target(self, checkpoint_id: str, destination: Path, name: str) -> Path:
        """Resolve one manifest entry to a real path inside the restore destination."""
        try:
            return resolve_inside(destination, name, context="restore target name")
        except PathPolicyError as exc:
            raise CheckpointIntegrityError(
                f"Refusing to restore checkpoint '{checkpoint_id}': '{name}' escapes the restore "
                f"destination: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_checkpoint(
        self,
        checkpoint_id: str,
        notes: str = "",
        source_path: Optional[str | Path] = None,
    ) -> Checkpoint:
        """Snapshot every state file of the project into an isolated directory."""
        if not checkpoint_id:
            raise CheckpointError("checkpoint_id must be a non-empty string")
        target_dir = self.checkpoint_dir(checkpoint_id)
        if target_dir.exists():
            raise CheckpointExistsError(f"Checkpoint '{checkpoint_id}' already exists at {target_dir}")

        source = Path(source_path).expanduser().resolve() if source_path else self.project_path
        if not source.exists():
            raise CheckpointError(f"Snapshot source does not exist: {source}")

        target_dir.mkdir(parents=True, exist_ok=False)

        # Read side: only allowlisted state files, each resolved inside the
        # snapshot source so a symlink planted in a project cannot redirect a
        # copy to somewhere else.
        copied_files: Dict[str, str] = {}
        for name in config.STATE_FILES:
            try:
                source_file = resolve_inside(source, name, context="snapshot source file")
            except PathPolicyError as exc:
                raise CheckpointError(
                    f"Refusing to snapshot '{name}' from {source}: {exc}"
                ) from exc
            if not source_file.is_file():
                continue
            destination = self._resolve_snapshot_file(checkpoint_id, target_dir, name)
            try:
                shutil.copy2(source_file, destination)
            except OSError as exc:
                raise CheckpointError(f"Failed to copy {source_file} into checkpoint: {exc}") from exc
            copied_files[name] = _sha256_file(destination)

        if not copied_files:
            target_dir.rmdir()
            raise CheckpointError(f"No state files found in {source}; refusing to save an empty checkpoint")

        checksum = self._compute_checksum(copied_files)

        metadata: Dict[str, Any] = {
            "id": checkpoint_id,
            "project": self.project_name,
            "created_at": utc_now_iso(),
            "phase": self._read_phase(),
            "version": self._read_version(),
            "notes": notes,
            "files": copied_files,
            "file_count": len(copied_files),
            "checksum": checksum,
            "checksum_algorithm": config.CHECKSUM_ALGORITHM,
            "source": str(source),
        }
        # Signing is opt-in by *key presence*, and the claim is persisted
        # explicitly. Recording ``signed`` alongside the signature is what makes
        # "delete the signature" a detectable tamper rather than a silent
        # downgrade to checksum-only verification.
        signing_key = _signing_key()
        if signing_key:
            key_id = _signing_key_id()
            metadata["signed"] = True
            metadata["key_id"] = key_id
            metadata["signature_algorithm"] = config.CHECKPOINT_SIGNATURE_ALGORITHM
            metadata["signature_version"] = config.CHECKPOINT_SIGNATURE_VERSION
            metadata["signature"] = _signature(
                signing_key,
                signature_payload(
                    checkpoint_id,
                    key_id,
                    str(metadata["created_at"]),
                    copied_files,
                ),
            )
        else:
            metadata["signed"] = False
            metadata["key_id"] = None
            metadata["signature_algorithm"] = None
            metadata["signature_version"] = config.CHECKPOINT_SIGNATURE_VERSION
            metadata["signature"] = None

        atomic_write_text(target_dir / config.CHECKPOINT_METADATA_FILE, json.dumps(metadata, indent=2) + "\n")

        logger.info(
            "checkpoint '%s' created (%d files, signed=%s, key_id=%s)",
            checkpoint_id,
            len(copied_files),
            bool(signing_key),
            metadata["key_id"],
        )

        checkpoint = Checkpoint.from_metadata(metadata, target_dir)

        index = self._load_index()
        entries = [entry for entry in index.get("checkpoints", []) if entry.get("id") != checkpoint_id]
        entries.append(
            {
                "id": checkpoint_id,
                "created_at": metadata["created_at"],
                "phase": metadata["phase"],
                "notes": notes,
                "checksum": checksum,
                "file_count": len(copied_files),
            }
        )
        entries.sort(key=lambda entry: str(entry.get("created_at", "")))
        index["checkpoints"] = entries
        self._save_index(index)
        return checkpoint

    def _compute_checksum(self, files: Dict[str, str]) -> str:
        digest = hashlib.sha256()
        for name in sorted(files):
            digest.update(name.encode("utf-8"))
            digest.update(files[name].encode("utf-8"))
        return digest.hexdigest()

    def _read_phase(self) -> str:
        project_file = self.project_path / config.PROJECT_FILE
        if not project_file.exists():
            return "UNKNOWN"
        try:
            with open(project_file, "r", encoding="utf-8") as handle:
                import yaml

                data = yaml.safe_load(handle) or {}
            return str((data.get("phase") or {}).get("current", "UNKNOWN"))
        except Exception:
            return "UNKNOWN"

    def _read_version(self) -> str:
        project_file = self.project_path / config.PROJECT_FILE
        if not project_file.exists():
            return "0.0.0"
        try:
            with open(project_file, "r", encoding="utf-8") as handle:
                import yaml

                data = yaml.safe_load(handle) or {}
            return str((data.get("project") or {}).get("version", "0.0.0"))
        except Exception:
            return "0.0.0"

    # ------------------------------------------------------------------
    # List / load / verify
    # ------------------------------------------------------------------

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        index = self._load_index()
        entries = list(index.get("checkpoints", []))
        entries.sort(key=lambda entry: str(entry.get("created_at", "")))
        return entries

    def has_checkpoint(self, checkpoint_id: str) -> bool:
        return any(entry.get("id") == checkpoint_id for entry in self.list_checkpoints())

    def _read_metadata(self, checkpoint_id: str) -> Dict[str, Any]:
        target_dir = self.checkpoint_dir(checkpoint_id)
        metadata_path = target_dir / config.CHECKPOINT_METADATA_FILE
        if not metadata_path.exists():
            raise CheckpointNotFoundError(
                f"Checkpoint '{checkpoint_id}' not found (no metadata at {metadata_path})"
            )
        try:
            with open(metadata_path, "r", encoding="utf-8") as handle:
                metadata = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"Corrupted metadata for checkpoint '{checkpoint_id}': {exc}") from exc
        if not isinstance(metadata, dict):
            raise CheckpointError(f"Corrupted metadata for checkpoint '{checkpoint_id}': not a JSON object")
        return metadata

    def load_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        target_dir = self.checkpoint_dir(checkpoint_id)
        metadata = self._read_metadata(checkpoint_id)
        return Checkpoint.from_metadata(metadata, target_dir)

    def evaluate_integrity(self, checkpoint_id: str) -> IntegrityReport:
        """Verify a snapshot and report *why* it passed or failed.

        Three ordered stages, each of which can end the evaluation:

        1. **Containment (no I/O).** Every declared filename must be a safe
           relative path *and* an allowlisted state file. Reusing
           :mod:`orchestrator.path_policy` here is deliberate: a MAC over
           attacker-chosen filenames is worthless if the same names can direct
           a write outside the project, so name validation gates signing.
        2. **Contents.** Per-file SHA-256 recomputed from disk, then the
           aggregate checksum.
        3. **Signature policy.** A four-row truth table driven by the persisted
           ``signed`` flag — *never* by the presence of the ``signature``
           field, which is itself attacker-writable.

           ==========  ==================  ==================================
           ``signed``   key available      outcome
           ==========  ==================  ==================================
           ``true``     yes                ``VERIFIED`` on MAC match, else
                                            ``TAMPERED`` (incl. missing or
                                            empty signature)
           ``true``     no                 ``UNVERIFIABLE``
           ``false``    yes                ``UNVERIFIABLE`` — a key is active
                                            but this snapshot never signed;
                                            never a silent downgrade
           ``false``    no                 ``UNSIGNED``
           ==========  ==================  ==================================

        Verdict-to-outcome policy, and why row 4 is not a refusal:

        ``VERIFIED``
            Passes. Provenance positively established.
        ``UNSIGNED`` (row 4: never signed, no key active)
            **Passes on checksums.** This row cannot be reached by rewriting
            metadata when signing is enabled — an attacker who flips
            ``signed`` to false while a key is active lands in row 3 and is
            refused. With no key active there is no signature claim to violate,
            and refusing would protect against nobody while breaking restore
            for every deployment that has not configured a key. Checksums were
            always the only mechanism available here; this states that
            explicitly instead of pretending otherwise.
        ``UNVERIFIABLE`` (rows 2 and 3)
            **Fails** unless ``CHECKPOINT_ALLOW_UNSIGNED`` is set. This is the
            row that carries signal: a signature claim exists but cannot be
            checked, so either a key has gone missing or an attacker stripped
            the proof.
        ``TAMPERED``
            Always fails, including under the escape hatch. Integrity failures
            are never downgrade-eligible.

        The bypass this closes: under the old field-presence gate, deleting
        ``signature`` from a ``signed: true`` snapshot skipped validation
        entirely and returned ``True``. That combination is now ``TAMPERED``.

        Never raises for an expected failure mode — the caller decides whether
        a non-``VERIFIED`` verdict is fatal.
        """
        metadata = self._read_metadata(checkpoint_id)
        checkpoint = Checkpoint.from_metadata(metadata, self.checkpoint_dir(checkpoint_id))

        # --- Stage 1: containment, pure, before any file is opened ---------
        try:
            names = validate_filenames(checkpoint.files.keys(), context="checkpoint file name")
        except PathPolicyError as exc:
            raise CheckpointIntegrityError(
                f"Checkpoint '{checkpoint_id}' declares an unsafe file name: {exc}"
            ) from exc

        # --- Stage 2: contents ------------------------------------------------
        computed: Dict[str, str] = {}
        for name in names:
            file_path = self._resolve_snapshot_file(checkpoint_id, checkpoint.directory, name)
            if not file_path.exists():
                raise CheckpointIntegrityError(
                    f"Checkpoint '{checkpoint_id}' is missing file '{name}'"
                )
            computed[name] = _sha256_file(file_path)
        for name, expected in checkpoint.files.items():
            actual = computed.get(name)
            if actual != expected:
                raise CheckpointIntegrityError(
                    f"Checkpoint '{checkpoint_id}' file '{name}' failed checksum verification "
                    f"(expected {expected}, got {actual})"
                )
        if self._compute_checksum(computed) != checkpoint.checksum:
            raise CheckpointIntegrityError(
                f"Checkpoint '{checkpoint_id}' aggregate checksum mismatch"
            )

        # --- Stage 3: signature policy ---------------------------------------
        signed, legacy_inference = self._declared_signed_flag(metadata)
        declared_key_id = str(metadata.get("key_id") or "").strip()
        active_key = _signing_key()
        active_key_id = _signing_key_id()
        signature = metadata.get("signature")
        signature_text = signature.strip() if isinstance(signature, str) else ""

        def report(verdict: SigningVerdict, detail: str) -> IntegrityReport:
            if verdict is not SigningVerdict.VERIFIED and legacy_inference:
                detail = f"{detail} (signing state inferred: metadata predates signed flag)"
            return IntegrityReport(
                checkpoint_id=checkpoint_id,
                verdict=verdict,
                detail=detail,
                signed=signed,
                key_id=declared_key_id or active_key_id,
                legacy_inference=legacy_inference,
                file_count=len(computed),
            )

        if not signed:
            if active_key:
                # Row 3. An active key exists yet this snapshot claims no
                # signature. Refusing is the point: this is the state an
                # attacker creates by stripping the signature field.
                return report(
                    SigningVerdict.UNVERIFIABLE,
                    "snapshot declares signed=false while a signing key is active "
                    f"(key_id={active_key_id!r}); refusing to downgrade",
                )
            # Row 4 — the only honest downgrade: never signed, no key present.
            return report(
                SigningVerdict.UNSIGNED,
                "snapshot was never signed (signed=false, no signing key configured)",
            )

        # signed is True from here on.
        if not signature_text:
            # signed: true with no signature is self-contradictory metadata and
            # is the exact shape produced by deleting the field.
            return report(
                SigningVerdict.TAMPERED,
                "snapshot declares signed=true but carries no signature",
            )

        if not active_key:
            # Row 2 — cannot check, but not evidence of tampering.
            return report(
                SigningVerdict.UNVERIFIABLE,
                "snapshot is signed but no CHECKPOINT_SIGNING_KEY is configured; "
                f"it requires key_id={declared_key_id or 'unknown'!r}",
            )

        if declared_key_id and declared_key_id != active_key_id:
            # Distinguished from a MAC mismatch so key rotation reads as an
            # operator problem rather than an attack.
            return report(
                SigningVerdict.UNVERIFIABLE,
                f"snapshot requires key_id={declared_key_id!r} but the active key is "
                f"{active_key_id!r}",
            )

        algorithm = str(metadata.get("signature_algorithm") or "")
        if algorithm and algorithm != config.CHECKPOINT_SIGNATURE_ALGORITHM:
            return report(
                SigningVerdict.UNVERIFIABLE,
                f"unsupported signature algorithm {algorithm!r}",
            )

        version = metadata.get("signature_version")
        if version is not None:
            try:
                version_int = int(version)
            except (TypeError, ValueError):
                return report(
                    SigningVerdict.TAMPERED,
                    f"signature_version is not an integer: {version!r}",
                )
            if version_int != config.CHECKPOINT_SIGNATURE_VERSION:
                return report(
                    SigningVerdict.UNVERIFIABLE,
                    f"signature_version {version_int} is not supported by this build "
                    f"(expected {config.CHECKPOINT_SIGNATURE_VERSION})",
                )

        # Bind the id read from the *in-memory* snapshot object rather than the
        # directory path: they are the same value here, but making the binding
        # explicit keeps it obvious that the id is inside the MAC.
        expected = _signature(
            active_key,
            signature_payload(
                checkpoint.checkpoint_id,
                declared_key_id or active_key_id,
                checkpoint.created_at,
                computed,
            ),
        )
        if not _signatures_equal(signature_text, expected):
            return report(
                SigningVerdict.TAMPERED,
                "HMAC signature does not match the recomputed contents "
                "(files, id, key_id or created_at were altered after signing)",
            )

        # Row 1. Signature recomputed from the bytes actually on disk.
        return report(
            SigningVerdict.VERIFIED,
            f"signature verified (key_id={declared_key_id or active_key_id!r}, "
            f"{len(computed)} files bound)",
        )

    @staticmethod
    def _declared_signed_flag(metadata: Dict[str, Any]) -> tuple[bool, bool]:
        """Read the persisted signing claim.

        Returns ``(signed, legacy_inference)``. For metadata written before the
        flag existed, the claim is inferred from signature presence so legacy
        snapshots are not misreported as tampered — but the inference is
        surfaced so a caller can log or refuse it. Note the asymmetry that
        matters: legacy inference is the *only* place signature presence is
        consulted, and it can never produce ``signed=False`` for a snapshot
        that carries a signature.
        """
        raw = metadata.get("signed")
        if isinstance(raw, bool):
            return raw, False
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in ("true", "false"):
                return lowered == "true", False
        legacy_signature = metadata.get("signature")
        legacy_has_signature = isinstance(legacy_signature, str) and bool(legacy_signature.strip())
        return legacy_has_signature, True

    def verify_checkpoint(self, checkpoint_id: str) -> bool:
        """Verify a snapshot, raising on any verdict that is not acceptable.

        Passes for ``VERIFIED`` and for ``UNSIGNED`` (an honest unsigned store
        with no key configured — checksums are the only integrity mechanism
        available there, and this records that plainly). Fails for
        ``UNVERIFIABLE`` unless ``CHECKPOINT_ALLOW_UNSIGNED`` is explicitly set,
        and always fails for ``TAMPERED``.

        Call :meth:`evaluate_integrity` instead when the verdict itself matters;
        the CLI surfaces it so an operator can see *why* a snapshot was trusted.
        """
        report = self.evaluate_integrity(checkpoint_id)

        if report.verdict.is_trusted:
            logger.info("checkpoint '%s' verified (%s)", checkpoint_id, report.detail)
            return True

        if report.verdict is SigningVerdict.UNSIGNED:
            # Row 4. Honest unsigned store, no key anywhere. Passes on
            # checksums; no signature claim exists to violate.
            logger.info(
                "checkpoint '%s' verified on checksums only (%s)",
                checkpoint_id,
                report.detail,
            )
            return True

        if report.verdict is SigningVerdict.UNVERIFIABLE and config.allow_unsigned_checkpoints():
            logger.warning(
                "checkpoint '%s' accepted WITHOUT signature verification: %s "
                "(CHECKPOINT_ALLOW_UNSIGNED is enabled)",
                checkpoint_id,
                report.detail,
            )
            return True

        raise CheckpointIntegrityError(
            f"Checkpoint '{checkpoint_id}' integrity check failed: "
            f"[{report.verdict.value}] {report.detail}"
        )

    # ------------------------------------------------------------------
    # Restore / export
    # ------------------------------------------------------------------

    def restore_checkpoint(
        self,
        checkpoint_id: str,
        target_path: Optional[str | Path] = None,
        verify: bool = True,
    ) -> Path:
        """Restore a checkpoint.

        Verification always happens before anything is written. When
        ``target_path`` is given the files are restored there instead of into
        the live project directory, which keeps the restore fully isolated.
        """
        # Pass 1 — validate every declared name before touching the filesystem.
        # Doing this first means a hostile manifest is refused without creating
        # a single directory or file, so there is no partial-restore damage
        # class to reason about.
        checkpoint = self.load_checkpoint(checkpoint_id)
        names = self._validate_manifest_files(checkpoint_id, checkpoint.files.keys())

        # Pass 2 — content verification, when requested. Runs after the name
        # policy so it can never be the thing that "approves" a traversal.
        if verify:
            self.verify_checkpoint(checkpoint_id)

        destination = Path(target_path).expanduser().resolve() if target_path else self.project_path
        destination.mkdir(parents=True, exist_ok=True)

        # Resolve every source and target up front. Doing the whole batch
        # before the first copy means a name that escapes is detected before
        # any file has been overwritten.
        plan: List[tuple[str, Path, Path]] = []
        for name in names:
            source_file = self._resolve_snapshot_file(checkpoint_id, checkpoint.directory, name)
            target_file = self._resolve_restore_target(checkpoint_id, destination, name)
            if not source_file.exists():
                raise CheckpointIntegrityError(
                    f"Checkpoint '{checkpoint_id}' is missing file '{name}'; refusing a partial restore"
                )
            plan.append((name, source_file, target_file))

        restored: List[str] = []
        for name, source_file, target_file in plan:
            try:
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, target_file)
            except OSError as exc:
                raise CheckpointError(
                    f"Failed to restore '{name}' from checkpoint '{checkpoint_id}': {exc} "
                    f"(restored so far: {', '.join(restored) if restored else 'none'})"
                ) from exc
            restored.append(name)

        logger.info(
            "checkpoint '%s' restored (%d files) into %s",
            checkpoint_id,
            len(restored),
            destination,
        )
        return destination

    def load_checkpoint_file(self, checkpoint_id: str, file_name: str) -> str:
        """Read one file out of a snapshot.

        Same containment rules as restore: the name must be allowlisted and
        must resolve inside the snapshot directory, so this read path cannot be
        used to pull arbitrary files into memory either.
        """
        checkpoint = self.load_checkpoint(checkpoint_id)
        if file_name not in checkpoint.files:
            raise CheckpointNotFoundError(
                f"Checkpoint '{checkpoint_id}' does not contain '{file_name}'"
            )
        self._validate_manifest_files(checkpoint_id, [file_name])
        file_path = self._resolve_snapshot_file(checkpoint_id, checkpoint.directory, file_name)
        with open(file_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def delete_checkpoint(self, checkpoint_id: str) -> None:
        target_dir = self.checkpoint_dir(checkpoint_id)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        index = self._load_index()
        index["checkpoints"] = [
            entry for entry in index.get("checkpoints", []) if entry.get("id") != checkpoint_id
        ]
        self._save_index(index)

    # ------------------------------------------------------------------
    # Directory backup
    # ------------------------------------------------------------------

    def create_backup(self, backup_id: str, notes: str = "") -> Path:
        """Full copy of the project directory (minus caches) under backups/."""
        if not backup_id:
            raise CheckpointError("backup_id must be a non-empty string")
        try:
            safe_id = validate_snapshot_id(backup_id, context="backup id")
            destination = resolve_inside(self.backups_root, safe_id, context="backup id")
        except PathPolicyError as exc:
            raise CheckpointError(f"Refusing backup id {backup_id!r}: {exc}") from exc
        if destination.exists():
            raise CheckpointExistsError(f"Backup '{backup_id}' already exists at {destination}")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                self.project_path,
                destination,
                ignore=_ignore_for_backup,
                dirs_exist_ok=False,
            )
        except OSError as exc:
            raise CheckpointError(f"Failed to create backup '{backup_id}': {exc}") from exc

        file_hashes: Dict[str, str] = {}
        for file_path in sorted(destination.rglob("*")):
            if file_path.is_file():
                file_hashes[str(file_path.relative_to(destination))] = _sha256_file(file_path)

        metadata = {
            "id": backup_id,
            "project": self.project_name,
            "created_at": utc_now_iso(),
            "notes": notes,
            "file_count": len(file_hashes),
            "checksum": _sha256_bytes(
                "".join(f"{name}:{digest}" for name, digest in sorted(file_hashes.items())).encode("utf-8")
            ),
        }
        atomic_write_text(destination / "backup_metadata.json", json.dumps(metadata, indent=2) + "\n")
        return destination

    def list_backups(self) -> List[str]:
        if not self.backups_root.exists():
            return []
        return sorted(entry.name for entry in self.backups_root.iterdir() if entry.is_dir())

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def print_checkpoint_list(self) -> str:
        entries = self.list_checkpoints()
        lines = [
            "=" * 60,
            f"CHECKPOINTS FOR {self.project_name}",
            "=" * 60,
        ]
        if not entries:
            lines.append("  [No checkpoints saved]")
        for entry in entries:
            lines.append(
                f"  - {entry.get('id')} | {entry.get('created_at')} | "
                f"phase={entry.get('phase')} | files={entry.get('file_count')} | "
                f"{entry.get('notes', '')}"
            )
        backups = self.list_backups()
        lines.append("")
        lines.append(f"BACKUPS ({len(backups)}):")
        if backups:
            for name in backups:
                lines.append(f"  - {name}")
        else:
            lines.append("  [None]")
        lines.append("=" * 60)
        return "\n".join(lines)


__all__ = [
    "Checkpoint",
    "CheckpointManager",
    "CheckpointError",
    "CheckpointNotFoundError",
    "CheckpointExistsError",
    "CheckpointIntegrityError",
]
