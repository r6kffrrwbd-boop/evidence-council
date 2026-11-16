"""Orchestrates intake and the approved run.

Intake (no file changes, no commands)
    Intake agent reads the project and the request, submits a checklist; the plan, the
    rendered persona scripts and the checklist are saved in a new run folder.

Run (only after approval; refuses if the plan changed since)
    0. Baseline   – isolated workspace; approved commands, journeys and probes on the
                    untouched project.
    1. Builder    – smallest change, file tools only.
    2. Evidence   – approved commands/journeys/load/security again; regressions vs baseline.
    3. Critic     – real diff + evidence → specific defects. Deterministic failures and
                    blocker/major defects send the work back to the Builder (step 1).
    4. Optimizer  – only with a named metric; keeps a change only if the metric improves by
                    the required margin and every check still passes; otherwise rolls back.
    5. Evaluator  – every requirement: verified / failed / AI judgment / untested.
    Report (report.json + report.md), changes.patch, event log, lessons from verified outcomes.
"""

from __future__ import annotations

import json
import re
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import CouncilError, EXIT_USAGE
from ..providers.base import CancelToken, ProviderError
from ..util import now_iso, read_json, redact_secrets, write_json
from . import journeys as J
from . import lessons as agent_lessons
from .agents import (BUILDER_SYSTEM, CRITIC_SYSTEM, EVALUATOR_SYSTEM, INTAKE_SYSTEM, OPTIMIZER_SYSTEM, SUBMIT_CHANGES,
                     SUBMIT_JUDGMENTS, SUBMIT_PLAN, SUBMIT_REVIEW, run_agent)
from .budget import Budget, BudgetExceeded, Limits
from .events import Reporter
from .evidence import Service, check_expectation, compare, run_command
from .models.base import ChatModel
from .plan import (PLAN_SCHEMA_ID, approve, check_plan, describe_check, fingerprint, lint, load_plan, normalise,
                   render_checklist, require_approval, save_plan, schema_problems)
from .security import probe_service, scan_diff, summarise
from .tools import AgentStuck, Toolbox, as_json_block
from .workspace import Workspace

SETUP_KEYS = {"service", "personas", "load", "optimizer", "limits", "security", "project_mode"}


def new_run_dir(home: Path) -> tuple[str, Path]:
    rid = "agent-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    d = home / "agent-runs" / rid
    d.mkdir(parents=True, exist_ok=False)
    d.chmod(0o700)
    return rid, d


def data_notice(model: ChatModel) -> str:
    if model.provider == "scripted":
        return "Nothing leaves this machine: the scripted test model is used (no AI provider is called)."
    return (f"Your request, the project files the agents choose to read (secret-looking files are excluded, secrets are redacted), "
            f"the diff and test output are sent to {model.provider} ({model.model}), a cloud API. Command output never includes your API keys.")


# ---------------------------------------------------------------------------
# intake

def _load_setup(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    setup = read_json(path)
    if not isinstance(setup, dict):
        raise CouncilError("setup file must be a JSON object", exit_code=EXIT_USAGE)
    unknown = set(setup) - SETUP_KEYS
    if unknown:
        raise CouncilError(f"setup file has unknown keys: {', '.join(sorted(unknown))}", hint=f"Allowed: {', '.join(sorted(SETUP_KEYS))}", exit_code=EXIT_USAGE)
    return setup


def _merge(request: str, project: Path, setup: dict[str, Any], submitted: dict[str, Any], model: ChatModel) -> dict[str, Any]:
    plan = {"schema": PLAN_SCHEMA_ID, "request": request,
            "project": {"path": str(project), "mode": setup.get("project_mode", "copy")},
            "items": submitted.get("items", []), "commands": submitted.get("commands", []),
            "open_questions": submitted.get("open_questions", []),
            "service": setup.get("service"), "personas": setup.get("personas", []), "load": setup.get("load", []),
            "optimizer": setup.get("optimizer"), "security": setup.get("security", {"enabled": True, "probe_service": True}),
            "limits": setup.get("limits", {}), "data_notice": data_notice(model),
            "intake": {"provider": model.provider, "model": model.model, "at": now_iso()}}
    for c in plan["commands"]:
        c.pop("sandbox", None)  # sandboxing off is a human decision, made by editing plan.json
    return plan


def intake(request: str, project: Path, model: ChatModel, home: Path, reporter: Reporter, setup_path: str | None = None,
           plan_file: str | None = None, limits: Limits | None = None, cancel: CancelToken | None = None) -> tuple[Path, dict[str, Any]]:
    project = Path(project).expanduser().resolve()
    ws = Workspace.readonly(project)
    setup = _load_setup(setup_path)
    rid, run_dir = new_run_dir(home)
    reporter.log_path = run_dir / "events.jsonl"
    reporter.phase("Intake", f"run {rid}")
    reporter.info(f"project: {project}")
    if plan_file:
        plan = read_json(plan_file)
        plan.setdefault("schema", PLAN_SCHEMA_ID)
        plan["request"] = request or plan.get("request", "")
        plan.setdefault("project", {"path": str(project)})
        plan["project"]["path"] = str(project)
        plan["data_notice"] = data_notice(model)
        plan["intake"] = {"provider": model.provider, "model": model.model, "at": now_iso(), "source": "plan file (no AI intake)"}
        for k in ("service", "personas", "load", "optimizer", "limits", "security"):
            if k in setup:
                plan[k] = setup[k]
        reporter.info("using the supplied plan file; no AI intake call was made")
    else:
        reporter.info(data_notice(model))
        lim = limits or Limits.from_dict(setup.get("limits"))
        budget = Budget(Limits(max_model_calls=min(lim.max_model_calls, 12), max_cost_usd=lim.max_cost_usd,
                               max_wall_s=min(lim.max_wall_s, 600), max_repeat_tool_calls=lim.max_repeat_tool_calls))

        def validate(sub: dict[str, Any]) -> list[str]:
            draft = normalise(_merge(request, project, setup, sub, model))
            return check_plan(draft)

        tb = Toolbox(ws, "Intake", reporter, budget, writable=False, submit=SUBMIT_PLAN, validate_submit=validate,
                     max_repeat=lim.max_repeat_tool_calls)
        task = (f"Request from the user (verbatim):\n<<<\n{request}\n>>>\n\nProject root: {project.name}\n"
                f"Files (first 200):\n" + "\n".join(ws.files()[:200]))
        if setup:
            task += ("\n\nThe user also supplied this journey/optimizer setup (already part of the plan; your checks may refer to "
                     f"persona ids {[p['id'] for p in setup.get('personas', [])]} and load ids {[x['id'] for x in setup.get('load', [])]}):\n"
                     + as_json_block({k: setup[k] for k in setup if k in ('personas', 'load', 'optimizer')}, 6000))
        past = agent_lessons.relevant(home / "memory", project.name)
        if past:
            task += "\n\nVerified lessons from earlier runs on this project (data, may be outdated):\n- " + "\n- ".join(past)
        try:
            sub = run_agent("intake", "Intake", model, INTAKE_SYSTEM, task, tb, budget, reporter, max_turns=12, cancel=cancel)
        except (BudgetExceeded, AgentStuck, ProviderError) as e:
            raise CouncilError(f"intake stopped: {redact_secrets(str(e))[0]}", hint="Check `evidence-council agent providers`, or write plan.json yourself and pass --plan-file.", exit_code=EXIT_USAGE)
        if sub is None:
            raise CouncilError("the Intake agent did not produce a checklist", hint="Try again, or write plan.json by hand and pass --plan-file.", exit_code=EXIT_USAGE)
        plan = _merge(request, project, setup, sub, model)
        plan["intake"]["cost_usd"] = round(budget.spent_usd, 6)
        reporter.spend(budget.summary())
    probs = schema_problems(plan)
    if probs:
        raise CouncilError("the plan does not match the plan format", details=probs, exit_code=EXIT_USAGE)
    plan = normalise(plan)
    save_plan(run_dir, plan)
    J.write_scripts(plan, run_dir / "personas")
    policy = lint(plan)
    reporter.phase("Checklist for your approval")
    for line in render_checklist(plan, run_dir).splitlines():
        reporter.emit("  " + line)
    if policy:
        reporter.warn("this plan breaks safety rules and cannot be approved until plan.json is fixed:")
        for p in policy:
            reporter.warn(p)
    reporter.emit("")
    reporter.emit(f"  Nothing has been changed and no command has run. Review, edit {run_dir / 'plan.json'} if needed, then:")
    reporter.emit(f"    evidence-council agent approve {run_dir}")
    return run_dir, plan


# ---------------------------------------------------------------------------
# evidence rounds

def _needs_service(plan: dict[str, Any]) -> bool:
    return bool(plan.get("service")) and bool(plan["personas"] or plan["load"] or plan.get("security", {}).get("probe_service", True))


def evidence_round(label: str, plan: dict[str, Any], ws: Workspace, run_dir: Path, reporter: Reporter, budget: Budget,
                   baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    reporter.info(f"Evidence checker: running approved checks ({label})")
    res: dict[str, Any] = {"label": label, "at": now_iso(), "commands": {}, "journeys": {}, "load": {}, "security": {"findings": []},
                           "service": None}
    tmp = run_dir / "tmp" / label
    for cmd in plan["commands"]:
        if cmd.get("kind") == "benchmark":
            continue
        budget.check_time()
        cap = max(5, int(budget.remaining_s()))
        r = run_command(cmd, ws.root, tmp, timeout_cap=cap)
        res["commands"][cmd["id"]] = r
        reporter.test(r)
        if r.get("sandbox_note"):
            reporter.warn(r["sandbox_note"])
    if _needs_service(plan):
        svc = Service(plan["service"], ws.root, tmp, run_dir / "logs" / f"service-{label}.log")
        ok = svc.start()
        res["service"] = {"started": ok, "startup_s": svc.startup_s, "error": svc.error, "sandboxed": svc.sandboxed}
        if ok:
            reporter.info(f"local app started on 127.0.0.1:{svc.port} in {svc.startup_s}s")
        else:
            reporter.warn(f"local app did not start: {svc.error}")
        try:
            personas = {p["id"]: p for p in plan["personas"]}
            scripts = run_dir / "personas"
            for pid, p in personas.items():
                if not ok:
                    res["journeys"][pid] = {"id": pid, "kind": "journey", "status": "error", "reasons": [svc.error]}
                    continue
                budget.check_time()
                raw = J.run_k6(scripts / f"{pid}.js", svc.base_url, run_dir / "k6" / label, timeout_s=180)
                st, reasons = J.judge_journey(p, raw)
                rec = {"id": pid, "kind": "journey", "status": st, "reasons": reasons, "raw": raw, "duration_s": raw.get("duration_s", 0),
                       "exit_code": raw.get("exit_code"), "failure_excerpt": "\n".join(reasons)}
                res["journeys"][pid] = rec
                reporter.test(rec)
            for lo in plan["load"]:
                if not ok:
                    res["load"][lo["id"]] = {"id": lo["id"], "kind": "load", "status": "error", "reasons": [svc.error]}
                    continue
                budget.check_time()
                raw = J.run_k6(scripts / f"load-{lo['id']}.js", svc.base_url, run_dir / "k6" / label, timeout_s=lo["duration_s"] + 90)
                st, reasons = J.judge_load(lo, personas[lo["persona_id"]], raw)
                rec = {"id": lo["id"], "kind": "load", "status": st, "reasons": reasons, "raw": raw, "duration_s": raw.get("duration_s", 0),
                       "exit_code": raw.get("exit_code"), "failure_excerpt": "\n".join(reasons),
                       "p95_ms": raw.get("p95_ms"), "requests": raw.get("requests")}
                res["load"][lo["id"]] = rec
                reporter.test(rec)
                if raw.get("p95_ms") is not None:
                    reporter.info(f"    load {lo['id']}: {raw.get('requests')} requests, p95 {raw['p95_ms']:.1f} ms, "
                                  f"failed {100 * (raw.get('failed_request_rate') or 0):.1f}%")
            if ok and plan.get("security", {}).get("enabled", True) and plan["security"].get("probe_service", True):
                res["security"]["findings"] += probe_service(svc.base_url)
        finally:
            svc.stop()
    if plan.get("security", {}).get("enabled", True) and ws.run_dir and (ws.run_dir / "snapshots" / "baseline").exists():
        patch, changes = ws.diff("baseline")
        res["security"]["findings"] += scan_diff(patch, changes)
        res["changes"] = changes
    res["security"]["counts"] = summarise(res["security"]["findings"])
    for f in res["security"]["findings"]:
        if f["severity"] in ("high", "medium"):
            reporter.warn(f"security ({f['severity']}): {f['rule']} {f.get('file') or ''}{':' + str(f['line']) if f.get('line') else ''} — {f['why']}")
    if baseline is not None:
        res["regressions"] = compare(baseline, res)
        for r in res["regressions"]:
            reporter.warn(r)
    write_json(res, run_dir / "evidence" / f"{label}.json")
    return res


def evaluate_checks(plan: dict[str, Any], ev: dict[str, Any], baseline: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Deterministic status of every acceptance check (AI judgments left 'pending')."""
    out = []
    for it in plan["items"]:
        checks = []
        for ch in it["acceptance"]["checks"]:
            t = ch["type"]
            d = {"type": t, "description": describe_check(ch, plan).split("  [")[0]}
            if t == "command":
                st, detail = check_expectation(ev["commands"].get(ch["command_id"]), ch)
                d.update(status=st, detail=detail, basis="verified" if st != "not_run" else "untested")
                if baseline is not None:
                    d["baseline_status"] = check_expectation(baseline["commands"].get(ch["command_id"]), ch)[0]
            elif t in ("journey", "load"):
                key = ch["persona_id"] if t == "journey" else ch["load_id"]
                r = (ev["journeys"] if t == "journey" else ev["load"]).get(key)
                if not r or r["status"] in ("not_run",):
                    d.update(status="not_run", detail=(r or {}).get("reasons", ["did not run"])[0] if r else "did not run", basis="untested")
                elif r["status"] == "error":
                    d.update(status="not_run", detail="; ".join(r.get("reasons", [])) or "error", basis="untested")
                else:
                    d.update(status="passed" if r["status"] == "passed" else "failed", detail="; ".join(r["reasons"]) or "all steps passed",
                             basis="verified")
                if baseline is not None:
                    br = (baseline["journeys"] if t == "journey" else baseline["load"]).get(key)
                    d["baseline_status"] = (br or {}).get("status", "not_run")
            elif t == "security":
                high = [f for f in ev["security"]["findings"] if f["severity"] == "high"]
                d.update(status="failed" if high else "passed", basis="verified",
                         detail="; ".join(f"{f['rule']} {f.get('file') or ''}" for f in high) or "no high-severity findings (heuristic scan)")
            elif t == "ai_judgment":
                d.update(status="pending", detail="", basis="ai_judgment", question=ch["question"])
            else:
                d.update(status="not_run", detail=ch.get("note", "manual check"), basis="untested")
            checks.append(d)
        out.append({"id": it["id"], "phrase": it["phrase"], "requirement": it["requirement"], "assumptions": it.get("assumptions", []),
                    "checks": checks})
    return out


def gate_reasons(plan: dict[str, Any], ev: dict[str, Any]) -> list[str]:
    """Objective reasons the work must go back to the Builder."""
    reasons = []
    for req in evaluate_checks(plan, ev):
        for ch in req["checks"]:
            if ch["status"] == "failed":
                reasons.append(f"{req['id']}: {ch['description']} — {ch['detail']}")
    for cid, r in ev["commands"].items():
        if r["status"] == "hung":
            reasons.append(f"command {cid} hung: {r['failure_excerpt']}")
    reasons += ev.get("regressions", [])
    for f in ev["security"]["findings"]:
        if f["severity"] == "high":
            reasons.append(f"security: {f['rule']} at {f.get('file')}:{f.get('line')} — {f['why']}")
    return list(dict.fromkeys(reasons))


def _evidence_digest(ev: dict[str, Any], limit: int = 9000) -> str:
    lines = []
    for cid, r in ev["commands"].items():
        lines.append(f"command {cid}: {r['status']} (exit {r.get('exit_code')}, {r.get('duration_s', 0):.2f}s)")
        bad = [n for n, s in r.get("tests", {}).get("names", {}).items() if s != "ok"]
        if bad:
            lines.append(f"  failing tests: {', '.join(bad[:15])}")
        if r["status"] != "passed":
            lines.append("  output (tail):\n" + "\n".join("    " + x for x in (r.get("stdout_tail", "") + "\n" + r.get("stderr_tail", "")).strip().splitlines()[-25:]))
    for kind in ("journeys", "load"):
        for jid, r in ev[kind].items():
            lines.append(f"{kind[:-1] if kind == 'journeys' else 'load'} {jid}: {r['status']}" + (f" — {'; '.join(r.get('reasons', []))}" if r.get("reasons") else ""))
    for f in ev["security"]["findings"]:
        lines.append(f"security {f['severity']}: {f['rule']} {f.get('file') or ''}:{f.get('line') or ''} {f['why']}")
    for r in ev.get("regressions", []):
        lines.append(r)
    s = "\n".join(lines)
    return s if len(s) <= limit else s[:limit] + "\n...[truncated]"


def _requirements_block(plan: dict[str, Any]) -> str:
    return as_json_block([{"id": i["id"], "phrase": i["phrase"], "requirement": i["requirement"], "assumptions": i.get("assumptions", []),
                           "acceptance": i["acceptance"]["description"],
                           "checks": [describe_check(c, plan) for c in i["acceptance"]["checks"]]} for i in plan["items"]], 12000)


def _truncate_patch(patch: str, limit: int = 30000) -> str:
    return patch if len(patch) <= limit else patch[:limit] + f"\n...[diff truncated; {len(patch) - limit} more characters]"


# ---------------------------------------------------------------------------
# optimizer

def measure(plan: dict[str, Any], ws: Workspace, run_dir: Path, reporter: Reporter, budget: Budget, label: str) -> dict[str, Any]:
    o = plan["optimizer"]
    m = o["metric"]
    cmd = next(c for c in plan["commands"] if c["id"] == m["command_id"])
    values, runs = [], []
    for i in range(o["runs"]):
        budget.check_time()
        r = run_command(cmd, ws.root, run_dir / "tmp" / f"metric-{label}-{i}", timeout_cap=max(5, int(budget.remaining_s())))
        runs.append({k: r.get(k) for k in ("status", "exit_code", "duration_s")})
        if r["status"] != "passed":
            return {"ok": False, "error": f"metric command {cmd['id']} {r['status']}: {r.get('failure_excerpt', '')[:200]}", "runs": runs}
        if m.get("regex"):
            mm = re.search(m["regex"], r["stdout_tail"] + "\n" + r["stderr_tail"])
            if not mm:
                return {"ok": False, "error": f"metric regex `{m['regex']}` did not match the output of {cmd['id']}", "runs": runs}
            values.append(float(mm.group(1)))
        else:
            values.append(r["duration_s"])
    med = statistics.median(values)
    reporter.info(f"metric {m['name']} ({label}): median {med:.4g}{' ' + m.get('unit', '') if m.get('unit') else ''} over {len(values)} runs {values}")
    return {"ok": True, "median": med, "values": values, "runs": runs}


def _improvement(before: float, after: float, direction: str) -> float:
    if before == 0:
        return 0.0
    return (before - after) / abs(before) * 100 if direction == "lower" else (after - before) / abs(before) * 100


# ---------------------------------------------------------------------------
# run

def run(run_dir: Path, model: ChatModel, home: Path, reporter: Reporter, overrides: dict[str, Any] | None = None,
        cancel: CancelToken | None = None, save_lessons: bool = True) -> dict[str, Any]:
    run_dir = Path(run_dir).expanduser().resolve()
    plan = load_plan(run_dir)
    approval = require_approval(run_dir, plan)
    if plan.get("intake", {}).get("provider") and plan["intake"]["provider"] != model.provider:
        raise CouncilError(f"this plan was approved for provider '{plan['intake']['provider']}' but the run would use '{model.provider}'",
                           hint="The data notice you approved names a provider. Run with the same provider, or start a new intake.", exit_code=EXIT_USAGE)
    limits = Limits.from_dict(plan["limits"]).lowered_by(overrides or {})
    budget = Budget(limits)
    reporter.log_path = run_dir / "events.jsonl"
    rid = run_dir.name
    report: dict[str, Any] = {"schema": "ec-agent-report/1", "run_id": rid, "request": plan["request"], "project": plan["project"]["path"],
                              "mode": plan["project"]["mode"], "provider": {"provider": model.provider, "model": model.model},
                              "approval": {"approved_at": approval["approved_at"], "fingerprint": approval["plan_fingerprint"]},
                              "started_at": now_iso(), "outcome": "stopped", "stop_reason": "", "rounds": [], "optimizer": None,
                              "repeated_work": [], "notes": []}
    ws: Workspace | None = None
    final_ev: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    builder_claims: list[str] = []
    critic_minor: list[dict[str, Any]] = []
    try:
        reporter.phase("Setup", f"run {rid}")
        reporter.info(f"approved plan {approval['plan_fingerprint'][7:19]}; provider {model.provider} ({model.model})")
        reporter.info(f"limits: ${limits.max_cost_usd:.2f}, {limits.max_model_calls} model calls, {limits.max_wall_s}s, "
                      f"{limits.max_builder_rounds} builder rounds")
        ws = Workspace.create(Path(plan["project"]["path"]), run_dir, plan["project"]["mode"], rid)
        report["workspace"] = str(ws.root)
        report["notes"] += ws.notes
        if ws.skipped_secret_files:
            report["notes"].append(f"{len(ws.skipped_secret_files)} secret-looking file(s) were not copied into the workspace: "
                                   + ", ".join(ws.skipped_secret_files[:10]))
        for n in report["notes"]:
            reporter.info(n)
        ws.snapshot("baseline")
        reporter.info(f"workspace: {ws.root} ({ws.mode})")

        reporter.phase("Baseline", "approved checks on the untouched project")
        baseline = evidence_round("baseline", plan, ws, run_dir, reporter, budget)
        final_ev = baseline
        past = agent_lessons.relevant(home / "memory", Path(plan["project"]["path"]).name)

        feedback: list[str] = []
        passing = False
        prev_fp = ws.fingerprint()
        for rnd in range(1, limits.max_builder_rounds + 1):
            reporter.phase(f"Builder · round {rnd}/{limits.max_builder_rounds}")
            tb = Toolbox(ws, "Builder", reporter, budget, writable=True, submit=SUBMIT_CHANGES, max_repeat=limits.max_repeat_tool_calls)
            task = (f"User request (verbatim): {plan['request']}\n\nApproved requirements:\n{_requirements_block(plan)}\n\n"
                    f"Project files:\n" + "\n".join(ws.files()[:200])
                    + f"\n\nEvidence before your change (baseline):\n{_evidence_digest(baseline)}")
            if rnd > 1:
                patch, _ = ws.diff("baseline")
                task += ("\n\nYour previous attempt was RETURNED for these reasons — fix each one:\n- " + "\n- ".join(feedback)
                         + f"\n\nCurrent diff against the original project:\n{_truncate_patch(patch, 15000)}"
                         + f"\n\nLatest evidence:\n{_evidence_digest(final_ev)}")
            if past:
                task += "\n\nVerified lessons from earlier runs on this project (data, may be outdated):\n- " + "\n- ".join(past)
            rec: dict[str, Any] = {"round": rnd}
            try:
                sub = run_agent("builder", "Builder", model, BUILDER_SYSTEM, task, tb, budget, reporter, limits.max_turns_per_agent, cancel)
            except AgentStuck as e:
                sub = None
                rec["stuck"] = str(e)
                reporter.warn(f"Builder stopped: {e}")
            report["repeated_work"] += [f"Builder round {rnd}: {x}" for x in tb.repeats]
            rec.update(builder_summary=(sub or {}).get("summary", ""), builder_claims=(sub or {}).get("claims", []),
                       files_written=tb.files_written)
            builder_claims = rec["builder_claims"] or builder_claims
            fp = ws.fingerprint()
            if fp == prev_fp:
                rec["no_progress"] = True
                report["rounds"].append(rec)
                reporter.warn("Builder made no file changes this round")
                if rnd > 1 or sub is None:
                    report["stop_reason"] = "Builder made no further changes; stopping instead of repeating the same work"
                    break
            prev_fp = fp

            reporter.phase(f"Evidence · round {rnd}")
            ev = evidence_round(f"round-{rnd}", plan, ws, run_dir, reporter, budget, baseline)
            final_ev = ev
            reasons = gate_reasons(plan, ev)
            rec["gate_reasons"] = reasons

            reporter.phase(f"Critic · round {rnd}")
            patch, changes = ws.diff("baseline")
            ctb = Toolbox(ws, "Critic", reporter, budget, writable=False, submit=SUBMIT_REVIEW, max_repeat=limits.max_repeat_tool_calls)
            ctask = (f"User request (verbatim): {plan['request']}\n\nApproved requirements:\n{_requirements_block(plan)}\n\n"
                     f"Builder's summary (unverified): {rec['builder_summary']}\nBuilder's claims (unverified): {rec['builder_claims']}\n\n"
                     f"Real diff against the original project:\n{_truncate_patch(patch)}\n\nReal evidence after the change:\n{_evidence_digest(ev)}\n\n"
                     f"Objective failures already detected (these go back to the Builder regardless): {reasons or 'none'}")
            try:
                review = run_agent("critic", "Critic", model, CRITIC_SYSTEM, ctask, ctb, budget, reporter, limits.max_turns_per_agent, cancel)
            except AgentStuck as e:
                review = None
                reporter.warn(f"Critic stopped: {e}")
            report["repeated_work"] += [f"Critic round {rnd}: {x}" for x in ctb.repeats]
            defects = (review or {}).get("defects", [])
            blocking = [d for d in defects if d.get("severity") in ("blocker", "major")]
            critic_minor += [d for d in defects if d.get("severity") == "minor"]
            rec["critic"] = {"verdict": (review or {}).get("verdict", "no review"), "defects": defects}
            for d in defects:
                reporter.info(f"Critic [{d.get('severity')}] {d.get('file', '')}{':' + str(d['line']) if d.get('line') else ''} {d.get('problem', '')}")
            feedback = reasons + [f"Critic ({d['severity']}): {d['problem']} Evidence: {d.get('evidence', '')} Fix: {d.get('fix', '')}"
                                  + (f" [{d['file']}{':' + str(d['line']) if d.get('line') else ''}]" if d.get("file") else "") for d in blocking]
            rec["returned"] = bool(feedback)
            report["rounds"].append(rec)
            reporter.spend(budget.summary())
            if not feedback:
                passing = True
                reporter.info("✓ every automated check passes and the Critic found no blocking defect")
                break
            if rnd < limits.max_builder_rounds:
                reporter.returned("Builder", feedback)
            else:
                report["stop_reason"] = f"still failing after {limits.max_builder_rounds} builder rounds"
                reporter.warn(report["stop_reason"])

        if passing and plan.get("optimizer"):
            report["optimizer"] = _optimize(plan, ws, run_dir, reporter, budget, model, limits, final_ev, baseline, cancel, report)
            final_ev = report["optimizer"].pop("_final_ev", final_ev)
        elif plan.get("optimizer"):
            report["optimizer"] = {"skipped": "optimizer runs only after every check passes"}

        report["outcome"] = "completed"
    except BudgetExceeded as e:
        report["stop_reason"] = f"limit reached: {e}"
        reporter.warn(report["stop_reason"])
    except ProviderError as e:
        report["stop_reason"] = f"model provider error: {redact_secrets(str(e))[0]}"
        reporter.warn(report["stop_reason"])
    except KeyboardInterrupt:
        report["stop_reason"] = "cancelled by you (Ctrl-C)"
        reporter.warn(report["stop_reason"])
    finally:
        if ws is not None and final_ev is not None:
            _finish(report, plan, ws, run_dir, reporter, budget, model, final_ev, baseline, builder_claims, critic_minor, home,
                    save_lessons, cancel)
    return report


def _optimize(plan, ws, run_dir, reporter, budget, model, limits, passing_ev, baseline, cancel, report) -> dict[str, Any]:
    o = plan["optimizer"]
    m = o["metric"]
    reporter.phase("Optimizer", f"goal: {o['goal']}")
    out: dict[str, Any] = {"goal": o["goal"], "metric_name": m["name"], "unit": m.get("unit", ""), "direction": m["direction"],
                           "min_improvement_pct": o["min_improvement_pct"], "runs": o["runs"], "attempts": []}
    ws.snapshot("passing-0")
    cur = measure(plan, ws, run_dir, reporter, budget, "before")
    if not cur["ok"]:
        out["skipped"] = cur["error"]
        reporter.warn(f"optimizer skipped: {cur['error']}")
        return out
    out["start_value"] = cur["median"]
    best_ev = passing_ev
    good = "passing-0"
    for k in range(1, limits.max_optimizer_attempts + 1):
        reporter.info(f"attempt {k}/{limits.max_optimizer_attempts}: current {m['name']} = {cur['median']:.4g} {m.get('unit', '')}")
        tb = Toolbox(ws, "Optimizer", reporter, budget, writable=True, submit=SUBMIT_CHANGES, max_repeat=limits.max_repeat_tool_calls)
        task = (f"Goal: {o['goal']}\nMetric: {m['name']} from command `{m['command_id']}`"
                + (f" (regex `{m['regex']}`)" if m.get("regex") else " (wall time)") + f"; {m['direction']} is better; current median "
                f"{cur['median']:.6g}. A change is kept only if it is at least {o['min_improvement_pct']}% better and every approved check "
                f"still passes.\n\nApproved requirements (must stay met):\n{_requirements_block(plan)}\n\nProject files:\n" + "\n".join(ws.files()[:200]))
        if out["attempts"]:
            task += "\n\nEarlier attempts (rolled back or kept):\n" + "\n".join(f"- {a['summary']} → {a['result']}" for a in out["attempts"])
        att: dict[str, Any] = {"attempt": k}
        try:
            sub = run_agent("optimizer", "Optimizer", model, OPTIMIZER_SYSTEM, task, tb, budget, reporter, limits.max_turns_per_agent, cancel)
        except AgentStuck as e:
            sub = None
            reporter.warn(f"Optimizer stopped: {e}")
        att.update(summary=(sub or {}).get("summary", "(no summary)"), files_written=tb.files_written)
        if not tb.files_written:
            ws.restore(good)
            att.update(result="no change made", kept=False)
            out["attempts"].append(att)
            continue
        ev = evidence_round(f"optimizer-{k}", plan, ws, run_dir, reporter, budget, baseline)
        reasons = gate_reasons(plan, ev)
        new_high = [f for f in ev["security"]["findings"] if f["severity"] == "high"]
        after = measure(plan, ws, run_dir, reporter, budget, f"attempt-{k}") if not reasons else {"ok": False, "error": "checks failed"}
        imp = _improvement(cur["median"], after["median"], m["direction"]) if after.get("ok") else None
        att.update(before=cur["median"], after=after.get("median"), improvement_pct=imp, gate_reasons=reasons)
        if reasons or new_high or not after.get("ok") or imp is None or imp < o["min_improvement_pct"]:
            why = (f"{len(reasons)} failing check(s) or regression(s), first: {reasons[0][:200]}" if reasons else f"security: {new_high[0]['rule']}" if new_high
                   else after.get("error") if not after.get("ok")
                   else f"improvement {imp:.1f}% is below the required {o['min_improvement_pct']}%")
            restored = ws.restore(good)
            att.update(result=f"rolled back ({why})", kept=False, restored_files=restored)
            reporter.returned("the previous passing version (rollback)", [why])
        else:
            good = f"passing-{k}"
            ws.snapshot(good)
            att.update(result=f"kept: {imp:.1f}% better", kept=True)
            reporter.info(f"✓ kept optimizer change: {m['name']} {cur['median']:.4g} → {after['median']:.4g} ({imp:.1f}% better)")
            cur = after
            best_ev = ev
            out["attempts"].append(att)
            continue
        out["attempts"].append(att)
        reporter.spend(budget.summary())
    out["final_value"] = cur["median"]
    out["_final_ev"] = best_ev
    return out


def _finish(report, plan, ws, run_dir, reporter, budget, model, final_ev, baseline, builder_claims, critic_minor, home, save_lessons, cancel):
    reporter.phase("Final evaluation")
    reqs = evaluate_checks(plan, final_ev, baseline)
    pending = [(r, c) for r in reqs for c in r["checks"] if c["status"] == "pending"]
    if pending:
        try:
            tb = Toolbox(ws, "Evaluator", reporter, budget, writable=False, submit=SUBMIT_JUDGMENTS)
            q = "\n".join(f"- {r['id']}: {c['question']} (requirement: {r['requirement']})" for r, c in pending)
            sub = run_agent("evaluator", "Evaluator", model, EVALUATOR_SYSTEM, f"Questions:\n{q}", tb, budget, reporter, 8, cancel)
            js = {j["requirement_id"]: j for j in (sub or {}).get("judgments", [])}
        except (BudgetExceeded, AgentStuck, ProviderError) as e:
            js = {}
            reporter.warn(f"AI judgments not made: {e}")
        for r, c in pending:
            j = js.get(r["id"])
            if j:
                c.update(status=j["judgment"], detail=j.get("reason", "") + (f" (cites {', '.join(j.get('cites', []))})" if j.get("cites") else ""))
            else:
                c.update(status="not_evaluated", detail="no AI judgment was made", basis="untested")
    for r in reqs:
        sts = [(c["basis"], c["status"]) for c in r["checks"]]
        if any(b == "verified" and s == "failed" for b, s in sts):
            r["status"] = "failed"
        elif all(b == "verified" and s == "passed" for b, s in sts):
            r["status"] = "verified"
        elif any(b == "ai_judgment" for b, _ in sts) and all(s in ("passed", "appears_met") for b, s in sts if b != "untested"):
            r["status"] = "ai_judgment"
        else:
            r["status"] = "untested"
    report["requirements"] = reqs
    report["counts"] = {k: sum(1 for r in reqs if r["status"] == k) for k in ("verified", "failed", "ai_judgment", "untested")}
    patch, changes = ws.diff("baseline")
    (run_dir / "changes.patch").write_text(patch, encoding="utf-8")
    report["changes"] = changes
    report["changed_paths"] = [c["path"] for c in changes]
    report["patch_file"] = str(run_dir / "changes.patch")
    report["regressions"] = final_ev.get("regressions", [])
    report["security"] = final_ev["security"]
    report["journeys"] = {k: {x: v[x] for x in ("status", "reasons")} for k, v in final_ev["journeys"].items()}
    report["load"] = {k: {x: v.get(x) for x in ("status", "reasons", "p95_ms", "requests")} for k, v in final_ev["load"].items()}
    report["untested_claims"] = builder_claims
    report["critic_minor"] = critic_minor
    report["budget"] = budget.summary()
    report["finished_at"] = now_iso()
    report["evidence_files"] = sorted(str(p) for p in (run_dir / "evidence").glob("*.json"))
    if save_lessons:
        try:
            report["lessons"] = agent_lessons.retain(report, home / "memory")
        except Exception as e:  # memory problems must not lose the report
            report["lessons"] = {"retained": [], "rejected": [], "error": str(e)}
    else:
        report["lessons"] = {"retained": [], "rejected": [], "skipped": "--no-lessons"}
    write_json(report, run_dir / "report.json")
    md = render_report(report)
    (run_dir / "report.md").write_text(md, encoding="utf-8")
    for line in md.splitlines()[:80]:
        reporter.emit("  " + line)
    reporter.emit("")
    reporter.emit(f"  Report: {run_dir / 'report.md'}   Patch: {run_dir / 'changes.patch'}")
    reporter.spend(budget.summary())


def render_report(r: dict[str, Any]) -> str:
    L = [f"# Evidence report — {r['run_id']}", "",
         f"**Request:** {r['request']}", f"**Outcome:** {r['outcome']}" + (f" — {r['stop_reason']}" if r.get("stop_reason") else ""),
         f"**Model provider:** {r['provider']['provider']} ({r['provider']['model']})"
         + ("  _(scripted test model: no AI was involved)_" if r["provider"]["provider"] == "scripted" else ""), ""]
    c = r.get("counts", {})
    L.append(f"**Requirements:** {c.get('verified', 0)} verified · {c.get('failed', 0)} failed · "
             f"{c.get('ai_judgment', 0)} AI judgment only · {c.get('untested', 0)} untested")
    L.append("")
    groups = [("verified", "## Verified by running checks"), ("failed", "## Failed checks"),
              ("ai_judgment", "## AI judgments (an AI reviewer's opinion — not proven by a test)"), ("untested", "## Not tested")]
    for key, title in groups:
        rs = [x for x in r.get("requirements", []) if x["status"] == key]
        if not rs:
            continue
        L.append(title)
        for x in rs:
            L.append(f"- **{x['id']}** — you said “{x['phrase']}” → {x['requirement']}")
            for ch in x["checks"]:
                tag = {"verified": "verified", "ai_judgment": "AI judgment", "untested": "untested"}[ch["basis"]]
                before = f"; before the change: {ch['baseline_status']}" if ch.get("baseline_status") else ""
                L.append(f"  - [{tag}] {ch['description']}: **{ch['status']}** — {ch.get('detail', '')}{before}")
            for a in x.get("assumptions", []):
                L.append(f"  - assumption you approved: {a}")
        L.append("")
    if r.get("regressions"):
        L += ["## Regressions (worked before, broken now)"] + [f"- {x}" for x in r["regressions"]] + [""]
    if r.get("rounds"):
        L.append("## What the agents did")
        for rd in r["rounds"]:
            L.append(f"- Round {rd['round']}: Builder changed {', '.join(rd.get('files_written') or []) or 'nothing'}"
                     + (f" — “{rd['builder_summary'][:200]}”" if rd.get("builder_summary") else ""))
            if rd.get("returned"):
                why = (rd.get("gate_reasons") or []) + [d["problem"] for d in rd.get("critic", {}).get("defects", []) if d.get("severity") in ("blocker", "major")]
                L.append(f"  - returned for another pass: {'; '.join(why)[:500]}")
        L.append("")
    o = r.get("optimizer")
    if o:
        L.append("## Optimizer")
        if o.get("skipped"):
            L.append(f"- skipped: {o['skipped']}")
        else:
            L.append(f"- goal: {o['goal']}; metric {o['metric_name']} ({o['direction']} is better), median of {o['runs']} runs")
            for a in o["attempts"]:
                nums = f" ({a['before']:.4g} → {a['after']:.4g})" if a.get("after") is not None and a.get("before") is not None else ""
                L.append(f"- attempt {a['attempt']}: {a['result']}{nums} — {a['summary'][:160]}")
            L.append(f"- final value: {o.get('final_value', o.get('start_value')):.4g} (started at {o.get('start_value', 0):.4g})")
        L.append("")
    sec = r.get("security", {})
    L.append("## Security scan (heuristic)")
    L.append(f"- {sec.get('counts', {}).get('high', 0)} high, {sec.get('counts', {}).get('medium', 0)} medium findings")
    for f in sec.get("findings", [])[:10]:
        L.append(f"  - {f['severity']}: {f['rule']} {f.get('file') or ''}{':' + str(f['line']) if f.get('line') else ''} — {f['why']}")
    L.append("")
    if r.get("journeys") or r.get("load"):
        L.append("## Customer journeys and load (k6)")
        for k, v in r.get("journeys", {}).items():
            L.append(f"- journey {k}: {v['status']}" + (f" — {'; '.join(v['reasons'])}" if v.get("reasons") else ""))
        for k, v in r.get("load", {}).items():
            extra = f" (p95 {v['p95_ms']:.1f} ms, {v['requests']} requests)" if v.get("p95_ms") is not None else ""
            L.append(f"- load {k}: {v['status']}{extra}" + (f" — {'; '.join(v['reasons'])}" if v.get("reasons") else ""))
        L.append("")
    if r.get("untested_claims"):
        L += ["## The Builder's own claims (not independently verified)"] + [f"- {x}" for x in r["untested_claims"]] + [""]
    if r.get("critic_minor"):
        L += ["## Minor remarks from the Critic (AI opinion, not blocking)"] + [f"- {d['problem']}" for d in r["critic_minor"][:10]] + [""]
    if r.get("repeated_work"):
        L += ["## Repeated work detected"] + [f"- {x}" for x in r["repeated_work"][:10]] + [""]
    L.append("## Changes")
    for ch in r.get("changes", []):
        L.append(f"- {ch['status']}: {ch['path']} (+{ch.get('added', 0)} −{ch.get('removed', 0)})")
    if not r.get("changes"):
        L.append("- no files changed")
    if r["mode"] == "copy":
        L.append(f"\nYour project was not modified. To apply the changes yourself: `cd {r['project']} && git apply {r.get('patch_file', 'changes.patch')}`")
    else:
        L.append("\nChanges are uncommitted in the agent's worktree branch; nothing was committed or pushed.")
    for n in r.get("notes", []):
        L.append(f"- note: {n}")
    b = r.get("budget", {})
    L += ["", "## Spend and limits", f"- ${b.get('spent_usd', 0):.4f} of ${b.get('max_cost_usd', 0):.2f}; {b.get('model_calls', 0)} model calls; "
          f"{b.get('input_tokens', 0)} input / {b.get('output_tokens', 0)} output tokens; {b.get('elapsed_s', 0):.0f}s"]
    les = r.get("lessons", {})
    if les.get("retained") or les.get("rejected"):
        L += ["", "## Lessons kept (verified outcomes only)"] + [f"- {x['statement']}" for x in les.get("retained", [])]
        L += [f"- not kept: {x['statement'][:120]}… (failed: {', '.join(x['failed'])})" for x in les.get("rejected", [])]
    return "\n".join(L) + "\n"


def load_report(run_dir: Path) -> dict[str, Any]:
    p = Path(run_dir) / "report.json"
    if not p.exists():
        raise CouncilError(f"no report in {run_dir} yet", hint=f"Run it first: evidence-council agent run {run_dir}", exit_code=EXIT_USAGE)
    return json.loads(p.read_text(encoding="utf-8"))


__all__ = ["intake", "run", "approve", "render_checklist", "load_plan", "load_report", "render_report", "fingerprint"]
