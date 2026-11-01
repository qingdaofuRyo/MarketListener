#!/usr/bin/env sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$project_dir/desktop/.venv/bin/python" "$project_dir/scripts/start_marketlistener.py" "$@"
