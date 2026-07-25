# Building & releasing

## Build the exe locally

From the repo root, in your virtual environment:

```powershell
pip install -e ".[build]"     # installs the app + PyInstaller
python scripts/build.py       # or: pyinstaller WF-PriceTracker.spec
```

The result is a single windowed executable at **`dist/WF-PriceTracker.exe`**.
`build/` and `dist/` are gitignored — the exe is distributed via GitHub Releases,
**not** committed to the repo.

**What's in the exe:** the **Tesseract** OCR engine only. EasyOCR is excluded on
purpose (it pulls in PyTorch, ~1–2 GB, and is fragile to freeze); use EasyOCR by
running from source. Tesseract itself is an external program and is **not**
bundled — users install it once:
`winget install --id UB-Mannheim.TesseractOCR -e`.

Runtime data (cache/logs/settings) is written to the per-user data dir
(`%LOCALAPPDATA%\WF-PriceTracker`), never next to the exe.

## Cut a release (recommended: automated)

A GitHub Actions workflow ([`.github/workflows/release.yml`](.github/workflows/release.yml))
builds the exe on a Windows runner and publishes a Release whenever you push a
version tag.

1. Bump the version in [`src/wf_pricer/__init__.py`](src/wf_pricer/__init__.py)
   (`__version__`) and move the `[Unreleased]` notes in
   [`CHANGELOG.md`](CHANGELOG.md) under the new version.
2. Commit those changes.
3. Tag and push:
   ```powershell
   git tag -a v1.0.0 -m "WF-PriceTracker 1.0.0"
   git push origin v1.0.0
   ```
4. Watch the **Actions** tab. When it finishes, a **Release** appears under the
   repo's *Releases* page with `WF-PriceTracker.exe` attached and auto-generated
   notes. Edit the notes if you like, then it's live for download.

> The workflow needs no secrets — it uses the built-in `GITHUB_TOKEN` (granted
> `contents: write` in the workflow) to create the release.

## Cut a release (manual alternative)

If you'd rather not use Actions:

1. Build locally: `python scripts/build.py`.
2. Create the release with the GitHub CLI:
   ```powershell
   gh release create v1.0.0 dist/WF-PriceTracker.exe --title "WF-PriceTracker 1.0.0" --notes-file notes.md
   ```
   …or on github.com: **Releases → Draft a new release → Choose a tag** (`v1.0.0`,
   *create new tag*) → write notes → **attach `dist/WF-PriceTracker.exe`** →
   **Publish release**.

## Versioning

Tags drive releases and follow [SemVer](https://semver.org): `vMAJOR.MINOR.PATCH`
(e.g. `v1.0.0`). Keep `__version__` and the git tag in sync.
