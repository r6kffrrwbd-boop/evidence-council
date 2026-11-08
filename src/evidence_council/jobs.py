"""Background council jobs: ``review --async``, then ``jobs list|status|result|cancel|remove``.

A job is a directory ``<home>/jobs/<job-id>/`` (mode 0700) holding:

* ``task.json``    — copy of the input (same sensitivity as the input)
* ``meta.json``    — job id, pid, created_at, run options (providers, local_only, config path)
* ``log.txt``      — the runner's stderr (secrets redacted by the normal logging path)
* ``result.json``  — the council output, written when the run ends (partial if cancelled)
* ``status.json``  — final status, exit code and verdict status, written last
* ``cancel``       — marker created by ``jobs cancel``

The runner is a detached ``evidence-council`` process in its own session, so it
survives the calling shell. Cancelling sends SIGINT (providers are cancelled cleanly and a
partial result is written), then SIGTERM after a grace period.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import CouncilError, EXIT_USAGE
from .util import now_iso, write_json

RUNNING, COMPLETED, FAILED, CANCELLED = "running", "completed", "failed", "cancelled"


def jobs_dir(home: Path) -> Path:
    return Path(home) / "jobs"


def _job_path(home: Path, job_id: str) -> Path:
    if not job_id.startswith("job-") or "/" in job_id or ".." in job_id:
        raise CouncilError(f"not a job id: {job_id!r}", hint="Job ids look like job-20261108T201500Z-1a2b3c4d; see `evidence-council jobs list`.")
    p = jobs_dir(home) / job_id
    if not p.is_dir():
        raise CouncilError(f"no such job: {job_id}", hint="List jobs with `evidence-council jobs list` (jobs live under --home).")
    return p


def submit(input_doc: dict, *, home: Path, config_path: str | None, providers: list[str] | None,
           local_only: bool, allow_private_to: list[str]) -> dict[str, Any]:
    """Start a detached review. The input must already be validated by the caller."""
    job_id = "job-" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    d = jobs_dir(home) / job_id
    d.mkdir(parents=True, mode=0o700)
    os.chmod(jobs_dir(home), 0o700)
    write_json(input_doc, d / "task.json")
    meta = {"job_id": job_id, "created_at": now_iso(), "task_id": input_doc.get("task", {}).get("id"),
            "config": str(Path(config_path).resolve()) if config_path else None, "home": str(Path(home).resolve()),
            "providers": providers, "local_only": local_only, "allow_private_to": allow_private_to, "pid": None}
    log = open(d / "log.txt", "ab")
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.Popen([sys.executable, "-m", "evidence_council", "_run-job", str(d)],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, env=env)
    meta["pid"] = proc.pid
    write_json(meta, d / "meta.json")
    return {"job_id": job_id, "status": RUNNING, "pid": proc.pid, "job_dir": str(d),
            "next": f"evidence-council jobs result {job_id}"}


def run_job(job_dir: str) -> int:
    """Entry point of the detached runner (hidden `_run-job` subcommand)."""
    from .config import load_config
    from .council import run_review
    from .providers import CancelToken

    d = Path(job_dir)
    meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else None
    for _ in range(50):  # submit() writes meta.json just after spawning us
        if meta:
            break
        time.sleep(0.1)
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else None
    tok = CancelToken()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: tok.set())
    code, verdict_status, error = 0, None, ""
    try:
        cfg = load_config(meta["config"], meta["home"])
        cfg.extra_allow_private_to = list(meta.get("allow_private_to") or [])
        res = run_review(json.loads((d / "task.json").read_text()), cfg, requested=meta.get("providers"),
                         local_only=bool(meta.get("local_only")), cancel=tok)
        write_json(res, d / "result.json")
        verdict_status = res["verdict"]["status"]
    except CouncilError as e:
        code, error = e.exit_code, f"{e.message} (hint: {e.hint})"
    except Exception as e:  # pragma: no cover - bug path
        code, error = 1, f"{type(e).__name__}: {e}"
    status = CANCELLED if (d / "cancel").exists() or tok.is_set() else (COMPLETED if code == 0 else FAILED)
    write_json({"status": status, "exit_code": code, "finished_at": now_iso(), "verdict_status": verdict_status,
                "error": error}, d / "status.json")
    return code


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def status(home: Path, job_id: str) -> dict[str, Any]:
    d = _job_path(home, job_id)
    meta = json.loads((d / "meta.json").read_text())
    out = {"job_id": job_id, "created_at": meta["created_at"], "task_id": meta.get("task_id"), "pid": meta.get("pid")}
    if (d / "status.json").exists():
        out.update(json.loads((d / "status.json").read_text()))
        out["result"] = str(d / "result.json") if (d / "result.json").exists() else None
    elif _alive(meta.get("pid")):
        out["status"] = RUNNING
        created = _dt.datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
        out["elapsed_s"] = round((_dt.datetime.now(_dt.timezone.utc) - created).total_seconds())
        out["cancel_requested"] = (d / "cancel").exists()
    else:
        out["status"] = FAILED
        out["error"] = "runner exited without writing a status (killed or crashed); see the log"
    return out


def list_jobs(home: Path) -> list[dict[str, Any]]:
    root = jobs_dir(home)
    if not root.exists():
        return []
    return [status(home, p.name) for p in sorted(root.iterdir(), reverse=True) if p.is_dir() and p.name.startswith("job-")]


def log_tail(home: Path, job_id: str, n: int = 15) -> str:
    p = _job_path(home, job_id) / "log.txt"
    return "\n".join(p.read_text(errors="replace").splitlines()[-n:]) if p.exists() else ""


def cancel(home: Path, job_id: str, grace_s: float = 10.0) -> dict[str, Any]:
    d = _job_path(home, job_id)
    st = status(home, job_id)
    if st["status"] != RUNNING:
        raise CouncilError(f"job {job_id} is not running (status: {st['status']})", hint="Only running jobs can be cancelled.", exit_code=EXIT_USAGE)
    (d / "cancel").touch()
    pid = st["pid"]
    try:
        os.killpg(pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    t_end = time.monotonic() + grace_s
    while time.monotonic() < t_end and not (d / "status.json").exists():
        time.sleep(0.2)
    if not (d / "status.json").exists():
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return {"job_id": job_id, "cancel_requested": True, "stopped": (d / "status.json").exists()}


def remove(home: Path, job_id: str) -> dict[str, Any]:
    d = _job_path(home, job_id)
    if status(home, job_id)["status"] == RUNNING:
        raise CouncilError(f"job {job_id} is still running", hint=f"Cancel it first: evidence-council jobs cancel {job_id}", exit_code=EXIT_USAGE)
    shutil.rmtree(d)
    return {"job_id": job_id, "removed": True}
