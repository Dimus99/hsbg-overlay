"""Entry point of the built .app.

``python -m hsbg`` cannot be the entry point of a frozen bundle: there is no
interpreter to hand ``-m`` to. This script is what PyInstaller compiles in, and
it does the one thing the module form gets for free — arming ``freeze_support``
before anything else, so the simulation workers (spawned processes, which
re-run this file) do not each open a second overlay.
"""
import multiprocessing
import sys

from hsbg.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
