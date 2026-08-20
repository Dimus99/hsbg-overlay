# PyInstaller recipe for a self-contained "HSBG Overlay.app".
#
# Build it with ./make_app.sh — that script generates the icon first and knows
# where to put the result. Running pyinstaller on this file by hand works too,
# as long as the icon exists at build/hsbg.icns.
#
# The bundle carries its own Python and PyObjC, so the finished .app runs on a
# Mac that has neither this project nor the .venv.
import re
from pathlib import Path

PROJECT = Path(SPECPATH).parent

# Single source of truth for the version: hsbg/__init__.py.
VERSION = re.search(r'__version__ = "([^"]+)"',
                    (PROJECT / "hsbg" / "__init__.py").read_text("utf-8")).group(1)

analysis = Analysis(
    [str(PROJECT / "packaging" / "entry.py")],
    pathex=[str(PROJECT)],
    # Imported inside functions and picked up by the static analysis, but named
    # here as well so a refactor into a lazier import cannot quietly drop them.
    hiddenimports=[
        "hsbg.app",
        "hsbg.launcher",
        "hsbg.ui.overlay",
        "hsbg.ui.hswindow",
        "hsbg.sim.runner",
    ],
    # Nothing here reads the network through requests or renders with numpy;
    # excluding the usual suspects keeps the bundle around 40 MB.
    excludes=["tkinter", "numpy", "PIL", "pytest", "setuptools", "pip"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="HSBG Overlay",
    debug=False,
    strip=False,
    upx=False,
    console=False,          # no terminal window when launched from Finder
    argv_emulation=False,   # we take flags from ourselves, not from Finder
    target_arch=None,       # build for whatever Mac is building
    codesign_identity=None,  # ad-hoc signed by make_app.sh
    entitlements_file=None,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="HSBG Overlay",
)

app = BUNDLE(
    collect,
    name="HSBG Overlay.app",
    icon=str(PROJECT / "build" / "hsbg.icns"),
    bundle_identifier="local.hsbg.overlay",
    version=VERSION,
    info_plist={
        "CFBundleName": "HSBG Overlay",
        "CFBundleDisplayName": "HSBG Overlay",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHighResolutionCapable": True,
        # The overlay reads window geometry through CGWindowList and may ask
        # for screen access on newer macOS; the launcher window is a normal app.
        "NSHumanReadableCopyright": "",
    },
)
