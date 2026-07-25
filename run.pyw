"""Silent launcher — starts the app with no console window (run with
pythonw.exe; WF-PriceTracker.bat does exactly this). Use run.py instead when
you want to watch log output for setup or debugging.

Works from a plain checkout by putting src/ on the path; after `pip install -e .`
the `wf-pricer` command works too.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wf_pricer.main import main

if __name__ == "__main__":
    main()
