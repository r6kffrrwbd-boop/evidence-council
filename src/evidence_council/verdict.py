"""Verdict stage: choose the best-supported option from check results only.

Selection rules (documented in docs/contract.md#verdict):

1. An option is **rejected** if any answer-level check (selecting or format) fails.
2. An option is **supported** if it is not rejected and at least one *selecting*
   check passed for it.
3. Exactly one supported option → ``supported``. Several → ``multiple_supported``
   (best ranked by fewer contradicted claims, then more passes; the tie-break is
   reported as non-decisive). None supported but some unrejected → ``insufficient_evidence``.
   Everything rejected → ``all_rejected``. Subjective task → ``subjective`` (no winner).
4. Support counts (how many proposals agreed) are never used to select; they are
   reported so the caller can see when the evidence overturned the popular answer.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _popular(options: list[dict]) -> str | None:
    if not options:
        return None
    if len(options) == 1:
        return options[0]["option_id"] if options[0]["support_count"] >= 2 else None
    a, b = options[0], options[1]
    return a["option_id"] if a["support_count"] > b["support_count"] else None


def summarise_judgments(judgments: list[dict], rubric: list[dict]) -> list[dict]:
    weights = {r["criterion"]: float(r.get("weight", 1)) for r in rubric}
    per: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for j in judgments:
        per[j["proposal_id"]][j["judge"]].append(float(j["score"]) * weights.get(j["criterion"], 1.0))
    total_w = sum(weights.values()) or 1.0
    out = []
    for pid, by_judge in per.items():
        means = {judge: sum(s) / total_w for judge, s in by_judge.items()}
        vals = list(means.values())
        out.append({"proposal_id": pid, "weighted_score_by_judge": {k: round(v, 2) for k, v in means.items()},
                    "mean": round(sum(vals) / len(vals), 2), "spread": round(max(vals) - min(vals), 2), "judges": len(vals)})
    out.sort(key=lambda x: -x["mean"])
    return out


def decide(input_doc: dict, proposals: list[dict], options: list[dict], verification: dict,
           claims: list[dict], judgments: list[dict] | None = None, providers_used: int = 0) -> tuple[dict, dict]:
    checks = verification.get("checks", [])
    kind = input_doc["task"].get("kind", "objective")
    popular = _popular(options)
    vote_note = "Support counts are reported for transparency only; they were not used to select an answer."
    base = {"selected_option": None, "selected_answer": None, "selected_proposals": [], "also_supported": [],
            "rationale": [], "rejected": [], "unverified": [], "popular_option": popular,
            "overturned_popular": False, "vote_note": vote_note}

    if not options:
        v = {**base, "status": "no_proposals", "rationale": ["No usable proposals were produced (all providers failed or were excluded, and no candidates were supplied)."]}
        return v, {"level": "high", "reasons": ["no proposals to evaluate"]}

    by_claim_status: dict[str, list[dict]] = defaultdict(list)
    for c in claims:
        by_claim_status[c["proposal_id"]].append(c)

    info = {}
    for o in options:
        res = [r for r in checks if r.get("target_option") == o["option_id"]]
        fails = [r for r in res if r["status"] == "fail"]
        sel_pass = [r for r in res if r["status"] == "pass" and r.get("can_select")]
        contradicted = sum(1 for pid in o["proposals"] for c in by_claim_status[pid] if c["status"] == "contradicted")
        claim_fail = [r for r in checks if r.get("target_proposal") in o["proposals"] and r["status"] == "fail"]
        info[o["option_id"]] = {"fails": fails, "sel_pass": sel_pass, "contradicted": contradicted,
                                "claim_fail": claim_fail, "passes": sum(1 for r in res if r["status"] == "pass")}

    rejected = []
    for o in options:
        i = info[o["option_id"]]
        if i["fails"]:
            rejected.append({"option_id": o["option_id"], "answer": o["display_answer"], "held_by": o["origins"],
                             "support_count": o["support_count"],
                             "reasons": [f"{r['check_id']} ({r['type']}) failed: {r['detail']}" for r in i["fails"]]})
    rejected_ids = {r["option_id"] for r in rejected}
    survivors = [o for o in options if o["option_id"] not in rejected_ids]
    supported = [o for o in survivors if info[o["option_id"]]["sel_pass"]]

    if kind == "subjective":
        summary = summarise_judgments(judgments or [], input_doc.get("rubric", []))
        rationale = ["Subjective task: no objective winner is declared. The rubric and each judge's scores are exposed instead."]
        if summary:
            for s in summary:
                detail = f"{s['judges']} judges, spread {s['spread']}" if s["judges"] > 1 else "1 judge only"
                rationale.append(f"{s['proposal_id']}: mean weighted rubric score {s['mean']} ({detail})")
        else:
            rationale.append("No independent judge was available, so proposals are listed without scores.")
        if rejected:
            rationale.append(f"{len(rejected)} option(s) failed format checks and are listed as rejected.")
        v = {**base, "status": "subjective", "rejected": rejected, "rationale": rationale,
             "unverified": ["Rubric scores are model judgments, not verification."], "judgment_summary": summary}
        return v, {"level": "medium", "reasons": ["subjective task: outcome depends on rubric judgments, which are opinions"]}

    unverified: list[str] = []
    reasons: list[str] = []
    if supported:
        ranked = sorted(supported, key=lambda o: (info[o["option_id"]]["contradicted"], -info[o["option_id"]]["passes"]))
        best = ranked[0]
        bi = info[best["option_id"]]
        status = "supported" if len(supported) == 1 else "multiple_supported"
        rationale = [f"Option {best['option_id']} passed selecting check(s): " +
                     "; ".join(f"{r['check_id']} ({r['type']}): {r['detail']}" for r in bi["sel_pass"])]
        if status == "multiple_supported":
            rationale.append("Other options also passed every selecting check: " + ", ".join(o["option_id"] for o in ranked[1:]) +
                             ". The ranking among them (fewer contradicted claims, then more passing checks) is not decisive evidence.")
        if bi["contradicted"]:
            rationale.append(f"Note: {bi['contradicted']} claim(s) in the selected option's reasoning are contradicted by evidence even though the answer passed.")
        for pid in best["proposals"]:
            for c in by_claim_status[pid]:
                if c["status"] in ("unverified", "citation_ok_content_unverified"):
                    unverified.append(f"claim {c['claim_id']}: {c['statement'][:200]}")
        v = {**base, "status": status, "selected_option": best["option_id"], "selected_answer": best["display_answer"],
             "selected_proposals": best["proposals"], "also_supported": [o["option_id"] for o in ranked[1:]],
             "rationale": rationale, "rejected": rejected, "unverified": unverified[:50]}
        v["overturned_popular"] = bool(popular and popular != best["option_id"] and popular in rejected_ids)
        if v["overturned_popular"]:
            pop = next(o for o in options if o["option_id"] == popular)
            v["rationale"].append(f"The most popular answer (option {popular}, {pop['support_count']} of {len(proposals)} proposals) was rejected by objective checks; agreement did not outweigh evidence.")
        not_run = [r for r in checks if r["status"] == "not_run"]
        errored = [r for r in checks if r["status"] == "error"]
        if len(bi["sel_pass"]) == 1:
            reasons.append("selection rests on a single selecting check")
        if not_run:
            reasons.append(f"{len(not_run)} check(s) could not run")
        if errored:
            reasons.append(f"{len(errored)} check(s) errored")
        if providers_used < 2 and any(p["source"] == "provider" for p in proposals):
            reasons.append(f"only {providers_used} provider(s) participated")
        if not any(p["source"] == "provider" for p in proposals):
            reasons.append("candidate-only review: drafts were checked against evidence; no independent proposals were requested")
        if any(p["parse"] == "text_fallback" for p in proposals if p["id"] in best["proposals"]):
            reasons.append("selected proposal did not return structured JSON")
        if status == "multiple_supported":
            reasons.append("several different answers passed the checks")
        level = "low" if not reasons else "medium"
        return v, {"level": level, "reasons": reasons or ["selected answer passed objective checks; no failed, errored or skipped checks"]}

    if not survivors:
        v = {**base, "status": "all_rejected", "rejected": rejected,
             "rationale": ["Every option failed at least one objective check; none can be recommended."],
             "unverified": []}
        return v, {"level": "high", "reasons": ["all options were contradicted by checks"]}

    rationale = ["No selecting check passed for any remaining option, so the council cannot pick an answer on evidence."]
    if verification.get("note"):
        rationale.append(verification["note"] + ".")
    errored = [r for r in checks if r["status"] == "error" and r.get("target_option")]
    if errored:
        rationale.append("Errored checks: " + "; ".join(f"{r['check_id']}: {r['detail']}" for r in errored[:5]))
    if len(survivors) == 1 and survivors[0]["support_count"] > 1:
        rationale.append(f"Providers agree on option {survivors[0]['option_id']}, but agreement is not verification.")
    clean = [o for o in survivors if info[o["option_id"]]["contradicted"] == 0 and not info[o["option_id"]]["claim_fail"]]
    if 0 < len(clean) < len(survivors):
        rationale.append("Claim-level evidence contradicts reasoning in: " +
                         ", ".join(o["option_id"] for o in survivors if o not in clean) + " (not decisive).")
    unverified = [f"option {o['option_id']}: {o['display_answer'][:200]}" for o in survivors]
    v = {**base, "status": "insufficient_evidence", "rejected": rejected, "rationale": rationale, "unverified": unverified,
         "unrejected_options": [o["option_id"] for o in survivors]}
    return v, {"level": "high", "reasons": ["no objective check selected an answer"]}
