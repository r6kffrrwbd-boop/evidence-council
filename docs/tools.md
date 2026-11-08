# Tool reference

One command, `evidence-council`, with one subcommand per tool. Every tool:

* reads JSON from a file (or `-` for stdin) and writes JSON to `--output`/`-o` or stdout;
* has `--help` with its contract and examples;
* reports failures as `error: …` + `hint: …` on stderr (or a JSON object with `--error-format json`)
  and a stable exit code (`evidence-council codes`).

Common options on every tool: `--config PATH`, `--home DIR`, `--verbose`, `--error-format text|json`.
Provider options on `review`, `propose`, `privacy`: `--providers a,b` (`none` = candidates only),
`--local-only`, `--allow-private-to NAME` (operator override, repeatable).

Python equivalents (stable): `evidence_council.council.run_review(input_doc, config)`,
`run_stage(stage, doc, config)`, `evidence_council.contract.validate(doc, kind)`,
`evidence_council.explain.reading(result)`, `evidence_council.lessons.check_candidate(lesson, store, result)`.

| Tool | Purpose | Input | Output | Exit codes |
|---|---|---|---|---|
| `review TASK` | full pipeline | input | output (result) | 0, 2, 3, 4, 5 |
| `propose TASK\|STATE` | independent proposals only | input or state | state (…`proposals`) | 0, 2, 5, 7 |
| `compare STATE` | options + disagreements | state | state (…`comparison`) | 0, 2, 7 |
| `verify STATE` | objective checks | state | state (…`verification`) | 0, 2, 7 |
| `verdict STATE` | decision + candidate lesson (+ judges for subjective) | state | result | 0, 2, 5, 7 |
| `explain RESULT [--json]` | plain-language reading + `must_not_claim` | result | markdown or reading JSON | 0, 2 |
| `providers [--probe] [--json]` | which providers are callable | config | listing | 0 |
| `privacy TASK [--show-payload NAME]` | routing dry run; calls nothing | input | routing JSON or the exact payload | 0, 2 |
| `validate FILE [--kind K]` | contract check | any | `{valid, kind, problems}` | 0 valid, 2 invalid |
| `schema KIND` | print JSON Schema | — | schema | 0 |
| `lessons list [--all]` | accepted (and retired) lessons | memory | `{memory, memory_version, lessons}` | 0 |
| `lessons check FILE [--result R]` | acceptance checks, no write | result or lesson | check report | 0 pass, 6 rejected |
| `lessons accept FILE [--result R]` | accept if all checks pass | result or lesson | report + `stored` | 0, 6 |
| `lessons retire ID --reason TEXT` | append a retirement | — | `{retired, memory_version}` | 0, 2 |
| `selftest` | offline mock council must overturn a wrong majority | — | PASS/FAIL lines | 0, 1 |
| `codes` | exit-code table | — | JSON | 0 |

## `explain --json` reading

```json
{"status": "supported", "headline": "Evidence supports one answer.", "answer": "11398661",
 "can_act_on_answer": true, "overturned_popular": true,
 "participants": ["codex", "ollama:qwen3.5:9b"], "failed_or_skipped_providers": ["claude: unavailable - ..."],
 "excluded_providers": ["grok: environment variable XAI_API_KEY is not set"],
 "checks_run": ["product (numeric_reference)"], "checks_not_run": [], "check_counts": {"run": 2, "passed": 1, ...},
 "why": ["..."], "rejected": ["option A (...): product (numeric_reference) failed: ..."], "unverified": [],
 "uncertainty": {"level": "medium", "reasons": ["..."]}, "candidate_lesson": "lesson-…",
 "must_not_claim": ["Do not describe any provider not listed in 'participants' as having participated.", "..."],
 "warnings": []}
```

## Common errors and fixes

| Message | Fix |
|---|---|
| `file not found` | check the path or use `-` for stdin |
| `… is not valid JSON: … at line L column C` | fix syntax; `evidence-council validate FILE` |
| `task input does not match the input contract` | the first `$.path: message` says what; compare with `examples/inputs/` |
| `state has not completed the 'X' stage` | run the named earlier stage, or `review` |
| `privacy policy excluded every selected provider` | use a local provider, or (operator) `--allow-private-to NAME` |
| `no provider produced a usable proposal` | `evidence-council providers --probe` |
| `refusing to start a council from inside a council provider call` | you are in a nested run; don't |
| `lesson rejected; failed checks: …` | each failed check's `detail` explains it; nothing was stored |
| `lesson memory integrity check failed` | the append-only log was edited; restore it or move it aside |
