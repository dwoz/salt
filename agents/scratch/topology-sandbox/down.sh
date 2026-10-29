#!/usr/bin/env bash
# Stop the topology sandbox.  Preserves PKI material under ./pki/ so
# `./up.sh` on next invocation reuses the same identities.  Add
# `--wipe` to also delete PKI (fresh identities next time).
set -euo pipefail
cd "$(dirname "$0")"

docker compose down -v

if [[ "${1:-}" == "--wipe" ]]; then
  echo ">>> wiping PKI"
  rm -rf pki/
fi
