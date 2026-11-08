"""Learning stage: candidate lessons and versioned, append-only lesson memory.

A review may emit a *candidate* lesson. It is never applied automatically.
``evidence-council lessons accept RESULT.json`` runs every acceptance check below
and, only if all pass, appends it to ``<home>/memory/lessons.jsonl`` — a hash-chained,
append-only log (each entry has ``seq``, ``prev_hash``, ``hash``). Retiring a lesson
appends a ``retire`` entry; nothing is rewritten or deleted.

This module never writes outside the memory directory. It does not touch
CLAUDE.md, AGENTS.md or any other agent's instructions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contract import validate
from .errors import CouncilError, EXIT_USAGE
from .privacy import scan_text
from .util import canonical_json, now_iso, sha256_digest
from .verify import SELECTING

INJECTION_PATTERNS = [
    r"ignore (all|any|the)? ?(previous|prior|above)", r"system prompt", r"disregard", r"you must always",
    r"\bclaude\.md\b", r"\bagents\.md\b", r"<script", r"rm -rf", r"curl .*\|\s*(sh|bash)", r"override (the )?(rules|instructions)",
]
DETERMINISTIC_TYPES = SELECTING | {"regex", "json_schema", "evidence_facts", "citations"}


# ---------------------------------------------------------------------------
# candidate creation

def make_candidate(result: dict) -> dict | None:
    """Build a candidate lesson from a completed verdict, or None when nothing was verified."""
    inp = result["input"]
    if not inp.get("options", {}).get("create_lesson", True):
        return None
    v = result.get("verdict") or {}
    if v.get("status") != "supported":
        return None
    t = inp["task"]
    opts = {o["option_id"]: o for o in result.get("options", [])}
    sel = opts[v["selected_option"]]
    checks = [c for c in result["verification"]["checks"] if c.get("target_option")]
    sel_types = sorted({c["type"] for c in checks if c["target_option"] == sel["option_id"] and c["status"] == "pass" and c["can_select"]})
    rejected = v.get("rejected", [])
    level = inp.get("privacy", {}).get("level", "private")
    private = level in ("private", "restricted")
    label = t.get("id") or result["task_digest"][7:19]
    subject = f"'{t['title']}'" if t.get("title") and not private else f"task {label}"
    domain = t.get("domain", "general")
    fmt = t.get("answer_format", "text")
    show = (lambda s: "") if private or fmt == "code" else (lambda s: f" '{s[:120]}'")
    if v.get("overturned_popular"):
        pop = opts[v["popular_option"]]
        statement = (f"In {domain} task {subject}, the majority answer{show(pop['display_answer'])} ({pop['support_count']} proposals) "
                     f"failed objective checks while the answer{show(sel['display_answer'])} passed {', '.join(sel_types)}. "
                     f"Provider agreement was wrong here: verify {fmt} answers with {', '.join(sel_types)} before trusting consensus.")
    elif rejected:
        statement = (f"In {domain} task {subject}, {len(rejected)} competing answer(s) failed objective checks; "
                     f"the answer{show(sel['display_answer'])} passed {', '.join(sel_types)}.")
    else:
        statement = f"In {domain} task {subject}, the answer{show(sel['display_answer'])} passed {', '.join(sel_types)}."
    by_prop = {p["id"]: p for p in result.get("proposals", [])}
    correct = sorted({by_prop[p]["origin"] for p in sel["proposals"] if by_prop[p]["source"] == "provider"})
    incorrect = sorted({by_prop[p]["origin"] for r in rejected for p in opts[r["option_id"]]["proposals"] if by_prop[p]["source"] == "provider"})
    body = {
        "schema_version": "1.0",
        "status": "candidate",
        "kind": "verified_outcome",
        "statement": statement,
        "subject_key": f"{domain}:{label}",
        "verified_answer": None if private else sel["normalized_answer"],
        "domain": domain,
        "tags": list(t.get("tags", [])),
        "source": {"run_id": result["run_id"], "task_id": t.get("id"), "task_digest": result["task_digest"],
                   "verdict_status": v["status"], "privacy_level": level},
        "evidence": {
            "checks": [{"check_id": c["check_id"], "type": c["type"], "status": c["status"], "ran": c["ran"],
                        "decisive": c["decisive"], "can_select": c["can_select"], "target_option": c["target_option"]} for c in checks],
            "rejected_answers": [] if private else [r["answer"][:200] for r in rejected],
            "providers_correct": correct,
            "providers_incorrect": incorrect,
        },
        "created_at": result["created_at"],
        "acceptance": None,
    }
    body["lesson_id"] = "lesson-" + hashlib.sha256(canonical_json({k: body[k] for k in ("statement", "subject_key", "source")}).encode()).hexdigest()[:16]
    return body


# ---------------------------------------------------------------------------
# store

class LessonStore:
    def __init__(self, memory_dir: Path):
        self.dir = Path(memory_dir)
        self.log = self.dir / "lessons.jsonl"

    def entries(self) -> list[dict]:
        if not self.log.exists():
            return []
        out, prev = [], "genesis"
        for n, line in enumerate(self.log.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            e = json.loads(line)
            expect = sha256_digest({k: v for k, v in e.items() if k != "hash"})
            if e.get("prev_hash") != prev or e.get("hash") != expect:
                raise CouncilError(f"lesson memory integrity check failed at line {n} of {self.log}",
                                   hint="The append-only log was edited by hand. Restore it from backup or move it aside to start a new memory.",
                                   exit_code=EXIT_USAGE)
            prev = e["hash"]
            out.append(e)
        return out

    def state(self) -> dict[str, dict]:
        lessons: dict[str, dict] = {}
        for e in self.entries():
            if e["op"] == "accept":
                lessons[e["lesson_id"]] = {**e["lesson"], "status": "accepted", "memory_version": e["seq"]}
            elif e["op"] == "retire" and e["lesson_id"] in lessons:
                lessons[e["lesson_id"]]["status"] = "retired"
                lessons[e["lesson_id"]]["retired_reason"] = e.get("reason", "")
        return lessons

    def accepted(self) -> list[dict]:
        return [l for l in self.state().values() if l["status"] == "accepted"]

    def version(self) -> int:
        es = self.entries()
        return es[-1]["seq"] if es else 0

    def _append(self, entry: dict) -> dict:
        es = self.entries()
        entry = {"seq": (es[-1]["seq"] + 1) if es else 1, **entry, "at": now_iso(), "prev_hash": es[-1]["hash"] if es else "genesis"}
        entry["hash"] = sha256_digest(entry)
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return entry

    def accept(self, lesson: dict, report: "CheckReport") -> dict:
        stored = {**lesson, "status": "accepted", "acceptance": {"checks": report.to_dict()["checks"], "accepted_at": now_iso()}}
        return self._append({"op": "accept", "lesson_id": lesson["lesson_id"], "lesson": stored})

    def retire(self, lesson_id: str, reason: str) -> dict:
        st = self.state()
        if lesson_id not in st:
            raise CouncilError(f"no lesson {lesson_id} in memory", hint="List lessons with: evidence-council lessons list", exit_code=EXIT_USAGE)
        if st[lesson_id]["status"] == "retired":
            raise CouncilError(f"lesson {lesson_id} is already retired", exit_code=EXIT_USAGE)
        return self._append({"op": "retire", "lesson_id": lesson_id, "reason": reason})

    def relevant(self, domain: str, tags: list[str], limit: int = 5) -> list[dict]:
        tagset = set(tags)
        scored = []
        for l in self.accepted():
            score = (2 if l["domain"] == domain else 0) + len(tagset & set(l.get("tags", [])))
            if score >= 2 and (tagset & set(l.get("tags", [])) or not tagset):
                scored.append((score, l))
        scored.sort(key=lambda x: -x[0])
        return [{"lesson_id": l["lesson_id"], "statement": l["statement"], "memory_version": l["memory_version"]} for _, l in scored[:limit]]


# ---------------------------------------------------------------------------
# acceptance checks

@dataclass
class CheckReport:
    lesson_id: str
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return all(c["passed"] for c in self.checks)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append({"check": name, "passed": bool(passed), "detail": detail})

    def to_dict(self) -> dict:
        return {"lesson_id": self.lesson_id, "accepted": self.accepted, "checks": self.checks,
                "failed": [c["check"] for c in self.checks if not c["passed"]]}


def check_candidate(lesson: dict, store: LessonStore, result: dict | None = None) -> CheckReport:
    rep = CheckReport(lesson.get("lesson_id", "?"))
    problems = validate(lesson, "lesson")
    rep.add("schema_valid", not problems, problems[0] if problems else "matches lesson contract")
    if problems:
        return rep
    rep.add("is_candidate", lesson["status"] == "candidate", f"status={lesson['status']}")
    rep.add("verdict_supported", lesson["source"]["verdict_status"] == "supported",
            f"source verdict status={lesson['source']['verdict_status']} (must be 'supported')")
    passing = [c for c in lesson["evidence"]["checks"] if (c.get("can_select") or c["type"] in SELECTING) and c["ran"] and c["status"] == "pass"]
    rep.add("has_passing_selecting_check", bool(passing),
            f"{len(passing)} selecting check(s) ran and passed" if passing else "no objective selecting check passed; an opinion cannot become a lesson")
    nondet = [c["type"] for c in lesson["evidence"]["checks"] if c["type"] not in DETERMINISTIC_TYPES]
    rep.add("only_deterministic_evidence", not nondet, "all linked checks are deterministic" if not nondet else f"non-deterministic evidence types: {nondet}")
    text = " ".join([lesson["statement"], str(lesson.get("verified_answer") or "")] + lesson["evidence"].get("rejected_answers", []))
    pii = {k: v for k, v in scan_text(text).items() if v}
    rep.add("no_private_data", not pii, "no phone/email/card/secret patterns" if not pii else f"private data patterns found: {pii}")
    rep.add("privacy_level_allows", lesson["source"]["privacy_level"] != "restricted", f"source privacy level {lesson['source']['privacy_level']}")
    inj = [p for p in INJECTION_PATTERNS if re.search(p, lesson["statement"], re.I)]
    rep.add("no_instruction_injection", not inj, "statement contains no instruction-override patterns" if not inj else f"matched {inj}")
    n = len(lesson["statement"])
    rep.add("statement_length", 20 <= n <= 1000, f"{n} chars (20..1000)")
    rep.add("linked_to_run", bool(lesson["source"]["run_id"]) and lesson["source"]["task_digest"].startswith("sha256:"), "run id and task digest present")
    if result is not None:
        ok = result.get("run_id") == lesson["source"]["run_id"] and result.get("task_digest") == lesson["source"]["task_digest"]
        key = lambda c: (c["check_id"], c.get("target_option"), c["status"], bool(c.get("can_select")))
        ids = {key(c) for c in result.get("verification", {}).get("checks", [])}
        claimed = {key(c) for c in lesson["evidence"]["checks"]}
        ok = ok and claimed <= ids and (result.get("verdict") or {}).get("status") == lesson["source"]["verdict_status"]
        rep.add("provenance_matches_result", ok, "lesson's run id, digest and check results match the result document" if ok
                else "lesson does not match the result it claims to come from")
    else:
        rep.add("provenance_matches_result", False, "no result document supplied; pass the review result (or --result) so provenance can be checked")
    state = store.state()
    dup = lesson["lesson_id"] in state or any(l["statement"] == lesson["statement"] and l["status"] == "accepted" for l in state.values())
    rep.add("not_duplicate", not dup, "new lesson" if not dup else "an identical lesson is already in memory")
    conflict = [l["lesson_id"] for l in state.values() if l["status"] == "accepted" and l["subject_key"] == lesson["subject_key"]
                and l.get("verified_answer") != lesson.get("verified_answer")]
    rep.add("no_conflict", not conflict, "no accepted lesson disagrees" if not conflict else
            f"conflicts with accepted {conflict}; retire it first if the new evidence is better (evidence-council lessons retire ID --reason ...)")
    return rep


def load_lesson_and_result(doc: dict, result_doc: dict | None) -> tuple[dict, dict | None]:
    if "lesson_id" in doc:
        return doc, result_doc
    if "run_id" in doc:
        lesson = doc.get("candidate_lesson")
        if not lesson:
            v = (doc.get("verdict") or {}).get("status", "missing")
            raise CouncilError(f"result {doc['run_id']} has no candidate lesson (verdict status: {v})",
                               hint="Lessons are only proposed for 'supported' verdicts with options.create_lesson=true.", exit_code=EXIT_USAGE)
        return lesson, doc
    raise CouncilError("file is neither a lesson nor a review result", hint="Pass the JSON written by `evidence-council review`.", exit_code=EXIT_USAGE)
