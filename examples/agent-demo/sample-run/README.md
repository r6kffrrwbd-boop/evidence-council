# Sample run (tiny-shop demo)

Produced on 2026-11-16 on macOS 14 (Intel) by `examples/agent-demo` with the **scripted test model** — the
model turns are a hand-written fixture, not AI output. Everything else is real: the copied workspace, the
approved unit tests in the macOS sandbox, the local app, real k6 browser/HTTP/load journeys (k6 v2.4.0,
headless Chrome), the security scan, the optimizer measurements and rollback, the report and the lessons.
Local paths are replaced with `~` / `<repo>` / `<tmp>`.

- `console.log` — what the user sees while it runs (phases, tool calls, results, why work was returned)
- `plan.json` — the approved checklist; `maya-bulk-buyer.js`, `load-checkout-load.js` — the rendered k6 scripts
- `evidence-baseline.json` — checks on the untouched project (Maya does not see the discount yet)
- `report.md` / `report.json` — final evidence report; `changes.patch` — the change, not applied to the project
