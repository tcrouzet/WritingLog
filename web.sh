#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

"$PROJECT_DIR/.venv-web/bin/python" "$PROJECT_DIR/scripts/export_data.py" "$@"
exec "$PROJECT_DIR/.venv-web/bin/python" "$PROJECT_DIR/scripts/web.py" "$@"
