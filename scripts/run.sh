#!/usr/bin/env bash
# Run a bot command from the image currently deployed to an environment.
# Usage: scripts/run.sh <app> <staging|production> <command> [args...]
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
require docker

app="${1:?usage: run.sh <app> <environment> <command> [args...]}"
environment="${2:?missing environment}"
shift 2
validate_app "$app"
validate_environment "$environment"

exec docker run --rm --pull always --cap-drop ALL "$(image_name "$app"):$environment" "$@"
