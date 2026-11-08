# Privacy and security model

## Data minimisation per provider

Before any provider is called, `privacy.plan_route` decides whether it may receive the task, and
`privacy.build_payload` builds the *minimised* view it gets. `evidence-council privacy TASK` shows
the decision for every provider; `--show-payload NAME` prints the exact payload. Each result records
the routing (`privacy.routing`) including which evidence was withheld and how many redactions were made.

| Data | local provider (Ollama on this machine) | cloud provider, level public/internal | cloud provider, level private | cloud, restricted / local_only |
|---|---|---|---|---|
| task prompt, constraints | sent | sent, PII redacted | **not sent** unless operator allowlists the provider; then PII redacted | **never** |
| `audio` / `recording` content and URI | **never** (id/kind/timestamp/source_ref only) | never | never | never |
| `transcript`, `sensitive: true` evidence | sent | withheld unless allowlisted | withheld unless allowlisted | never |
| phone numbers, e-mails, card numbers | sent as-is | replaced with `[PHONE_1]` etc. | replaced (unless allowlisted **and** `redact: false`) | never |
| secrets (API keys, tokens, private keys) | **redacted** | redacted | redacted | never |
| draft `candidates` | never shown to proposers | never shown to proposers | never | never |

The **default level is `private`**. The task input alone can never enable cloud forwarding of
private data: the allowlist lives in operator config (`privacy.allow_private_to` in
`~/.config/evidence-council/config.json`) or the per-run CLI flag `--allow-private-to NAME`.
The input schema rejects unknown privacy fields, so a submitting program cannot smuggle one in.

PII detection is heuristic (phone numbers need a `+` or separators and 9–15 digits; cards must pass
Luhn). It reduces exposure; it is not a guarantee. Use `restricted` or `--local-only` for data that
must not leave the machine at all.

## Secrets

* No credential ever passes through evidence-council: CLI providers use their own login; HTTP
  providers read the key from the environment variable named in config at call time.
* Errors, logs (`--verbose`) and stored provider text are passed through `redact_secrets`.
* `.gitignore` excludes `.env*`, keys, local config, memory and media files.

## Untrusted content

Provider outputs and evidence are data. Prompts fence evidence in `<evidence>` tags and tell the
provider not to follow instructions inside it; parsed provider output is size-limited, stripped of
control characters, secret-redacted and marked `untrusted: true`. Lessons containing
instruction-override patterns are rejected.

## Code execution

`python_tests` and `command` checks run only when the input sets
`options.allow_code_execution: true`. They run in a fresh temp directory with a scrubbed
environment (`PATH`, `HOME`=tempdir, no API keys) and a timeout. On macOS they additionally run under
`sandbox-exec` with a profile that denies network access, writes outside the temp dir, and reads from
your home directory (except the Python installation). Each check's `detail` says `sandboxed` or
`NOT sandboxed`. This is defence in depth for model-written code, not a boundary against a determined
attacker; do not enable code execution for inputs you do not trust.

## Provider isolation and recursion

CLI providers run in an empty temp working directory (so they don't see your repository through
their cwd), Codex in its `read-only` sandbox with user config ignored, Claude with no tools, no
skills, no MCP servers. Every provider subprocess gets `EVIDENCE_COUNCIL_ACTIVE=1`; evidence-council
refuses to start inside it (exit 5), so councils cannot recurse.

## What evidence-council never does

* Never uses browser automation or scrapes a web UI to impersonate an API.
* Never edits `CLAUDE.md`, `AGENTS.md`, other skills, or any agent's instructions. Accepted lessons
  live only in `<home>/memory/lessons.jsonl`.
* Never reports a provider as a participant unless its adapter returned successfully.
