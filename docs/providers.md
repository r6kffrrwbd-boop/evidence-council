# Providers and adapters

## Verified capability table

Observed on the author's machine on **2026-11-08** (macOS 14, Intel x86_64) with
`evidence-council providers --probe` and the real runs in `examples/real-runs/`. "Callable" means the
adapter returned a response to a real prompt; having a subscription or an installed binary is not enough.
Your results will differ — run `evidence-council providers --probe` yourself.

| Provider | Adapter / transport | Locality | Version seen | Callable here? | What was observed |
|---|---|---|---|---|---|
| Codex | `codex exec` (read-only sandbox, `--ephemeral --ignore-user-config`) | cloud (OpenAI) | codex-cli 0.154.0 | **yes** | Probe reply `PONG` in ~12 s; ~20k tokens per call (the CLI's own agent context); USD cost not reported by the CLI |
| Ollama `qwen3.5:9b` | HTTP `127.0.0.1:11434/api/generate`, JSON mode | local | Ollama 0.40.13 | **yes** | Slow on CPU (cold load + generation ~1–3 min); reports prompt/eval token counts; cost 0 |
| Ollama `qwen3.5:4b` | same | local | Ollama 0.40.13 | **yes** | ~50 s cold probe; follows the JSON response format |
| Ollama `phi3` | same | local | — | installed, not used by default | `models: "auto"` picks the two largest installed models; list models explicitly to use it |
| Claude Code | `claude -p --output-format json`, no tools/skills/MCP | cloud (Anthropic) | 2.1.296 | **no** | `claude auth status` → not logged in for headless use; a call returns "OAuth session expired". Fix: run `claude`, `/login`, or set `ANTHROPIC_API_KEY` |
| Gemini CLI | `gemini -p` in an empty, explicitly trusted temp dir | cloud (Google) | 0.40.1 | **no** | Google returns `UNSUPPORTED_CLIENT` ("no longer supported for Gemini Code Assist for individuals") |
| Hermes Agent | `hermes -z` one-shot | cloud (as configured: OpenRouter) | v0.18.2 | **no** | Exits 0 but prints `HTTP 404: This model is unavailable for free`; the adapter treats this as a failure, not a proposal. Fix: choose a working model in Hermes (`model` / `provider` in this tool's config) |
| Grok (xAI) | OpenAI-compatible HTTP, `XAI_API_KEY` | cloud | — | **no** | No CLI and no `XAI_API_KEY`; adapter is implemented but **unverified** against the live API |
| OpenRouter / LM Studio / other | OpenAI-compatible HTTP | cloud or local (by URL) | — | not configured | disabled by default; enable in config |

Never added: browser automation of chat web UIs. A subscription without a programmatic interface is
not a provider.

## Adapter interface

```python
class Adapter:
    name: str; locality: "local" | "cloud"; timeout_s: float; retries: int
    def check(self) -> Availability        # no prompt sent: binary / login / server / key-env check
    def _call(self, prompt, timeout_s, cancel) -> RawResponse   # one attempt; raise ProviderError
    def generate(self, prompt, cancel, timeout_s=None, retries=None, deadline=None) -> CallResult  # never raises
```

* **Timeout:** per provider (`timeout_s`) and per run (`options.deadline_s`); subprocesses are killed as a
  process group.
* **Retries:** only for transient failures (HTTP 408/409/425/429/5xx, "overloaded", "rate limit"),
  exponential backoff capped at 8 s, `retries` per provider (default 1).
* **Cancellation:** Ctrl-C sets a shared cancel token; in-flight subprocesses are killed and HTTP waits
  abandoned; attempts are recorded as `cancelled`.
* **Failure isolation:** `unavailable`, `timeout`, `error`, `cancelled`, `parse_error` are recorded in
  `providers.attempted`; the council continues with the rest.
* **Tokens/cost:** reported when the provider reports them; `cost_usd: null` with a `cost_note` otherwise
  (subscription CLIs don't expose per-call cost).
* **Recursion:** every provider subprocess gets `EVIDENCE_COUNCIL_ACTIVE=1`; a nested council is refused.
* **Ollama:** calls to one server are serialised (one model at a time) to avoid memory thrash on small machines.

## Configuration

`~/.config/evidence-council/config.json` (or `--config`, or `$EVIDENCE_COUNCIL_CONFIG`). If present,
its `providers` list replaces the defaults.

```json
{
  "providers": [
    {"name": "codex", "type": "codex", "timeout_s": 300},
    {"name": "ollama", "type": "ollama", "models": ["qwen3.5:9b", "phi3:latest"], "timeout_s": 420},
    {"name": "claude", "type": "claude", "model": "sonnet"},
    {"name": "hermes", "type": "hermes", "model": "some/working-model", "provider": "openrouter", "locality": "cloud"},
    {"name": "grok", "type": "openai_compat", "base_url": "https://api.x.ai/v1", "model": "grok-4", "api_key_env": "XAI_API_KEY"},
    {"name": "lmstudio", "type": "openai_compat", "base_url": "http://127.0.0.1:1234/v1", "model": "local-model", "api_key_env": ""}
  ],
  "privacy": {"allow_private_to": []},
  "defaults": {"timeout_s": 240, "deadline_s": 900, "retries": 1, "min_participants": 1, "max_parallel": 4}
}
```

Fully offline: list only `ollama` (and/or local `openai_compat`) providers, or pass `--local-only`.
`type: "mock"` providers exist for tests and demos and are always labelled `(MOCK)` by `explain`.
