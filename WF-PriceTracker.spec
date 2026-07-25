# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for WF-PriceTracker.

Produces a single windowed Windows executable (dist/WF-PriceTracker.exe):

    pyinstaller WF-PriceTracker.spec        # or: python scripts/build.py

The packaged exe uses the **Tesseract** OCR engine (fast, tiny). EasyOCR is
deliberately excluded: it pulls in PyTorch (~1-2 GB) which bloats the exe and is
fragile to freeze. To use EasyOCR, run from source / a pip install instead.

Note: Tesseract is an external program pytesseract shells out to - it is NOT
bundled. Users still install it once (winget install UB-Mannheim.TesseractOCR).
"""

a = Analysis(
    ["run.pyw"],
    pathex=["src"],
    binaries=[],
    datas=[],
    # Imported lazily in the code (so PyInstaller's static analysis misses them)
    # or via a dynamically-selected backend.
    hiddenimports=[
        "pytesseract",
        "pystray._win32",
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Keep the exe lean: none of these are needed for the Tesseract build, and
    # excluding them stops PyInstaller pulling in the heavy ML stack by accident.
    excludes=[
        "easyocr", "torch", "torchvision", "torchaudio",
        "paddle", "paddleocr", "paddlex",
        "cv2", "scipy", "matplotlib", "IPython", "notebook",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WF-PriceTracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # windowed app (no console); logs go to the app-data log file
    disable_windowed_traceback=False,
    icon="assets/icon.ico",
)
