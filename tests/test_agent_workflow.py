"""End-to-end agent workflow on the synthetic tiny-shop project, driven by the SCRIPTED test model.

These tests run the real pipeline (workspace copy/worktree, approved commands in the macOS
sandbox, local app, security scan, optimizer with rollback, report, lessons). The model turns
are a hand-written fixture (examples/agent-demo/scripted-run.json), so they test the workflow,
not AI quality. Tests marked ``k6`` also run real k6 browser/HTTP/load journeys and are
skipped when k6 is not installed.
"""

import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evidence_council.agent import journeys as J
from evidence_council.agent import plan as P
from evidence_council.agent import runner
from evidence_council.agent.events import Reporter
from evidence_council.agent.models import GeminiModel, ScriptedModel
from evidence_council.config import DEFAULT_CONFIG
from evidence_council.errors import CouncilError

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "agent-demo"
REQUEST = (DEMO / "request.txt").read_text().strip()
SCRIPT = json.loads((DEMO / "scripted-run.json").read_text())


def tree_hash(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(str(p.relative_to(d)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def run_demo(tmp_path, monkeypatch, setup_changes=None, overrides=None, script=None, k6=False, project=None):
    if not k6:
        monkeypatch.setattr(J, "k6_available", lambda: None)
    project = project or (tmp_path / "tiny-shop")
    if not project.exists():
        shutil.copytree(DEMO / "tiny-shop", project, ignore=shutil.ignore_patterns("__pycache__"))
    setup = json.loads((DEMO / "setup.json").read_text())
    setup.update(setup_changes or {})
    (tmp_path / "setup.json").write_text(json.dumps(setup))
    home = tmp_path / "home"
    out = io.StringIO()
    model = ScriptedModel(script or SCRIPT)
    run_dir, plan = runner.intake(REQUEST, project, model, home, Reporter(stream=out), str(tmp_path / "setup.json"))
    P.approve(run_dir, P.load_plan(run_dir), "test")
    report = runner.run(run_dir, model, home, Reporter(stream=out), overrides)
    return report, run_dir, out.getvalue(), model


def test_full_workflow_without_k6(tmp_path, monkeypatch):
    before = tree_hash(DEMO / "tiny-shop")
    report, run_dir, out, model = run_demo(tmp_path, monkeypatch)
    assert report["outcome"] == "completed", report.get("stop_reason")
    status = {r["id"]: r["status"] for r in report["requirements"]}
    assert status == {"R1": "verified", "R2": "untested", "R3": "ai_judgment"}
    r1 = next(r for r in report["requirements"] if r["id"] == "R1")
    assert all(c["baseline_status"] == "failed" and c["status"] == "passed" for c in r1["checks"])
    # visible progress: phases, tool calls, test results, and why work went back
    for needle in ["━━ Baseline", "━━ Builder · round 1/3", "Builder → replace_in_file(path=shop.py", "✗ test unit-tests: failed",
                   "↺ Returned to Builder for another pass because:", "Critic (major): Exactly 10 units", "━━ Optimizer",
                   "rollback", "kept optimizer change", "━━ Final evaluation"]:
        assert needle in out, needle
    assert len(report["rounds"]) == 2 and report["rounds"][0]["returned"] and not report["rounds"][1]["returned"]
    opt = report["optimizer"]
    assert [a["kept"] for a in opt["attempts"]] == [False, True]
    assert "rolled back" in opt["attempts"][0]["result"] and opt["final_value"] < opt["start_value"]
    # the workspace holds the kept optimisation, not the rolled-back one
    final_shop = (run_dir / "workspace" / "shop.py").read_text()
    assert "lru_cache" in final_shop and "_LAST_PRICE" not in final_shop
    # your project is untouched; the patch applies cleanly to a fresh copy
    assert tree_hash(DEMO / "tiny-shop") == before
    copy = tmp_path / "apply-check"
    shutil.copytree(DEMO / "tiny-shop", copy, ignore=shutil.ignore_patterns("__pycache__"))
    r = subprocess.run(["git", "apply", "--check", str(run_dir / "changes.patch")], cwd=copy, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # report separates verified results, AI judgments and untested claims
    md = (run_dir / "report.md").read_text()
    for h in ["## Verified by running checks", "## AI judgments", "## Not tested", "## The Builder's own claims (not independently verified)"]:
        assert h in md
    # lessons only from verified outcomes
    kinds = sorted(x["statement"].split(",")[1].strip()[:11] for x in report["lessons"]["retained"])
    assert len(report["lessons"]["retained"]) == 2 and all("R2" not in x["statement"] for x in report["lessons"]["retained"])
    assert kinds
    # provider state was replayed unchanged (the scripted model raises otherwise) and never logged
    events = (run_dir / "events.jsonl").read_text()
    assert "sig-builder" not in events and "sig-builder" not in out
    assert report["budget"]["model_calls"] == 14 and report["budget"]["spent_usd"] > 0


@pytest.mark.skipif(not shutil.which("k6"), reason="k6 not installed")
def test_full_workflow_with_real_k6_journeys(tmp_path, monkeypatch):
    report, run_dir, out, _ = run_demo(tmp_path, monkeypatch, k6=True)
    assert report["outcome"] == "completed"
    status = {r["id"]: r["status"] for r in report["requirements"]}
    assert status["R1"] == "verified" and status["R2"] == "verified"
    r2 = next(r for r in report["requirements"] if r["id"] == "R2")
    assert r2["checks"][0]["baseline_status"] == "failed"        # Maya did not see the discount before the change
    assert report["load"]["checkout-load"]["status"] == "passed" and report["load"]["checkout-load"]["requests"] > 100
    base = json.loads((run_dir / "evidence" / "baseline.json").read_text())
    assert any("10% off" in r for r in base["journeys"]["maya-bulk-buyer"]["reasons"])


def test_spend_limit_stops_the_run_and_still_writes_a_report(tmp_path, monkeypatch):
    report, run_dir, out, _ = run_demo(tmp_path, monkeypatch, overrides={"max_cost_usd": 0.004})
    assert report["outcome"] == "stopped" and "spend limit" in report["stop_reason"]
    assert (run_dir / "report.md").exists() and "limit reached" in out
    assert report["budget"]["spent_usd"] <= 0.004


def test_builder_stuck_in_repeated_calls_is_stopped(tmp_path, monkeypatch):
    script = json.loads(json.dumps(SCRIPT))
    script["builder"] = [{"text": "", "tool_calls": [{"name": "read_file", "args": {"path": "shop.py"}}]} for _ in range(6)]
    report, run_dir, out, _ = run_demo(tmp_path, monkeypatch, script=script)
    assert report["repeated_work"] and "repeated work: Builder called read_file" in out
    assert report["rounds"][0].get("stuck") and "no further changes" in report["stop_reason"]
    assert report["counts"]["failed"] >= 1 and report["lessons"]["retained"] == []


def test_worktree_mode_creates_a_branch_and_never_commits(tmp_path, monkeypatch):
    proj = tmp_path / "tiny-shop"
    shutil.copytree(DEMO / "tiny-shop", proj, ignore=shutil.ignore_patterns("__pycache__"))
    git = lambda *a: subprocess.run(["git", "-C", str(proj), *a], capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q")
    git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "-A")
    git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "init")
    head = git("rev-parse", "HEAD")
    branch = git("branch", "--show-current")
    report, run_dir, out, _ = run_demo(tmp_path, monkeypatch, setup_changes={"project_mode": "worktree"}, project=proj)
    assert report["outcome"] == "completed" and report["mode"] == "worktree"
    assert git("rev-parse", "HEAD") == head and git("branch", "--show-current") == branch
    agent_branch = f"ec-agent/{run_dir.name}"
    assert git("rev-parse", agent_branch) == head                  # branch exists, no commits added
    assert git("status", "--porcelain") == ""                       # your checkout untouched
    wt = subprocess.run(["git", "-C", str(run_dir / "workspace"), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert "shop.py" in wt                                           # changes are uncommitted in the worktree


def test_run_refuses_a_different_provider_than_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(J, "k6_available", lambda: None)
    shutil.copytree(DEMO / "tiny-shop", tmp_path / "p", ignore=shutil.ignore_patterns("__pycache__"))
    run_dir, _ = runner.intake(REQUEST, tmp_path / "p", ScriptedModel(SCRIPT), tmp_path / "h", Reporter(stream=io.StringIO()),
                               str(DEMO / "setup.json"))
    P.approve(run_dir, P.load_plan(run_dir), "test")
    gem = GeminiModel(DEFAULT_CONFIG["agent"]["providers"]["gemini"], DEFAULT_CONFIG["agent"]["pricing"], {})
    with pytest.raises(CouncilError, match="approved for provider 'scripted'"):
        runner.run(run_dir, gem, tmp_path / "h", Reporter(stream=io.StringIO()))
    assert not (run_dir / "workspace").exists()


def test_cli_end_to_end_intake_show_approve_run_report(tmp_path, monkeypatch, capsys):
    from evidence_council.cli import main
    monkeypatch.setattr(J, "k6_available", lambda: None)
    monkeypatch.setenv("EVIDENCE_COUNCIL_HOME", str(tmp_path / "home"))
    shutil.copytree(DEMO / "tiny-shop", tmp_path / "p", ignore=shutil.ignore_patterns("__pycache__"))
    script = str(DEMO / "scripted-run.json")
    assert main(["agent", "intake", "--project", str(tmp_path / "p"), "--setup", str(DEMO / "setup.json"), "--script", script, REQUEST]) == 0
    out = capsys.readouterr().out
    run_dir = out.split("run folder: ")[1].strip()
    fp = out.split("Plan fingerprint: ")[1].split()[0]
    assert "Nothing has been changed and no command has run" in out
    assert main(["agent", "run", run_dir, "--script", script]) == 2               # not approved yet
    assert "not been approved" in capsys.readouterr().err
    assert main(["agent", "approve", run_dir, "--confirm", fp]) == 0
    assert main(["agent", "run", run_dir, "--script", script]) == 0
    capsys.readouterr()
    assert main(["agent", "report", run_dir, "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["counts"]["verified"] == 1 and rep["provider"]["provider"] == "scripted"


@pytest.mark.skipif(not shutil.which("k6"), reason="k6 not installed")
def test_real_k6_detects_repeated_requests_and_a_hanging_endpoint(tmp_path):
    """A page that fetches the same API 25 times, and an API call that takes 4 s, against personas allowing 5 requests / 1 s."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    page = ("<html><body><p id='n'>0</p><script>(async () => { for (let i = 0; i < 25; i++) {"
            " await fetch('/api/price'); document.getElementById('n').textContent = String(i + 1); } })();</script></body></html>")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/slow"):
                time.sleep(4)
            body = page.encode() if self.path == "/" else b'{"price": 1}'
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if self.path == "/" else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        loop = {"id": "looping-page", "name": "visitor", "kind": "browser", "max_requests": 5, "max_request_ms": 3000,
                "steps": [{"action": "goto", "path": "/", "label": "open", "timeout_ms": 10000},
                          {"action": "expect_text", "selector": "#n", "contains": "25", "label": "wait for loop", "timeout_ms": 10000}]}
        slow = {"id": "slow-api", "name": "widget", "kind": "http", "max_requests": 1, "max_request_ms": 1000,
                "steps": [{"action": "request", "method": "GET", "path": "/slow", "label": "slow call", "timeout_ms": 10000, "expect_status": 200}]}
        results = {}
        for p in (loop, slow):
            script = tmp_path / f"{p['id']}.js"
            script.write_text(J.render_persona(p))
            raw = J.run_k6(script, base, tmp_path / "k6", 120)
            results[p["id"]] = J.judge_journey(p, raw)
    finally:
        srv.shutdown()
    st, reasons = results["looping-page"]
    assert st == "failed" and any("unintended repeated work" in r for r in reasons), reasons
    st, reasons = results["slow-api"]
    assert st == "failed" and any("slow or hanging request" in r for r in reasons), reasons
