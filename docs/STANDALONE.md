# Standalone Builds

HanHarness can be packaged for users who do not have Python, pip, or pipx installed.
The supported approach is a PyInstaller `onedir` build.

## Why `onedir`

HanHarness is a Python CLI, but the default terminal UI is a React/Ink frontend
under `frontend/terminal`. A practical desktop distribution therefore needs:

- the Python runtime and Python dependencies;
- HanHarness and ohmo entry points;
- `frontend/terminal` assets and `node_modules`;
- a bundled Node.js runtime for the React TUI.

For this reason, `onedir` is preferred over `onefile`.

## Build On macOS

```bash
uv sync --extra dev --extra standalone
uv run python scripts/build_standalone.py --clean
```

The output is:

```text
dist/HanHarness/
  HanPlanet-CLI
  ohmo
  _internal/
```

Run it:

```bash
./dist/HanHarness/HanPlanet-CLI --version
./dist/HanHarness/HanPlanet-CLI -p "hello"
./dist/HanHarness/HanPlanet-CLI
```

## Build On Windows

Run this on Windows, not macOS. PyInstaller builds are platform-specific.

```powershell
uv sync --extra dev --extra standalone
uv run python scripts/build_standalone.py --clean
.\dist\HanHarness\HanPlanet-CLI.exe --version
```

## Bundling Node.js

Node.js is bundled by default. The build script downloads the matching Node.js
runtime for the current build platform into `.standalone-cache/node/` and passes
it to PyInstaller automatically.

The default Node.js version is pinned in `scripts/build_standalone.py`. Override
it when needed:

```bash
uv run python scripts/build_standalone.py --clean --node-version 22.13.1
```

If you intentionally do not bundle Node.js, the target machine needs `node`
available on `PATH` for the default React TUI. Non-interactive mode can still
work without Node:

```bash
uv run python scripts/build_standalone.py --clean --no-bundle-node
./dist/HanHarness/HanPlanet-CLI -p "hello"
```

To use a pre-downloaded Node.js runtime instead of the automatic download, pass
the Node distribution root or `bin` directory:

```bash
uv run python scripts/build_standalone.py --clean --node-dir /path/to/node-v20.x/bin
```

On Windows:

```powershell
uv run python scripts/build_standalone.py --clean --node-dir C:\path\to\node-v20.x-win-x64
```

The launcher prepends the bundled Node directory to `PATH` at runtime.

## Notes

- Build macOS binaries on macOS and Windows binaries on Windows.
- The output directory can be zipped and distributed.
- Code signing/notarization is still required for a polished macOS release.
- Windows SmartScreen reputation/signing is separate from this build step.
