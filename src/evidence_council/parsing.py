"""Parse untrusted provider output into a proposal record, and normalise answers for comparison."""

from __future__ import annotations

import json
import re
from typing import Any

from .util import clean_untrusted

FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n(.*?)```", re.S)
NUMBER_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _balanced_objects(text: str):
    """Yield top-level {...} substrings (string-aware)."""
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start : i + 1]


def extract_json_object(text: str, required_key: str = "answer") -> dict | None:
    text = text.strip()
    candidates = [text] + FENCE_RE.findall(text) + list(_balanced_objects(text))
    for c in candidates:
        try:
            obj = json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and required_key in obj:
            return obj
    return None


def _str_list(v: Any, limit: int, max_items: int = 30) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [clean_untrusted(x, limit) for x in v[:max_items] if x not in (None, "")]


def parse_proposal(text: str, pid: str, origin: str, answer_format: str, limit: int = 20000, extract: str | None = None) -> dict[str, Any]:
    """Turn raw provider text into a proposal dict. Never raises; marks parse quality."""
    obj = extract_json_object(text)
    parse = "json"
    if obj is None:
        parse = "text_fallback"
        obj = {"answer": text}
    answer = obj.get("answer", "")
    if not isinstance(answer, str):
        answer = json.dumps(answer, ensure_ascii=False) if isinstance(answer, (dict, list)) else str(answer)
    if answer_format == "code":
        answer = extract_code(answer) if parse == "json" else extract_code(text)
    claims = []
    raw_claims = obj.get("claims", [])
    if isinstance(raw_claims, list):
        for i, c in enumerate(raw_claims[:100]):
            if isinstance(c, str):
                c = {"statement": c}
            if not isinstance(c, dict) or not c.get("statement"):
                continue
            claim = {"id": f"{pid}.c{i + 1}", "statement": clean_untrusted(c["statement"], 2000)}
            if c.get("key"):
                claim["key"] = clean_untrusted(c["key"], 80)
                claim["value"] = c.get("value") if isinstance(c.get("value"), (int, float, bool)) or c.get("value") is None else clean_untrusted(c.get("value"), 500)
            ev = c.get("evidence_ids", [])
            claim["evidence_ids"] = [clean_untrusted(x, 80) for x in ev if isinstance(x, str)][:20] if isinstance(ev, list) else []
            if c.get("quote"):
                claim["quote"] = clean_untrusted(c["quote"], 1000)
            if c.get("timestamp"):
                claim["timestamp"] = clean_untrusted(c["timestamp"], 64)
            claims.append(claim)
    conf = obj.get("confidence")
    conf = float(conf) if isinstance(conf, (int, float)) and 0 <= float(conf) <= 1 else None
    return {
        "id": pid,
        "source": "provider",
        "origin": origin,
        "answer": clean_untrusted(answer, limit),
        "normalized_answer": normalize_answer(answer, answer_format, extract),
        "summary": clean_untrusted(obj.get("summary", ""), 2000),
        "assumptions": _str_list(obj.get("assumptions"), 500),
        "claims": claims,
        "tradeoffs": _str_list(obj.get("tradeoffs"), 500),
        "uncertainties": _str_list(obj.get("uncertainties"), 500),
        "self_confidence": conf,
        "parse": parse,
        "untrusted": True,
    }


def candidate_to_proposal(c: dict[str, Any], answer_format: str, extract: str | None = None) -> dict[str, Any]:
    claims = []
    for i, cl in enumerate(c.get("claims", [])):
        claims.append({"id": cl.get("id") or f"{c['id']}.c{i + 1}", **{k: v for k, v in cl.items() if k != "id"}})
        claims[-1].setdefault("evidence_ids", [])
    answer = extract_code(c["answer"]) if answer_format == "code" else c["answer"]
    return {
        "id": f"cand:{c['id']}",
        "source": "candidate",
        "origin": "input:" + (c.get("label") or c["id"]),
        "answer": answer,
        "normalized_answer": normalize_answer(answer, answer_format, extract),
        "summary": c.get("summary", ""),
        "assumptions": list(c.get("assumptions", [])),
        "claims": claims,
        "tradeoffs": [],
        "uncertainties": [],
        "self_confidence": None,
        "parse": "input",
        "untrusted": True,
    }


def extract_code(text: str) -> str:
    blocks = FENCE_RE.findall(text or "")
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return (text or "").strip() + "\n"


def parse_number(text: str) -> float | None:
    if text is None:
        return None
    s = str(text).strip()
    m = NUMBER_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def format_number(x: float) -> str:
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return repr(round(x, 10))


def apply_extract(answer: str, extract: str | None) -> str:
    """Return the first match of the task author's ``answer_extract`` regex (group 1 if present), else the answer."""
    if not extract or not answer:
        return answer or ""
    m = re.search(extract, answer)
    if not m:
        return answer
    return m.group(1) if m.groups() else m.group(0)


def normalize_answer(answer: str, answer_format: str, extract: str | None = None) -> str:
    """Canonical form used to group proposals into options. Not a correctness judgement."""
    a = apply_extract(answer or "", extract)
    if answer_format == "number":
        n = parse_number(a)
        return format_number(n) if n is not None else "unparseable:" + _norm_text(a)[:80]
    if answer_format == "code":
        lines = [ln.rstrip() for ln in a.strip().splitlines() if ln.strip() and not ln.strip().startswith("#")]
        return "code:" + __import__("hashlib").sha256("\n".join(lines).encode()).hexdigest()[:16]
    if answer_format == "json":
        try:
            return json.dumps(json.loads(a), sort_keys=True, separators=(",", ":"))
        except (json.JSONDecodeError, ValueError):
            return "unparseable:" + _norm_text(a)[:80]
    return _norm_text(a)


def _norm_text(a: str) -> str:
    a = re.sub(r"\s+", " ", a.strip().casefold())
    return a.strip(" .!;:\"'")


def normalize_value(v: Any) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return format_number(float(v))
    n = parse_number(str(v)) if v is not None and re.fullmatch(r"\s*[-+]?[\d,]+(\.\d+)?\s*", str(v)) else None
    if n is not None:
        return format_number(n)
    return _norm_text(str(v)) if v is not None else ""
