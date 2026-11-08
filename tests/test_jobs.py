"""Background jobs: review --async, then jobs list/status/result/cancel/remove (real detached processes, mock providers)."""

import json
import os
import stat
import time

from conftest import mock, run_cli, task, write

CHECK = [{"id": "r", "type": "numeric_reference", "expression": "2+2"}]


def _setup(tmp_path, delay):
    cfg = write(tmp_path, "c.json", {"providers": [mock("slow", "4", delay_s=delay, timeout_s=120), mock("fast", "5")]})
    t = write(tmp_path, "t.json", task("2+2?", fmt="number", checks=CHECK))
    home = str(tmp_path / "home")
    return cfg, t, home


def _submit(tmp_path, delay):
    cfg, t, home = _setup(tmp_path, delay)
    rc, out, err = run_cli("review", t, "--async", "--config", cfg, "--home", home)
    assert rc == 0, err
    info = json.loads(out)
    assert info["status"] == "running" and info["job_id"].startswith("job-")
    return info["job_id"], home


def _wait(home, job_id, want, timeout=30):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        st = json.loads(run_cli("jobs", "status", job_id, "--home", home)[1])
        if st["status"] in want:
            return st
        time.sleep(0.3)
    raise AssertionError(f"job did not reach {want}: {st}")


def test_async_review_completes_and_result_is_fetched(tmp_path):
    t0 = time.monotonic()
    job, home = _submit(tmp_path, delay=2)
    assert time.monotonic() - t0 < 15                     # returned without waiting for providers
    rc, _, err = run_cli("jobs", "result", job, "--home", home)
    assert rc == 8 and "still running" in err and "hint:" in err
    st = _wait(home, job, {"completed"})
    assert st["verdict_status"] == "supported" and st["exit_code"] == 0
    rc, out, _ = run_cli("jobs", "result", job, "--home", home)
    res = json.loads(out)
    assert rc == 0 and res["verdict"]["selected_answer"] == "4" and sorted(res["providers"]["used"]) == ["fast", "slow"]
    rc, md, _ = run_cli("jobs", "result", job, "--home", home, "--explain")
    assert "## Council verdict: supported" in md
    copy = tmp_path / "copy.json"
    assert run_cli("jobs", "result", job, "--home", home, "-o", str(copy))[0] == 0 and json.loads(copy.read_text())["run_id"] == res["run_id"]
    jd = os.path.join(home, "jobs", job)
    assert stat.S_IMODE(os.stat(jd).st_mode) == 0o700     # task copy is private to the owner
    listing = json.loads(run_cli("jobs", "list", "--home", home)[1])["jobs"]
    assert [j["job_id"] for j in listing] == [job]


def test_cancel_keeps_partial_result(tmp_path):
    job, home = _submit(tmp_path, delay=60)
    _wait(home, job, {"running"})
    time.sleep(1.0)                                         # let the runner reach the provider calls
    rc, out, _ = run_cli("jobs", "cancel", job, "--home", home)
    assert rc == 0 and json.loads(out)["cancel_requested"] is True
    st = _wait(home, job, {"cancelled"}, timeout=20)
    assert st["status"] == "cancelled"
    rc, out, err = run_cli("jobs", "result", job, "--home", home)
    assert rc == 0 and "partial result" in err
    res = json.loads(out)
    assert {a["provider"]: a["status"] for a in res["providers"]["attempted"]}["slow"] == "cancelled"
    assert any("cancelled" in w for w in res["warnings"])
    rc, _, err = run_cli("jobs", "cancel", job, "--home", home)
    assert rc == 2 and "not running" in err


def test_remove_and_errors(tmp_path):
    job, home = _submit(tmp_path, delay=0)
    _wait(home, job, {"completed"})
    assert json.loads(run_cli("jobs", "remove", job, "--home", home)[1])["removed"] is True
    rc, _, err = run_cli("jobs", "status", job, "--home", home)
    assert rc == 2 and "no such job" in err and "jobs list" in err
    rc, _, err = run_cli("jobs", "status", "../../etc", "--home", home)
    assert rc == 2 and "not a job id" in err
    rc, _, err = run_cli("jobs", "--home", home)
    assert rc == 2 and "missing jobs action" in err


def test_async_validates_input_before_starting(tmp_path):
    cfg, _, home = _setup(tmp_path, 0)
    bad = write(tmp_path, "bad.json", {"schema_version": "1.0", "task": {}})
    rc, _, err = run_cli("review", bad, "--async", "--config", cfg, "--home", home)
    assert rc == 2 and "'prompt' is a required property" in err
    assert not os.path.exists(os.path.join(home, "jobs"))
    rc, _, _ = run_cli("review", bad, "--async", "--config", cfg, "--home", home, env_extra={"EVIDENCE_COUNCIL_ACTIVE": "1"})
    assert rc == 5
