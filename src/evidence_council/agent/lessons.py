"""Agent lessons: kept only when a verified outcome supports them.

A lesson is derived *deterministically* from the evidence report (never written by a
model), and only from:

* a requirement whose automated check failed on the untouched project and passed after
  the change (a verified fix), or
* an optimizer change that was kept because the measured metric improved and every
  approved check still passed.

Each candidate passes acceptance checks (evidence present, status verified, no secrets
or personal data, no instruction-like text) before it is appended to the hash-chained
store at ``<home>/memory/agent/lessons.jsonl``. AI judgments and untested claims never
become lessons. Nothing is written to CLAUDE.md, AGENTS.md or any agent instructions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..lessons import INJECTION_PATTERNS, CheckReport, LessonStore
from ..privacy import scan_text
from ..util import sha256_digest


def candidates(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    project = Path(report["project"]).name
    for req in report["requirements"]:
        if req["status"] != "verified":
            continue
        fixed = [ch for ch in req["checks"] if ch["basis"] == "verified" and ch["status"] == "passed" and ch.get("baseline_status") == "failed"]
        if not fixed:
            continue
        what = req["requirement"].split(". ")[0].rstrip(".")[:200]
        statement = (f"In project '{project}', requirement {req['id']} ({what}) was met by changing "
                     f"{', '.join(report.get('changed_paths', [])[:5]) or 'the project'}; {len(fixed)} approved check(s) failed before "
                     f"the change and passed after it.")
        out.append({"kind": "verified_fix", "statement": statement, "requirement_id": req["id"],
                    "evidence": {"checks": [ch["description"] for ch in fixed], "baseline_status": "failed", "final_status": "passed"}})
    opt = report.get("optimizer") or {}
    for att in opt.get("attempts", []):
        if att.get("kept"):
            statement = (f"In project '{project}', the change to {', '.join(att.get('files_written', [])[:5])} moved "
                         f"{opt['metric_name']} from {att['before']:.4g} to {att['after']:.4g} {opt.get('unit', '')} "
                         f"(median of {opt['runs']} runs, {att['improvement_pct']:.1f}% better) with every approved check passing.")
            out.append({"kind": "verified_improvement", "statement": statement.replace("  ", " "),
                        "evidence": {"before": att["before"], "after": att["after"], "improvement_pct": att["improvement_pct"]}})
    for c in out:
        c["domain"] = "coding"
        c["tags"] = ["agent", project]
        c["subject_key"] = f"agent:{project}:{c.get('requirement_id', 'optimizer')}"
        c["source_run"] = report["run_id"]
        c["lesson_id"] = "lesson-" + sha256_digest({"s": c["statement"], "run": report["run_id"]})[7:23]
    return out


def check(c: dict[str, Any], report: dict[str, Any]) -> CheckReport:
    rep = CheckReport(c["lesson_id"])
    rep.add("from_this_report", c["source_run"] == report["run_id"], "lesson derived from this run's evidence report")
    rep.add("run_completed", report["outcome"] == "completed", f"run outcome: {report['outcome']}")
    if c["kind"] == "verified_fix":
        req = next((r for r in report["requirements"] if r["id"] == c["requirement_id"]), None)
        rep.add("requirement_verified", bool(req and req["status"] == "verified"), f"requirement status: {req and req['status']}")
    else:
        rep.add("improvement_measured", c["evidence"]["improvement_pct"] > 0, f"{c['evidence']['improvement_pct']:.1f}% measured")
    rep.add("no_regressions", not report.get("regressions"), f"{len(report.get('regressions', []))} regression(s) in final evidence")
    found = {k: v for k, v in scan_text(c["statement"]).items() if v}
    rep.add("no_private_data", not found, "no secrets/phone numbers/e-mails in the statement" if not found else f"found {sorted(found)}")
    bad = [p for p in INJECTION_PATTERNS if re.search(p, c["statement"], re.I)]
    rep.add("no_instructions", not bad, "statement contains no instruction-like text" if not bad else f"matched {bad}")
    return rep


def retain(report: dict[str, Any], memory_dir: Path) -> dict[str, list[dict[str, Any]]]:
    store = LessonStore(memory_dir / "agent")
    kept, rejected = [], []
    existing = set(store.state())
    for c in candidates(report):
        rep = check(c, report)
        if not rep.accepted:
            rejected.append({"statement": c["statement"], "failed": rep.to_dict()["failed"]})
            continue
        if c["lesson_id"] in existing:
            continue
        lesson = {k: c[k] for k in ("lesson_id", "kind", "statement", "domain", "tags", "subject_key", "source_run", "evidence")}
        e = store.accept(lesson, rep)
        kept.append({"lesson_id": c["lesson_id"], "statement": c["statement"], "memory_version": e["seq"]})
    return {"retained": kept, "rejected": rejected, "memory": str(store.log)}


def relevant(memory_dir: Path, project: str, limit: int = 5) -> list[str]:
    store = LessonStore(memory_dir / "agent")
    try:
        return [l["statement"] for l in store.accepted() if project in l.get("tags", [])][-limit:]
    except Exception:
        return []
