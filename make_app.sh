#!/bin/bash
# Build "HSBG Overlay.app" — a real, self-contained macOS application.
#
#   ./make_app.sh                  build into dist/
#   ./make_app.sh /Applications    build and install there
#
# The bundle carries its own Python interpreter, PyObjC and the hsbg package,
# so it runs on a Mac that has neither this project folder nor the .venv:
# drag the .app anywhere, copy it to another Mac — it works.
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="$(pwd -P)"
DEST="${1:-$PROJECT/dist}"
APP="$DEST/HSBG Overlay.app"
PY=./.venv/bin/python

if [ ! -x "$PY" ]; then
    echo "No virtualenv yet. Run ./setup.sh first." >&2
    exit 1
fi

# PyInstaller is a build-time dependency only — it is not part of requirements.
if ! "$PY" -c 'import PyInstaller' >/dev/null 2>&1; then
    echo "Installing PyInstaller (build-time only)…"
    "$PY" -m pip install --quiet "pyinstaller>=6.11"
fi

# ---------------------------------------------------------------- icon
mkdir -p build
ICONSET="build/hsbg.iconset"
rm -rf "$ICONSET"
"$PY" tools/appicon.py "$ICONSET" >/dev/null
iconutil -c icns "$ICONSET" -o build/hsbg.icns
rm -rf "$ICONSET"

# ---------------------------------------------------------------- bundle
rm -rf build/hsbg dist/"HSBG Overlay.app" dist/"HSBG Overlay"
"$PY" -m PyInstaller --noconfirm --clean \
    --distpath dist --workpath build/hsbg \
    packaging/hsbg.spec

# PyInstaller leaves the COLLECT folder next to the .app; it is the same files
# again (the .app links to them), and only the bundle is the deliverable.
rm -rf dist/"HSBG Overlay"

# ------------------------------------------------------------- signature
# Unsigned bundles will not launch on Apple Silicon. An ad-hoc signature is
# enough for running it yourself; distributing to other Macs needs a Developer
# ID and notarisation, and there Gatekeeper still asks on first launch.
codesign --force --deep --sign - --timestamp=none dist/"HSBG Overlay.app" 2>/dev/null \
    || echo "Warning: signing failed — the app may refuse to launch." >&2

# ---------------------------------------------------------------- install
if [ "$DEST" != "$PROJECT/dist" ]; then
    mkdir -p "$DEST"
    rm -rf "$APP"
    ditto dist/"HSBG Overlay.app" "$APP"
fi

# Nudge Finder/LaunchServices to pick up the new bundle and its icon.
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP" >/dev/null 2>&1 || true

echo
echo "Done: $APP"
echo "Size: $(du -sh "$APP" | cut -f1)"
echo "Double-click it, drag it to the Dock or into /Applications."
