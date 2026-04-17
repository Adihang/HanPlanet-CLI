"""Build HanHarness standalone binaries with PyInstaller.

This script intentionally produces a directory build instead of a single-file
binary. HanHarness ships a React/Ink terminal frontend, so the runtime needs
frontend assets and optionally a bundled Node.js runtime next to the executable.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "terminal"
SPEC = ROOT / "packaging" / "pyinstaller" / "hanharness.spec"


def _run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build standalone HanHarness executables.")
    parser.add_argument(
        "--skip-frontend-install",
        action="store_true",
        help="Do not run npm ci in frontend/terminal before packaging.",
    )
    parser.add_argument(
        "--node-dir",
        help=(
            "Optional Node.js runtime directory to bundle. Pass either the Node distribution root "
            "or its bin directory. Without this, target machines need Node.js on PATH for the React TUI."
        ),
    )
    parser.add_argument("--clean", action="store_true", help="Remove build/ and dist/HanHarness before building.")
    args = parser.parse_args()

    if args.clean:
        shutil.rmtree(ROOT / "build", ignore_errors=True)
        shutil.rmtree(ROOT / "dist" / "HanHarness", ignore_errors=True)

    if not args.skip_frontend_install:
        _run(["npm", "ci", "--no-audit", "--no-fund"], cwd=FRONTEND)

    env = os.environ.copy()
    if args.node_dir:
        env["HANHARNESS_BUNDLED_NODE_DIR"] = str(Path(args.node_dir).expanduser().resolve())

    _run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(SPEC)], env=env)
    print(f"\nStandalone build written to: {ROOT / 'dist' / 'HanHarness'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
