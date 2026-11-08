"""Deterministic, plain-language reading of a council result.

``reading(result)`` returns a small JSON object with the facts a caller must relay
and the claims it must *not* make; ``render_markdown(result)`` turns it into text.
Claude's skill uses this so its summary cannot drift from the result file.
"""

from __future__ import annotations

from typing import Any

HEADLINES = {
    "supported": "Evidence supports one answer.",
    "multiple_supported": "Several different answers passed every objective check.",
    "insufficient_evidence": "Insufficient evidence: no answer could be verified.",
    "all_rejected": "Every proposed answer failed objective checks.",
    "subjective": "Subjective task: no objective winner; competing judgments shown.",
    "no_proposals": "No usable proposals were produced.",
}


def reading(result: dict) -> dict[str, Any]:
    v = result.get("verdict") or {}
    status = v.get("status", "incomplete")
    attempted = result.get("providers", {}).get("attempted", [])
    adapters = {a["provider"]: a.get("adapter") for a in attempted}
    used = result.get("providers", {}).get("used", [])
    participants = [f"{p} (MOCK)" if adapters.get(p) == "mock" else p for p in used]
    failed = [f"{a['provider']}: {a['status']}" + (f" - {a['error'][:120]}" if a.get("error") else "")
              for a in attempted if a["status"] != "ok"]
    checks = (result.get("verification") or {}).get("checks", [])
    ran = [c for c in checks if c["ran"]]
    run_summary = sorted({f"{c['check_id']} ({c['type']})" for c in ran})
    not_run = sorted({f"{c['check_id']}: {c['detail'][:120]}" for c in checks if c["status"] == "not_run"})
    must_not = ["Do not describe any provider not listed in 'participants' as having participated."]
    if status in ("insufficient_evidence", "all_rejected", "no_proposals", "subjective"):
        must_not.append("Do not present any answer as verified or correct.")
    if status == "subjective":
        must_not.append("Do not declare an objective winner; report the rubric and the judges' scores as opinions.")
    if any(adapters.get(p) == "mock" for p in used):
        must_not.append("Mock providers are test fixtures: do not describe this as a real multi-model run.")
    if v.get("vote_note"):
        must_not.append("Do not describe the result as a vote; agreement was not used to select.")
    r = {
        "status": status,
        "headline": HEADLINES.get(status, "Result is incomplete (verdict stage not run)."),
        "answer": v.get("selected_answer") if status in ("supported", "multiple_supported") else None,
        "can_act_on_answer": status == "supported",
        "overturned_popular": bool(v.get("overturned_popular")),
        "participants": participants,
        "failed_or_skipped_providers": failed,
        "excluded_providers": [f"{e['provider']}: {e['reason']}" for e in result.get("providers", {}).get("excluded", [])],
        "checks_run": run_summary,
        "checks_not_run": not_run,
        "check_counts": (result.get("verification") or {}).get("summary"),
        "why": v.get("rationale", []),
        "rejected": [f"option {r['option_id']} ({r['answer'][:80]}): " + "; ".join(r["reasons"])[:400] for r in v.get("rejected", [])],
        "unverified": v.get("unverified", [])[:10],
        "uncertainty": result.get("uncertainty"),
        "candidate_lesson": (result.get("candidate_lesson") or {}).get("lesson_id"),
        "must_not_claim": must_not,
        "warnings": result.get("warnings", []),
    }
    return r


def render_markdown(result: dict) -> str:
    r = reading(result)
    out = [f"## Council verdict: {r['status']}", "", r["headline"]]
    if r["answer"] is not None:
        out += ["", f"**Answer:** {r['answer']}"]
    if r["overturned_popular"]:
        out += ["", "**The most popular answer was overturned by objective evidence.**"]
    out += ["", "**Participants (adapters that actually ran):** " + (", ".join(r["participants"]) or "none")]
    if r["failed_or_skipped_providers"]:
        out += ["**Did not participate:** " + "; ".join(r["failed_or_skipped_providers"])]
    if r["excluded_providers"]:
        out += ["**Excluded:** " + "; ".join(r["excluded_providers"][:8])]
    out += ["", "**Checks actually run:** " + (", ".join(r["checks_run"]) or "none")]
    if r["checks_not_run"]:
        out += ["**Checks not run:** " + "; ".join(r["checks_not_run"])]
    if r["why"]:
        out += ["", "**Why:**"] + [f"- {w}" for w in r["why"]]
    if r["rejected"]:
        out += ["", "**Rejected:**"] + [f"- {x}" for x in r["rejected"]]
    if r["unverified"]:
        out += ["", "**Still unverified:**"] + [f"- {x}" for x in r["unverified"]]
    u = r["uncertainty"] or {}
    if u:
        out += ["", f"**Uncertainty:** {u.get('level')} — " + "; ".join(u.get("reasons", []))]
    if r["candidate_lesson"]:
        out += ["", f"Candidate lesson {r['candidate_lesson']} proposed (not accepted; run `evidence-council lessons check` to review)."]
    if r["warnings"]:
        out += ["", "**Warnings:** " + "; ".join(r["warnings"])]
    return "\n".join(out) + "\n"
