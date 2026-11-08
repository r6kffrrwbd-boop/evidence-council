# Contributing

Thanks for helping. Ground rules that keep the council honest:

1. **Evidence over votes.** Changes must never let agreement between providers select an answer.
   `verdict.py` rules are part of the public contract (docs/contract.md#verdict).
2. **No claimed participation without a successful adapter run.** New adapters implement
   `check()` (no prompt sent) and `_call()`; `generate()` handles timeouts, retries and cancellation.
   Never add browser automation or web-UI scraping as an "API".
3. **Privacy defaults stay conservative.** Default level is `private`; task input must never be able
   to unlock cloud forwarding. Add a test in `tests/test_privacy.py` for any routing change.
4. **Contract changes** are additive within `schema_version` 1.x. Update the schema, docs/contract.md,
   an example in `examples/`, and tests.
5. **Tests:** `pip install -r requirements-dev.lock -e .` then `pytest`. Deterministic tests use mock
   providers only. Real-provider tests are opt-in: `EC_REAL=1 pytest -m real` or `scripts/run_real_e2e.sh`.
6. **No secrets or personal data** in commits, fixtures or examples. Run `scripts/scan_secrets.sh`
   before opening a PR.
7. Only add third-party code whose licence is compatible with MIT, with attribution.

Adding a provider: add an adapter class to `src/evidence_council/providers/`, register it in
`ADAPTER_TYPES`, add a default config entry if it is commonly installed, and record its *verified*
behaviour in docs/providers.md (date, version, what you actually observed).
