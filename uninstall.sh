#!/usr/bin/env bash
# Remove what install.sh created. Keeps lesson memory unless --purge.
#   ./uninstall.sh            remove venv, skill, PATH link
#   ./uninstall.sh --purge    also remove lesson memory and ~/.config/evidence-council
set -euo pipefail
DATA_DIR="${EC_DATA_DIR:-$HOME/.local/share/evidence-council}"
SKILL_DIR="${EC_SKILL_DIR:-$HOME/.claude/skills/evidence-council}"
BIN_DIR="${EC_BIN_DIR:-$HOME/.local/bin}"
PURGE=0; [ "${1:-}" = "--purge" ] && PURGE=1

if [ -d "$SKILL_DIR" ]; then
  if [ -f "$SKILL_DIR/.installed-by-evidence-council" ]; then rm -rf "$SKILL_DIR"; echo "removed $SKILL_DIR"
  else echo "left $SKILL_DIR (not created by install.sh)"; fi
fi
LINK="$BIN_DIR/evidence-council"
if [ -L "$LINK" ] && readlink "$LINK" | grep -q evidence-council; then rm -f "$LINK"; echo "removed $LINK"; fi
rm -rf "$DATA_DIR/venv" && echo "removed $DATA_DIR/venv"
if [ "$PURGE" = 1 ]; then
  rm -rf "$DATA_DIR" "${XDG_CONFIG_HOME:-$HOME/.config}/evidence-council"
  echo "purged lesson memory and config"
else
  [ -d "$DATA_DIR/memory" ] && echo "kept lesson memory in $DATA_DIR/memory (use --purge to delete)"
fi
