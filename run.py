"""Console launcher — runs the app and streams log output to the terminal.
Handy for first-time setup and debugging. For everyday use, double-click
WF-PriceTracker.bat (or run run.pyw with pythonw.exe) for a silent, no-console
launch.

Works from a plain checkout by putting src/ on the path; after `pip install -e .`
you can instead run the `wf-pricer` command or `python -m wf_pricer`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wf_pricer.main import main

if __name__ == "__main__":
    main()
