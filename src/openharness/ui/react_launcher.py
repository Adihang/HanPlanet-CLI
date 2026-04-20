"""Launch the default React terminal frontend."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path


def _resolve_theme() -> str:
    """Read the theme name from settings, defaulting to 'default'."""
    try:
        from openharness.config.settings import load_settings
        return load_settings().theme or "default"
    except Exception:
        return "default"


def _resolve_npm() -> str:
    """Resolve the npm executable (npm.cmd on Windows)."""
    return shutil.which("npm") or "npm"


def _bundled_node_dir() -> Path | None:
    """Return a bundled Node.js bin directory when present in a standalone build."""
    base = Path(__file__).resolve().parent.parent / "_node"
    candidates = [base]
    if sys.platform == "win32":
        candidates.append(base / "node-vendored")
    else:
        candidates.extend([base / "bin", base / "node-vendored" / "bin"])
    for candidate in candidates:
        node_name = "node.exe" if sys.platform == "win32" else "node"
        if (candidate / node_name).exists():
            return candidate
    return None


def _with_bundled_node_path(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment with bundled Node.js prepended to PATH when available."""
    updated = dict(env or os.environ)
    node_dir = _bundled_node_dir()
    if node_dir is None:
        return updated
    existing = updated.get("PATH", "")
    updated["PATH"] = str(node_dir) if not existing else f"{node_dir}{os.pathsep}{existing}"
    return updated


def _resolve_node() -> str | None:
    """Return the node executable path, preferring the bundled runtime."""
    node_dir = _bundled_node_dir()
    if node_dir is not None:
        node_name = "node.exe" if sys.platform == "win32" else "node"
        node_bin = node_dir / node_name
        if node_bin.exists():
            return str(node_bin)
    return shutil.which("node")


def _resolve_tsx(frontend_dir: Path) -> tuple[str, ...]:
    """Resolve the tsx command to invoke directly, bypassing ``npm exec``.

    On Windows / WSL the ``npm exec -- tsx`` wrapper chain often spawns
    intermediate ``cmd.exe`` / shell processes that break TTY stdin
    inheritance.  Calling the ``tsx`` binary directly preserves the TTY so
    that Ink's ``useInput`` (which requires raw-mode stdin) keeps working.

    Returns a tuple of command parts, e.g. ``("path/to/tsx",)`` or
    ``("npm", "exec", "--", "tsx")`` as last-resort fallback.
    """
    # 1. Prefer the locally-installed binary
    bin_dir = frontend_dir / "node_modules" / ".bin"
    if sys.platform == "win32":
        for name in ("tsx.cmd", "tsx.ps1", "tsx"):
            candidate = bin_dir / name
            if candidate.exists():
                return (str(candidate),)
    else:
        candidate = bin_dir / "tsx"
        if candidate.exists():
            return (str(candidate),)

    # 2. Fall back to a globally-installed tsx
    global_tsx = shutil.which("tsx")
    if global_tsx:
        return (global_tsx,)

    # 3. Last resort — go through npm exec (may break TTY on Windows/WSL)
    return (_resolve_npm(), "exec", "--", "tsx")


def get_frontend_dir() -> Path:
    """Return the React terminal frontend directory.

    Checks in order:
    1. Bundled inside the installed package (pip install)
    2. Development repo layout (source checkout)
    """
    # 1. Bundled inside package: openharness/_frontend/
    pkg_frontend = Path(__file__).resolve().parent.parent / "_frontend"
    if (pkg_frontend / "package.json").exists():
        return pkg_frontend

    # 2. Development repo: <repo>/frontend/terminal/
    repo_root = Path(__file__).resolve().parents[3]
    dev_frontend = repo_root / "frontend" / "terminal"
    if (dev_frontend / "package.json").exists():
        return dev_frontend

    # Fallback to package path (will error with clear message)
    return pkg_frontend


def build_backend_command(
    *,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    permission_mode: str | None = None,
) -> list[str]:
    """Return the command used by the React frontend to spawn the backend host."""
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--backend-only"]
    else:
        command = [sys.executable, "-m", "openharness", "--backend-only"]
    if cwd:
        command.extend(["--cwd", cwd])
    if model:
        command.extend(["--model", model])
    if max_turns is not None:
        command.extend(["--max-turns", str(max_turns)])
    if base_url:
        command.extend(["--base-url", base_url])
    if system_prompt:
        command.extend(["--system-prompt", system_prompt])
    if api_key:
        command.extend(["--api-key", api_key])
    if api_format:
        command.extend(["--api-format", api_format])
    if permission_mode:
        command.extend(["--permission-mode", permission_mode])
    return command


async def launch_react_tui(
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    permission_mode: str | None = None,
) -> int:
    """Launch the React terminal frontend as the default UI."""
    frontend_dir = get_frontend_dir()
    package_json = frontend_dir / "package.json"
    if not package_json.exists():
        raise RuntimeError(f"React terminal frontend is missing: {package_json}")

    npm = _resolve_npm()

    if not (frontend_dir / "node_modules").exists():
        import sys
        from rich.console import Console
        from rich.progress import Progress, SpinnerColumn, TextColumn

        _con = Console(file=sys.stderr)
        _con.print()
        # Installing React TUI dependencies on first run (최초 실행 시 의존성 설치)
        _con.print("[bold cyan]📦  React TUI 의존성 설치 중...[/bold cyan]")
        _con.print("[dim]  (최초 실행 시 1회. 잠시 기다려주세요)[/dim]")

        install = await asyncio.create_subprocess_exec(
            npm,
            "install",
            "--no-fund",
            "--no-audit",
            cwd=str(frontend_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]npm install[/cyan]"),
            console=_con,
            transient=True,
        ) as _progress:
            _progress.add_task("installing", total=None)
            ret = await install.wait()

        if ret != 0:
            _err = b""
            if install.stderr:
                _err = await install.stderr.read()
            raise RuntimeError(
                f"npm install 실패 (exit {ret}):\n{_err.decode(errors='replace')}"
            )

        _con.print("[bold green]✅  설치 완료![/bold green]")
        _con.print()

    env = _with_bundled_node_path(os.environ.copy())
    env["OPENHARNESS_FRONTEND_CONFIG"] = json.dumps(
        {
            "backend_command": build_backend_command(
                cwd=cwd or str(Path.cwd()),
                model=model,
                max_turns=max_turns,
                base_url=base_url,
                system_prompt=system_prompt,
                api_key=api_key,
                api_format=api_format,
                permission_mode=permission_mode,
            ),
            "initial_prompt": prompt,
            "theme": _resolve_theme(),
        }
    )
    tsx_cmd = _resolve_tsx(frontend_dir)
    process = await asyncio.create_subprocess_exec(
        *tsx_cmd,
        "src/index.tsx",
        cwd=str(frontend_dir),
        env=env,
        stdin=None,
        stdout=None,
        stderr=None,
    )
    return await process.wait()


__all__ = ["build_backend_command", "get_frontend_dir", "launch_react_tui"]
