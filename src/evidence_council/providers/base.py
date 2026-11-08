"""Common provider-adapter interface.

Every adapter implements two methods:

* ``check()``  — cheap availability test that never sends a prompt (binary on PATH,
  local server reachable, API-key env var present).
* ``_call(prompt, timeout_s, cancel)`` — one attempt; returns :class:`RawResponse`
  or raises :class:`ProviderError`.

:meth:`Adapter.generate` wraps ``_call`` with timing, bounded retries for transient
failures, cancellation and error classification, and never raises: an unavailable
or failing provider yields a :class:`CallResult` with a non-``ok`` status so the
council can continue with the others.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

from ..util import redact_secrets

Locality = Literal["local", "cloud"]
Status = Literal["ok", "unavailable", "timeout", "error", "cancelled", "parse_error"]

RECURSION_ENV = "EVIDENCE_COUNCIL_ACTIVE"


class CancelToken(threading.Event):
    """Set it to cancel all in-flight provider calls (SIGINT does this in the CLI)."""


class ProviderError(Exception):
    def __init__(self, message: str, status: Status = "error", transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient


@dataclass
class Availability:
    available: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawResponse:
    text: str
    tokens: dict[str, int] | None = None
    cost_usd: float | None = None
    cost_note: str = ""


@dataclass
class CallResult:
    provider: str
    adapter: str
    locality: Locality
    status: Status
    text: str = ""
    attempts: int = 0
    duration_s: float = 0.0
    tokens: dict[str, int] | None = None
    cost_usd: float | None = None
    cost_note: str = ""
    error: str = ""

    def to_attempt(self, role: str = "proposer") -> dict[str, Any]:
        d = asdict(self)
        d.pop("text")
        d["role"] = role
        d["duration_s"] = round(self.duration_s, 2)
        if not d["error"]:
            d.pop("error")
        return d


class Adapter:
    adapter_type = "base"
    default_locality: Locality = "cloud"

    def __init__(self, spec: dict[str, Any], defaults: dict[str, Any]):
        self.spec = spec
        self.name: str = spec["name"]
        self.locality: Locality = spec.get("locality", self.default_locality)
        self.timeout_s: float = float(spec.get("timeout_s", defaults.get("timeout_s", 240)))
        self.retries: int = int(spec.get("retries", defaults.get("retries", 1)))
        self.model: str = spec.get("model", "")

    # -- to implement -------------------------------------------------
    def check(self) -> Availability:  # pragma: no cover - overridden
        raise NotImplementedError

    def _call(self, prompt: str, timeout_s: float, cancel: CancelToken) -> RawResponse:  # pragma: no cover
        raise NotImplementedError

    # -- shared behaviour ----------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "adapter": self.adapter_type, "locality": self.locality, "model": self.model or None,
                "timeout_s": self.timeout_s, "retries": self.retries}

    def generate(self, prompt: str, cancel: CancelToken | None = None, timeout_s: float | None = None,
                 retries: int | None = None, deadline: float | None = None) -> CallResult:
        cancel = cancel or CancelToken()
        timeout = float(timeout_s or self.timeout_s)
        max_retries = self.retries if retries is None else retries
        res = CallResult(provider=self.name, adapter=self.adapter_type, locality=self.locality, status="error")
        t0 = time.monotonic()
        avail = self.check()
        if not avail.available:
            res.status, res.error = "unavailable", avail.reason
            res.duration_s = time.monotonic() - t0
            return res
        attempt = 0
        while True:
            attempt += 1
            res.attempts = attempt
            if cancel.is_set():
                res.status, res.error = "cancelled", "cancelled before call"
                break
            this_timeout = timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    res.status, res.error = "timeout", "council deadline reached before call"
                    break
                this_timeout = min(timeout, remaining)
            try:
                raw = self._call(prompt, this_timeout, cancel)
                res.status, res.text = "ok", raw.text
                res.tokens, res.cost_usd, res.cost_note = raw.tokens, raw.cost_usd, raw.cost_note
                res.error = ""
                break
            except ProviderError as e:
                res.status = e.status
                res.error = redact_secrets(str(e))[0][:800]
                if e.transient and attempt <= max_retries and not cancel.is_set():
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                break
            except Exception as e:  # adapter bug or unexpected environment: isolate it
                res.status = "error"
                res.error = redact_secrets(f"{type(e).__name__}: {e}")[0][:800]
                break
        res.duration_s = time.monotonic() - t0
        return res


# ---------------------------------------------------------------------------
# helpers for adapters

def child_env(strip_prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """Environment for provider subprocesses: marks recursion, optionally strips vars."""
    env = {k: v for k, v in os.environ.items() if not any(k.startswith(p) for p in strip_prefixes)}
    env[RECURSION_ENV] = "1"
    env.setdefault("NO_COLOR", "1")
    return env


def which(binary: str) -> str | None:
    return shutil.which(binary)


def run_subprocess(argv: list[str], stdin_text: str | None, timeout_s: float, cancel: CancelToken,
                   env: dict[str, str] | None = None, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command with timeout + cancellation; kills the whole process group on either."""
    workdir = cwd or tempfile.mkdtemp(prefix="ec-provider-")
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=workdir,
                                env=env, start_new_session=True)
    except FileNotFoundError:
        raise ProviderError(f"executable not found: {argv[0]}", status="unavailable")
    except OSError as e:
        raise ProviderError(f"could not start {argv[0]}: {e}", status="error")
    t_end = time.monotonic() + timeout_s
    first = True
    out_parts: list[str] = []
    err_parts: list[str] = []
    while True:
        try:
            o, e = proc.communicate(input=stdin_text if first else None, timeout=0.25)
            out_parts.append(o or "")
            err_parts.append(e or "")
            break
        except subprocess.TimeoutExpired:
            first = False
            if cancel.is_set() or time.monotonic() > t_end:
                _kill_group(proc)
                try:
                    o, e = proc.communicate(timeout=5)
                except Exception:
                    o, e = "", ""
                if cancel.is_set():
                    raise ProviderError("cancelled", status="cancelled")
                raise ProviderError(f"timed out after {timeout_s:.0f}s", status="timeout")
    return proc.returncode, "".join(out_parts), "".join(err_parts)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        time.sleep(0.5)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def http_json(url: str, payload: dict[str, Any] | None, timeout_s: float, cancel: CancelToken,
              headers: dict[str, str] | None = None, method: str | None = None) -> dict[str, Any]:
    """POST/GET JSON in a worker thread so cancellation is honoured promptly."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                box["body"] = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:600] if e.fp else ""
            box["http_error"] = (e.code, body)
        except Exception as e:
            box["exc"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    t_end = time.monotonic() + timeout_s + 2
    while th.is_alive():
        th.join(0.25)
        if cancel.is_set():
            raise ProviderError("cancelled", status="cancelled")
        if time.monotonic() > t_end:
            raise ProviderError(f"timed out after {timeout_s:.0f}s", status="timeout")
    if "http_error" in box:
        code, body = box["http_error"]
        transient = code in (408, 409, 425, 429) or code >= 500
        raise ProviderError(f"HTTP {code}: {redact_secrets(body)[0]}", status="error", transient=transient)
    if "exc" in box:
        e = box["exc"]
        if isinstance(e, TimeoutError) or "timed out" in str(e):
            raise ProviderError(f"timed out after {timeout_s:.0f}s", status="timeout")
        if isinstance(e, urllib.error.URLError):
            raise ProviderError(f"connection failed: {e.reason}", status="unavailable", transient=False)
        raise ProviderError(f"{type(e).__name__}: {e}", status="error", transient=True)
    try:
        return json.loads(box.get("body", ""))
    except json.JSONDecodeError:
        raise ProviderError("response was not JSON", status="error")
