"""Goal-driven auto-planning: when a plan must be split into atomic stages.

Three concerns live in one module because they move together:

1. **Detection** — is this goal a GUI application or a multi-module program?
   (:func:`scope_of`, :func:`should_decompose`).
2. **Rules** — what the planning agent is told to emit (:data:`PLANNING_RULES`,
   appended to the ``build_plan`` contract so the model sees them at planning
   time).
3. **Enforcement** — :func:`expand_implementation_stages`, the deterministic
   pass that splices atomic stages into a plan that still arrived as one broad
   "implement it" task. Prompt rules are advisory; this is not.

Why the split matters: a single "build the dashboard" task cannot be reviewed,
cannot be dispatched in parallel with anything, and hides the seam where a
backend change breaks a canvas widget. The chain is ordered, not a bag::

    Backend Data Layer  ->  UI Canvas Components  ->  Application Launcher

Nothing can be drawn before the data it shows exists, and nothing can be
launched before the canvas it hosts. Multi-module (non-GUI) goals get the same
spine with the middle stage named for interfaces instead of widgets.

This module is deliberately dependency-free (stdlib only): ``state_manager``
and ``orchestrator`` both import it, so it must never import either of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Sequence, Tuple

__all__ = [
    "EDIT",
    "GUI_SIGNALS",
    "MULTI_MODULE_SIGNALS",
    "PLANNING_RULES",
    "Stage",
    "GUI_STAGES",
    "MULTI_MODULE_STAGES",
    "expand_implementation_stages",
    "implementation_join_index",
    "scope_of",
    "should_decompose",
    "stages_for",
    "tokens",
]

#: Words that mark a goal as a *graphical* application. Token-matched (so
#: "gui" is not matched inside "guide" and "quit" never yields "ui"), and
#: deliberately free of broad words like "web" or "app": "build a web
#: billing service" is not a GUI goal and must not be decomposed as one.
GUI_SIGNALS: FrozenSet[str] = frozenset(
    {
        "gui",
        "tkinter",
        "pyqt",
        "pyside",
        "qt",
        "canvas",
        "dashboard",
        "desktop",
        "frontend",
        "webui",
        "interface",
        "interfaces",
        "graphical",
        "graphics",
        "electron",
        "ui",
    }
)

#: Words that mark a goal as *several modules/packages* that must be wired
#: together — the other case where one implementation task is too coarse.
MULTI_MODULE_SIGNALS: FrozenSet[str] = frozenset(
    {
        "module",
        "modules",
        "package",
        "packages",
        "plugin",
        "plugins",
        "microservice",
        "microservices",
        "monorepo",
        "layered",
        "layers",
    }
)

#: Titles of verbs that describe an implementation task. A plan whose
#: implementation task uses none of these can still be expanded (single
#: software_agent task fallback), but a verb match is preferred when there
#: are several candidates.
IMPLEMENTATION_VERBS: FrozenSet[str] = frozenset(
    {
        "implement",
        "implementation",
        "implementing",
        "build",
        "building",
        "develop",
        "developing",
        "create",
        "creating",
        "construct",
        "constructing",
        "deliver",
        "delivering",
        "assemble",
        "assembling",
        "wire",
        "wiring",
        "code",
        "coding",
        "ship",
        "shipping",
        "integrate",
        "integration",
        "write",
        "writing",
        "port",
        "porting",
    }
)

_TOKEN_RE = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class Stage:
    """One atomic step of the implementation chain."""

    title: str
    acceptance_criteria: Tuple[str, ...]
    notes: str
    expected_output: str = ""

    def as_spec(
        self,
        priority: str,
        input_files: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """Render the stage as a plan spec (whitelist fields only)."""
        return {
            "title": self.title,
            "owner": "software_agent",
            "priority": priority,
            "dependencies": [],
            "expected_outputs": [self.expected_output] if self.expected_output else [],
            "acceptance_criteria": list(self.acceptance_criteria),
            "notes": self.notes,
            "input_files": list(input_files),
        }


#: GUI spine. The titles are the contract: tests, the planning prompt and the
#: expansion all speak them, so a change here is a visible, reviewed change.
GUI_STAGES: Tuple[Stage, ...] = (
    Stage(
        title="Backend Data Layer",
        expected_output="docs/BACKEND_DATA_LAYER.md",
        acceptance_criteria=(
            "Data model, storage and service functions exist and are importable",
            "No UI widget or entry-point code inside the data layer",
        ),
        notes=(
            "Stage 1 of 3 — build the data layer first: model, persistence and "
            "the services the UI will call. Deliver only backend code; the "
            "canvas and launcher come in the next stages."
        ),
    ),
    Stage(
        title="UI Canvas Components",
        expected_output="docs/UI_CANVAS_COMPONENTS.md",
        acceptance_criteria=(
            "Every canvas component the architecture specifies is implemented",
            "Components read the data layer through its declared interface only",
        ),
        notes=(
            "Stage 2 of 3 — implement the canvas components against the backend "
            "data layer already delivered. No new storage, no direct file I/O: "
            "components consume the data layer's interface."
        ),
    ),
    Stage(
        title="Application Launcher",
        expected_output="",
        acceptance_criteria=(
            "A runnable entry point (main.py) starts the application",
            "The launcher wires the data layer and canvas together",
        ),
        notes=(
            "Stage 3 of 3 — the entry point. Wire the data layer and the canvas "
            "into one runnable application and add main.py to expected_outputs "
            "so the Definition of Done checks it exists."
        ),
    ),
)

#: Same spine for a multi-module, non-GUI program: interfaces between modules
#: replace the canvas stage.
MULTI_MODULE_STAGES: Tuple[Stage, ...] = (
    GUI_STAGES[0],
    Stage(
        title="Module Interface Layer",
        expected_output="docs/MODULE_INTERFACE_LAYER.md",
        acceptance_criteria=(
            "Module interfaces are declared before their implementations",
            "No module imports another module's internals",
        ),
        notes=(
            "Stage 2 of 3 — declare and implement the interfaces between "
            "modules: signatures, data shapes and error contracts. Callers must "
            "depend on the interface, never on another module's internals."
        ),
    ),
    GUI_STAGES[2],
)

#: Appended to the ``build_plan`` contract in
#: :meth:`MasterOrchestrator.build_plan`.
PLANNING_RULES = (
    "Decomposition rules — implementation must be atomic:\n"
    "- When the goal is a GUI application (graphical interface, desktop or web "
    "UI, canvas, dashboard, frontend) or a multi-module / multi-package program, "
    "do NOT emit one broad \"implement everything\" task. Emit this dependency "
    "chain, each stage owned by software_agent, each delivering its own file:\n"
    '  {"title": "Backend Data Layer"} -> '
    '  {"title": "UI Canvas Components"} -> '
    '  {"title": "Application Launcher"}\n'
    "- For a non-GUI multi-module goal replace \"UI Canvas Components\" with "
    '"Module Interface Layer"; the other two titles stay.\n'
    "- Dependencies chain in order: the canvas/interface stage depends on the "
    "data layer, the launcher depends on the canvas/interface stage, and later "
    "tasks (test, review, docs) depend on the launcher.\n"
    "- The launcher owns the runnable entry point (main.py); add it to that "
    "stage's expected_outputs.\n"
    "- A plan that arrives as a single implementation task for such a goal is "
    "expanded into these stages automatically, so emitting them yourself is "
    "what keeps your own ordering intact."
)


def tokens(text: str) -> FrozenSet[str]:
    """Lower-cased word tokens of ``text`` (``"Build a GUI"`` -> ``{build, gui}``)."""
    return frozenset(_TOKEN_RE.findall(str(text or "").lower()))


def scope_of(*texts: str) -> str:
    """``"gui"``, ``"multi-module"`` or ``""`` — the scope implied by ``texts``.

    GUI wins when both apply: a dashboard is a GUI *and* often multi-module,
    and the canvas stage is the one that must not be skipped.
    """
    words: FrozenSet[str] = frozenset()
    for text in texts:
        words |= tokens(text)
    if words & GUI_SIGNALS:
        return "gui"
    if words & MULTI_MODULE_SIGNALS:
        return "multi-module"
    return ""


def should_decompose(*texts: str) -> bool:
    """True when the goal/task text calls for atomic implementation stages."""
    return bool(scope_of(*texts))


def stages_for(scope: str) -> Tuple[Stage, ...]:
    """The stage chain for a detected scope (``""`` -> empty tuple)."""
    if scope == "gui":
        return GUI_STAGES
    if scope == "multi-module":
        return MULTI_MODULE_STAGES
    return ()


def implementation_index(specs: Sequence[Dict[str, Any]]) -> int:
    """Index of the task that should be split, or ``-1``.

    Prefers a ``software_agent`` task whose title reads as implementation
    work; falls back to the first ``software_agent`` task. Returns ``-1`` when
    there is nothing to split, so a plan without an implementation task is
    never invented one.
    """
    candidates = [
        index
        for index, spec in enumerate(specs)
        if isinstance(spec, dict)
        and str(spec.get("owner") or "").strip() == "software_agent"
        and str(spec.get("title") or "").strip()
    ]
    if not candidates:
        return -1
    for index in candidates:
        if tokens(str(specs[index].get("title") or "")) & IMPLEMENTATION_VERBS:
            return index
    return candidates[0]


def expand_implementation_stages(
    specs: Sequence[Dict[str, Any]],
    goal: str = "",
) -> List[Dict[str, Any]]:
    """Splice the atomic stage chain in place of the implementation task.

    No-op (returns the specs unchanged) when the goal/task text carries no
    GUI or multi-module signal, when there is no implementation task, or when
    the plan *already* contains stage titles — expansion is idempotent, so a
    model that followed :data:`PLANNING_RULES` is not decomposed twice.

    Ids: when the specs carry ``TASK-NNN`` ids they are renumbered in order
    and every dependency remapped (predecessors of the original task become
    predecessors of the first stage; dependents of the original task depend on
    the last stage). Specs without ids — the starter skeleton — are spliced
    only, because the caller assigns dependencies after appending.
    """
    original = list(specs)
    if not original:
        return original
    target = implementation_index(original)
    if target < 0:
        return original
    title = str(original[target].get("title") or "")
    scope = scope_of(goal, title)
    if not scope:
        return original
    stages = stages_for(scope)

    known = {stage.title for stage in stages}
    known |= {stage.title for stage in GUI_STAGES}
    known |= {stage.title for stage in MULTI_MODULE_STAGES}
    if any(str(spec.get("title") or "").strip() in known for spec in original):
        return original

    base = original[target]
    priority = str(base.get("priority") or "HIGH").strip() or "HIGH"
    inherited_outputs = [
        str(item) for item in (base.get("expected_outputs") or []) if str(item).strip()
    ]
    inherited_criteria = [
        str(item) for item in (base.get("acceptance_criteria") or []) if str(item).strip()
    ]
    inherited_inputs = [
        str(item) for item in (base.get("input_files") or []) if str(item).strip()
    ]
    # The starter skeleton names outputs at the project root
    # (``IMPLEMENTATION.md``); planned graphs put them under ``docs/``.
    # New stage outputs follow whichever style the task is replacing.
    style_bare = bool(inherited_outputs) and all(
        "/" not in item for item in inherited_outputs
    )

    rendered: List[Dict[str, Any]] = []
    for position, stage in enumerate(stages):
        spec = stage.as_spec(
            priority=priority,
            # Stage 1 inherits the original's reading list; the rest start
            # clean and get context files attached by the plan-ingest path.
            input_files=inherited_inputs if position == 0 else [],
        )
        outputs: List[str] = []
        if position == len(stages) - 1:
            # The launcher completes the original task, so it carries the
            # original deliverable (what dependents read) plus its own
            # acceptance criteria verbatim.
            outputs = [
                item
                for item in dict.fromkeys(inherited_outputs or [stage.expected_output])
                if item
            ]
            spec["acceptance_criteria"] = inherited_criteria + list(
                stage.acceptance_criteria
            )
        elif stage.expected_output:
            outputs = [stage.expected_output.replace("docs/", "") if style_bare else stage.expected_output]
        spec["expected_outputs"] = outputs
        rendered.append(spec)

    expanded = original[:target] + rendered + original[target + 1 :]

    has_ids = any(str(spec.get("id") or "").strip() for spec in expanded)
    if not has_ids:
        return expanded

    # Renumber in order, then remap every dependency through the index of the
    # task it referred to. The original task's id resolves to the LAST stage
    # for outside callers and to stage 1 only through the explicit chain
    # built below — hence the explicit assignments after the general remap.
    new_ids = [f"TASK-{index + 1:03d}" for index in range(len(expanded))]
    old_ids = [str(spec.get("id") or "").strip() for spec in expanded]
    owner_index: Dict[str, int] = {}
    for position, old_id in enumerate(old_ids):
        if old_id and old_id not in owner_index:
            owner_index[old_id] = position
    original_id = str(base.get("id") or "").strip()
    if original_id:
        # Dependents of the original task must wait for the whole chain.
        owner_index[original_id] = target + len(stages) - 1

    for position, spec in enumerate(expanded):
        spec["id"] = new_ids[position]
        if position == target:
            resolved: List[str] = []
            for dep in base.get("dependencies") or []:
                mapped = owner_index.get(str(dep))
                if mapped is not None and mapped != position:
                    resolved.append(new_ids[mapped])
            spec["dependencies"] = _dedupe(resolved)
        elif target < position < target + len(stages):
            spec["dependencies"] = [new_ids[position - 1]]
        else:
            resolved = []
            for dep in spec.get("dependencies") or []:
                mapped = owner_index.get(str(dep))
                if mapped is not None and mapped != position:
                    resolved.append(new_ids[mapped])
            spec["dependencies"] = _dedupe(resolved)
    return expanded


def implementation_join_index(specs: Sequence[Dict[str, Any]]) -> int:
    """Index of the last implementation-owned task in ``specs``.

    That task's completion is what unblocks everything after it: in the
    starter skeleton every task before it chains from the previous one, and
    every task after it depends on it. Defaults to ``2`` — the position of
    the implementation task in the un-expanded five-task skeleton — so
    behaviour is unchanged when there is no software task at all (the stage
    titles carry no implementation verb, which is why this asks for *last*
    software task rather than reusing :func:`implementation_index`).
    """
    owned = [
        index
        for index, spec in enumerate(specs)
        if isinstance(spec, dict)
        and str(spec.get("owner") or "").strip() == "software_agent"
    ]
    return owned[-1] if owned else 2


def _dedupe(values: Sequence[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
