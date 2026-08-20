#!/bin/bash
# Launch the overlay, or one of the development tools.
#
#   ./run.sh                       overlay
#   ./run.sh --launcher            window with a start/stop button
#   ./run.sh --check               environment diagnostics
#   ./run.sh --headless            no window, console output
#   ./run.sh --tool replay -- --combats --sim
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ./.venv/bin/python ]; then
    echo "No virtualenv yet. Run ./setup.sh first." >&2
    exit 1
fi

if [ "${1:-}" = "--tool" ]; then
    tool="${2:-}"
    if [ -z "$tool" ] || [ ! -f "tools/$tool.py" ]; then
        echo "Name a tool: replay | accuracy | preview | divergence | debugreview | calibrate" >&2
        exit 1
    fi
    shift 2
    [ "${1:-}" = "--" ] && shift
    exec ./.venv/bin/python "tools/$tool.py" "$@"
fi

exec ./.venv/bin/python -m hsbg "$@"
