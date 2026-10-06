#!/usr/bin/env bash
# Decide what CD should ship. Exports: apps (JSON), version (image tag), production (true/false).
#   tag push  <app>-v<semver>  -> that app, version <semver>, production
#   main push                  -> apps changed since the previous main commit, sha tag, staging
#   manual dispatch            -> INPUT_APP (all|<app>), sha tag, production if INPUT_PRODUCTION
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT" || exit 1

event="${GITHUB_EVENT_NAME:-workflow_dispatch}"
ref_type="${GITHUB_REF_TYPE:-branch}"
ref_name="${GITHUB_REF_NAME:-}"

if [[ "$event" == push && "$ref_type" == tag ]]; then
  [[ "$ref_name" =~ ^(.+)-v([0-9]+\.[0-9]+\.[0-9]+([-.+][0-9A-Za-z.-]+)?)$ ]] \
    || die "tag '$ref_name' must look like <app>-v<semver>"
  app="${BASH_REMATCH[1]}"
  validate_app "$app"
  set_output apps "$(json_array "$app")"
  set_output version "${BASH_REMATCH[2]}"
  set_output production true
elif [[ "$event" == push ]]; then
  "$(dirname "$0")/changed-apps.sh" "${BEFORE_SHA:-}" HEAD
  set_output version "$(sha_tag)"
  set_output production false
else
  input="${INPUT_APP:-all}"
  if [[ "$input" == all ]]; then
    set_output apps "$(json_array "${APPS[@]}")"
  else
    validate_app "$input"
    set_output apps "$(json_array "$input")"
  fi
  set_output version "$(sha_tag)"
  set_output production "${INPUT_PRODUCTION:-false}"
fi
