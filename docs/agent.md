# Coding agent (`evidence-council agent`)

An evidence-checked coding agent for people who may not know technical terms. You describe what you want;
it shows you a checklist of what it understood and how each item will be checked; **nothing changes and no
command runs until you approve that exact checklist**. Then it works on a copy of your project (or a new git
branch), checks its own work by running only the commands you approved, and writes a report that separates
**verified results**, **AI judgments** and **untested claims**.

This is a tool for careful, small changes. It is not AGI and has no measured accuracy figure: it has not been
benchmarked against a defined task set. What the tests in this repository show is listed in
[test-results.md](test-results.md).

## Workflow

```
 request ─► Intake ─► checklist + plan.json ─► YOU APPROVE (fingerprint) ─► run:
   Baseline: approved checks on the untouched copy
   ┌─► Builder (file tools only, smallest change)
   │   Evidence checker (approved commands, k6 journeys + light load, security scan, regressions vs baseline)
   └── Critic (real diff + real evidence → specific defects)  ── returned with reasons, up to N rounds
   Optimizer (one named metric; keep only if ≥X% better AND every check passes; else roll back)
   Final evaluator (verified / failed / AI judgment / untested) → report.md, report.json, changes.patch
```

| Role | Can do | Cannot do |
|---|---|---|
| Intake | read project files; submit the checklist | change files, run commands |
| Builder | read, search, write and edit files inside the workspace | run commands, touch files outside the workspace, read `.env`/keys |
| Evidence checker (not AI) | run approved commands, start the approved local app, run k6, scan the diff | run anything not in the approved plan |
| Critic | read files; submit defects with quoted evidence | change files |
| Optimizer | edit files for one measured goal | keep a change that fails a check or does not move the metric |
| Final evaluator | read files; answer the plan's `ai_judgment` questions | mark anything "verified" — only checks do that |

## Commands

```bash
evidence-council agent providers                      # model provider status (sends nothing)
evidence-council agent intake --project DIR "request" [--setup setup.json] [--plan-file plan.json]
evidence-council agent show RUN_DIR                   # checklist again (after editing plan.json)
evidence-council agent approve RUN_DIR                # type 'approve' in a terminal
evidence-council agent approve RUN_DIR --confirm FINGERPRINT   # non-interactive, after a person approved
evidence-council agent run RUN_DIR [--max-cost 0.25] [--max-calls 20] [--max-minutes 10] [--no-lessons]
evidence-council agent report RUN_DIR [--json]
```

Run folders live in `~/.local/share/evidence-council/agent-runs/<run-id>/` (owner-only permissions):
`plan.json`, `approval.json`, `personas/*.js`, `workspace/`, `snapshots/`, `evidence/*.json`, `k6/`, `logs/`,
`events.jsonl`, `report.json`, `report.md`, `changes.patch`.

Exit codes: 0 completed with no failed requirement · 2 usage/approval problem · 9 the run stopped early or a
requirement failed (read the report).

## What you approve

`plan.json` (written by Intake, editable by you) contains:

- **items** — one per meaningful phrase: your words (`phrase`), the technical `requirement`, `assumptions`,
  and `acceptance` with checks:
  `command` (exit code + optional output regex), `journey` (a k6 persona), `load` (k6 light load),
  `security` (no high findings), `ai_judgment` (a question for the evaluator — reported as opinion),
  `manual` (you check it — reported as untested).
- **commands** — argv lists (no shell), timeout, `network: none|localhost`, `sandbox` (macOS `sandbox-exec`, on by default).
- **service** — the local app to start for journeys (`{port}` is replaced with a free local port).
- **personas** and **load** — see below.
- **optimizer** — `goal`, `metric` (`command_id` + `regex` with one number group, or wall time), `direction`,
  `min_improvement_pct`, `runs` (median).
- **limits** — `max_cost_usd`, `max_model_calls`, `max_wall_s`, `max_builder_rounds`, `max_optimizer_attempts`,
  `max_turns_per_agent`, `max_repeat_tool_calls`, `max_tool_calls`.
- **data_notice** — which provider receives what.

The approval fingerprint is a SHA-256 of `plan.json` **and every rendered persona script**. Editing either after
approval cancels the approval. `--setup FILE` supplies `service`, `personas`, `load`, `optimizer`, `limits`,
`security` and `project_mode` (`copy` or `worktree`) so they are part of the checklist from the start.

### Safety rules (enforced at intake, approval and run)

Refused in any command: `rm`, `sudo`, shells (`sh -c …`), `curl`/`wget`/`ssh`/`scp`, `git commit|push|pull|
reset|checkout|…`, package installs (`pip install`, `npm install`, `npx`, `brew`), deploy tools (`vercel`, `fly`,
`heroku`, `kubectl`, `terraform`, `aws`, `gcloud`, …), any non-localhost URL, and arguments that look like a
production target. An optimizer goal about code size ("fewer lines") is refused: an improvement must be a
behaviour metric, with clarity kept as a constraint.

Always: the project is copied (secret-looking files such as `.env`, `*.pem`, `*.key` are not copied and the agent
cannot read them) or checked out as a new branch `ec-agent/<run-id>` in a separate worktree. Nothing is committed,
pushed or deployed. Approved commands get a minimal environment without your API keys, and their output is
secret-redacted before it is logged or shown to a model.

## Customer journeys (k6)

Personas are declarative and reviewable; the agent renders them into k6 scripts in `personas/` (step data is
embedded as JSON, never spliced into code). Targets are always the local app the run started
(`http://127.0.0.1:<port>`).

```json
{"id": "maya-bulk-buyer", "name": "Maya, buys mugs in bulk", "kind": "browser",
 "steps": [{"action": "goto", "path": "/"}, {"action": "fill", "selector": "#qty", "value": "12"},
           {"action": "click", "selector": "#calc"}, {"action": "expect_text", "selector": "#total", "contains": "10% off"}],
 "max_requests": 6, "max_request_ms": 3000}
```

- Browser steps: `goto`, `fill`, `click`, `wait_for`, `expect_text` (each with `timeout_ms`, default 10 s).
- HTTP steps: `request` with `method`, `path`, optional `body`, `expect_status` (default 200), `contains`.
- Load: `{"id", "persona_id" (http), "vus" ≤ 20, "duration_s" ≤ 60, "p95_ms", "max_error_rate"}`.
- Detected: failed steps (with the text that was on the page), **repeated work** (more requests per journey than
  `max_requests`, e.g. a page re-fetching in a loop), **hanging/slow requests** (slowest request over
  `max_request_ms`; per-step timeouts), load p95 and error rate.

k6 is optional: without it, journey and load checks are reported as **not run / untested**, never as passed.
Install it with `scripts/install_k6.sh` (official release, SHA-256 verified) — Homebrew does not ship a bottle
for macOS 14 on Intel. Browser journeys need Chrome or Chromium installed.

## Visibility

The terminal shows each phase, every tool call with its arguments (file contents are shown as a size), what each
agent says in its visible reply, every test result with failing lines, security findings, regressions, spend, and
**why work was returned** to the Builder or rolled back. The same events go to `events.jsonl` (redacted). Provider
reasoning state (Gemini thought signatures, OpenAI encrypted reasoning, Anthropic thinking signatures) is kept only
to send back to the provider; it is never printed, logged or summarised, and no chain-of-thought is requested.

## Limits

Before every model call the agent estimates the worst case (input estimate + the full output-token allowance) and
refuses the call if it could push spend past `max_cost_usd`. Prices come from the `agent.pricing` table in config
(USD per million tokens; check your provider's price page). A model missing from the table is charged at a
conservative fallback ($15 / $75 per million) so the limit still holds. `--max-cost`, `--max-calls` and
`--max-minutes` can lower the approved limits for one run, never raise them. When a limit is hit the run stops and
still writes the report with what was verified so far.

Repeated identical tool calls with no file change in between are answered from the first result and flagged;
after `max_repeat_tool_calls + 2` the agent is stopped as stuck. A Builder round with no file change stops the
loop instead of repeating it.

## Model providers

| Provider | Status in this repository | Config |
|---|---|---|
| Gemini (default) | adapter implemented; offline tests and a local fake-server HTTP test pass; **not yet run against Google's API** (no key on the test machine) | `GEMINI_API_KEY`, model `gemini-2.5-flash` |
| OpenAI (Responses API) | mapping implemented and unit-tested offline; not run against the API | `OPENAI_API_KEY`, set `agent.providers.openai.model` |
| Anthropic (Messages API) | mapping implemented and unit-tested offline; not run against the API | `ANTHROPIC_API_KEY`, set `agent.providers.anthropic.model` |
| scripted | deterministic fixture for tests and demos; **not AI** | `--script FILE` |

```json
{"agent": {"provider": "gemini",
           "providers": {"gemini": {"model": "gemini-2.5-flash", "api_key_env": "GEMINI_API_KEY"}},
           "pricing": {"gemini-2.5-flash": {"input": 0.30, "output": 2.50}}}}
```

API keys are read only from the environment variable named in `api_key_env` and sent in a request header
(`x-goog-api-key`, `Authorization`, `x-api-key`), never in a URL. A config file containing an inline key is
rejected. A plan records the provider its data notice names; running it with another provider is refused.

### Adding a provider

Subclass `ChatModel` (`src/evidence_council/agent/models/base.py`) and implement:

- `to_wire(system, messages, tools) -> dict` — replay any assistant `Message.native` from your provider verbatim
  (that is how tool-call IDs and reasoning state survive), map `ToolResult.call_id` to the provider's id field;
- `from_wire(body) -> ModelTurn` — visible text, `ToolCall(id, name, args)`, `Usage`, and the raw reply in
  `native`;
- `_request(body, timeout_s, cancel)` — one HTTP call with the key in a header.

Register it in `models/__init__.py`. The workflow, tools and report do not change.

## Lessons

Lessons are derived from the report by code (not written by a model) and kept only for verified outcomes: a
requirement whose automated check failed on the untouched project and passed after the change, or an optimizer
change kept because the metric improved with every check passing. Each passes acceptance checks (evidence
present, no secrets or personal data, no instruction-like text) before it is appended to the hash-chained
`~/.local/share/evidence-council/memory/agent/lessons.jsonl`. Later runs on the same project show them to Intake
and Builder as data. Nothing is written to CLAUDE.md or any agent instructions.

## Known limitations

- The diff security scan is a heuristic (patterns on added lines + an error-page probe), not an audit.
- The macOS sandbox blocks network, writes outside the workspace/run folder and reads of file contents in your
  home folder; projects whose tests need those must set `"sandbox": false` per command (visible in the checklist).
  It occasionally refuses a local port bind; the local app is retried on a new port.
- Test results are parsed for unittest and pytest verbose output; other runners are judged by exit code and
  `expect_output`.
- Snapshots copy text files up to 2 MB; larger files are fingerprinted but not restorable by rollback.
- Only one provider is used per run (the one named in the approved data notice).
