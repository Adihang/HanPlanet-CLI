"""Build HanPlanet CLI standalone package and copy it to the Hanplanet HDD."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path("/Volumes/HANPLANET_HDD/Hanplanet/HanPlanet-CLI")
BUILD_OUTPUT = ROOT / "dist" / "HanPlanet-CLI"


def _run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a standalone HanPlanet CLI package into the Hanplanet HDD path."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Final package directory. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--skip-frontend-install",
        action="store_true",
        help="Reuse existing frontend/terminal/node_modules instead of running npm ci.",
    )
    args = parser.parse_args()

    build_command = [sys.executable, "scripts/build_standalone.py", "--clean"]
    if args.skip_frontend_install:
        build_command.append("--skip-frontend-install")
    _run(build_command)

    if not BUILD_OUTPUT.exists():
        raise SystemExit(f"Standalone build output not found: {BUILD_OUTPUT}")

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(output, ignore_errors=True)
    shutil.copytree(BUILD_OUTPUT, output)
    print(f"\nPackaged HanPlanet CLI written to: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
