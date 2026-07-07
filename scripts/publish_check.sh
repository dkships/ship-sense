#!/usr/bin/env bash
# Publish preflight: everything that must be true before this repo goes public.
# Run via `make publish-check`. Exits non-zero on any failure.
#
# The one risk this cannot fix: this repo's git HISTORY predates the privacy
# split and contains client-derived names. Publishing is therefore a FRESH
# single-commit export (`make export-public`), never a push of this history.
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0
say() { printf '%s\n' "$*"; }
bad() { say "FAIL  $*"; fail=1; }
ok()  { say "ok    $*"; }

# 1. Private paths must be gitignored and untracked.
for p in cases/private_x keys/x reviews/x notes/x outputs/x retired/x .env CLAUDE.md; do
  git check-ignore -q "$p" 2>/dev/null && ok "ignored: ${p%x}*" || bad "NOT ignored: $p"
done
tracked_private=$(git ls-files | grep -E '^(reviews|notes|outputs)/|^\.env$|^CLAUDE\.md$' || true)
[ -z "$tracked_private" ] && ok "no private paths tracked" || bad "tracked private paths: $tracked_private"

# 2. No private item id — or id prefix (the client-name token before the first
#    underscore) — may appear in any tracked file. Both lists are derived at
#    runtime from the local private bank (never embedded here). Keys and retired
#    items are scanned too, so an orphan or retired id still contributes its tokens.
ids=$( (ls cases/*/*.yaml keys/*.yaml retired/*/*.yaml 2>/dev/null || true) | xargs -n1 basename 2>/dev/null | sed 's/\.yaml$//' | grep -v '^example_' | sort -u)
prefixes=$(printf '%s\n' $ids | sed 's/_.*/_/' | sort -u)
leak=""
for tok in $ids $prefixes; do
  hits=$(git grep -l -- "$tok" 2>/dev/null || true)
  [ -n "$hits" ] && leak="$leak $tok($hits)"
done
[ -z "$leak" ] && ok "no private item ids or prefixes in tracked files" || bad "private ids leaked:$leak"

# 3. Tests green (includes the docs/README/card drift guards + privacy guard).
.venv/bin/python -m pytest -q >/dev/null 2>&1 && ok "test suite passes" || bad "test suite fails"

# 4. Private-bank mechanical audit (strict only on source/provenance, not David sign-off).
.venv/bin/python -m src.bank_audit --strict >/dev/null 2>&1 && ok "bank audit passes" || bad "bank audit fails"

# 5. LICENSE exists.
[ -f LICENSE ] && ok "LICENSE present" || bad "LICENSE missing"

# 6. Working tree clean: export-public ships committed HEAD via `git archive`, so
#    a dirty tree would silently publish stale content while looking current.
[ -z "$(git status --porcelain)" ] && ok "working tree clean (export ships HEAD)" \
  || bad "working tree dirty — commit first; export-public ships committed HEAD only"

if [ "$fail" -ne 0 ]; then
  say ""; say "Preflight FAILED — fix before any publish."; exit 1
fi
say ""
say "Preflight passed. REMINDER: publish only via 'make export-public' (fresh"
say "single-commit repo). Never 'git push' this repo's history to a public remote."
