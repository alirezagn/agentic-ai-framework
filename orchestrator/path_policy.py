"""Path containment policy — the single source of truth for "may this path be
read or written?".

Security model
--------------
Every path that reaches the filesystem from *untrusted* input — a checkpoint's
own ``metadata.json``, a restored filename, a snapshot id — must pass through
this module before it is opened. Containment is enforced twice, deliberately:

1. **Syntactic rejection** of the shapes that have no legitimate use in this
   codebase: absolute paths, drive letters, ``..`` segments, ``.`` segments,
   empty segments (``a//b``), trailing separators, NUL bytes and backslashes.
2. **Semantic containment** after resolution: the candidate is resolved (which
   collapses ``..`` *and* follows symlinks) and must then be inside the
   resolved root. This is what catches the case syntax alone cannot — a
   *symlink* inside the project pointing at ``/etc``.

Both passes must pass. A check that only rejects the literal string ``..`` is
trivially defeated by a symlink; a check that only resolves is easy to get
wrong on a path that does not exist yet.

Why this is a separate module
-----------------------------
Containment was previously reimplemented inline in at least three places
(``_apply_edits``, ``_materialize_artifacts``, ``CheckpointManager``), and one
of them — checkpoint restore — omitted it entirely. Centralising the rule makes
"did you check containment?" a one-line answer (``grep resolve_inside``) and
gives the security regression suite a single surface to attack.

Deliberate rejections that may look surprising
-----------------------------------------------
``..`` is **rejected**, never sanitised. Rewriting ``a/../../x`` to
``a_/_/_/x`` (the previous checkpoint-id behaviour) turns an attack into a
silent no-op instead of a loud error, which hides both the attempt and the bug
that allowed it. Fail closed, fail loudly, and name the offending value.

Backslash (``\\``) is rejected as a separator. On POSIX it is a legal filename
character, but no filename in ``config.STATE_FILES`` contains one, and
accepting it means a Windows-authored manifest can smuggle a separator past a
POSIX-only check. Rejecting it is portable; accepting it is not.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, List, Union

__all__ = [
    "PathPolicyError",
    "PathLike",
    "resolve_inside",
    "validate_relative_name",
    "validate_state_file_name",
    "validate_snapshot_id",
    "validate_filenames",
    "is_state_file_name",
]


class PathPolicyError(ValueError):
    """A path was rejected by the containment policy.

    Subclasses :class:`ValueError` so callers that already funnel malformed
    input into a ``ValueError`` handler keep working; callers that care can
    catch this type specifically and distinguish "untrusted path" from "wrong
    argument".
    """


#: Accepted input types for a path-ish argument. Deliberately narrow: ``bytes``
#: and ``os.PathLike[str]`` implementations that return non-``str`` are rejected
#: rather than coerced, because coercion is where encoding bugs live.
PathLike = Union[str, "os.PathLike[str]"]


def _describe(value: Any) -> str:
    """Render an arbitrary rejected value for an error message, bounded in size."""
    text = repr(value)
    if len(text) > 120:
        return text[:117] + "..."
    return text


def _as_text(value: PathLike, *, argument: str) -> str:
    """Normalise a path-ish argument to ``str`` or raise :class:`PathPolicyError`."""
    if isinstance(value, str):
        return value
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__fspath__"):
        unwrapped = os.fspath(value)
        if isinstance(unwrapped, str):
            return unwrapped
        raise PathPolicyError(
            f"{argument} must be str or PathLike[str], got {type(value).__name__} "
            f"yielding {type(unwrapped).__name__}"
        )
    raise PathPolicyError(
        f"{argument} must be str or PathLike[str], got {type(value).__name__}: {_describe(value)}"
    )


def validate_relative_name(name: PathLike, *, context: str = "path") -> str:
    """Validate that ``name`` is a safe *relative* path, returning it unchanged.

    This is the syntactic pass. It rejects, with a message naming the reason:

    * non ``str``/``PathLike[str]`` input (including ``bytes``)
    * empty or whitespace-only input
    * NUL bytes
    * absolute POSIX paths (``/etc/passwd``)
    * Windows drive-qualified or UNC paths (``C:\\\\x``, ``\\\\\\\\host\\\\share``)
    * any ``..`` or ``.`` path segment
    * empty segments (``a//b``, ``a/./b``)
    * trailing separators (``dir/``)
    * backslashes, which are treated as separators on every platform

    Returns the input as ``str`` when it is valid, so call sites can use the
    result directly.
    """
    text = _as_text(name, argument=context)

    if not text:
        raise PathPolicyError(f"{context} must not be empty")
    if text.strip() != text or not text.strip():
        raise PathPolicyError(
            f"{context} must not be blank or contain leading/trailing whitespace: {_describe(name)}"
        )
    if "\x00" in text:
        raise PathPolicyError(f"{context} must not contain NUL bytes: {_describe(name)}")
    if "\\" in text:
        raise PathPolicyError(
            f"{context} must not contain backslashes (use '/' as the separator): {_describe(name)}"
        )

    # Absolute in POSIX terms.
    if text.startswith("/"):
        raise PathPolicyError(f"{context} must be relative, got absolute path: {_describe(name)}")

    # Absolute in Windows terms: drive-qualified ("C:x") or UNC-ish ("//host").
    windows = PureWindowsPath(text)
    if windows.drive or windows.root:
        raise PathPolicyError(
            f"{context} must be relative, got drive-qualified or UNC path: {_describe(name)}"
        )

    segments = text.split("/")
    for segment in segments:
        if segment == "":
            raise PathPolicyError(
                f"{context} must not contain empty path segments: {_describe(name)}"
            )
        if segment == ".":
            raise PathPolicyError(
                f"{context} must not contain '.' segments: {_describe(name)}"
            )
        if segment == "..":
            raise PathPolicyError(
                f"{context} must not contain '..' segments: {_describe(name)}"
            )

    return text


def resolve_inside(root: PathLike, candidate: PathLike, *, context: str = "path") -> Path:
    """Resolve ``candidate`` relative to ``root`` and assert it stays inside.

    Runs the syntactic pass (:func:`validate_relative_name`), joins, then
    resolves both sides and requires containment. Resolution is what defeats
    symlink escapes and any ``..`` that survived string handling.

    The returned path is fully resolved (symlinks collapsed), so callers should
    use it verbatim for I/O rather than re-joining the name themselves.

    :raises PathPolicyError: if the candidate is malformed or escapes ``root``.
    """
    text = validate_relative_name(candidate, context=context)

    root_path = Path(_as_text(root, argument="root")).expanduser()
    if not root_path.exists():
        # A missing root cannot be escaped from meaningfully, but callers rely
        # on an unambiguous error rather than a confusing resolution failure.
        raise PathPolicyError(f"root does not exist: {_describe(root)}")
    resolved_root = root_path.resolve()

    target = (resolved_root / text).resolve()

    if target == resolved_root:
        raise PathPolicyError(
            f"{context} must not resolve to the root itself: {_describe(candidate)}"
        )
    if not target.is_relative_to(resolved_root):
        raise PathPolicyError(
            f"{context} escapes its root: {_describe(candidate)} -> {target}"
        )
    return target


def is_state_file_name(name: str) -> bool:
    """True when ``name`` is a member of the allowlisted state-file set.

    Import is deferred to keep this module importable in contexts where
    ``config`` is only partially initialised.
    """
    from . import config

    return name in set(config.STATE_FILES)


def validate_state_file_name(name: PathLike, *, context: str = "state file name") -> str:
    """Validate ``name`` as a relative path *and* require it to be a state file.

    This is the deny-by-default allowlist: a filename that is not in
    ``config.STATE_FILES`` is refused outright, regardless of where it points.
    A traversal string can never be a member of that fixed tuple, so the
    traversal class of attack is closed structurally rather than by pattern
    matching.

    The name is still validated as a relative path first, so that the *reason*
    reported for a hostile value is the specific one ("not a state file",
    "contains '..'") rather than a generic failure.
    """
    from . import config

    text = validate_relative_name(name, context=context)

    allowed: Iterable[str] = tuple(config.STATE_FILES)
    if text not in set(allowed):
        raise PathPolicyError(
            f"{context} is not an allowlisted state file: {_describe(name)} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )
    return text


def validate_filenames(names: Iterable[PathLike], *, context: str = "checkpoint file name") -> List[str]:
    """Validate every name in ``names``, returning them as a list of ``str``.

    Pure and I/O free by design: call this *before* touching the filesystem so
    a malformed manifest is rejected without having created anything.
    """
    validated: list[str] = []
    for name in names:
        validated.append(validate_state_file_name(name, context=context))
    return validated


def validate_snapshot_id(snapshot_id: PathLike, *, context: str = "snapshot id") -> str:
    """Validate a checkpoint or backup identifier used as a single path segment.

    Stricter than :func:`validate_relative_name`: an id becomes one directory
    name, so it must not contain a separator at all. Unlike the previous
    ``replace("/", "_").replace("..", "_")`` sanitiser, this raises instead of
    silently rewriting, so a traversal attempt is visible in logs rather than
    becoming a no-op.
    """
    text = _as_text(snapshot_id, argument=context)

    if not text:
        raise PathPolicyError(f"{context} must not be empty")
    if "\x00" in text:
        raise PathPolicyError(f"{context} must not contain NUL bytes: {_describe(snapshot_id)}")
    if text.strip() != text:
        raise PathPolicyError(
            f"{context} must not contain leading/trailing whitespace: {_describe(snapshot_id)}"
        )
    if text in (".", ".."):
        raise PathPolicyError(f"{context} must not be a relative path reference: {_describe(snapshot_id)}")
    if "/" in text or "\\" in text:
        raise PathPolicyError(
            f"{context} must be a single path segment without separators: {_describe(snapshot_id)}"
        )

    windows = PureWindowsPath(text)
    if windows.drive or windows.root:
        raise PathPolicyError(
            f"{context} must not be drive-qualified or UNC: {_describe(snapshot_id)}"
        )

    return text
