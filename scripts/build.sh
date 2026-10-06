#!/usr/bin/env bash
# Build wheels and per-app container images.
# Usage: scripts/build.sh [--no-docker] [--push] [app...]   (default: all apps)
# Env: VERSION (extra image tag), IMAGE_REGISTRY, DOCKER_BUILD_FLAGS (e.g. cache flags in CI).
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT" || exit 1

docker_build=1 push=0 apps=()
while (($#)); do
  case "$1" in
    --no-docker) docker_build=0 ;;
    --push) push=1 ;;
    -*) die "unknown flag $1" ;;
    *) validate_app "$1"; apps+=("$1") ;;
  esac
  shift
done
((${#apps[@]})) || apps=("${APPS[@]}")

require uv
log "Building wheels into dist/"
rm -rf dist
uv build --package studio-core --out-dir dist
for app in "${apps[@]}"; do uv build --package "$app" --out-dir dist; done

((docker_build)) || exit 0
require docker

sha="$(sha_tag)"
version="${VERSION:-$sha}"
read -r -a extra_flags <<< "${DOCKER_BUILD_FLAGS:-}"
for app in "${apps[@]}"; do
  image="$(image_name "$app")"
  tags=(--tag "$image:$sha")
  [[ "$version" != "$sha" ]] && tags+=(--tag "$image:$version")
  output=(--load)
  ((push)) && output=(--push)
  log "Building $image ($version)"
  docker buildx build \
    --file docker/Dockerfile \
    --build-arg "APP=$app" \
    --build-arg "VERSION=$version" \
    --build-arg "REVISION=$(commit_sha)" \
    "${tags[@]}" "${output[@]}" "${extra_flags[@]}" .
done
