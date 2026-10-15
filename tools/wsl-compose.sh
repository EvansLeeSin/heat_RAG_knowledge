#!/usr/bin/env bash
# Never load a NAS Compose file or implicitly read a project .env.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${KNOWLEDGE_WSL_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/heat-knowledge-wsl/runtime.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing WSL runtime configuration: $ENV_FILE" >&2
  exit 2
fi
if [[ "$EUID" -eq 0 ]]; then
  DOCKER=(docker)
else
  DOCKER=(sudo -n docker)
fi
exec "${DOCKER[@]}" compose --project-name heat-knowledge-wsl \
  --env-file "$ENV_FILE" -f "$ROOT/docker-compose.wsl.yml" "$@"
