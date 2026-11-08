"""Verification stage: deterministic checks that are independent of every proposer.

No model is consulted here. A council vote is never a check.

Check classes
-------------
* **selecting** (``numeric_reference``, ``reference_value``, ``fact_answer``,
  ``python_tests``, ``command``): answer-level; a pass can select an option, a fail rejects it.
* **format** (``regex``, ``json_schema``): answer-level; a fail rejects, a pass cannot select.
* **claim-level** (``evidence_facts``, ``citations``): check individual claims against the
  supplied evidence; failures are reported against the proposal and mark the claim
  contradicted or its citation fabricated.

When the input has evidence but no explicit claim-level checks, ``evidence_facts`` and
``citations`` are added automatically (``auto: true``).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .parsing import apply_extract, normalize_value, parse_number, format_number
from .safe_eval import UnsafeExpression, safe_eval
from .sandbox import python_argv, run_untrusted

SELECTING = {"numeric_reference", "reference_value", "fact_answer", "python_tests", "command"}
FORMAT = {"regex", "json_schema"}
CLAIM_LEVEL = {"evidence_facts", "citations"}
EXECUTING = {"python_tests", "command"}


def _result(check: dict, status: str, detail: str, *, option: str | None = None, proposal: str | None = None,
            claim: str | None = None, ran: bool = True, t0: float | None = None,
            decisive: bool | None = None, can_select: bool | None = None) -> dict[str, Any]:
    ctype = check["type"]
    return {
        "check_id": check["id"], "type": ctype, "auto": bool(check.get("auto")),
        "decisive": (ctype in SELECTING or ctype in FORMAT) if decisive is None else decisive,
        "can_select": (ctype in SELECTING) if can_select is None else can_select,
        "target_option": option, "target_proposal": proposal, "target_claim": claim,
        "status": status, "ran": ran and status != "not_run", "detail": detail[:1500],
        "duration_s": round(time.monotonic() - t0, 3) if t0 else 0.0,
    }


def effective_checks(input_doc: dict) -> list[dict]:
    checks = [dict(c) for c in input_doc.get("checks", [])]
    types = {c["type"] for c in checks}
    ev = input_doc.get("evidence", [])
    if ev and "evidence_facts" not in types and any(e.get("facts") for e in ev):
        checks.append({"id": "auto.evidence_facts", "type": "evidence_facts", "auto": True})
    if ev and "citations" not in types:
        checks.append({"id": "auto.citations", "type": "citations", "auto": True, "require_citations": False})
    return checks


def _facts_index(evidence: list[dict]) -> dict[str, list[tuple[str, Any]]]:
    idx: dict[str, list[tuple[str, Any]]] = {}
    for e in evidence:
        for f in e.get("facts", []):
            idx.setdefault(f["key"], []).append((e["id"], f["value"]))
    return idx


def _norm_quote(s: str) -> str:
    return re.sub(r"\s+", " ", s.casefold()).strip(" .\"'")


def _values_equal(answer: Any, ref: Any, numeric: bool, case_sensitive: bool = False, tol: float | None = None) -> bool:
    if numeric or isinstance(ref, (int, float)) and not isinstance(ref, bool):
        a, r = parse_number(str(answer)), parse_number(str(ref))
        if a is None or r is None:
            return False
        tol = tol if tol is not None else 1e-9 * max(1.0, abs(r))
        return abs(a - r) <= tol
    if case_sensitive:
        return str(answer).strip() == str(ref).strip()
    return normalize_value(answer) == normalize_value(ref)


def run_answer_check(check: dict, option: dict, answer: str, input_doc: dict) -> dict[str, Any]:
    t0 = time.monotonic()
    ctype = check["type"]
    fmt = input_doc["task"].get("answer_format", "text")
    oid = option["option_id"]
    opts = input_doc.get("options", {})
    if ctype in EXECUTING and not opts.get("allow_code_execution"):
        return _result(check, "not_run", "code execution not permitted: set options.allow_code_execution=true in the input to run this check",
                       option=oid, ran=False)
    try:
        if ctype == "numeric_reference":
            ref = safe_eval(check["expression"])
            got = parse_number(answer)
            if got is None:
                return _result(check, "fail", f"answer is not a number (expected {format_number(float(ref))} = {check['expression']})", option=oid, t0=t0)
            ok = _values_equal(got, ref, True, tol=check.get("tolerance"))
            return _result(check, "pass" if ok else "fail",
                           f"computed {check['expression']} = {format_number(float(ref))}; answer {format_number(got)}", option=oid, t0=t0)
        core = apply_extract(answer, input_doc["task"].get("answer_extract"))
        if ctype == "reference_value":
            answer = core
            ok = _values_equal(answer, check["value"], fmt == "number", bool(check.get("case_sensitive")), check.get("tolerance"))
            return _result(check, "pass" if ok else "fail", f"reference value {check['value']!r}; answer {answer[:200]!r}", option=oid, t0=t0)
        if ctype == "fact_answer":
            vals = _facts_index(input_doc.get("evidence", [])).get(check["fact_key"], [])
            if not vals:
                return _result(check, "error", f"no evidence fact with key '{check['fact_key']}'", option=oid, t0=t0)
            distinct = {normalize_value(v) for _, v in vals}
            if len(distinct) > 1:
                return _result(check, "error", f"evidence is self-contradictory for '{check['fact_key']}': " +
                               ", ".join(f"{eid}={v}" for eid, v in vals), option=oid, t0=t0)
            eid, ref = vals[0]
            answer = core
            ok = _values_equal(answer, ref, fmt == "number", bool(check.get("case_sensitive")), check.get("tolerance"))
            return _result(check, "pass" if ok else "fail", f"evidence {eid} records {check['fact_key']}={ref!r}; answer {answer[:200]!r}", option=oid, t0=t0)
        if ctype == "regex":
            ok = re.search(check["pattern"], answer) is not None
            return _result(check, "pass" if ok else "fail", f"pattern {check['pattern']!r} {'matched' if ok else 'did not match'}", option=oid, t0=t0)
        if ctype == "json_schema":
            from jsonschema import Draft202012Validator
            try:
                doc = json.loads(answer)
            except (json.JSONDecodeError, ValueError) as e:
                return _result(check, "fail", f"answer is not JSON: {e}", option=oid, t0=t0)
            errs = [e.message for e in Draft202012Validator(check["schema"]).iter_errors(doc)]
            return _result(check, "fail" if errs else "pass", "; ".join(errs[:3]) or "valid against schema", option=oid, t0=t0)
        if ctype == "python_tests":
            mod = check.get("module_name", "solution")
            r = run_untrusted(python_argv("test_ec_check.py"), {f"{mod}.py": answer, "test_ec_check.py": check["test_code"]},
                              None, float(check.get("timeout_s", 30)))
            box = "sandboxed" if r.sandboxed else "NOT sandboxed (no sandbox-exec)"
            if r.timed_out:
                return _result(check, "fail", f"tests timed out ({box})", option=oid, t0=t0)
            tail = (r.stderr.strip() or r.stdout.strip()).splitlines()[-4:]
            return _result(check, "pass" if r.returncode == 0 else "fail",
                           f"exit {r.returncode} ({box}); " + " | ".join(tail)[:900], option=oid, t0=t0)
        if ctype == "command":
            r = run_untrusted(list(check["argv"]), {}, answer, float(check.get("timeout_s", 30)))
            box = "sandboxed" if r.sandboxed else "NOT sandboxed"
            if r.timed_out:
                return _result(check, "fail", f"command timed out ({box})", option=oid, t0=t0)
            tail = (r.stderr.strip() or r.stdout.strip()).splitlines()[-3:]
            return _result(check, "pass" if r.returncode == 0 else "fail", f"exit {r.returncode} ({box}); " + " | ".join(tail)[:600], option=oid, t0=t0)
    except UnsafeExpression as e:
        return _result(check, "error", f"reference expression rejected: {e}", option=oid, t0=t0)
    except re.error as e:
        return _result(check, "error", f"invalid regex: {e}", option=oid, t0=t0)
    except Exception as e:  # a check must never crash the council
        return _result(check, "error", f"{type(e).__name__}: {e}", option=oid, t0=t0)
    return _result(check, "error", f"unknown check type {ctype}", option=oid, ran=False)


def run_claim_checks(check: dict, proposal: dict, input_doc: dict) -> list[dict[str, Any]]:
    out = []
    evidence = {e["id"]: e for e in input_doc.get("evidence", [])}
    facts = _facts_index(input_doc.get("evidence", []))
    pid = proposal["id"]
    for c in proposal.get("claims", []):
        t0 = time.monotonic()
        cid = c["id"]
        if check["type"] == "evidence_facts":
            if not c.get("key"):
                continue
            vals = facts.get(c["key"], [])
            cited = set(c.get("evidence_ids") or [])
            if cited:
                vals = [v for v in vals if v[0] in cited] or vals
            if not vals:
                out.append(_result(check, "not_run", f"no evidence fact for key '{c['key']}'", proposal=pid, claim=cid, ran=False))
                continue
            refs = {normalize_value(v) for _, v in vals}
            claimed = normalize_value(c.get("value"))
            if claimed in refs:
                out.append(_result(check, "pass", f"{c['key']}={c.get('value')!r} matches evidence {', '.join(e for e, _ in vals)}", proposal=pid, claim=cid, t0=t0))
            else:
                out.append(_result(check, "fail", f"claimed {c['key']}={c.get('value')!r} but evidence " +
                                   ", ".join(f"{e} records {v!r}" for e, v in vals), proposal=pid, claim=cid, t0=t0))
        elif check["type"] == "citations":
            ids = c.get("evidence_ids") or []
            if not ids:
                if check.get("require_citations"):
                    out.append(_result(check, "fail", "claim cites no evidence (citations required)", proposal=pid, claim=cid, t0=t0))
                continue
            missing = [i for i in ids if i not in evidence]
            if missing:
                out.append(_result(check, "fail", f"cites evidence that does not exist: {', '.join(missing)}", proposal=pid, claim=cid, t0=t0))
                continue
            problems, notes = [], []
            if c.get("quote"):
                withheld = [i for i in ids if evidence[i]["kind"] in ("audio", "recording") or not evidence[i].get("content")]
                found = any(_norm_quote(c["quote"]) in _norm_quote(evidence[i].get("content", "")) for i in ids)
                if not found and len(withheld) == len(ids):
                    out.append(_result(check, "not_run", "quoted evidence has no inspectable text content", proposal=pid, claim=cid, ran=False))
                    continue
                if not found:
                    problems.append("quote not found in cited evidence")
                else:
                    notes.append("quote found")
            if c.get("timestamp"):
                stamps = {evidence[i].get("timestamp") for i in ids if evidence[i].get("timestamp")}
                if stamps and c["timestamp"] not in stamps:
                    problems.append(f"timestamp {c['timestamp']} does not match cited evidence ({', '.join(sorted(stamps))})")
                elif stamps:
                    notes.append("timestamp matches")
            if problems:
                out.append(_result(check, "fail", "; ".join(problems), proposal=pid, claim=cid, t0=t0))
            else:
                out.append(_result(check, "pass", "cited evidence exists" + ("; " + "; ".join(notes) if notes else ""), proposal=pid, claim=cid, t0=t0))
    return out


def _aggregate_claims(check: dict, options: list[dict], claim_results: list[dict]) -> list[dict]:
    """Opt-in (``decisive: true``): lift claim-level results to the option level.

    evidence_facts: option passes when >=1 keyed claim matched evidence and none was contradicted
    (can select); fails when any claim was contradicted. citations: fails when any citation is
    fabricated or mismatched (rejects only, never selects).
    """
    out = []
    for o in options:
        rs = [r for r in claim_results if r["target_proposal"] in o["proposals"]]
        fails = [r for r in rs if r["status"] == "fail"]
        passes = [r for r in rs if r["status"] == "pass"]
        sel = check["type"] == "evidence_facts"
        if fails:
            status, detail = "fail", f"{len(fails)} claim(s) failed: " + "; ".join(f"{r['target_claim']}: {r['detail']}" for r in fails[:4])
        elif passes:
            status, detail = "pass", f"{len(passes)} claim(s) checked against evidence, none contradicted"
        else:
            status, detail = "not_run", "no checkable claims in this option"
        out.append(_result(check, status, detail, option=o["option_id"], ran=status != "not_run",
                           decisive=True, can_select=sel))
    return out


def run_verification(input_doc: dict, proposals: list[dict], options: list[dict]) -> dict[str, Any]:
    checks = effective_checks(input_doc)
    by_id = {p["id"]: p for p in proposals}
    results: list[dict[str, Any]] = []
    for check in checks:
        if check["type"] in CLAIM_LEVEL:
            claim_results = []
            for p in proposals:
                claim_results.extend(run_claim_checks(check, p, input_doc))
            results.extend(claim_results)
            if check.get("decisive") and not check.get("auto"):
                results.extend(_aggregate_claims(check, options, claim_results))
        else:
            for o in options:
                answer = by_id[o["proposals"][0]]["answer"]
                results.append(run_answer_check(check, o, answer, input_doc))
    summary = {
        "run": sum(1 for r in results if r["ran"]),
        "passed": sum(1 for r in results if r["status"] == "pass"),
        "failed": sum(1 for r in results if r["status"] == "fail"),
        "errored": sum(1 for r in results if r["status"] == "error"),
        "not_run": sum(1 for r in results if r["status"] == "not_run"),
    }
    note = ""
    if not checks:
        note = "no objective checks were defined and no evidence was supplied; nothing could be verified"
    elif not any(c["type"] in SELECTING for c in checks):
        note = "no answer-selecting check was defined; the verdict cannot select an answer on evidence alone"
    return {"checks": results, "summary": summary, "note": note, "defined_checks": [
        {"id": c["id"], "type": c["type"], "auto": bool(c.get("auto"))} for c in checks]}


def annotate_claims(claims: list[dict], check_results: list[dict]) -> list[dict]:
    by_claim: dict[str, list[dict]] = {}
    for r in check_results:
        if r.get("target_claim"):
            by_claim.setdefault(r["target_claim"], []).append(r)
    for c in claims:
        rs = by_claim.get(c["claim_id"], [])
        if any(r["status"] == "fail" and r["type"] == "evidence_facts" for r in rs):
            c["status"] = "contradicted"
        elif any(r["status"] == "fail" for r in rs):
            c["status"] = "citation_problem"
        elif any(r["status"] == "pass" and r["type"] == "evidence_facts" for r in rs):
            c["status"] = "supported"
        elif any(r["status"] == "pass" for r in rs):
            c["status"] = "citation_ok_content_unverified"
        else:
            c["status"] = "unverified"
        c["checks"] = [r["check_id"] + ":" + r["status"] for r in rs]
    return claims
