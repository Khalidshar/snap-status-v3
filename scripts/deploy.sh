#!/usr/bin/env bash
# Promote an already-published image to an environment by moving its registry tag,
# keeping the replaced image as <env>-previous for one-step rollback, then smoke-test it.
# Usage: scripts/deploy.sh <app> <staging|production> <source-tag>
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
require docker

app="${1:?usage: deploy.sh <app> <environment> <source-tag>}"
environment="${2:?missing environment}"
source_tag="${3:?missing source tag (e.g. sha-0123456789ab or 1.2.0)}"
validate_app "$app"
validate_environment "$environment"

image="$(image_name "$app")"
docker buildx imagetools inspect "$image:$source_tag" >/dev/null \
  || die "$image:$source_tag does not exist in the registry"

if docker buildx imagetools inspect "$image:$environment" >/dev/null 2>&1; then
  log "Saving current $environment as $image:$environment-previous"
  docker buildx imagetools create --tag "$image:$environment-previous" "$image:$environment"
fi

log "Promoting $image:$source_tag -> $environment"
docker buildx imagetools create --tag "$image:$environment" "$image:$source_tag"

docker pull --quiet "$image:$environment" >/dev/null
"$(dirname "$0")/smoke.sh" "$app" "$image:$environment"

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf "### %s → %s\n\n\`%s:%s\` ← \`%s\`\n" \
    "$app" "$environment" "$image" "$environment" "$source_tag" >> "$GITHUB_STEP_SUMMARY"
fi
log "Deployed $app $source_tag to $environment"
