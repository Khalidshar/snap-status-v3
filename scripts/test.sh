#!/usr/bin/env bash
# Run tests. Usage: scripts/test.sh [all|core|snap-bot|status-bot] [extra pytest args...]
# "all" enforces the coverage gate from pyproject.toml and writes reports/ for CI.
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
require uv
cd "$REPO_ROOT" || exit 1

target="${1:-all}"; shift || true
case "$target" in
  all)
    mkdir -p reports
    uv run --frozen pytest --cov --cov-report=term --cov-report=xml:reports/coverage.xml \
      --junitxml=reports/junit.xml "$@"
    ;;
  core)       uv run --frozen pytest tests packages/studio-core/tests "$@" ;;
  snap-bot)   uv run --frozen pytest apps/snap-bot/tests "$@" ;;
  status-bot) uv run --frozen pytest apps/status-bot/tests "$@" ;;
  *) die "unknown test target '$target' (expected: all, core, ${APPS[*]})" ;;
esac
