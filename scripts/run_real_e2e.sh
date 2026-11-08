#!/usr/bin/env bash
# REAL end-to-end runs against providers on this machine (not mocked).
# Writes results to examples/real-runs/. Usage: scripts/run_real_e2e.sh [path/to/evidence-council]
set -uo pipefail
cd "$(dirname "$0")/.."
EC="${1:-evidence-council}"
OUT=examples/real-runs
mkdir -p "$OUT"
echo "== provider probe (sends a one-line prompt to each available provider)"
"$EC" providers --probe --json > "$OUT/providers-probe.json"; echo "probe exit $?"
CALLABLE=$(python3 -c "import json;print(','.join(json.load(open('$OUT/providers-probe.json'))['callable']))")
echo "callable: $CALLABLE"
for name in real-arithmetic coding-leap-year incident-evidence; do
  echo "== review $name with providers: $CALLABLE"
  "$EC" review "examples/inputs/$name.json" --providers "$CALLABLE" --output "$OUT/$name.result.json" --verbose
  echo "review exit $?"
  "$EC" explain "$OUT/$name.result.json" > "$OUT/$name.explain.md"
done
