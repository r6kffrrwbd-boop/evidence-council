#!/usr/bin/env bash
# Idempotent installer for evidence-council.
#
#   ./install.sh                 install / upgrade, check providers (no prompts sent), run self-test
#   ./install.sh --probe         also send a one-line test prompt to each available provider
#   ./install.sh --no-skill      install the CLI only (no ~/.claude/skills entry)
#   ./install.sh --no-selftest   skip the offline self-test
#
# Creates:
#   ~/.local/share/evidence-council/venv         isolated Python environment (pinned deps)
#   ~/.claude/skills/evidence-council/           Claude Code skill (SKILL.md, references/, bin/)
#   ~/.local/bin/evidence-council                symlink to the skill's launcher
# Never modifies other skills, CLAUDE.md, or provider configuration.
# Override locations with EC_DATA_DIR, EC_SKILL_DIR, EC_BIN_DIR, EC_PYTHON.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${EC_DATA_DIR:-$HOME/.local/share/evidence-council}"
VENV="$DATA_DIR/venv"
SKILL_DIR="${EC_SKILL_DIR:-$HOME/.claude/skills/evidence-council}"
BIN_DIR="${EC_BIN_DIR:-$HOME/.local/bin}"
MARKER=".installed-by-evidence-council"
PROBE=0; SKILL=1; SELFTEST=1
for a in "$@"; do
  case "$a" in
    --probe) PROBE=1 ;;
    --no-skill) SKILL=0 ;;
    --no-selftest) SELFTEST=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "error: unknown option $a"; echo "hint: ./install.sh --help"; exit 2 ;;
  esac
done

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$1" >&2; [ -n "${2:-}" ] && printf 'hint: %s\n' "$2" >&2; exit 1; }

# 1. Python >= 3.10 (prefer versions the pinned wheels are verified on)
PY="${EC_PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3.12 python3.11 python3.13 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$(command -v "$c")"; break
    fi
  done
fi
[ -n "$PY" ] || die "Python 3.10+ not found" "Install Python 3.12 (e.g. brew install python@3.12) or set EC_PYTHON=/path/to/python3"
say "Python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# 2. Isolated environment (re-created only if missing or its Python is gone)
mkdir -p "$DATA_DIR"
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c 'import sys' >/dev/null 2>&1; then
  say "creating virtual environment at $VENV"
  rm -rf "$VENV"
  "$PY" -m venv "$VENV"
else
  say "reusing virtual environment at $VENV"
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "pip>=23" >/dev/null
say "installing pinned dependencies (requirements.lock)"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r "$REPO/requirements.lock"
say "installing evidence-council from $REPO"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --no-deps --force-reinstall "$REPO"

# 3. Claude Code skill (only touches its own directory)
LAUNCHER="$VENV/bin/evidence-council"
if [ "$SKILL" = 1 ]; then
  if [ -e "$SKILL_DIR" ] && [ ! -f "$SKILL_DIR/$MARKER" ]; then
    die "$SKILL_DIR exists and was not created by this installer" "Move it aside, or install with EC_SKILL_DIR=/other/path ./install.sh"
  fi
  say "installing skill to $SKILL_DIR"
  mkdir -p "$SKILL_DIR/references" "$SKILL_DIR/bin" "$SKILL_DIR/examples"
  cp "$REPO/skill/SKILL.md" "$SKILL_DIR/SKILL.md"
  cp "$REPO"/docs/*.md "$SKILL_DIR/references/"
  cp -R "$REPO/examples/inputs" "$SKILL_DIR/examples/"
  cat > "$SKILL_DIR/bin/evidence-council" <<EOF
#!/usr/bin/env bash
exec "$LAUNCHER" "\$@"
EOF
  chmod +x "$SKILL_DIR/bin/evidence-council"
  echo "$REPO" > "$SKILL_DIR/$MARKER"
  LAUNCHER="$SKILL_DIR/bin/evidence-council"
fi

# 4. Command on PATH (never overwrite someone else's file)
mkdir -p "$BIN_DIR"
LINK="$BIN_DIR/evidence-council"
if [ -L "$LINK" ] || [ ! -e "$LINK" ]; then
  ln -sfn "$LAUNCHER" "$LINK"
  say "linked $LINK -> $LAUNCHER"
else
  say "WARNING: $LINK exists and is not a symlink; left untouched. Use $LAUNCHER directly."
fi
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) say "note: $BIN_DIR is not on PATH; add it or call $LAUNCHER" ;; esac

# 5. Provider check and self-test
say "provider availability (cheap checks, no prompts sent):"
"$LAUNCHER" providers || true
if [ "$PROBE" = 1 ]; then
  say "probing providers (one short prompt each):"
  "$LAUNCHER" providers --probe || true
fi
if [ "$SELFTEST" = 1 ]; then
  say "offline self-test (mock providers):"
  "$LAUNCHER" selftest
fi
say "done. Try: evidence-council review \"$REPO/examples/inputs/numeric-overturn.json\" --config \"$REPO/examples/configs/mock-overturn.json\" --output /tmp/ec-result.json && evidence-council explain /tmp/ec-result.json"
