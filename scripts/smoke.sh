#!/usr/bin/env bash
# Smoke-test an app image offline: healthcheck + dry-run must succeed with no network.
# Usage: scripts/smoke.sh <app> [image-ref]   (default ref: <registry>/<app>:sha-<commit>)
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
require docker
require python3

app="${1:?usage: smoke.sh <app> [image-ref]}"
validate_app "$app"
ref="${2:-$(image_name "$app"):$(sha_tag)}"

run() { docker run --rm --network none --read-only --cap-drop ALL "$ref" "$@"; }

log "Smoke: $ref healthcheck"
run healthcheck | python3 -c '
import json, sys
out = json.load(sys.stdin)
assert out["ok"] is True and out["app"] == sys.argv[1], out
print(json.dumps(out))' "$app"

log "Smoke: $ref dry-run"
run dry-run --requested-by smoke | python3 -c '
import json, sys
out = json.load(sys.stdin)
assert out["ok"] is True and out["job"]["status"] == "QUEUED", out
print(json.dumps(out))'
log "Smoke passed for $ref"
