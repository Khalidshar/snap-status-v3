#!/usr/bin/env bash
# Shared helpers for the build/test/deploy scripts. Source this file; do not execute it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPS=(snap-bot status-bot)
ENVIRONMENTS=(staging production)
OWNER="${GITHUB_REPOSITORY_OWNER:-khalidshar}"
IMAGE_REGISTRY="${IMAGE_REGISTRY:-ghcr.io/${OWNER,,}}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not installed"; }

_one_of() {
  local value="$1" kind="$2"; shift 2
  local item
  for item in "$@"; do [[ "$item" == "$value" ]] && return 0; done
  die "unknown $kind '$value' (expected one of: $*)"
}
validate_app() { _one_of "$1" app "${APPS[@]}"; }
validate_environment() { _one_of "$1" environment "${ENVIRONMENTS[@]}"; }

image_name() { printf '%s/%s' "$IMAGE_REGISTRY" "$1"; }

commit_sha() { git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown; }

# Immutable per-commit image tag, e.g. sha-0123456789ab.
sha_tag() {
  local sha="${GITHUB_SHA:-$(commit_sha)}"
  [[ "$sha" == unknown ]] && { echo dev; return; }
  printf 'sha-%s' "${sha:0:12}"
}

json_array() {
  local out="" item
  for item in "$@"; do out+="${out:+,}\"$item\""; done
  printf '[%s]' "$out"
}

set_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"
  fi
  printf '%s=%s\n' "$1" "$2"
}
