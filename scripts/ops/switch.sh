#!/usr/bin/env bash
# Owner-operated migration only. No host, credentials or state are accessed in dry run.
set -euo pipefail
exec python3 "$(dirname "$0")/switch_control.py" "$@"
