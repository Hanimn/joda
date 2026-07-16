#!/usr/bin/env bash
# joda container entrypoint: dispatch web vs. worker on the first argument.
set -euo pipefail

role="${1:-web}"

case "$role" in
  web)
    exec uvicorn backend.app:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    exec python -m backend.run_worker
    ;;
  cleanup)
    exec python -m backend.cleanup
    ;;
  *)
    echo "unknown role: $role (expected: web | worker | cleanup)" >&2
    exit 64
    ;;
esac
