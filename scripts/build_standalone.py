"""Build HanPlanet CLI standalone binaries with PyInstaller.

This script intentionally produces a directory build instead of a single-file
binary. HanPlanet CLI ships a React/Ink terminal frontend, so the runtime needs
frontend assets and optionally a bundled Node.js runtime next to the executable.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "terminal"
SPEC = ROOT / "packaging" / "pyinstaller" / "hanharness.spec"
DEFAULT_NODE_VERSION = "22.13.1"
CACHE = ROOT / ".standalone-cache"


def _run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _node_platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "win"
    if sys.platform.startswith("linux"):
        return "linux"
    raise SystemExit(f"Unsupported platform for bundled Node.js: {sys.platform}")


def _node_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    raise SystemExit(f"Unsupported CPU architecture for bundled Node.js: {machine}")


def _download_node(version: str) -> Path:
    node_platform = _node_platform()
    node_arch = _node_arch()
    archive_ext = "zip" if node_platform == "win" else "tar.xz"
    dirname = f"node-v{version}-{node_platform}-{node_arch}"
    archive = CACHE / "node" / f"{dirname}.{archive_ext}"
    extracted = CACHE / "node" / dirname
    if (extracted / ("node.exe" if node_platform == "win" else "bin/node")).exists():
        return extracted

    archive.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://nodejs.org/dist/v{version}/{dirname}.{archive_ext}"
    print(f"+ download {url}", flush=True)
    urllib.request.urlretrieve(url, archive)

    shutil.rmtree(extracted, ignore_errors=True)
    shutil.unpack_archive(str(archive), str(archive.parent))
    if not extracted.exists():
        raise SystemExit(f"Failed to unpack Node.js archive: {archive}")
    return extracted


def main() -> int:
    parser = argparse.ArgumentParser(description="Build standalone HanPlanet CLI executables.")
    parser.add_argument(
        "--skip-frontend-install",
        action="store_true",
        help="Do not run npm ci in frontend/terminal before packaging.",
    )
    parser.add_argument(
        "--node-dir",
        help=(
            "Optional Node.js runtime directory to bundle. Pass either the Node distribution root "
            "or its bin directory. Overrides the automatic Node.js download."
        ),
    )
    parser.add_argument(
        "--no-bundle-node",
        action="store_true",
        help="Do not bundle Node.js. Target machines need Node.js on PATH for the React TUI.",
    )
    parser.add_argument(
        "--node-version",
        default=os.environ.get("HANPLANET_CLI_NODE_VERSION", DEFAULT_NODE_VERSION),
        help=f"Node.js version to download when bundling automatically. Default: {DEFAULT_NODE_VERSION}.",
    )
    parser.add_argument("--clean", action="store_true", help="Remove build/ and dist/HanPlanet-CLI before building.")
    args = parser.parse_args()

    if args.clean:
        shutil.rmtree(ROOT / "build", ignore_errors=True)
        shutil.rmtree(ROOT / "dist" / "HanPlanet-CLI", ignore_errors=True)

    if not args.skip_frontend_install:
        _run(["npm", "ci", "--no-audit", "--no-fund"], cwd=FRONTEND)

    env = os.environ.copy()
    if args.node_dir:
        env["HANPLANET_CLI_BUNDLED_NODE_DIR"] = str(Path(args.node_dir).expanduser().resolve())
    elif not args.no_bundle_node:
        env["HANPLANET_CLI_BUNDLED_NODE_DIR"] = str(_download_node(args.node_version))

    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as _f:
        _version = tomllib.load(_f)["project"]["version"]
    (ROOT / "VERSION").write_text(_version, encoding="utf-8")
    print(f"VERSION file written: {_version}")

    _run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(SPEC)], env=env)
    print(f"\nStandalone build written to: {ROOT / 'dist' / 'HanPlanet-CLI'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
