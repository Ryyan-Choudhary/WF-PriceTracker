"""Build the standalone Windows executable into dist/WF-PriceTracker.exe.

Usage (from the repo root, in your venv):
    pip install -e ".[build]"
    python scripts/build.py

This just wraps PyInstaller with WF-PriceTracker.spec after clearing old output.
The result is a single windowed exe you can double-click or attach to a GitHub
Release. See RELEASING.md.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    for stale in ("build", "dist"):
        shutil.rmtree(ROOT / stale, ignore_errors=True)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "WF-PriceTracker.spec"],
        cwd=ROOT,
        check=True,
    )
    exe = ROOT / "dist" / "WF-PriceTracker.exe"
    print(f"\nBuilt: {exe}" if exe.exists() else "\nBuild finished but exe not found - check output above.")


if __name__ == "__main__":
    main()
