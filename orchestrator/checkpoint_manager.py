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
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .state_manager import StateError, atomic_write_text, utc_now_iso

logger = logging.getLogger(__name__)


class CheckpointError(StateError):
    """Base error for checkpoint operations."""


class CheckpointNotFoundError(CheckpointError):
    """The requested checkpoint id is not in the index."""


class CheckpointExistsError(CheckpointError):
    """A checkpoint with the same id already exists."""


class CheckpointIntegrityError(CheckpointError):
    """Stored checksums do not match the checkpoint contents."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _signing_key() -> Optional[str]:
    return config.SYSTEM_KEYS.resolve("checkpoint_signing_key")


def _signature(key: str, checkpoint_id: str, checksum: str) -> str:
    message = f"{checkpoint_id}:{checksum}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


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
        safe_id = checkpoint_id.replace("/", "_").replace("..", "_")
        return self.checkpoints_root / safe_id

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

        copied_files: Dict[str, str] = {}
        for name in config.STATE_FILES:
            source_file = source / name
            if not source_file.exists():
                continue
            destination = target_dir / name
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
        signing_key = _signing_key()
        if signing_key:
            metadata["signature"] = _signature(signing_key, checkpoint_id, checksum)
            metadata["signature_algorithm"] = "HMAC-SHA256"
        atomic_write_text(target_dir / config.CHECKPOINT_METADATA_FILE, json.dumps(metadata, indent=2) + "\n")

        logger.info(
            "checkpoint '%s' created (%d files, signed=%s)",
            checkpoint_id,
            len(copied_files),
            bool(signing_key),
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

    def verify_checkpoint(self, checkpoint_id: str) -> bool:
        """Raise CheckpointIntegrityError when stored files no longer match."""
        metadata = self._read_metadata(checkpoint_id)
        checkpoint = Checkpoint.from_metadata(metadata, self.checkpoint_dir(checkpoint_id))
        computed: Dict[str, str] = {}
        for name in checkpoint.files:
            file_path = checkpoint.directory / name
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

        # Optional HMAC signature: binds the declared checksum to the key so
        # files *and* metadata rewritten together still fail verification.
        signature = str(metadata.get("signature") or "")
        if signature:
            signing_key = _signing_key()
            if signing_key:
                expected = _signature(signing_key, checkpoint_id, checkpoint.checksum)
                if not hmac.compare_digest(signature, expected):
                    raise CheckpointIntegrityError(
                        f"Checkpoint '{checkpoint_id}' signature verification failed"
                    )
        return True

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
        if verify:
            self.verify_checkpoint(checkpoint_id)
        else:
            self.load_checkpoint(checkpoint_id)

        checkpoint = self.load_checkpoint(checkpoint_id)
        destination = Path(target_path).expanduser().resolve() if target_path else self.project_path
        destination.mkdir(parents=True, exist_ok=True)

        for name in checkpoint.files:
            source_file = checkpoint.directory / name
            target_file = destination / name
            try:
                shutil.copy2(source_file, target_file)
            except OSError as exc:
                raise CheckpointError(
                    f"Failed to restore '{name}' from checkpoint '{checkpoint_id}': {exc}"
                ) from exc
        return destination

    def load_checkpoint_file(self, checkpoint_id: str, file_name: str) -> str:
        checkpoint = self.load_checkpoint(checkpoint_id)
        if file_name not in checkpoint.files:
            raise CheckpointNotFoundError(
                f"Checkpoint '{checkpoint_id}' does not contain '{file_name}'"
            )
        file_path = checkpoint.directory / file_name
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
        safe_id = backup_id.replace("/", "_").replace("..", "_")
        destination = self.backups_root / safe_id
        if destination.exists():
            raise CheckpointExistsError(f"Backup '{backup_id}' already exists at {destination}")
        try:
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
