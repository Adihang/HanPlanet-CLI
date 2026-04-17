# Standalone Builds

HanHarness can be packaged for users who do not have Python, pip, or pipx installed.
The supported approach is a PyInstaller `onedir` build.

## Why `onedir`

HanHarness is a Python CLI, but the default terminal UI is a React/Ink frontend
under `frontend/terminal`. A practical desktop distribution therefore needs:

- the Python runtime and Python dependencies;
- HanHarness and ohmo entry points;
- `frontend/terminal` assets and `node_modules`;
- Node.js on the user's `PATH`, or a bundled Node.js runtime.

For this reason, `onedir` is preferred over `onefile`.

## Build On macOS

```bash
uv sync --extra dev --extra standalone
uv run python scripts/build_standalone.py --clean
```

The output is:

```text
dist/HanHarness/
  hanharness
  ohmo
  _internal/
```

Run it:

```bash
./dist/HanHarness/hanharness --version
./dist/HanHarness/hanharness -p "hello"
./dist/HanHarness/hanharness
```

## Build On Windows

Run this on Windows, not macOS. PyInstaller builds are platform-specific.

```powershell
uv sync --extra dev --extra standalone
uv run python scripts/build_standalone.py --clean
.\dist\HanHarness\hanharness.exe --version
```

## Bundling Node.js

If you do not bundle Node.js, the target machine needs `node` available on
`PATH` for the default React TUI. Non-interactive mode can still work without
Node:

```bash
hanharness -p "hello"
```

To bundle Node.js, download the matching Node.js distribution for the build
platform and pass its root or `bin` directory:

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
