#!/usr/bin/env bash
# Install the official k6 release binary into ~/.local/bin after verifying its SHA-256.
# Optional: only needed for customer-journey (k6) checks in `evidence-council agent`.
# Usage: scripts/install_k6.sh [VERSION]      (default below; override with K6_VERSION or the first argument)
set -euo pipefail

VERSION="${1:-${K6_VERSION:-v2.4.0}}"
DEST="${EC_BIN_DIR:-$HOME/.local/bin}"

case "$(uname -s)-$(uname -m)" in
  Darwin-x86_64) ASSET="k6-${VERSION}-macos-amd64.zip" ;;
  Darwin-arm64)  ASSET="k6-${VERSION}-macos-arm64.zip" ;;
  Linux-x86_64)  ASSET="k6-${VERSION}-linux-amd64.tar.gz" ;;
  Linux-aarch64) ASSET="k6-${VERSION}-linux-arm64.tar.gz" ;;
  *) echo "error: unsupported platform $(uname -s)-$(uname -m); see https://grafana.com/docs/k6/latest/set-up/install-k6/" >&2; exit 2 ;;
esac
BASE="https://github.com/grafana/k6/releases/download/${VERSION}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ec-k6.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

echo "downloading $ASSET and its checksums from github.com/grafana/k6 ..."
curl -fsSL --retry 3 -o "$WORK/$ASSET" "$BASE/$ASSET"
curl -fsSL --retry 3 -o "$WORK/checksums.txt" "$BASE/k6-${VERSION}-checksums.txt"
WANT="$(awk -v f="$ASSET" '$2 == f {print $1}' "$WORK/checksums.txt")"
GOT="$(shasum -a 256 "$WORK/$ASSET" | awk '{print $1}')"
if [ -z "$WANT" ] || [ "$WANT" != "$GOT" ]; then
  echo "error: checksum mismatch for $ASSET (expected ${WANT:-none}, got $GOT); nothing installed" >&2
  exit 1
fi
echo "checksum ok: $GOT"
mkdir -p "$WORK/x" "$DEST"
case "$ASSET" in
  *.zip) unzip -q "$WORK/$ASSET" -d "$WORK/x" ;;
  *) tar -xzf "$WORK/$ASSET" -C "$WORK/x" ;;
esac
BIN="$(find "$WORK/x" -type f -name k6 | head -1)"
install -m 0755 "$BIN" "$DEST/k6"
"$DEST/k6" version
case ":$PATH:" in *":$DEST:"*) ;; *) echo "note: add $DEST to your PATH to use k6" ;; esac
