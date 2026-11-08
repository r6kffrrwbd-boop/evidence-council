"""Adapters for locally installed agent CLIs: Codex, Claude Code, Gemini CLI, Hermes.

All of them run in a fresh empty temporary directory (so the provider cannot see
the caller's repository through its working directory), receive the prompt on
stdin where the CLI supports it, and inherit ``EVIDENCE_COUNCIL_ACTIVE=1`` so a
provider that tries to call evidence-council again is refused (no recursion).
They use the user's existing CLI login; no credentials pass through this program.
"""

from __future__ import annotations

import json
import os
import re
import tempfile

from .base import Adapter, Availability, CancelToken, ProviderError, RawResponse, child_env, run_subprocess, which

# Variables that bind a child `claude` process to the parent desktop/IDE session.
CLAUDE_SESSION_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_PREVIEW_")


class CodexAdapter(Adapter):
    """OpenAI Codex CLI via `codex exec` (non-interactive, read-only sandbox, ephemeral session)."""

    adapter_type = "codex"
    default_locality = "cloud"

    def check(self) -> Availability:
        exe = which(self.spec.get("binary", "codex"))
        if not exe:
            return Availability(False, "codex CLI not found on PATH (install: npm i -g @openai/codex)")
        try:
            rc, out, err = run_subprocess([exe, "login", "status"], None, 20, CancelToken(), env=child_env())
        except ProviderError as e:
            return Availability(False, f"`codex login status` failed: {e}")
        if rc != 0:
            return Availability(False, "codex is not logged in (run: codex login)")
        return Availability(True, (out or err).strip().splitlines()[0] if (out or err).strip() else "logged in")

    def _call(self, prompt: str, timeout_s: float, cancel: CancelToken) -> RawResponse:
        exe = which(self.spec.get("binary", "codex")) or "codex"
        work = tempfile.mkdtemp(prefix="ec-codex-")
        last = os.path.join(work, "last_message.txt")
        argv = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "-s", "read-only", "--color", "never", "-o", last]
        if self.model:
            argv += ["-m", self.model]
        argv.append("-")
        rc, out, err = run_subprocess(argv, prompt, timeout_s, cancel, env=child_env(), cwd=work)
        text = ""
        if os.path.exists(last):
            with open(last, encoding="utf-8", errors="replace") as f:
                text = f.read()
        if rc != 0 or not text.strip():
            tail = (err or out).strip().splitlines()[-3:]
            transient = any(s in (err + out).lower() for s in ("rate limit", "429", "503", "temporarily", "overloaded"))
            raise ProviderError(f"codex exited {rc}: {' | '.join(tail)[:400]}", transient=transient)
        tokens = None
        m = re.search(r"tokens used\s*\n?\s*([\d,]+)", out + "\n" + err)
        if m:
            tokens = {"total": int(m.group(1).replace(",", ""))}
        return RawResponse(text=text, tokens=tokens, cost_usd=None,
                           cost_note="subscription CLI; per-call USD cost not reported")


class ClaudeAdapter(Adapter):
    """Claude Code CLI via `claude -p` with no tools, no skills, no MCP servers, no session persistence."""

    adapter_type = "claude"
    default_locality = "cloud"

    def _env(self) -> dict[str, str]:
        env = child_env(CLAUDE_SESSION_PREFIXES)
        # A desktop/IDE parent session injects a proxy base URL that only works for the parent.
        if "CLAUDECODE" in os.environ and not self.spec.get("keep_base_url"):
            env.pop("ANTHROPIC_BASE_URL", None)
        return env

    def check(self) -> Availability:
        exe = which(self.spec.get("binary", "claude"))
        if not exe:
            return Availability(False, "claude CLI not found on PATH")
        if os.environ.get("ANTHROPIC_API_KEY"):
            return Availability(True, "ANTHROPIC_API_KEY present")
        try:
            rc, out, _ = run_subprocess([exe, "auth", "status"], None, 20, CancelToken(), env=self._env())
            info = json.loads(out)
        except (ProviderError, json.JSONDecodeError, ValueError):
            return Availability(True, "auth status unknown; will be confirmed by the call")
        if not info.get("loggedIn"):
            return Availability(False, "claude CLI is not logged in for headless use (run `claude` then /login, or set ANTHROPIC_API_KEY)")
        return Availability(True, f"logged in ({info.get('authMethod', 'unknown')})")

    def _call(self, prompt: str, timeout_s: float, cancel: CancelToken) -> RawResponse:
        exe = which(self.spec.get("binary", "claude")) or "claude"
        argv = [exe, "-p", "--output-format", "json", "--tools", "", "--disable-slash-commands",
                "--no-session-persistence", "--strict-mcp-config"]
        if self.model:
            argv += ["--model", self.model]
        rc, out, err = run_subprocess(argv, prompt, timeout_s, cancel, env=self._env())
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            raise ProviderError(f"claude exited {rc} without JSON output: {(err or out).strip()[-300:]}")
        if data.get("is_error") or rc != 0:
            msg = str(data.get("result") or data.get("terminal_reason") or "unknown error")
            transient = any(s in msg.lower() for s in ("overloaded", "rate", "529", "503"))
            status = "unavailable" if "authenticate" in msg.lower() or "login" in msg.lower() else "error"
            raise ProviderError(f"claude: {msg[:300]}", status=status, transient=transient)
        u = data.get("usage") or {}
        tokens = {"input": int(u.get("input_tokens", 0)), "output": int(u.get("output_tokens", 0))}
        cost = data.get("total_cost_usd")
        return RawResponse(text=str(data.get("result", "")), tokens=tokens,
                           cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
                           cost_note="as reported by claude CLI (may be notional under a subscription)")


class GeminiAdapter(Adapter):
    """Google Gemini CLI via `gemini -p`, run in an empty, explicitly trusted temp directory."""

    adapter_type = "gemini"
    default_locality = "cloud"

    def check(self) -> Availability:
        if not which(self.spec.get("binary", "gemini")):
            return Availability(False, "gemini CLI not found on PATH")
        return Availability(True, "binary present; auth is confirmed only by a call (use `providers --probe`)")

    def _call(self, prompt: str, timeout_s: float, cancel: CancelToken) -> RawResponse:
        exe = which(self.spec.get("binary", "gemini")) or "gemini"
        argv = [exe, "--skip-trust", "-p", prompt]
        if self.model:
            argv += ["-m", self.model]
        env = child_env()
        rc, out, err = run_subprocess(argv, None, timeout_s, cancel, env=env)
        if rc != 0 or not out.strip():
            text = err + out
            hint = ""
            if "UNSUPPORTED_CLIENT" in text or "no longer supported" in text:
                hint = " (Google reports this client is no longer supported for this account tier)"
            if hint:
                raise ProviderError(f"gemini exited {rc}{hint} [UNSUPPORTED_CLIENT]", status="unavailable")
            lines = [l for l in text.strip().splitlines() if l.strip() and not l.strip().startswith("at ")]
            raise ProviderError(f"gemini exited {rc}: {lines[-1][:300] if lines else ''}")
        return RawResponse(text=out, cost_note="usage not reported by gemini CLI text mode")


class HermesAdapter(Adapter):
    """Nous Hermes Agent via `hermes -z` (one-shot). Locality depends on its configured provider."""

    adapter_type = "hermes"
    default_locality = "cloud"
    FAILURE_MARKERS = ("API call failed", "No API key", "Error:", "Traceback (most recent call last)")

    def check(self) -> Availability:
        if not which(self.spec.get("binary", "hermes")):
            return Availability(False, "hermes CLI not found on PATH")
        return Availability(True, "binary present; model reachability is confirmed only by a call")

    def _call(self, prompt: str, timeout_s: float, cancel: CancelToken) -> RawResponse:
        exe = which(self.spec.get("binary", "hermes")) or "hermes"
        argv = [exe, "-z", prompt, "--ignore-rules"]
        if self.model:
            argv += ["-m", self.model]
        if self.spec.get("provider"):
            argv += ["--provider", self.spec["provider"]]
        rc, out, err = run_subprocess(argv, None, timeout_s, cancel, env=child_env())
        text = out.strip()
        # Hermes can exit 0 while printing an upstream failure; never count that as a proposal.
        if rc != 0 or not text or any(text.startswith(m) for m in self.FAILURE_MARKERS):
            raise ProviderError(f"hermes failed (exit {rc}): {(text or err).strip()[:300]}",
                                transient="429" in text or "503" in text)
        return RawResponse(text=text, cost_note="usage not reported by hermes one-shot mode")
