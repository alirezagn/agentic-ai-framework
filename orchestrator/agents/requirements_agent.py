"""RequirementsAgent — specialised context parsing node.

Turns the payload it receives (PROJECT_MEMORY.md, task notes, explicitly
listed input files) into a structured, validated requirements dataset:

* extracts block-format ``REQ-ID:`` records (framework/02 format),
* extracts Markdown-table ``| REQ-001 | ... |`` rows,
* validates ids, types, priorities, statuses and acceptance criteria,
* flags vague, unmeasurable acceptance criteria,
* builds a requirement -> task -> test traceability map,
* reports assumptions and open questions.

The agent never invents user constraints: anything it cannot parse from the
supplied context is reported as an open question instead of being fabricated.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .. import config
from .base_agent import AgentOutput, AgentOutputError, BaseAgent, register_agent


class RequirementsParseError(AgentOutputError):
    """Raised when requirement context cannot be parsed at all."""


_BLOCK_HEADER_RE = re.compile(r"^\s*(?:[-*]\s*)?REQ-ID\s*:\s*(REQ-\d{3,})\s*$", re.IGNORECASE)
_FIELD_RE = re.compile(r"^\s*(?:[-*]\s*)?(Title|Type|Priority|Description|Rationale|"
                       r"Acceptance Criteria|Dependencies|Related Risks|Related Tests|"
                       r"Status)\s*:\s*(.*)$", re.IGNORECASE)
_TABLE_ROW_RE = re.compile(
    r"^\s*\|\s*(REQ-\d{3,})\s*\|\s*([^|]*?)\s*\|\s*([A-Za-z ]+?)\s*\|\s*$"
)
_TEST_ID_RE = re.compile(r"TEST-\d{3,}")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _split_multi(value: str) -> List[str]:
    if not value:
        return []
    parts = re.split(r"[,;]|(?:\band\b)|(?:\bwith\b)", value)
    cleaned = [_clean(part) for part in parts if _clean(part)]
    return cleaned


@register_agent("requirements_agent")
class RequirementsAgent(BaseAgent):
    """Parse requirement context and return a validated requirements dataset."""

    AGENT_ID = "requirements_agent"

    SYSTEM_RULES = (
        "Requirements rules:\n"
        "- Never invent user constraints; record unknowns as open questions.\n"
        "- Prefer measurable acceptance criteria (numbers, thresholds, test ids).\n"
        "- Record assumptions explicitly.\n"
        "- Keep REQ ids stable; flag duplicates instead of renumbering silently."
    )

    # ------------------------------------------------------------------
    # Parsing primitives
    # ------------------------------------------------------------------

    @staticmethod
    def parse_requirement_block(block: Dict[str, str]) -> Dict[str, Any]:
        """Normalise one block-format requirement record."""
        req_id = _clean(block.get("req_id", ""))
        if not req_id:
            raise RequirementsParseError("Requirement block is missing REQ-ID")
        req_type = _clean(block.get("type", "")).upper().replace("-", "-")
        priority = _clean(block.get("priority", "")).upper()
        status = _clean(block.get("status", "")).upper()
        return {
            "id": req_id,
            "title": _clean(block.get("title", "")),
            "type": req_type,
            "priority": priority,
            "description": _clean(block.get("description", "")),
            "rationale": _clean(block.get("rationale", "")),
            "acceptance_criteria": _split_multi(block.get("acceptance_criteria", "")),
            "dependencies": _split_multi(block.get("dependencies", "")),
            "related_risks": _split_multi(block.get("related_risks", "")),
            "related_tests": _split_multi(block.get("related_tests", "")),
            "status": status,
            "source_format": "block",
        }

    @classmethod
    def extract_block_requirements(cls, text: str) -> List[Dict[str, Any]]:
        """Extract ``REQ-ID: REQ-001`` style records from free text."""
        requirements: List[Dict[str, Any]] = []
        lines = text.splitlines()
        current: Optional[Dict[str, str]] = None

        def flush() -> None:
            nonlocal current
            if current is not None and current.get("req_id"):
                requirements.append(cls.parse_requirement_block(current))
            current = None

        for line in lines:
            header = _BLOCK_HEADER_RE.match(line)
            if header:
                flush()
                current = {"req_id": header.group(1)}
                continue
            if current is None:
                continue
            field = _FIELD_RE.match(line)
            if field:
                key = field.group(1).strip().lower().replace(" ", "_")
                value = field.group(2).strip()
                if key in current and current[key]:
                    current[key] = f"{current[key]}; {value}"
                else:
                    current[key] = value
                continue
            if line.strip() == "" and current.get("description"):
                flush()
        flush()
        return requirements

    @classmethod
    def extract_table_requirements(cls, text: str) -> List[Dict[str, Any]]:
        """Extract ``| REQ-001 | Title | STATUS |`` rows from Markdown tables."""
        requirements: List[Dict[str, Any]] = []
        for line in text.splitlines():
            match = _TABLE_ROW_RE.match(line)
            if not match:
                continue
            req_id = match.group(1)
            title = _clean(match.group(2))
            status = _clean(match.group(3)).upper()
            if title.lower() in ("title", "id", "req-id"):
                continue
            requirements.append(
                {
                    "id": req_id,
                    "title": title,
                    "type": "",
                    "priority": "",
                    "description": "",
                    "rationale": "",
                    "acceptance_criteria": [],
                    "dependencies": [],
                    "related_risks": [],
                    "related_tests": [],
                    "status": status,
                    "source_format": "table",
                }
            )
        return requirements

    @classmethod
    def extract_requirements(cls, text: str) -> List[Dict[str, Any]]:
        """Extract requirements from any supported format, block wins on id clash."""
        block_reqs = cls.extract_block_requirements(text)
        table_reqs = cls.extract_table_requirements(text)
        merged: Dict[str, Dict[str, Any]] = {}
        for req in table_reqs:
            merged[req["id"]] = req
        for req in block_reqs:
            existing = merged.get(req["id"])
            if existing is None:
                merged[req["id"]] = req
                continue
            merged[req["id"]] = {**existing, **{k: v for k, v in req.items() if v}}
        ordered = [merged[req_id] for req_id in sorted(merged)]
        return ordered

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_requirements(requirements: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
        """Return (errors, warnings) for a parsed requirement set."""
        errors: List[str] = []
        warnings: List[str] = []
        seen: List[str] = []

        for req in requirements:
            req_id = str(req.get("id", ""))
            if not re.fullmatch(r"REQ-\d{3,}", req_id):
                errors.append(f"Malformed requirement id '{req_id}'")
                continue
            if req_id in seen:
                errors.append(f"Duplicate requirement id '{req_id}'")
            seen.append(req_id)

            status = str(req.get("status", "")).upper()
            if status and status not in config.REQUIREMENT_STATUSES:
                errors.append(f"{req_id}: invalid status '{status}'")
            req_type = str(req.get("type", "")).upper()
            if req_type and req_type not in config.REQUIREMENT_TYPES:
                warnings.append(f"{req_id}: unknown type '{req_type}'")
            priority = str(req.get("priority", "")).upper()
            if priority and priority not in config.REQUIREMENT_PRIORITIES:
                warnings.append(f"{req_id}: unknown priority '{priority}'")
            if not str(req.get("title", "")).strip():
                errors.append(f"{req_id}: missing title")

            criteria = [str(item) for item in req.get("acceptance_criteria") or []]
            is_mandatory = priority == "MUST" or status == "APPROVED"
            if is_mandatory and not criteria:
                warnings.append(f"{req_id}: no measurable acceptance criteria recorded")
            for criterion in criteria:
                lowered = criterion.lower()
                for vague in config.VAGUE_ACCEPTANCE_TERMS:
                    if re.search(rf"\b{re.escape(vague)}\b", lowered):
                        warnings.append(
                            f"{req_id}: acceptance criterion uses vague term '{vague}' "
                            f"— replace with a measurable threshold"
                        )
                        break
        return errors, warnings

    # ------------------------------------------------------------------
    # Traceability
    # ------------------------------------------------------------------

    @staticmethod
    def build_traceability(
        requirements: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        haystack_texts: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, List[str]]]:
        """Map requirement -> tasks and requirement -> tests."""
        trace: Dict[str, Dict[str, List[str]]] = {}
        haystack_texts = haystack_texts or []
        for req in requirements:
            req_id = str(req.get("id", ""))
            related_tasks: List[str] = []
            for task in tasks:
                blob_parts: List[str] = [str(task.get("id", "")), str(task.get("title", ""))]
                for key in ("notes", "expected_outputs", "acceptance_criteria"):
                    value = task.get(key)
                    if isinstance(value, list):
                        blob_parts.extend(str(item) for item in value)
                    elif value:
                        blob_parts.append(str(value))
                blob = " ".join(blob_parts)
                if re.search(rf"\b{re.escape(req_id)}\b", blob):
                    related_tasks.append(str(task.get("id")))
            related_tests: List[str] = []
            for text in haystack_texts:
                for line in text.splitlines():
                    if req_id in line:
                        for test_id in _TEST_ID_RE.findall(line):
                            if test_id not in related_tests:
                                related_tests.append(test_id)
            for declared in req.get("related_tests") or []:
                if declared not in related_tests:
                    related_tests.append(str(declared))
            trace[req_id] = {"tasks": related_tasks, "tests": related_tests}
        return trace

    # ------------------------------------------------------------------
    # Context gathering
    # ------------------------------------------------------------------

    def gather_context_text(self, payload: Dict[str, Any]) -> str:
        parts: List[str] = []
        task = payload.get("task") or {}
        parts.append(str(payload.get("project_memory") or ""))
        context = payload.get("context") or {}
        for key in sorted(context):
            parts.append(f"### source: {key}\n{context[key]}")
        criteria = task.get("acceptance_criteria") or []
        if isinstance(criteria, list):
            parts.append("\n".join(str(item) for item in criteria))
        expected = task.get("expected_outputs") or []
        if isinstance(expected, list):
            parts.append("\n".join(str(item) for item in expected))
        return "\n".join(parts)

    @staticmethod
    def extract_open_questions(context_text: str, requirements: List[Dict[str, Any]]) -> List[str]:
        questions: List[str] = []
        for match in re.finditer(r"^\s*(?:[-*]\s*)?(?:\?\s*)(.+)$", context_text, re.MULTILINE):
            question = _clean(match.group(1))
            if question:
                questions.append(question)
        for line in context_text.splitlines():
            lowered = line.strip().lower()
            if lowered.startswith(("open question:", "tbd:", "unknown:", "assumption:")):
                cleaned = _clean(line)
                if cleaned and cleaned not in questions:
                    questions.append(cleaned)
        if not requirements:
            questions.append("No requirements could be parsed from the supplied context")
        return questions

    @staticmethod
    def extract_assumptions(context_text: str) -> List[str]:
        assumptions: List[str] = []
        for line in context_text.splitlines():
            lowered = line.strip().lower()
            if lowered.startswith("assumption:"):
                assumption = _clean(line.split(":", 1)[1])
                if assumption and assumption not in assumptions:
                    assumptions.append(assumption)
        return assumptions

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task = payload.get("task") or {}
        task_id = str(task.get("id", ""))

        context_text = self.gather_context_text(payload)
        if not context_text.strip():
            raise RequirementsParseError(
                f"Task '{task_id}' supplied no requirement context to parse"
            )

        requirements = self.extract_requirements(context_text)
        errors, warnings = self.validate_requirements(requirements)
        if errors:
            raise RequirementsParseError(
                "Requirement validation failed: " + "; ".join(errors)
            )

        tasks = []
        try:
            tasks = self.state_manager.load_tasks()
        except Exception:
            tasks = []

        haystack = [context_text]
        try:
            haystack.append(self.state_manager.load_decisions())
        except Exception:
            pass
        traceability = self.build_traceability(requirements, tasks, haystack)

        approved = [r for r in requirements if r.get("status") == "APPROVED"]
        proposed = [r for r in requirements if r.get("status") == "PROPOSED"]
        rejected = [r for r in requirements if r.get("status") == "REJECTED"]
        untested = [
            req_id
            for req_id, entry in traceability.items()
            if not entry["tests"] and entry["tasks"]
        ]

        data: Dict[str, Any] = {
            "requirements": requirements,
            "count": len(requirements),
            "approved": len(approved),
            "proposed": len(proposed),
            "rejected": len(rejected),
            "traceability": traceability,
            "requirements_without_tests": untested,
            "assumptions": self.extract_assumptions(context_text),
            "open_questions": self.extract_open_questions(context_text, requirements),
            "status_summary": {
                "approved": len(approved),
                "proposed": len(proposed),
                "rejected": len(rejected),
                "total": len(requirements),
            },
        }

        summary = (
            f"Parsed {len(requirements)} requirements "
            f"({len(approved)} approved, {len(proposed)} proposed, {len(rejected)} rejected)"
        )
        artifacts = [
            name
            for name in (payload.get("context") or {}).keys()
            if str(name).upper().startswith("REQUIREMENTS")
        ]
        return self.completed(
            task_id=task_id,
            summary=summary,
            data=data,
            artifacts=artifacts,
            warnings=warnings,
        )


__all__ = ["RequirementsAgent", "RequirementsParseError"]
