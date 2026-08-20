#!/bin/bash
# Create the virtualenv and install dependencies.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON=""
for candidate in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        arch=$("$candidate" -c 'import platform; print(platform.machine())' 2>/dev/null || echo "")
        if [ "$arch" = "arm64" ]; then PYTHON="$candidate"; break; fi
        [ -z "$PYTHON" ] && PYTHON="$candidate"
    fi
done

if [ -z "$PYTHON" ]; then
    echo "No Python 3 found. Install it with: brew install python@3.13" >&2
    exit 1
fi

echo "Using $PYTHON ($("$PYTHON" -c 'import platform,sys; print(platform.machine(), sys.version.split()[0])'))"
"$PYTHON" -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

echo
echo "Done. Next:"
echo "  ./run.sh --check    check the environment"
echo "  ./run.sh            start the overlay"
