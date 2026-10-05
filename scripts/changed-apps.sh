#!/usr/bin/env bash
# Print (and export to $GITHUB_OUTPUT as "apps") the JSON list of apps affected by a diff.
# Shared code/config/build changes affect every app. No/unknown base => every app.
# Usage: scripts/changed-apps.sh [base-ref] [head-ref]
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT" || exit 1

base="${1:-}"
head="${2:-HEAD}"

if [[ -z "$base" || "$base" =~ ^0+$ ]] || ! git cat-file -e "$base^{commit}" 2>/dev/null; then
  set_output apps "$(json_array "${APPS[@]}")"
  exit 0
fi

declare -A hit=()
while IFS= read -r path; do
  case "$path" in
    packages/*|config/*|docker/*|scripts/*|pyproject.toml|uv.lock|.dockerignore)
      for app in "${APPS[@]}"; do hit[$app]=1; done ;;
    apps/*)
      app="${path#apps/}"; app="${app%%/*}"
      [[ " ${APPS[*]} " == *" $app "* ]] && hit[$app]=1 ;;
  esac
done < <(git diff --name-only "$base" "$head")

selected=()
for app in "${APPS[@]}"; do [[ -n "${hit[$app]:-}" ]] && selected+=("$app"); done
set_output apps "$(json_array "${selected[@]}")"
