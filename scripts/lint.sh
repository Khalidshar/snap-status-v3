#!/usr/bin/env bash
# Static checks: ruff (lint + format), mypy (strict), shellcheck and actionlint when installed.
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
require uv
cd "$REPO_ROOT" || exit 1

log "ruff check";        uv run --frozen ruff check .
log "ruff format check"; uv run --frozen ruff format --check .
log "mypy (strict)";     uv run --frozen mypy

if command -v shellcheck >/dev/null 2>&1; then
  log "shellcheck"; shellcheck -x scripts/*.sh
else
  log "shellcheck not installed; skipping"
fi
if command -v actionlint >/dev/null 2>&1; then
  log "actionlint"; actionlint
else
  log "actionlint not installed; skipping (CI runs it)"
fi
