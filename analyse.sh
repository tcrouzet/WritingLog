#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MODE=${1:-incremental}

case "$MODE" in
  full|incremental)
    if [ "$#" -gt 0 ]; then
      shift
    fi
    ;;
  *)
    echo "Usage : ./analyse.sh [full|incremental] [options]" >&2
    exit 2
    ;;
esac

exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/analyze_vault.py" "$MODE" "$@"
