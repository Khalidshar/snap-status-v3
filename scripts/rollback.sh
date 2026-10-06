#!/usr/bin/env bash
# Roll an environment back to a given image tag, or to <env>-previous when no tag is given.
# Usage: scripts/rollback.sh <app> <staging|production> [tag]
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

app="${1:?usage: rollback.sh <app> <environment> [tag]}"
environment="${2:?missing environment}"
target="${3:-$environment-previous}"
validate_app "$app"
validate_environment "$environment"

log "Rolling back $app in $environment to $target"
exec "$(dirname "$0")/deploy.sh" "$app" "$environment" "$target"
