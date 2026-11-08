"""Privacy routing: decide, per provider, whether it may receive the task and exactly what it receives.

Rules (also in docs/privacy.md):

1. ``audio`` / ``recording`` evidence content and URIs are never forwarded to any
   provider. Only id, kind, title, timestamp and source_ref travel.
2. Secrets (API keys, tokens, private keys) are redacted for every provider, local included.
3. ``restricted`` level or ``local_only`` → cloud providers receive nothing. No override.
4. ``private`` level (the default) → cloud providers receive nothing unless the
   operator allowlists that provider in config (``privacy.allow_private_to``) or with
   ``--allow-private-to NAME``. The task input alone can never unlock this.
5. For cloud providers, ``transcript`` evidence and items marked ``sensitive`` are
   dropped unless that provider is allowlisted; phone numbers, e-mail addresses and
   card numbers are replaced with placeholders unless allowlisted *and* ``redact: false``.
6. Draft ``candidates`` are never shown to proposers (independence); judges see
   proposals only after the same redaction.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, asdict
from typing import Any

from .util import redact_secrets

NEVER_FORWARD_KINDS = {"audio", "recording"}
SENSITIVE_KINDS = {"transcript"}

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?)?\d{3,4}[\s.\-]?\d{3,4}(?![\w])")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _is_phone(m: str) -> bool:
    digits = re.sub(r"\D", "", m)
    if not 9 <= len(digits) <= 15:
        return False
    # Require a '+' or human-style separators; bare digit runs are too often plain numbers.
    return m.lstrip().startswith("+") or bool(re.search(r"[\s.\-()]", m.strip()))


def scan_text(text: str) -> dict[str, int]:
    counts = {"phone": 0, "email": 0, "card": 0, "secret": 0}
    if not text:
        return counts
    counts["email"] = len(EMAIL_RE.findall(text))
    cards = [m.group(0) for m in CARD_RE.finditer(text) if _luhn_ok(re.sub(r"\D", "", m.group(0)))]
    counts["card"] = len(cards)
    for c in cards:
        text = text.replace(c, " ")
    counts["phone"] = sum(1 for m in PHONE_RE.finditer(text) if _is_phone(m.group(0)))
    counts["secret"] = sum(redact_secrets(text)[1].values())
    return counts


class Redactor:
    """Replaces PII with stable placeholders ([PHONE_1], [EMAIL_2]...) across one payload."""

    def __init__(self, pii: bool):
        self.pii = pii
        self.maps: dict[str, dict[str, str]] = {"phone": {}, "email": {}, "card": {}}
        self.counts: dict[str, int] = {}

    def _sub(self, kind: str, value: str) -> str:
        m = self.maps[kind]
        if value not in m:
            m[value] = f"[{kind.upper()}_{len(m) + 1}]"
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return m[value]

    def __call__(self, text: str | None) -> str | None:
        if not text:
            return text
        text, sc = redact_secrets(text)
        for k, v in sc.items():
            self.counts["secret"] = self.counts.get("secret", 0) + v
        if self.pii:
            text = EMAIL_RE.sub(lambda m: self._sub("email", m.group(0)), text)
            text = CARD_RE.sub(lambda m: self._sub("card", m.group(0)) if _luhn_ok(re.sub(r"\D", "", m.group(0))) else m.group(0), text)
            text = PHONE_RE.sub(lambda m: self._sub("phone", m.group(0)) if _is_phone(m.group(0)) else m.group(0), text)
        return text


@dataclass
class Route:
    provider: str
    locality: str
    allowed: bool
    reason: str
    included_evidence: list[str] = field(default_factory=list)
    excluded_evidence: list[dict[str, str]] = field(default_factory=list)
    redactions: dict[str, int] = field(default_factory=dict)
    pii_redaction: bool = True
    allowlisted: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("pii_redaction")
        d.pop("allowlisted")
        return d


def findings(input_doc: dict) -> dict[str, Any]:
    """Summarise private data present in the input (counts only, never values)."""
    total = {"phone": 0, "email": 0, "card": 0, "secret": 0}
    texts = [input_doc["task"].get("prompt", "")] + list(input_doc.get("constraints", []))
    for e in input_doc.get("evidence", []):
        texts += [e.get("content", ""), e.get("title", "")]
    for c in input_doc.get("candidates", []):
        texts += [c.get("answer", ""), c.get("summary", "")] + [cl.get("statement", "") for cl in c.get("claims", [])]
    for t in texts:
        for k, v in scan_text(t or "").items():
            total[k] += v
    return {
        "pii_counts": total,
        "never_forward_evidence": [e["id"] for e in input_doc.get("evidence", []) if e["kind"] in NEVER_FORWARD_KINDS],
        "sensitive_evidence": [e["id"] for e in input_doc.get("evidence", []) if e["kind"] in SENSITIVE_KINDS or e.get("sensitive")],
    }


def plan_route(input_doc: dict, provider: str, locality: str, allowlist: set[str]) -> Route:
    p = input_doc.get("privacy", {})
    level = p.get("level", "private")
    local_only = bool(p.get("local_only"))
    redact_flag = bool(p.get("redact", True))
    evidence = input_doc.get("evidence", [])
    allowlisted = provider in allowlist or any(provider.startswith(a + ":") for a in allowlist)

    if locality == "local":
        r = Route(provider, locality, True, "local provider: data stays on this machine", pii_redaction=False, allowlisted=allowlisted)
        for e in evidence:
            if e["kind"] in NEVER_FORWARD_KINDS:
                r.excluded_evidence.append({"id": e["id"], "reason": f"{e['kind']} content is never forwarded (metadata only)"})
            else:
                r.included_evidence.append(e["id"])
        return r

    if local_only:
        return Route(provider, locality, False, "privacy.local_only is true: cloud providers excluded")
    if level == "restricted":
        return Route(provider, locality, False, "privacy.level=restricted: cloud providers excluded (no override)")
    if level == "private" and not allowlisted:
        return Route(provider, locality, False,
                     "privacy.level=private: cloud provider not allowlisted (operator may allow via config privacy.allow_private_to or --allow-private-to "
                     + provider + ")")
    reason = "allowlisted by operator for private data" if allowlisted and level == "private" else f"privacy.level={level}: cloud allowed with minimisation"
    r = Route(provider, locality, True, reason, pii_redaction=not (allowlisted and not redact_flag), allowlisted=allowlisted)
    for e in evidence:
        if e["kind"] in NEVER_FORWARD_KINDS:
            r.excluded_evidence.append({"id": e["id"], "reason": f"{e['kind']} content is never forwarded (metadata only)"})
        elif (e["kind"] in SENSITIVE_KINDS or e.get("sensitive")) and not allowlisted:
            r.excluded_evidence.append({"id": e["id"], "reason": "sensitive/transcript evidence withheld from cloud provider (not allowlisted)"})
        else:
            r.included_evidence.append(e["id"])
    return r


def build_payload(input_doc: dict, route: Route) -> dict[str, Any]:
    """The minimised view of the task that one provider may see. Fills route.redactions."""
    red = Redactor(pii=route.pii_redaction)
    t = input_doc["task"]
    payload: dict[str, Any] = {
        "task": {
            "title": red(t.get("title", "")),
            "prompt": red(t["prompt"]),
            "domain": t.get("domain", "general"),
            "kind": t.get("kind", "objective"),
            "answer_format": t.get("answer_format", "text"),
            "choices": [red(c) for c in t.get("choices", [])],
        },
        "constraints": [red(c) for c in input_doc.get("constraints", [])],
        "rubric": copy.deepcopy(input_doc.get("rubric", [])),
        "evidence": [],
        "withheld_evidence": [],
    }
    included = set(route.included_evidence)
    for e in input_doc.get("evidence", []):
        meta = {k: e[k] for k in ("id", "kind", "title", "timestamp", "source_ref", "recorded_at") if k in e}
        for k in ("title", "source_ref"):
            if k in meta:
                meta[k] = red(meta[k])
        if e["id"] in included:
            item = dict(meta)
            if "content" in e:
                item["content"] = red(e["content"])
            if "facts" in e:
                item["facts"] = [{**f, "value": red(f["value"]) if isinstance(f["value"], str) else f["value"]} for f in e["facts"]]
            payload["evidence"].append(item)
        elif e["kind"] in NEVER_FORWARD_KINDS:
            payload["withheld_evidence"].append(meta)  # metadata only, so references stay resolvable
    route.redactions = dict(red.counts)
    return payload


def redact_for(route: Route, text: str) -> str:
    """Redact arbitrary text (e.g. other proposals shown to a judge) for this route."""
    return Redactor(pii=route.pii_redaction)(text) or ""
