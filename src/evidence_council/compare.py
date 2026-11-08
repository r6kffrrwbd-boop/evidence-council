"""Comparison stage: group proposals into answer options and surface disagreements.

Nothing here decides who is right. Agreement counts are recorded only to report
whether the eventual verdict overturned the popular answer.
"""

from __future__ import annotations

import re
import string
from collections import defaultdict
from typing import Any

from .parsing import normalize_value


def build_options(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for p in proposals:
        key = p["normalized_answer"]
        if key not in groups:
            order.append(key)
        groups[key].append(p)
    first_seen = {k: i for i, k in enumerate(order)}
    order.sort(key=lambda k: (-len(groups[k]), first_seen[k]))
    options = []
    for i, key in enumerate(order):
        letters = string.ascii_uppercase
        oid = letters[i] if i < 26 else f"O{i + 1}"
        ps = groups[key]
        display = ps[0]["answer"]
        options.append({
            "option_id": oid,
            "normalized_answer": key,
            "display_answer": display if len(display) <= 400 else display[:380] + " ...[see proposals]",
            "proposals": [p["id"] for p in ps],
            "origins": [p["origin"] for p in ps],
            "support_count": len(ps),
        })
    return options


def _norm_sentence(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", s.casefold())).strip()


def find_disagreements(proposals: list[dict[str, Any]], options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_id = {p["id"]: p for p in proposals}
    if len(options) > 1:
        out.append({
            "type": "answer",
            "topic": "final answer",
            "positions": [{"option_id": o["option_id"], "answer": o["display_answer"], "held_by": o["origins"]} for o in options],
        })
    # keyed claims: same key, different values
    keyed: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for p in proposals:
        for c in p.get("claims", []):
            if c.get("key"):
                keyed[c["key"]][normalize_value(c.get("value"))].append(p["origin"])
    for key, vals in sorted(keyed.items()):
        if len(vals) > 1:
            out.append({
                "type": "claim",
                "topic": f"value of '{key}'",
                "positions": [{"value": v, "held_by": hs} for v, hs in vals.items()],
            })
    # assumptions held by some but not all proposals, when answers differ
    if len(proposals) > 1:
        holders: dict[str, list[str]] = defaultdict(list)
        text_of: dict[str, str] = {}
        for p in proposals:
            for a in p.get("assumptions", []):
                k = _norm_sentence(a)
                if k and p["origin"] not in holders[k]:
                    holders[k].append(p["origin"])
                    text_of.setdefault(k, a)
        all_origins = [p["origin"] for p in proposals]
        for k, hs in holders.items():
            if len(hs) < len(all_origins):
                out.append({
                    "type": "assumption",
                    "topic": text_of[k][:300],
                    "positions": [{"held_by": hs}, {"not_stated_by": [o for o in all_origins if o not in hs]}],
                })
    confs = [(p["origin"], p.get("self_confidence")) for p in proposals if p.get("self_confidence") is not None]
    if len(confs) > 1 and max(c for _, c in confs) - min(c for _, c in confs) >= 0.4:
        out.append({"type": "confidence", "topic": "self-reported confidence spread (not evidence)",
                    "positions": [{"held_by": [o], "confidence": c} for o, c in confs]})
    _ = by_id
    return out


def collect_claims(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flat list of all claims with their proposal id; verification fills status later."""
    out = []
    for p in proposals:
        for c in p.get("claims", []):
            out.append({"claim_id": c["id"], "proposal_id": p["id"], "origin": p["origin"], "statement": c["statement"],
                        "key": c.get("key"), "value": c.get("value"), "evidence_ids": c.get("evidence_ids", []),
                        "status": "unchecked"})
    return out
