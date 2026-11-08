"""JSON contract: schema loading, validation and input defaults.

Kinds: ``input`` (review request), ``output`` (review result / stage state),
``lesson`` (candidate or accepted lesson).
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

from .errors import CouncilError, EXIT_USAGE

Kind = Literal["input", "output", "lesson"]
KINDS: tuple[str, ...] = ("input", "output", "lesson")


@lru_cache(maxsize=None)
def load_schema(kind: str) -> dict:
    if kind not in KINDS:
        raise CouncilError(f"unknown schema kind '{kind}'", hint=f"Use one of: {', '.join(KINDS)}")
    text = resources.files("evidence_council").joinpath("schemas", f"{kind}.schema.json").read_text(encoding="utf-8")
    return json.loads(text)


def validate(doc: Any, kind: str) -> list[str]:
    """Return a list of human-readable problems ('$.path: message'); empty means valid."""
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(load_schema(kind))
    problems = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)
        problems.append(f"{path}: {err.message}")
    return problems


def detect_kind(doc: Any) -> str:
    """Best-effort guess of a document's kind (for `validate` without --kind)."""
    if isinstance(doc, dict):
        if "lesson_id" in doc:
            return "lesson"
        if "run_id" in doc or "stages_completed" in doc:
            return "output"
    return "input"


def require_valid(doc: Any, kind: str, label: str = "document") -> None:
    problems = validate(doc, kind)
    if problems:
        hint = {
            "input": "Compare with examples/inputs/*.json or print the schema with: evidence-council schema input",
            "output": "Outputs are produced by evidence-council itself; regenerate it rather than editing by hand.",
            "lesson": "Lessons are produced by `review`; regenerate rather than editing by hand.",
        }[kind]
        raise CouncilError(
            f"{label} does not match the {kind} contract ({len(problems)} problem(s)); first: {problems[0]}",
            hint=hint,
            exit_code=EXIT_USAGE,
            details=problems[:50],
        )


def with_defaults(input_doc: dict) -> dict:
    """Return a deep copy of a *valid* input with documented defaults filled in."""
    d = copy.deepcopy(input_doc)
    t = d["task"]
    t.setdefault("domain", "general")
    t.setdefault("kind", "objective")
    t.setdefault("answer_format", "text")
    t.setdefault("tags", [])
    d.setdefault("evidence", [])
    d.setdefault("constraints", [])
    d.setdefault("candidates", [])
    d.setdefault("checks", [])
    d.setdefault("rubric", [])
    p = d.setdefault("privacy", {})
    p.setdefault("level", "private")
    p.setdefault("local_only", False)
    p.setdefault("redact", True)
    pr = d.setdefault("providers", {})
    pr.setdefault("requested", [])
    pr.setdefault("exclude", [])
    pr.setdefault("judges", [])
    o = d.setdefault("options", {})
    o.setdefault("allow_code_execution", False)
    o.setdefault("create_lesson", True)
    o.setdefault("use_lessons", True)
    ids = [e["id"] for e in d["evidence"]]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise CouncilError(f"duplicate evidence ids: {', '.join(dupes)}", hint="Give every evidence item a unique id.", exit_code=EXIT_USAGE)
    cids = [c["id"] for c in d["checks"]]
    if len(cids) != len(set(cids)):
        raise CouncilError("duplicate check ids", hint="Give every check a unique id.", exit_code=EXIT_USAGE)
    return d
