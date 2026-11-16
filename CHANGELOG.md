# Changelog

## 0.3.0 — 2026-11-16

- **Coding agent** (`evidence-council agent intake|show|approve|run|report|providers`): Intake checklist from a
  plain-language request (phrase → requirement → assumptions → observable check), approval bound to a SHA-256 of the
  plan and rendered persona scripts, then Builder → Evidence checker → Critic loop, Optimizer with measured goal and
  rollback, Final evaluator separating verified / failed / AI judgment / untested. Report, patch and event log per run.
- Provider-neutral messages with Gemini (default), OpenAI Responses and Anthropic Messages adapters that replay
  provider state verbatim (tool-call IDs, thought signatures, encrypted reasoning, thinking signatures); scripted
  model for offline tests. Keys only from environment variables, sent in headers; inline keys in config rejected.
- Customer journeys with k6: declarative browser/HTTP personas rendered to reviewable scripts, light load (≤20 VUs,
  ≤60 s), detection of failed steps, repeated requests and hanging requests. `scripts/install_k6.sh`.
- Safety: copy or worktree workspace, secret files not copied, command policy (no shells, deploys, commits, installs,
  non-local URLs), macOS sandbox for approved commands, hang detection, regression detection, diff security scan and
  error-page probe, repeated-tool-call and no-progress detection, spend/call/time limits checked before each call.
- Lessons from verified outcomes only, in a separate hash-chained memory.
- Sandbox profiles allow file *metadata* reads (needed when the Python environment lives in your home folder).
- Exit code 9: agent run stopped early or a requirement failed.

## 0.2.0 — 2026-11-08
- **Premises:** `premises[]` in the input, labelled `observed` / `not_verified` and sent to providers as
  `OBSERVED:` / `NOT VERIFIED:`. Premises naming an evidence fact are checked; a contradicted premise is
  reported first, raises uncertainty to high, and blocks the candidate lesson.
- **Background jobs:** `review --async` and `jobs list|status|result|cancel|remove`; exit code 8 while running.
- `task.answer_extract` (regex) to compare the core value of an answer (fixes a false rejection seen in a real run).

## 0.1.0 — 2026-11-08
- Initial release: CLI and Claude Code skill, JSON contract 1.0, provider adapters, privacy routing,
  deterministic verification, verdict rules, candidate lessons with versioned memory.
