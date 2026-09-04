#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing $ROOT/.env (copy .env.example and add provider keys)" >&2
  exit 1
fi

set -a
source "$ROOT/.env"
set +a
exec "$@"
