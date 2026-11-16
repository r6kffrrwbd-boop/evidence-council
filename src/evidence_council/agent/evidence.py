"""Evidence checker: runs only the approved commands and records what actually happened.

For every approved command it records the exact argv, exit code, status
(``passed`` / ``failed`` / ``hung`` / ``error``), wall time, the last part of stdout and
stderr (secrets redacted), the individual test results it can parse (unittest and
pytest verbose output), and whether the macOS sandbox was applied.

A command that does not finish within its timeout is reported as ``hung`` and its whole
process group is killed. Commands run with ``shell=False`` in the workspace, with a minimal
environment that carries no API keys, and — on macOS, unless the approved plan says
otherwise — inside ``sandbox-exec`` with network denied (or limited to localhost) and writes
limited to the workspace and the run's temp folder.

``compare`` turns a baseline and a later evidence round into regressions: a command or
test that passed before and does not now, or fewer tests running than before.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..util import now_iso, redact_secrets

UNITTEST_LINE = re.compile(r"^(test\w*) \(([\w.]+)\)(?:\n.*)?\s\.\.\.\s(ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)\s*$", re.M)
UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
PYTEST_LINE = re.compile(r"^([\w/.\-]+::[\w\[\]\-.:]+) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)", re.M)
PYTEST_SUMMARY = re.compile(r"=+ (?:(\d+) failed)?(?:, )?(?:(\d+) passed)?.* in [\d.]+s")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sandbox_profile(writable: list[str], network: str) -> str:
    home = str(Path.home().resolve())
    allow_reads = {str(Path(p).resolve()) for p in (*writable, sys.prefix, sys.base_prefix, sys.exec_prefix)}
    reads = " ".join(f'(subpath "{p}")' for p in sorted(allow_reads))
    writes = " ".join(f'(subpath "{Path(p).resolve()}")' for p in writable)
    net = "(deny network*)"
    if network == "localhost":
        net += ('(allow network-outbound (remote ip "localhost:*"))'
                '(allow network-inbound (local ip "localhost:*"))'
                '(allow network-bind (local ip "localhost:*"))')
    return ("(version 1)(allow default)" + net
            + '(deny file-write* (subpath "/"))'
            + f'(allow file-write* {writes} (literal "/dev/null") (literal "/dev/tty"))'
            + f'(deny file-read* (subpath "{home}"))'
            + f"(allow file-read* {reads})"
            + "(allow file-read-metadata)")   # stat/realpath on parent folders only, never contents


def command_env(home: Path, tmp: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment: no API keys or tokens from your shell reach approved commands."""
    path = os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")
    env = {"PATH": path, "HOME": str(home), "TMPDIR": str(tmp), "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONUNBUFFERED": "1", "NO_COLOR": "1", "EVIDENCE_COUNCIL_ACTIVE": "1", "CI": "1"}
    env.update(extra or {})
    return env


def _wrap(argv: list[str], use_sandbox: bool, writable: list[str], network: str) -> tuple[list[str], bool]:
    if use_sandbox and sys.platform == "darwin" and shutil.which("sandbox-exec") and os.environ.get("EC_DISABLE_SANDBOX") != "1":
        return ["sandbox-exec", "-p", sandbox_profile(writable, network)] + argv, True
    return argv, False


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        for _ in range(10):
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def parse_tests(text: str) -> dict[str, Any]:
    names: dict[str, str] = {}
    for m in UNITTEST_LINE.finditer(text):
        st = m.group(3)
        where = m.group(2) if m.group(2).endswith("." + m.group(1)) else f"{m.group(2)}.{m.group(1)}"
        names[where] = "ok" if st in ("ok", "expected failure") or st.startswith("skipped") else st
    for m in PYTEST_LINE.finditer(text):
        names[m.group(1)] = {"PASSED": "ok", "SKIPPED": "ok", "XFAIL": "ok"}.get(m.group(2), m.group(2))
    ran = None
    m = UNITTEST_RAN.search(text)
    if m:
        ran = int(m.group(1))
    elif names:
        ran = len(names)
    out: dict[str, Any] = {}
    if ran is not None:
        out = {"ran": ran, "failed": sum(1 for v in names.values() if v != "ok"), "names": names}
    return out


def run_command(cmd: dict[str, Any], ws_root: Path, run_tmp: Path, port: int | None = None,
                timeout_cap: int | None = None) -> dict[str, Any]:
    argv = [a.replace("{port}", str(port)) if port else a for a in cmd["argv"]]
    timeout = int(min(cmd.get("timeout_s", 120), timeout_cap or 10 ** 6))
    tmp = run_tmp / f"cmd-{cmd['id']}"
    tmp.mkdir(parents=True, exist_ok=True)
    env = command_env(tmp, tmp, {"EC_PORT": str(port)} if port else None)
    full, sandboxed = _wrap(argv, cmd.get("sandbox", True), [str(ws_root), str(tmp)], cmd.get("network", "none"))
    rec: dict[str, Any] = {"id": cmd["id"], "kind": cmd.get("kind", "test"), "argv": argv, "started_at": now_iso(),
                           "sandboxed": sandboxed, "network": cmd.get("network", "none"), "timeout_s": timeout}
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(full, cwd=str(ws_root), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    except (FileNotFoundError, PermissionError) as e:
        rec.update(status="error", exit_code=None, duration_s=0.0, stdout_tail="", stderr_tail=str(e),
                   failure_excerpt=f"could not start: {e}")
        return rec
    try:
        out, err = proc.communicate(timeout=timeout)
        hung = False
    except subprocess.TimeoutExpired:
        _kill(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        hung = True
    dur = time.monotonic() - t0
    out = redact_secrets(out or "")[0]
    err = redact_secrets(err or "")[0]
    rec["duration_s"] = round(dur, 3)
    rec["exit_code"] = None if hung else proc.returncode
    rec["stdout_tail"] = out[-6000:]
    rec["stderr_tail"] = err[-6000:]
    rec["tests"] = parse_tests(out + "\n" + err)
    if hung:
        rec["status"] = "hung"
        rec["failure_excerpt"] = f"did not finish within {timeout}s; process group was killed (possible hang or infinite loop)"
    else:
        rec["status"] = "passed" if proc.returncode == 0 else "failed"
        if proc.returncode != 0:
            rec["failure_excerpt"] = _excerpt(out + "\n" + err)
    if sandboxed and "Operation not permitted" in err and proc.returncode not in (0, None):
        rec["sandbox_note"] = ("the macOS sandbox may have blocked this command (it denies network and reads from your home folder "
                               "outside the workspace); if that is the cause, set \"sandbox\": false for it in plan.json and approve again")
    return rec


def _excerpt(text: str, n: int = 12) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    key = [i for i, ln in enumerate(lines) if re.search(r"(FAIL|ERROR|Error|Traceback|assert|Exception|failed)", ln)]
    if key:
        start = max(0, key[0] - 1)
        return "\n".join(lines[start:start + n])
    return "\n".join(lines[-n:])


def check_expectation(result: dict[str, Any] | None, check: dict[str, Any]) -> tuple[str, str]:
    """Apply one 'command' acceptance check to a command result -> (passed|failed|not_run, detail)."""
    if not result:
        return "not_run", "command did not run"
    if result["status"] in ("hung", "error"):
        return "failed", result.get("failure_excerpt", result["status"])
    want = int(check.get("expect_exit", 0))
    if result["exit_code"] != want:
        return "failed", f"exit code {result['exit_code']}, expected {want}"
    pat = check.get("expect_output")
    if pat:
        text = result.get("stdout_tail", "") + "\n" + result.get("stderr_tail", "")
        m = re.search(pat, text, re.M)
        if not m:
            return "failed", f"output did not match `{pat}`"
        return "passed", f"exit {want}; output matched: {m.group(0)[:160]!r}"
    return "passed", f"exit {want}"


class Service:
    """The approved local app, started for journeys/probes and always stopped afterwards."""

    def __init__(self, spec: dict[str, Any], ws_root: Path, run_tmp: Path, log_path: Path):
        self.spec = spec
        self.ws_root = ws_root
        self.run_tmp = run_tmp
        self.log_path = log_path
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        self.sandboxed = False
        self.startup_s: float | None = None
        self.error = ""

    def start(self, attempts: int = 3) -> bool:
        """Start the app and wait until ready_path answers. The macOS sandbox occasionally refuses a port bind
        (seen ~1 in 60 binds on macOS 14); in that case retry on a fresh port."""
        for n in range(attempts):
            if n:
                self.port = free_port()
                self.base_url = f"http://127.0.0.1:{self.port}"
            if self._start_once():
                return True
            log = self.log_path.read_text(encoding="utf-8", errors="replace")[-2000:] if self.log_path.exists() else ""
            if not ("Operation not permitted" in log or "Address already in use" in log):
                return False
            self.stop()
        return False

    def _start_once(self) -> bool:
        argv = [a.replace("{port}", str(self.port)) for a in self.spec["argv"]]
        tmp = self.run_tmp / "service"
        tmp.mkdir(parents=True, exist_ok=True)
        full, self.sandboxed = _wrap(argv, self.spec.get("sandbox", True), [str(self.ws_root), str(tmp)], "localhost")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = self.log_path.open("a", encoding="utf-8")
        t0 = time.monotonic()
        try:
            self.proc = subprocess.Popen(full, cwd=str(self.ws_root), env=command_env(tmp, tmp, {"EC_PORT": str(self.port), "PORT": str(self.port)}),
                                         stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self.error = f"could not start service: {e}"
            return False
        deadline = t0 + self.spec.get("startup_timeout_s", 20)
        url = self.base_url + self.spec.get("ready_path", "/")
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.error = f"service exited with code {self.proc.returncode} before becoming ready (see {self.log_path.name})"
                return False
            try:
                with urllib.request.urlopen(url, timeout=1) as r:
                    if r.status < 500:
                        self.startup_s = round(time.monotonic() - t0, 2)
                        return True
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    self.startup_s = round(time.monotonic() - t0, 2)
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        self.error = f"service did not answer {self.spec.get('ready_path', '/')} within {self.spec.get('startup_timeout_s', 20)}s"
        return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            _kill(self.proc)


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Regressions between two evidence rounds (command results keyed by id)."""
    out = []
    b, c = baseline.get("commands", {}), current.get("commands", {})
    for cid, br in b.items():
        cr = c.get(cid)
        if not cr:
            continue
        if br["status"] == "passed" and cr["status"] != "passed":
            out.append(f"regression: command {cid} passed before the change and is now {cr['status']}")
        bt, ct = br.get("tests", {}), cr.get("tests", {})
        if bt.get("names") and ct.get("names") is not None:
            for name, st in bt["names"].items():
                now = ct["names"].get(name)
                if st == "ok" and now not in (None, "ok"):
                    out.append(f"regression: test {name} passed before and now reports {now}")
                elif st == "ok" and now is None and ct.get("ran") is not None:
                    out.append(f"regression: test {name} passed before and no longer runs (removed or renamed?)")
        if bt.get("ran") is not None and ct.get("ran") is not None and ct["ran"] < bt["ran"]:
            out.append(f"regression: command {cid} ran {ct['ran']} tests, fewer than the {bt['ran']} before the change")
    for jid, bj in baseline.get("journeys", {}).items():
        cj = current.get("journeys", {}).get(jid)
        if cj and bj["status"] == "passed" and cj["status"] != "passed":
            out.append(f"regression: journey {jid} passed before and is now {cj['status']}")
    return out
