# Test results

Recorded on 2026-11-08, macOS 14 (Intel x86_64), CPython 3.12.13. Re-run them yourself; nothing here
should be taken on trust.

## 1. Deterministic suite (MOCK providers, offline)

`pytest` → **112 passed** (real-provider test deselected by default).

| File | Tests | Covers |
|---|---|---|
| `tests/test_contract.py` | 22 | every example input/output validates; invalid inputs report `$.path`; defaults (privacy `private`, no code execution); task input cannot carry privacy overrides; JSON syntax errors give line/column + hint |
| `tests/test_disagreement.py` | 3 | answer / keyed-claim / assumption / confidence disagreements; unanimous agreement still `insufficient_evidence`; proposers never see each other or the draft candidates |
| `tests/test_provider_failure.py` | 6 | timeout, error, unavailable, transient-then-ok (retried), malformed output, all-fail (exit 3, result still written), cancellation, run deadline, unknown provider, secret redaction in errors |
| `tests/test_insufficient_evidence.py` | 6 | no checks; errored check; self-contradictory evidence; code checks without permission (`not_run`); format-only check cannot select; `all_rejected` |
| `tests/test_privacy.py` | 9 | private default excludes cloud; recordings never forwarded (even locally); `restricted` not overridable; `--local-only`; operator allowlist still redacts; transcripts withheld from cloud; secrets redacted for all; `privacy` dry-run and `--show-payload`; exit 4 |
| `tests/test_lessons.py` | 14 | candidate only from verified outcomes; accept → versioned memory; rejections (tampered verdict, injection text, PII, no selecting check, model-judgment evidence, restricted, length, missing provenance); conflict until retired; tamper detection; CLI flow writes only `memory/lessons.jsonl` and leaves `CLAUDE.md` untouched; lessons are context, never a substitute for a check |
| `tests/test_overturn.py` | 5 | **popular wrong answer overturned** by recomputation (CLI), by unit tests (sandboxed), and by log evidence; fabricated citations caught; regression for the false overturn found in a real run |
| `tests/test_cli_tools.py` | 26 | every tool has `--help` with examples; SKILL.md documents every tool, its reference links resolve, and every command in it parses; `explain --json` interpretation of supported vs insufficient results; staged pipeline equals `review`; stage-order and JSON error formats; recursion guard (exit 5); `providers`, `selftest`, `codes` |
| `tests/test_premises.py` | 5 | OBSERVED / NOT VERIFIED labels reach providers (redacted); not-verified premises listed as unverified and raise uncertainty; premise confirmed or contradicted by an evidence fact; contradiction puts the framing error first, sets high uncertainty and blocks the lesson; schema and `explain` output |
| `tests/test_jobs.py` | 4 | `review --async` returns at once with a job id; `jobs result` exits 8 while running, then returns the result (JSON, `--explain`, `-o`); job dir is owner-only; cancel stops providers and keeps a partial result; remove; path-traversal ids rejected; invalid input and nested calls refused before a job starts |
| `tests/test_subjective_and_safety.py` | 12 | subjective task → no winner, judges never score their own proposal; safe arithmetic evaluator accepts arithmetic and rejects code; macOS sandbox blocks network and home reads; sandbox timeout |

`evidence-council selftest` (also run by `install.sh`): 9/9 PASS — offline mock council overturns a
2-vs-1 wrong product with a recomputation check.

## 2. Real end-to-end runs (NOT mocked)

`scripts/run_real_e2e.sh` against the providers on this machine. Raw results and explanations are in
`examples/real-runs/`.

**Probe** (`providers-probe.json`): callable = `codex`, `ollama:qwen3.5:9b`, `ollama:qwen3.5:4b`.
Not callable: `claude` (headless login expired), `gemini` (Google: UNSUPPORTED_CLIENT), `hermes`
(its configured OpenRouter model returns HTTP 404), `grok` (no `XAI_API_KEY`).

| Task | Participants | Answers | Checks actually run | Verdict |
|---|---|---|---|---|
| `real-arithmetic` (48·37 + 23·59 − 418) | codex, qwen3.5:9b, qwen3.5:4b | codex **2715**; 9b 1846; 4b 1408 | `recompute` (numeric_reference) ×3 options | `supported` 2715. Both local models' answers rejected by recomputation. No majority existed, so `overturned_popular` is false. |
| `coding-leap-year` | codex, qwen3.5:9b, qwen3.5:4b | three different correct implementations | `unit-tests` (python_tests, sandboxed) ×3 — all pass | `multiple_supported`: tests cannot distinguish them; the tool says so instead of picking a "winner" |
| `incident-evidence` run 1 | codex, qwen3.5:9b, qwen3.5:4b | "14:32 UTC" ×2, "14:32" ×1 | `first-error` (fact_answer), auto citations/facts | **Bug found:** strict text comparison rejected "14:32 UTC" and falsely reported an overturned majority. Kept as `incident-evidence.run1-false-rejection.*`. Fixed with author-controlled `task.answer_extract`; regression test added. |
| `incident-evidence` run 2 (after fix) | codex, qwen3.5:9b, qwen3.5:4b | all three: 14:32 (with/without "UTC") — one option | `first-error` (fact_answer) pass; auto evidence_facts and citations | `supported` 14:32 UTC; no rejection, no false overturn |

Real-run timings: codex 12–24 s per call (~20k tokens, CLI agent context); qwen3.5:9b 175–366 s and
qwen3.5:4b 51–463 s per call on CPU.

**Honest limits.** The overturn of a *popular* wrong answer is demonstrated deterministically (mocks,
`selftest`, `test_overturn.py`). In the real runs, local models were wrong but never formed a majority,
so no real popular-answer overturn occurred. Claude, Gemini, Hermes and Grok adapters are implemented
but were not callable here, so they are unverified against live services.


## 3. Coding agent (0.3.0) — recorded 2026-11-16, same machine, k6 v2.4.0, Chrome

`pytest` → **156 passed** in total (112 earlier + 44 agent tests), 1 real-provider test deselected.

| File | Tests | Covers |
|---|---|---|
| `tests/test_agent_models.py` | 11 | Gemini replays `thoughtSignature` parts verbatim, echoes `functionCall.id` only when Gemini supplied one, hides `thought` parts, strips schema keywords Gemini rejects; OpenAI keeps `reasoning.encrypted_content` and `call_id`; Anthropic keeps `thinking`/`redacted_thinking` signatures and groups `tool_result`s by `tool_use_id`; inline config keys rejected; spend limit refuses a call *before* it is made; limits only lowered; **HTTP round trip against a fake Gemini server on 127.0.0.1**: key only in the `x-goog-api-key` header, signatures sent back, HTTP 429 retried; error bodies redacted |
| `tests/test_agent_safety.py` | 25 | policy blocks `git push/commit`, `npm install`, `pip install`, `curl`, `sh -c`, `rm`, non-local URLs, `vercel`, production targets; "fewer lines" refused as an optimizer goal; approval bound to plan **and** persona scripts (edit after approval → refused); `--confirm` must match; workspace copy skips `.env`, blocks `..`, absolute paths, `.git`, keys; snapshot/restore exact; `replace_in_file` needs a unique match; read-only agents cannot write; repeated identical calls flagged then stopped; **hung command killed** (status `hung`); commands get no API keys and output is redacted; macOS sandbox blocks network and writes outside the workspace; regressions (failing, removed, fewer tests); unittest parsing; security scan rules; k6 step data stays JSON; non-local targets refused; load capped at 20 VUs / 60 s |
| `tests/test_agent_workflow.py` | 8 | full pipeline with the **scripted** model on a copy of `tiny-shop`: round 1 fails a test → returned to Builder with reasons → round 2 passes; Critic defect forwarded; optimizer attempt 1 breaks tests → **rolled back**, attempt 2 kept (metric 2.2 s → ~3 ms per 1000 carts); report separates verified / AI judgment / untested; project untouched; `changes.patch` applies with `git apply --check`; lessons only from verified outcomes; provider state never logged. Also: spend limit stops the run and still writes a report; a Builder stuck in repeated calls is stopped; **worktree mode** creates `ec-agent/<run>` with no commit and leaves your checkout clean; a run with a different provider than approved is refused; CLI intake → run refused before approval → approve → run → report. **Real k6** (skipped without k6): browser journey fails on the untouched shop and passes after the change, load test passes; a page that re-fetches an API 25 times is reported as *unintended repeated work*; an endpoint taking 4 s is reported as *slow or hanging* |

### Real k6 runs

`examples/agent-demo/sample-run/` is a complete run: the browser persona (headless Chrome via k6) failed on the
untouched shop — "text was: Total: $144.00" — and passed after the change; the 5-user, 8-second load test made
~4,000–7,000 requests with p95 between 9 and 16 ms in each round.

**Found and fixed while testing:** the first browser navigation sometimes took 0.8–4.4 s of *browser start-up*
on this Intel Mac, which k6's whole-request metric counted, so a healthy page was flagged as "slow or hanging"
(seen in 2 of 10 repeats). Browser journeys now judge each request by the browser's own request timing (sent →
first byte); after the fix, 10 of 10 repeats passed with per-request times of 1–9 ms. The macOS sandbox also
refused a local port bind in ~1 of 60 attempts (never without the sandbox); the local app is now retried on a
new port.

### What is NOT verified

- **No real AI model has run the coding agent.** No `GEMINI_API_KEY` (or OpenAI/Anthropic key) was available on
  this machine. The Gemini adapter is tested against recorded reply shapes and a local fake server, not against
  Google's API; OpenAI and Anthropic mappings are tested offline only. All workflow runs used the scripted model,
  which proves the pipeline, limits and checks — not the quality of an AI Builder or Critic.
- No accuracy figure exists: the agent has not been run against a defined benchmark.
- The security scan is a heuristic; the macOS sandbox is defence in depth, not a security boundary.
- Tested on macOS 14 Intel only; Linux runs without the sandbox.
