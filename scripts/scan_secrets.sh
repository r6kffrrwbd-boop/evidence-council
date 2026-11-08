#!/usr/bin/env bash
# Scan tracked files AND full git history for secret-like strings and personal data.
# Exit 1 if anything suspicious is found.
set -uo pipefail
cd "$(dirname "$0")/.."
PAT='(sk-ant-[A-Za-z0-9_-]{10,}|sk-(proj-|or-v1-)?[A-Za-z0-9_-]{20,}|xai-[A-Za-z0-9]{16,}|(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{16,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|BEGIN [A-Z ]*PRIVATE KEY)'
# fixtures intentionally contain fake tokens to test redaction; list them here
ALLOW='tests/test_privacy.py|tests/test_provider_failure.py|scripts/scan_secrets.sh|src/evidence_council/util.py'
status=0
echo "== working tree"
git ls-files -z | xargs -0 grep -nIE "$PAT" 2>/dev/null | grep -vE "^($ALLOW):" && status=1
echo "== history (all commits)"
git log -p --all --no-color 2>/dev/null | grep -nE "^\+.*$PAT" | grep -vE "REDACTED|abcdefghijklmnop|ABCDEFGHIJKLMNOP" && status=1
echo "== personal paths / e-mail"
git ls-files -z | xargs -0 grep -nIE '/Users/[a-z]+/|@gmail\.com|@icloud\.com' 2>/dev/null | grep -vE '^scripts/scan_secrets.sh:' && status=1
[ $status = 0 ] && echo "no secrets or personal data found" || echo "REVIEW the matches above"
exit $status
