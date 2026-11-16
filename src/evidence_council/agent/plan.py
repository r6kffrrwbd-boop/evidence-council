"""The approved plan: requirement checklist, commands, personas, limits — and its approval.

``plan.json`` is written by Intake (or by hand) inside a run folder. Nothing that changes
files or runs commands happens until ``approval.json`` exists and its fingerprint matches
the current ``plan.json`` and rendered persona scripts. Editing either after approval
invalidates the approval.

Policy checks (``lint``) run at intake, at approval and again at run time. Plans that
would deploy, commit, push, install packages, reach a non-local host, use a shell, or call
an "optimisation" a reduction in line count are refused.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

import jsonschema

from ..errors import CouncilError, EXIT_USAGE
from ..util import canonical_json, now_iso, read_json, sha256_digest, write_json
from .budget import Limits

PLAN_SCHEMA_ID = "ec-agent-plan/1"
_ID = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$"}

CHECK_SCHEMA = {
    "type": "object",
    "required": ["type"],
    "properties": {
        "type": {"enum": ["command", "journey", "load", "security", "ai_judgment", "manual"]},
        "command_id": _ID, "persona_id": _ID, "load_id": _ID,
        "expect_exit": {"type": "integer"},
        "expect_output": {"type": "string", "description": "regular expression that must appear in the command output"},
        "question": {"type": "string"}, "note": {"type": "string"},
    },
    "additionalProperties": False,
}

PERSONA_STEP = {
    "type": "object", "required": ["action"],
    "properties": {
        "action": {"enum": ["goto", "fill", "click", "wait_for", "expect_text", "request"]},
        "label": {"type": "string"}, "path": {"type": "string"}, "selector": {"type": "string"},
        "value": {"type": "string"}, "contains": {"type": "string"},
        "method": {"enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]}, "body": {},
        "expect_status": {"type": "integer"}, "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 30000},
    },
    "additionalProperties": False,
}

PLAN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema", "request", "project", "items", "commands"],
    "properties": {
        "schema": {"const": PLAN_SCHEMA_ID},
        "request": {"type": "string", "minLength": 1},
        "project": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string"}, "mode": {"enum": ["copy", "worktree"]}}, "additionalProperties": False},
        "items": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["id", "phrase", "requirement", "acceptance"],
            "properties": {
                "id": _ID, "phrase": {"type": "string", "minLength": 1}, "requirement": {"type": "string", "minLength": 1},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "acceptance": {"type": "object", "required": ["description", "checks"], "properties": {
                    "description": {"type": "string"}, "checks": {"type": "array", "minItems": 1, "items": CHECK_SCHEMA}},
                    "additionalProperties": False},
            }, "additionalProperties": False}},
        "commands": {"type": "array", "items": {
            "type": "object", "required": ["id", "argv", "purpose"],
            "properties": {
                "id": _ID, "argv": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "purpose": {"type": "string"}, "kind": {"enum": ["test", "build", "benchmark"]},
                "timeout_s": {"type": "integer", "minimum": 1, "maximum": 1800},
                "network": {"enum": ["none", "localhost"]}, "sandbox": {"type": "boolean"},
            }, "additionalProperties": False}},
        "service": {"type": ["object", "null"], "required": ["argv"], "properties": {
            "argv": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "ready_path": {"type": "string"}, "startup_timeout_s": {"type": "integer", "minimum": 1, "maximum": 120},
            "sandbox": {"type": "boolean"}}, "additionalProperties": False},
        "personas": {"type": "array", "items": {
            "type": "object", "required": ["id", "name", "kind", "steps"],
            "properties": {
                "id": _ID, "name": {"type": "string"}, "description": {"type": "string"},
                "kind": {"enum": ["browser", "http"]}, "steps": {"type": "array", "minItems": 1, "items": PERSONA_STEP},
                "max_requests": {"type": "integer", "minimum": 1, "maximum": 500},
                "max_request_ms": {"type": "integer", "minimum": 50, "maximum": 60000},
            }, "additionalProperties": False}},
        "load": {"type": "array", "items": {
            "type": "object", "required": ["id", "persona_id"],
            "properties": {
                "id": _ID, "persona_id": _ID, "vus": {"type": "integer", "minimum": 1, "maximum": 20},
                "duration_s": {"type": "integer", "minimum": 1, "maximum": 60},
                "p95_ms": {"type": "number", "minimum": 1}, "max_error_rate": {"type": "number", "minimum": 0, "maximum": 1},
            }, "additionalProperties": False}},
        "optimizer": {"type": ["object", "null"], "required": ["goal", "metric"], "properties": {
            "goal": {"type": "string"},
            "metric": {"type": "object", "required": ["command_id"], "properties": {
                "command_id": _ID, "regex": {"type": "string"}, "name": {"type": "string"},
                "direction": {"enum": ["lower", "higher"]}, "unit": {"type": "string"}}, "additionalProperties": False},
            "min_improvement_pct": {"type": "number", "minimum": 1, "maximum": 95},
            "runs": {"type": "integer", "minimum": 1, "maximum": 9}}, "additionalProperties": False},
        "security": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "probe_service": {"type": "boolean"}},
                     "additionalProperties": False},
        "limits": {"type": "object"},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "data_notice": {"type": "string"},
        "intake": {"type": "object"},
    },
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# policy

BLOCKED_PROGRAMS = {
    "rm", "rmdir", "sudo", "su", "doas", "ssh", "scp", "sftp", "rsync", "curl", "wget", "nc", "ncat", "telnet", "ftp",
    "dd", "mkfs", "shutdown", "reboot", "launchctl", "crontab", "osascript", "open", "security", "defaults",
    "docker", "podman", "kubectl", "helm", "terraform", "pulumi", "ansible", "aws", "gcloud", "az", "vercel",
    "netlify", "fly", "flyctl", "heroku", "firebase", "supabase", "gh", "brew", "apt", "apt-get", "yum", "port",
    "sh", "bash", "zsh", "fish", "csh", "tcsh", "ksh", "eval", "xargs", "env", "nohup", "killall", "pkill", "kill",
}
BLOCKED_SUBCOMMANDS = {
    "git": {"commit", "push", "pull", "fetch", "merge", "rebase", "reset", "checkout", "switch", "clean", "tag",
            "remote", "clone", "am", "apply", "cherry-pick", "revert", "stash", "branch", "worktree", "config", "submodule"},
    "npm": {"publish", "install", "i", "add", "ci", "update", "uninstall", "login", "adduser", "deploy", "exec"},
    "npx": {"*"}, "pnpm": {"publish", "install", "i", "add", "update", "deploy", "dlx"},
    "yarn": {"publish", "install", "add", "upgrade", "deploy", "dlx"},
    "pip": {"install", "uninstall", "download"}, "pip3": {"install", "uninstall", "download"},
    "uv": {"pip", "add", "sync", "publish", "tool"}, "poetry": {"add", "install", "publish", "update"},
    "cargo": {"publish", "install"}, "twine": {"*"}, "make": {"deploy", "publish", "release", "install", "push"},
}
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "0.0.0.0")
URL_RE = re.compile(r"(?i)\b(?:https?|wss?|ftp)://([^/\s:'\"]+)")
LOC_GOALS = re.compile(r"(?i)\b(lines? of code|loc|line count|fewer lines|shorter code|less code|code size)\b")


def lint_argv(argv: list[str], where: str) -> list[str]:
    problems = []
    prog = Path(argv[0]).name
    if prog in BLOCKED_PROGRAMS:
        problems.append(f"{where}: '{prog}' is not allowed (deploys, network tools, shells and destructive commands are blocked; give the program and its arguments directly)")
    subs = BLOCKED_SUBCOMMANDS.get(prog)
    if prog in ("python", "python3") and len(argv) >= 3 and argv[1] == "-m" and argv[2] in ("pip", "twine"):
        problems.append(f"{where}: installing or publishing packages is not allowed; install dependencies yourself before running")
    if subs:
        rest = [a for a in argv[1:] if not a.startswith("-")]
        if "*" in subs or (rest and rest[0] in subs):
            shown = (prog + " " + (rest[0] if rest else "")).strip()
            problems.append(f"{where}: '{shown}' is not allowed (no commits, pushes, installs, publishing or deploys)")
    for a in argv:
        for host in URL_RE.findall(a):
            if host.lower() not in LOCAL_HOSTS:
                problems.append(f"{where}: refers to non-local host '{host}'; only localhost is allowed (no production access)")
        if re.search(r"(?i)\b(prod|production)\b", a) and re.search(r"(?i)(deploy|db|database|url|host|env)", a):
            problems.append(f"{where}: argument '{a[:60]}' looks like a production target")
    return problems


def lint(plan: dict[str, Any]) -> list[str]:
    """Policy problems that block approval. Schema problems are reported separately."""
    problems: list[str] = []
    cmd_ids = [c["id"] for c in plan.get("commands", [])]
    persona_ids = [p["id"] for p in plan.get("personas", [])]
    load_ids = [lo["id"] for lo in plan.get("load", [])]
    for label, ids in (("command", cmd_ids), ("persona", persona_ids), ("load", load_ids),
                       ("requirement", [i["id"] for i in plan.get("items", [])])):
        dup = {x for x in ids if ids.count(x) > 1}
        if dup:
            problems.append(f"duplicate {label} id(s): {', '.join(sorted(dup))}")
    for c in plan.get("commands", []):
        problems += lint_argv(c["argv"], f"command {c['id']}")
    if plan.get("service"):
        problems += lint_argv(plan["service"]["argv"], "service")
    for it in plan.get("items", []):
        for ch in it["acceptance"]["checks"]:
            t = ch["type"]
            if t == "command" and ch.get("command_id") not in cmd_ids:
                problems.append(f"{it['id']}: check refers to unknown command '{ch.get('command_id')}'")
            if t == "journey" and ch.get("persona_id") not in persona_ids:
                problems.append(f"{it['id']}: check refers to unknown persona '{ch.get('persona_id')}'")
            if t == "load" and ch.get("load_id") not in load_ids:
                problems.append(f"{it['id']}: check refers to unknown load test '{ch.get('load_id')}'")
            if t == "ai_judgment" and not ch.get("question"):
                problems.append(f"{it['id']}: ai_judgment check needs a 'question'")
            if t == "command" and ch.get("expect_output"):
                try:
                    re.compile(ch["expect_output"])
                except re.error as e:
                    problems.append(f"{it['id']}: expect_output is not a valid regular expression: {e}")
    if (plan.get("personas") or plan.get("load")) and not plan.get("service"):
        problems.append("personas/load tests need a 'service' (the local app command to start)")
    for lo in plan.get("load", []):
        if lo["persona_id"] in persona_ids:
            p = next(p for p in plan["personas"] if p["id"] == lo["persona_id"])
            if p["kind"] != "http":
                problems.append(f"load {lo['id']}: load tests use an http persona, not a browser persona")
    for p in plan.get("personas", []):
        for s in p["steps"]:
            path = s.get("path", "")
            if path and not path.startswith("/"):
                problems.append(f"persona {p['id']}: step path '{path}' must start with '/' (paths are relative to the local app)")
            if URL_RE.search(path or ""):
                problems.append(f"persona {p['id']}: step path must not contain a full URL")
            need = {"goto": ["path"], "fill": ["selector", "value"], "click": ["selector"], "wait_for": ["selector"],
                    "expect_text": ["selector", "contains"], "request": ["method", "path"]}[s["action"]]
            missing = [k for k in need if k not in s]
            if missing:
                problems.append(f"persona {p['id']}: step '{s['action']}' is missing {', '.join(missing)}")
            if p["kind"] == "http" and s["action"] != "request":
                problems.append(f"persona {p['id']}: http personas only use 'request' steps")
            if p["kind"] == "browser" and s["action"] == "request":
                problems.append(f"persona {p['id']}: browser personas use goto/fill/click/wait_for/expect_text")
    opt = plan.get("optimizer")
    if opt:
        if opt["metric"]["command_id"] not in cmd_ids:
            problems.append(f"optimizer metric refers to unknown command '{opt['metric']['command_id']}'")
        text = f"{opt['goal']} {opt['metric'].get('name', '')}"
        if LOC_GOALS.search(text):
            problems.append("optimizer goal is about code size; fewer lines is not accepted as an improvement on its own. "
                            "Name a behaviour metric (time, memory, requests, errors) and keep clarity as a constraint.")
        if opt["metric"].get("regex"):
            try:
                if re.compile(opt["metric"]["regex"]).groups < 1:
                    problems.append("optimizer metric regex needs one capture group around the number")
            except re.error as e:
                problems.append(f"optimizer metric regex is invalid: {e}")
    return problems


def schema_problems(plan: Any) -> list[str]:
    v = jsonschema.Draft202012Validator(PLAN_SCHEMA)
    out = []
    for e in sorted(v.iter_errors(plan), key=lambda e: list(e.absolute_path)):
        path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in e.absolute_path)
        out.append(f"{path}: {e.message[:200]}")
    return out


def normalise(plan: dict[str, Any]) -> dict[str, Any]:
    plan.setdefault("schema", PLAN_SCHEMA_ID)
    plan.setdefault("personas", [])
    plan.setdefault("load", [])
    plan.setdefault("service", None)
    plan.setdefault("optimizer", None)
    plan.setdefault("open_questions", [])
    plan.setdefault("security", {"enabled": True, "probe_service": True})
    plan["project"].setdefault("mode", "copy")
    plan["limits"] = Limits.from_dict(plan.get("limits")).to_dict()
    for c in plan.get("commands", []):
        c.setdefault("kind", "test")
        c.setdefault("timeout_s", 120)
        c.setdefault("network", "none")
        c.setdefault("sandbox", True)
    for it in plan.get("items", []):
        it.setdefault("assumptions", [])
    for p in plan.get("personas", []):
        for n, s in enumerate(p["steps"], 1):
            s.setdefault("label", f"{n}. " + {
                "goto": lambda: f"open {s.get('path')}", "fill": lambda: f"type '{s.get('value')}' into {s.get('selector')}",
                "click": lambda: f"click {s.get('selector')}", "wait_for": lambda: f"wait for {s.get('selector')}",
                "expect_text": lambda: f"check {s.get('selector')} shows '{s.get('contains')}'",
                "request": lambda: f"{s.get('method')} {s.get('path')}"}[s["action"]]())
            s.setdefault("timeout_ms", 10000)
            if s["action"] == "request":
                s.setdefault("expect_status", 200)
        p.setdefault("max_request_ms", 5000)
        p.setdefault("max_requests", 50 if p["kind"] == "browser" else len(p["steps"]))
    for lo in plan.get("load", []):
        lo.setdefault("vus", 5)
        lo.setdefault("duration_s", 10)
        lo.setdefault("p95_ms", 500)
        lo.setdefault("max_error_rate", 0.01)
    if plan.get("service"):
        plan["service"].setdefault("ready_path", "/")
        plan["service"].setdefault("startup_timeout_s", 20)
        plan["service"].setdefault("sandbox", True)
    if plan.get("optimizer"):
        o = plan["optimizer"]
        o.setdefault("min_improvement_pct", 10)
        o.setdefault("runs", 3)
        o["metric"].setdefault("direction", "lower")
        o["metric"].setdefault("name", o["metric"]["command_id"])
    return plan


def check_plan(plan: Any) -> list[str]:
    probs = schema_problems(plan)
    if probs:
        return probs
    return lint(plan)


# ---------------------------------------------------------------------------
# storage + approval

def load_plan(run_dir: Path) -> dict[str, Any]:
    p = Path(run_dir) / "plan.json"
    if not p.exists():
        raise CouncilError(f"no plan.json in {run_dir}", hint="Create one with: evidence-council agent intake --project DIR \"your request\"", exit_code=EXIT_USAGE)
    plan = read_json(p)
    probs = schema_problems(plan)
    if probs:
        raise CouncilError("plan.json does not match the plan format", hint="Fix the listed fields (see docs/agent.md).", details=probs, exit_code=EXIT_USAGE)
    return normalise(plan)


def save_plan(run_dir: Path, plan: dict[str, Any]) -> None:
    write_json(plan, Path(run_dir) / "plan.json")


def fingerprint(run_dir: Path, plan: dict[str, Any]) -> str:
    """Hash of the plan plus every rendered persona script — what approval covers."""
    from .journeys import render_all
    scripts = render_all(plan)
    return sha256_digest(canonical_json({"plan": plan, "scripts": scripts}))


def approve(run_dir: Path, plan: dict[str, Any], method: str) -> dict[str, Any]:
    probs = check_plan(plan)
    if probs:
        raise CouncilError("the plan breaks a safety rule and cannot be approved", hint="Edit plan.json to remove the listed problems, then approve again.",
                           details=probs, exit_code=EXIT_USAGE)
    rec = {"plan_fingerprint": fingerprint(run_dir, plan), "approved_at": now_iso(), "method": method,
           "approved": {"requirements": [i["id"] for i in plan["items"]], "commands": [c["id"] for c in plan["commands"]],
                        "service": bool(plan.get("service")), "personas": [p["id"] for p in plan["personas"]],
                        "load": [lo["id"] for lo in plan["load"]], "optimizer": bool(plan.get("optimizer")),
                        "limits": plan["limits"]}}
    write_json(rec, Path(run_dir) / "approval.json")
    return rec


def require_approval(run_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    ap = Path(run_dir) / "approval.json"
    if not ap.exists():
        raise CouncilError("this plan has not been approved, so nothing will run",
                           hint=f"Review the checklist and approve it: evidence-council agent approve {run_dir}", exit_code=EXIT_USAGE)
    rec = read_json(ap)
    fp = fingerprint(run_dir, plan)
    if rec.get("plan_fingerprint") != fp:
        raise CouncilError("plan.json or a persona script changed after it was approved; the approval no longer applies",
                           hint=f"Review the changed checklist and approve again: evidence-council agent approve {run_dir}", exit_code=EXIT_USAGE)
    probs = check_plan(plan)
    if probs:
        raise CouncilError("the plan breaks a safety rule", details=probs, exit_code=EXIT_USAGE)
    return rec


# ---------------------------------------------------------------------------
# plain-English checklist

def _argv(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def describe_check(ch: dict[str, Any], plan: dict[str, Any]) -> str:
    t = ch["type"]
    if t == "command":
        s = f"command `{ch['command_id']}` must exit {ch.get('expect_exit', 0)}"
        if ch.get("expect_output"):
            s += f" and its output must match `{ch['expect_output']}`"
        return s + "  [automated]"
    if t == "journey":
        p = next((p for p in plan["personas"] if p["id"] == ch["persona_id"]), None)
        return f"customer journey `{ch['persona_id']}`" + (f" ({p['name']})" if p else "") + " must pass every step  [automated, k6]"
    if t == "load":
        lo = next((x for x in plan["load"] if x["id"] == ch["load_id"]), {})
        return (f"load test `{ch['load_id']}`: {lo.get('vus')} users for {lo.get('duration_s')}s, p95 under {lo.get('p95_ms')} ms, "
                f"errors under {lo.get('max_error_rate', 0) * 100:.0f}%  [automated, k6]")
    if t == "security":
        return "no high-severity security findings in the change  [automated scan]"
    if t == "ai_judgment":
        return f"AI reviewer answers: \"{ch['question']}\"  [AI judgment — not proof]"
    return f"you check this yourself: {ch.get('note', '')}  [manual — will be reported as untested]"


def render_checklist(plan: dict[str, Any], run_dir: Path | None = None) -> str:
    L = [f"Your request: \"{plan['request']}\"", "", "What I understood — please check every line:"]
    for it in plan["items"]:
        L.append(f"  [{it['id']}] You said:   \"{it['phrase']}\"")
        L.append(f"        I will:     {it['requirement']}")
        for a in it.get("assumptions", []):
            L.append(f"        Assuming:   {a}")
        L.append(f"        Done when:  {it['acceptance']['description']}")
        for ch in it["acceptance"]["checks"]:
            L.append(f"        Checked by: {describe_check(ch, plan)}")
    L += ["", f"Project: {plan['project']['path']}  (work happens in a {'separate git branch/worktree' if plan['project']['mode'] == 'worktree' else 'copy'}; your files are not edited)"]
    L.append("Commands that will run (nothing else will):")
    for c in plan["commands"]:
        L.append(f"  - {c['id']}: {_argv(c['argv'])}   ({c['purpose']}; timeout {c['timeout_s']}s; network: {c['network']}; "
                 f"{'macOS sandbox' if c['sandbox'] else 'NO sandbox'})")
    if plan.get("service"):
        s = plan["service"]
        L.append(f"Local app for journeys: {_argv(s['argv'])}  (localhost only; ready when {s['ready_path']} answers)")
    if plan["personas"]:
        L.append("Customer personas (rendered k6 scripts are in personas/ — read them before approving):")
        for p in plan["personas"]:
            L.append(f"  - {p['id']} ({p['kind']}): {p['name']} — {p.get('description', '')}".rstrip(" —"))
            for s in p["steps"]:
                L.append(f"      · {s['label']}" + (f" → expect HTTP {s['expect_status']}" + (f" with body containing {s['contains']}" if s.get("contains") else "")
                                                       if s["action"] == "request" else ""))
            L.append(f"      limits: at most {p['max_requests']} requests, each under {p['max_request_ms']} ms")
    for lo in plan["load"]:
        L.append(f"Light load: `{lo['id']}` runs persona {lo['persona_id']} with {lo['vus']} users for {lo['duration_s']}s; "
                 f"pass if p95 < {lo['p95_ms']} ms and errors < {lo['max_error_rate'] * 100:.0f}%")
    if plan.get("optimizer"):
        o = plan["optimizer"]
        m = o["metric"]
        L.append(f"Optimizer goal: {o['goal']}")
        L.append(f"  measured by: `{m['command_id']}`" + (f" output `{m['regex']}`" if m.get("regex") else " run time")
                 + f", {m['direction']} is better, median of {o['runs']} runs; kept only if ≥{o['min_improvement_pct']}% better AND every check still passes; otherwise rolled back")
    sec = plan.get("security", {})
    L.append(f"Security checks: {'on' if sec.get('enabled', True) else 'off'} (diff scan for secrets/dangerous calls"
             + (", error-page probe of the local app" if sec.get("probe_service", True) and plan.get("service") else "") + ")")
    lim = plan["limits"]
    L.append(f"Limits: ${lim['max_cost_usd']:.2f} API spend, {lim['max_model_calls']} model calls, {lim['max_wall_s'] // 60} min, "
             f"{lim['max_builder_rounds']} builder rounds, {lim['max_optimizer_attempts']} optimizer attempts")
    if plan.get("data_notice"):
        L.append(f"Data: {plan['data_notice']}")
    if plan.get("open_questions"):
        L.append("Open questions (answer by editing plan.json before approving):")
        L += [f"  ? {q}" for q in plan["open_questions"]]
    if run_dir is not None:
        L += ["", f"Plan fingerprint: {fingerprint(run_dir, plan)[7:19]}"]
    return "\n".join(L)


def load_json_file(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
