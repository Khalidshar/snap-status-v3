#!/usr/bin/env bash
# Scan for committed secrets with gitleaks (project rules in .gitleaks.toml).
# Usage: scripts/secret-scan.sh [git-log-range]   e.g. origin/main..HEAD (default: full history)
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT" || exit 1

GITLEAKS_IMAGE="${GITLEAKS_IMAGE:-ghcr.io/gitleaks/gitleaks:v8.30.1}"
range="${1:-}"
args=(git --config .gitleaks.toml --redact --verbose --no-banner)
[[ -n "$range" ]] && args+=("--log-opts=$range")

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks "${args[@]}" .
else
  require docker
  docker run --rm -v "$REPO_ROOT:/repo" -w /repo --entrypoint sh "$GITLEAKS_IMAGE" -c \
    'git config --global --add safe.directory /repo && exec gitleaks "$@" /repo' gitleaks "${args[@]}"
fi
