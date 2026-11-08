"""Premise labelling: separate what the task author has observed from what is only assumed.

Providers receive a description, never the system itself, so they reason correctly from
whatever the task asserts — and agree with each other while doing it. A false premise
therefore produces confident unanimity. Each input premise is declared ``observed``
(with how it was confirmed) or ``not_verified`` (with what would confirm it), and is sent
to providers labelled ``OBSERVED:`` / ``NOT VERIFIED:``.

Where a premise names an evidence fact (``fact_key`` + ``value``) it is also checked here,
deterministically:

* ``confirmed_by_evidence``  — the evidence fact matches;
* ``contradicted_by_evidence`` — it does not. The task's own framing is wrong: the verdict
  says so, uncertainty becomes high, and no candidate lesson is proposed.

Premises that stay ``not_verified`` are listed in ``verdict.unverified``.
"""

from __future__ import annotations

from typing import Any

from .parsing import normalize_value

CONFIRMED = "confirmed_by_evidence"
CONTRADICTED = "contradicted_by_evidence"


def check_premises(input_doc: dict) -> list[dict[str, Any]]:
    facts: dict[str, list[tuple[str, Any]]] = {}
    for e in input_doc.get("evidence", []):
        for f in e.get("facts", []):
            facts.setdefault(f["key"], []).append((e["id"], f["value"]))
    out = []
    for i, p in enumerate(input_doc.get("premises", []), 1):
        rec = {"id": p.get("id") or f"premise{i}", "statement": p["statement"], "declared": p["status"],
               "status": p["status"], "basis": p.get("basis", ""), "detail": ""}
        if p.get("fact_key"):
            vals = facts.get(p["fact_key"], [])
            if not vals:
                rec["detail"] = f"no evidence fact '{p['fact_key']}' to check against"
            else:
                refs = {normalize_value(v) for _, v in vals}
                where = ", ".join(f"{eid}={v!r}" for eid, v in vals)
                if len(refs) == 1 and normalize_value(p.get("value")) in refs:
                    rec["status"], rec["detail"] = CONFIRMED, f"matches evidence {where}"
                elif normalize_value(p.get("value")) in refs:
                    rec["detail"] = f"evidence disagrees with itself ({where}); premise left as declared"
                else:
                    rec["status"], rec["detail"] = CONTRADICTED, f"premise says {p['fact_key']}={p.get('value')!r} but evidence {where}"
        out.append(rec)
    return out


def apply_to_verdict(premises: list[dict], verdict: dict, uncertainty: dict) -> None:
    """Fold premise results into the verdict and uncertainty (in place)."""
    contradicted = [p for p in premises if p["status"] == CONTRADICTED]
    unverified = [p for p in premises if p["status"] == "not_verified"]
    for p in contradicted:
        verdict["rationale"].insert(0, f"Premise {p['id']} is contradicted by the evidence ({p['detail']}). "
                                       "The task's own framing is wrong; correct it before acting on any answer.")
    for p in unverified:
        verdict.setdefault("unverified", []).insert(0, f"premise {p['id']} (NOT VERIFIED): {p['statement'][:200]}"
                                                    + (f" — would be confirmed by: {p['basis']}" if p["basis"] else ""))
    if contradicted:
        uncertainty["level"] = "high"
        uncertainty["reasons"].insert(0, f"{len(contradicted)} task premise(s) contradicted by evidence")
    elif unverified:
        if uncertainty["level"] == "low":
            uncertainty["level"] = "medium"
        uncertainty["reasons"].append(f"answer may rest on {len(unverified)} unverified premise(s)")
