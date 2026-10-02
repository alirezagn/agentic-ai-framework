"""Static import-consistency check — the deterministic half of the
``INTERFACE_ALIGNMENT_CONTRACT``.

Why this exists: a consumer that guesses a producer's name
(``from src.metrics_collector import get_metrics_snapshot`` while the module
actually defines ``get_system_metrics``) is perfectly importable *syntax* —
nothing imports the code while the agent writes it, so the mistake only
surfaces in the operator's terminal, after the task is already DONE. Prompt
rules alone did not stop it: the contract said "read the producer first", and
the model still inferred the name.

This module closes that gap with a check that runs at delivery time, inside
:func:`orchestrator.agents.base_agent.delivery_problems` (so it participates
in the Definition of Done and in the one automatic repair round):

- every intra-project ``from X import Y`` resolves to a real file, and ``Y``
  must be a name that file actually defines at module level (definitions,
  assignments, imports — plus submodules for packages);
- ``import a.b`` inside the project must point at a file that exists;
- a delivered file must at least parse — a file that does not compile can
  never be imported, whatever it claims to contain.

Deliberately *no* subprocess is spawned: the check is pure ``ast`` work on
files already on disk, so the suite stays offline and hermetic, and the
operator's interpreter is never asked to import an agent's half-finished
code. Scope is symmetric with the task's own responsibility: a file is only
judged when this task delivered it (or when it imports a module this task
delivered), so pre-existing breakage elsewhere in the project never fails an
unrelated task.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set

#: Directories that are never agent-authored application code.
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "docs",
        "site-packages",
        "node_modules",
        ".git",
        ".venv",
        "venv",
        "build",
        "dist",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

#: How many exported names to name in a failure message (keeps the note that
#: the repair round feeds back to the model readable).
_MAX_NAMES_IN_MESSAGE = 8


class _Target:
    """A resolved intra-project import target."""

    __slots__ = ("rel", "names")

    def __init__(self, rel: str, names: Optional[Set[str]]) -> None:
        #: Project-relative POSIX path of the module file, or of the package
        #: directory for namespace packages.
        self.rel = rel
        #: Exported top-level names, or ``None`` when the module defines
        #: ``__getattr__`` (PEP 562) and therefore accepts any attribute.
        self.names = names


def import_contract_problems(
    project_path: Path,
    delivered: Iterable[str] = (),
) -> List[str]:
    """Return delivery-blocking import problems for ``project``.

    ``delivered`` is the set of paths this task delivered or edited (the
    ``data.documents`` / ``data.edits`` / ``data.edits_applied`` keys). A file
    is checked when it is in that set, or when it imports a module that is —
    the consumer side and the producer side of the same drift.

    Pure function of the filesystem; never spawns a process.
    """
    project = Path(project_path)
    if not project.is_dir():
        return []
    touched = {_normalize(entry) for entry in delivered if isinstance(entry, str)}
    if not touched:
        return []

    trees: Dict[str, ast.Module] = {}
    problems: List[str] = []
    for rel, path in _project_py_files(project).items():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            trees[rel] = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            if _is_touched(rel, touched):
                problems.append(
                    f"{rel} does not parse (line {exc.lineno}: {exc.msg}) — "
                    "it cannot be imported; deliver valid Python"
                )
            continue

    cache: Dict[str, Optional[_Target]] = {}

    def target_for(base_dir: str, parts: Sequence[str]) -> Optional[_Target]:
        key = f"{base_dir}::{'.'.join(parts)}"
        if key not in cache:
            cache[key] = _resolve_target(project, trees, base_dir, parts)
        return cache[key]

    for importer_rel, tree in trees.items():
        importer_touched = _is_touched(importer_rel, touched)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                problems.extend(
                    _from_problems(project, importer_rel, node, touched,
                                   importer_touched, target_for)
                )
            elif isinstance(node, ast.Import):
                problems.extend(
                    _import_problems(project, importer_rel, node, touched,
                                     importer_touched, target_for)
                )
    return sorted(set(problems))


# ---------------------------------------------------------------------------
# Scanning helpers
# ---------------------------------------------------------------------------


def _normalize(entry: str) -> str:
    return entry.replace("\\", "/").lstrip("./")


def _is_touched(rel: str, touched: Set[str]) -> bool:
    # Mirrors delivery_problems' has_channel(): the payload may name a file by
    # its project-relative path or by its bare filename.
    return rel in touched or Path(rel).name in touched


def _project_py_files(project: Path) -> Dict[str, Path]:
    """All application ``.py`` files in the project, as rel-path -> path."""
    found: Dict[str, Path] = {}
    for path in sorted(project.rglob("*.py")):
        try:
            rel = path.relative_to(project)
        except ValueError:
            continue
        if any(
            part in _SKIP_DIRS or (part.startswith(".") and part not in (".", ".."))
            for part in rel.parts[:-1]
        ):
            continue
        found[rel.as_posix()] = path
    return found


def _resolve_target(
    project: Path,
    trees: Dict[str, ast.Module],
    base_dir: str,
    parts: Sequence[str],
) -> Optional[_Target]:
    """Locate a module below ``base_dir`` and collect what it exports."""
    current = Path(base_dir)
    for part in parts:
        if not part or part == ".":
            continue
        current = current / part
    rel = current.as_posix()
    if rel in ("", "."):
        rel = "."
    as_file = None if rel == "." else project / (rel + ".py")
    as_dir = project if rel == "." else project / rel
    if as_file is not None and as_file.is_file():
        file_rel = as_file.relative_to(project).as_posix()
        names = _names_for_file(file_rel, trees)
        return _Target(file_rel, names)
    if as_dir.is_dir():
        init = as_dir / "__init__.py"
        names: Set[str] = set()
        if init.is_file():
            init_rel = init.relative_to(project).as_posix()
            init_names = _names_for_file(init_rel, trees)
            if init_names is None:
                return _Target(as_dir.relative_to(project).as_posix(), None)
            names |= init_names
        for child in as_dir.iterdir():
            if child.name.startswith("_"):
                continue
            if child.is_file() and child.suffix == ".py":
                names.add(child.stem)
            elif child.is_dir():
                names.add(child.name)
        return _Target(as_dir.relative_to(project).as_posix(), names)
    return None


def _names_for_file(rel: str, trees: Dict[str, ast.Module]) -> Optional[Set[str]]:
    tree = trees.get(rel)
    if tree is None:
        try:
            source = Path(rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return set()
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError:
            return set()
        trees[rel] = tree
    names = _collect_names(tree)
    return None if "__getattr__" in names else names


def _collect_names(tree: ast.Module) -> Set[str]:
    """Top-level names a module exports: defs, classes, assignments, imports.

    Conditional and ``try``-wrapped definitions are included (``if TYPE_CHECKING:``
    guards and ``try: import x / except: def x`` fallbacks are common), so a
    legitimate name is never reported missing.
    """
    names: Set[str] = set()

    def visit(nodes: Iterable[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    _assign_targets(target, names)
            elif isinstance(node, ast.AnnAssign):
                _assign_targets(node.target, names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
            elif isinstance(node, ast.If):
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.Try):
                visit(node.body)
                visit(node.orelse)
                visit(node.finalbody)
                for handler in node.handlers:
                    visit(handler.body)

    visit(tree.body)
    return names


def _assign_targets(target: ast.expr, names: Set[str]) -> None:
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _assign_targets(element, names)
    elif isinstance(target, ast.Starred):
        _assign_targets(target.value, names)


# ---------------------------------------------------------------------------
# Per-import checks
# ---------------------------------------------------------------------------


def _describe_names(names: Optional[Set[str]]) -> str:
    if not names:
        return "it exports nothing"
    usable = sorted(name for name in names if not name.startswith("__"))
    shown = usable[:_MAX_NAMES_IN_MESSAGE]
    suffix = ", ..." if len(usable) > len(shown) else ""
    return "it defines: " + ", ".join(shown) + suffix


def _in_project(project: Path, first_component: str) -> bool:
    """True when the import's first component names something in the project."""
    return (project / first_component).exists() or (
        project / f"{first_component}.py"
    ).exists()


def _from_problems(
    project: Path,
    importer_rel: str,
    node: ast.ImportFrom,
    touched: Set[str],
    importer_touched: bool,
    target_for,
) -> List[str]:
    module = node.module or ""
    level = node.level or 0
    importer_dir = Path(importer_rel).parent.as_posix()
    if importer_dir == ".":
        importer_dir = "."

    if level > 0:
        base = Path(importer_dir)
        for _ in range(level - 1):
            base = base.parent
        base_dir = base.as_posix()
        if base_dir.startswith(".."):
            return []
        parts = module.split(".") if module else []
        target = target_for(base_dir, parts)
        if target is None:
            # A relative import cannot leave the project: unresolved means the
            # producer the task believes in does not exist.
            if importer_touched:
                specifier = "." * level + module
                return [
                    f"{importer_rel} does `from {specifier} import ...` but no "
                    "module exists at that relative path — fix the import or "
                    "deliver the module"
                ]
            return []
    else:
        if not module:
            return []
        parts = module.split(".")
        if not _in_project(project, parts[0]):
            return []  # third-party or stdlib — not ours to verify
        target = target_for(".", parts)
        if target is None:
            if importer_touched:
                return [
                    f"{importer_rel} imports {module}, which does not exist in "
                    "the project — check the module path you read"
                ]
            return []

    if not (importer_touched or _is_touched(target.rel, touched)):
        return []

    names = target.names
    if names is None:
        return []  # module defines __getattr__: any name is legal

    wildcard = any(alias.name == "*" for alias in node.names)
    if wildcard:
        return []

    missing = [
        alias.name for alias in node.names
        if alias.name not in names
        and not _is_submodule(target.rel, alias.name, project)
    ]
    if not missing:
        return []
    specifier = ("." * level + module) if level else module
    return [
        f"{importer_rel} imports {', '.join(missing)} from {specifier}, which "
        f"does not define {'it' if len(missing) == 1 else 'them'} — read the "
        f"producer file ({target.rel}); {_describe_names(names)}"
    ]


def _is_submodule(target_rel: str, name: str, project: Path) -> bool:
    """True when ``name`` is a submodule of the target package directory."""
    if name.startswith("_"):
        return False
    base = project / Path(target_rel)
    if not base.is_dir():
        return False
    return (base / f"{name}.py").is_file() or (base / name).is_dir()


def _import_problems(
    project: Path,
    importer_rel: str,
    node: ast.Import,
    touched: Set[str],
    importer_touched: bool,
    target_for,
) -> List[str]:
    if not importer_touched:
        return []
    problems: List[str] = []
    for alias in node.names:
        parts = alias.name.split(".")
        first = parts[0]
        if not (project / first).exists() and not (project / f"{first}.py").exists():
            continue  # third-party or stdlib — not ours to verify
        target = target_for(".", parts)
        if target is None:
            problems.append(
                f"{importer_rel} imports {alias.name}, which does not exist in "
                "the project — check the module path you read"
            )
    return problems
