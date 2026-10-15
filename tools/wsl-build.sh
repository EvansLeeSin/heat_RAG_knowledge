#!/usr/bin/env bash
# CPU image build. Host networking is build-only, for the local WSL proxy;
# it does not expose any application's runtime containers to the host network.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$EUID" -eq 0 ]]; then
  DOCKER=(docker)
else
  DOCKER=(sudo -n docker)
fi
ARGS=()
[[ -z "${HTTP_PROXY:-${http_proxy:-}}" ]] || ARGS+=(--build-arg "HTTP_PROXY=${HTTP_PROXY:-${http_proxy:-}}")
[[ -z "${HTTPS_PROXY:-${https_proxy:-}}" ]] || ARGS+=(--build-arg "HTTPS_PROXY=${HTTPS_PROXY:-${https_proxy:-}}")
ARGS+=(--build-arg "NO_PROXY=${NO_PROXY:-${no_proxy:-localhost,127.0.0.1}}")
exec "${DOCKER[@]}" buildx build --load --allow network.host --network host \
  "${ARGS[@]}" --file "$ROOT/Dockerfile.wsl" \
  --tag heat-knowledge-service:wsl "$ROOT"
