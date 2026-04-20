# -*- mode: python ; coding: utf-8 -*-
# ruff: noqa: F821
"""PyInstaller spec for HanPlanet CLI standalone builds.

Build from the repository root with:
    pyinstaller --clean --noconfirm packaging/pyinstaller/hanharness.spec

Optional:
    HANPLANET_CLI_BUNDLED_NODE_DIR=/path/to/node-or-node/bin pyinstaller ...
"""

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path.cwd().resolve()
FRONTEND = ROOT / "frontend" / "terminal"


def _datas() -> list[tuple[str, str]]:
    datas: list[tuple[str, str]] = []
    for source, target in (
        (FRONTEND / "package.json", "openharness/_frontend"),
        (FRONTEND / "src", "openharness/_frontend/src"),
        (FRONTEND / "node_modules", "openharness/_frontend/node_modules"),
    ):
        if source.exists():
            datas.append((str(source), target))

    node_dir = os.environ.get("HANPLANET_CLI_BUNDLED_NODE_DIR", "").strip()
    if node_dir:
        path = Path(node_dir).expanduser().resolve()
        if path.exists():
            datas.append((str(path), "openharness/_node"))

    version_file = ROOT / "VERSION"
    if version_file.exists():
        datas.append((str(version_file), "."))

    datas.extend(collect_data_files("openharness", include_py_files=False))
    datas.extend(collect_data_files("ohmo", include_py_files=False))
    return datas


hiddenimports = sorted(
    set(collect_submodules("openharness"))
    | set(collect_submodules("ohmo"))
)


hanplanet_cli_analysis = Analysis(
    [str(ROOT / "packaging" / "pyinstaller" / "hanharness_entry.py")],
    pathex=[str(ROOT), str(ROOT / "src")],
    binaries=[],
    datas=_datas(),
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
hanplanet_cli_pyz = PYZ(hanplanet_cli_analysis.pure)
hanplanet_cli_exe = EXE(
    hanplanet_cli_pyz,
    hanplanet_cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="hanplanet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

ohmo_analysis = Analysis(
    [str(ROOT / "packaging" / "pyinstaller" / "ohmo_entry.py")],
    pathex=[str(ROOT), str(ROOT / "src")],
    binaries=[],
    datas=_datas(),
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
ohmo_pyz = PYZ(ohmo_analysis.pure)
ohmo_exe = EXE(
    ohmo_pyz,
    ohmo_analysis.scripts,
    [],
    exclude_binaries=True,
    name="ohmo",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    hanplanet_cli_exe,
    ohmo_exe,
    hanplanet_cli_analysis.binaries,
    hanplanet_cli_analysis.datas,
    ohmo_analysis.binaries,
    ohmo_analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="HanPlanet-CLI",
)
