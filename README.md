# evidence-council

Ask several AI systems for **independent** answers, find out **why** they disagree, check their claims
against **objective evidence** (tests, recomputation, the logs and documents you supply), and get a
**justified verdict** — or an honest `insufficient_evidence`. A council vote is never treated as
verification. Verified outcomes can become reusable lessons, but only after they pass explicit checks.

Works for coding, research, business workflows, and review of drafts produced by other programs
(e.g. coaching notes checked against timestamped call observations). Runs fully offline with local
models, and with as few as two participants — or zero, when you only want drafts checked against evidence.

```
task.json ──► proposals (independent, per-provider minimised data)
          ──► comparison (answers, keyed claims, assumptions, confidence)
          ──► verification (deterministic checks; no model involved)
          ──► verdict (supported / multiple_supported / insufficient_evidence / all_rejected / subjective)
          ──► candidate lesson (accepted into versioned memory only after acceptance checks)
```

## Install

```bash
git clone https://github.com/r6kffrrwbd-boop/evidence-council.git
cd evidence-council
./install.sh
```

`install.sh` is idempotent. It creates an isolated venv in `~/.local/share/evidence-council/venv`,
installs exact-pinned dependencies (`requirements.lock`), installs the Claude Code skill to
`~/.claude/skills/evidence-council`, links `~/.local/bin/evidence-council`, checks which providers are
callable (without sending prompts), and runs an offline self-test. Add `--probe` to send each available
provider a one-line test prompt. It never touches other skills, `CLAUDE.md`, or provider configuration.

**Manual install:** `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.lock && .venv/bin/pip install --no-deps .`
then copy `skill/SKILL.md` and `docs/*.md` (as `references/`) into `~/.claude/skills/evidence-council/`.

**Remove:** `./uninstall.sh` (keeps lesson memory) or `./uninstall.sh --purge`.

## Use

```bash
evidence-council providers                         # what is callable here (no prompts sent)
evidence-council review task.json --output result.json
evidence-council explain result.json               # plain-language reading
evidence-council review task.json --local-only -o result.json
evidence-council privacy task.json --show-payload codex   # exactly what a provider would receive
evidence-council review task.json --async          # background job; prints a job id
evidence-council jobs result JOB_ID --explain      # fetch it later (exit 8 while running)
```

In Claude Code, invoke the skill directly (`/evidence-council …`) or ask for "the evidence council".
SKILL.md tells Claude which tool to call when, how to read results, and what it must not claim.

Every tool has `--help` with examples; full reference in [docs/tools.md](docs/tools.md).

## Example: overturning a popular wrong answer

[`examples/inputs/numeric-overturn.json`](examples/inputs/numeric-overturn.json) asks for 3847 × 2963 and
defines one check: recompute it. With the three mock providers in
[`examples/configs/mock-overturn.json`](examples/configs/mock-overturn.json), two agree on 11,398,561 and one
says 11,398,661:

```bash
evidence-council review examples/inputs/numeric-overturn.json --config examples/configs/mock-overturn.json -o result.json
evidence-council explain result.json
```

```
## Council verdict: supported
**Answer:** 11398661
**The most popular answer was overturned by objective evidence.**
**Checks actually run:** product (numeric_reference)
- option A (11398561): product (numeric_reference) failed: computed 3847 * 2963 = 11398661; answer 11398561
```

More examples (inputs, mock configs and outputs): code settled by tests
([coding-leap-year](examples/inputs/coding-leap-year.json)), a log contradicting a status page
([incident-evidence](examples/outputs/incident-evidence.explain.md)), a subjective task with rubric
judges ([business-subjective](examples/outputs/business-subjective.explain.md)), and draft coaching notes
checked against timestamped observations with nothing sent to any model
([audio-sales-coach-review](examples/outputs/audio-sales-coach-review.explain.md)).
Files in `examples/outputs/` were produced with **mock** providers; real runs are in
[`examples/real-runs/`](examples/real-runs/).

## JSON contract

Stable, versioned (`schema_version: "1.0"`), shipped as JSON Schema: `evidence-council schema input|output|lesson`.
Input: task, evidence (with timestamps, source refs, facts), constraints, premises labelled
OBSERVED / NOT VERIFIED (checked against evidence where they name a fact), candidates, checks, rubric,
privacy level, requested providers. Output: providers actually used, every attempt with status/tokens/cost,
privacy routing, proposals, options, disagreements, checked claims, verification results, verdict,
uncertainty, candidate lesson. Details: [docs/contract.md](docs/contract.md).

## Privacy

Default level is `private`: cloud providers receive nothing unless the operator allowlists one in config
or with `--allow-private-to NAME` — a task file cannot do it. Recordings are never forwarded; transcripts
and sensitive evidence are withheld from cloud providers; phone numbers/e-mails are redacted for cloud;
secrets are redacted for everyone. `restricted` and `--local-only` keep everything on the machine.
See [docs/privacy.md](docs/privacy.md).

## Providers

Adapters share one interface (availability check without prompting; timeout, bounded retries for
transient errors, cancellation, token/cost reporting). A failing provider never fails the council, and
a provider is listed as a participant only if its adapter returned successfully. No browser automation.
See [docs/providers.md](docs/providers.md) for the capability table **as verified on the author's machine**.

## Tests

```bash
pip install -r requirements-dev.lock && pip install -e .
pytest                         # deterministic, offline, mock providers only
EC_REAL=1 pytest -m real -s    # REAL providers on this machine (opt-in)
scripts/run_real_e2e.sh        # REAL runs; writes examples/real-runs/
```

The deterministic suite covers disagreement detection, provider failure/timeout/retry/cancellation,
insufficient evidence, privacy routing, candidate-lesson rejection, JSON validation, overturning a
popular wrong answer (arithmetic, unit tests, log evidence), subjective judging, and Claude-facing tool
invocation and interpretation. Test results: [docs/test-results.md](docs/test-results.md).

## License

MIT — see [LICENSE](LICENSE). No third-party skill code is included.
