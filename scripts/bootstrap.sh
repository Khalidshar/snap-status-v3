#!/usr/bin/env bash
# Create the local dev environment (all workspace packages + dev tools) and install git hooks.
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
require uv
cd "$REPO_ROOT" || exit 1

log "Syncing workspace (locked)"
uv sync --all-packages --frozen

if [[ -d .git ]]; then
  log "Installing pre-commit hooks"
  uvx pre-commit install --install-hooks
fi
log "Done. Try: make lint test"
