"""Run untrusted candidate code for ``python_tests`` / ``command`` checks.

Code runs in a fresh temporary directory with a scrubbed environment (no API keys,
HOME pointed at the temp dir) and a timeout. On macOS, ``sandbox-exec`` additionally
denies network access, writes outside the temp dir, and reads from the user's home
(except this Python installation). On other platforms only the scrubbed env, temp dir
and timeout apply — ``sandboxed`` in the result says which happened.
This is defence in depth, not a security boundary for hostile code; see docs/privacy.md.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .providers.base import CancelToken, ProviderError, run_subprocess


@dataclass
class ExecResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    sandboxed: bool


def _profile(workdir: str) -> str:
    home = str(Path.home().resolve())
    allow_reads = {str(Path(p).resolve()) for p in (workdir, sys.prefix, sys.base_prefix, sys.exec_prefix)}
    reads = " ".join(f'(subpath "{p}")' for p in sorted(allow_reads))
    return (
        "(version 1)(allow default)(deny network*)"
        '(deny file-write* (subpath "/"))'
        f'(allow file-write* (subpath "{workdir}") (literal "/dev/null"))'
        f'(deny file-read* (subpath "{home}"))'
        f"(allow file-read* {reads})"
    )


def run_untrusted(argv: list[str], files: dict[str, str], stdin_text: str | None, timeout_s: float,
                  cancel: CancelToken | None = None) -> ExecResult:
    work = os.path.realpath(tempfile.mkdtemp(prefix="ec-check-"))
    try:
        for name, content in files.items():
            Path(work, name).write_text(content, encoding="utf-8")
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": work, "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        sandboxed = False
        full = list(argv)
        if sys.platform == "darwin" and shutil.which("sandbox-exec") and os.environ.get("EC_DISABLE_SANDBOX") != "1":
            full = ["sandbox-exec", "-p", _profile(work)] + full
            sandboxed = True
        try:
            rc, out, err = run_subprocess(full, stdin_text, timeout_s, cancel or CancelToken(), env=env, cwd=work)
            return ExecResult(rc, out[-4000:], err[-4000:], False, sandboxed)
        except ProviderError as e:
            if e.status == "timeout":
                return ExecResult(None, "", str(e), True, sandboxed)
            return ExecResult(None, "", str(e), False, sandboxed)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def python_argv(script: str) -> list[str]:
    return [sys.executable, "-E", "-s", "-B", script]


def _selfcheck() -> None:  # pragma: no cover - manual diagnostic
    r = run_untrusted(python_argv("t.py"), {"t.py": "print('ok')"}, None, 10)
    print(r)


if __name__ == "__main__":  # pragma: no cover
    _selfcheck()
    _ = subprocess
