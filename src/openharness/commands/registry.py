"""Slash command registry."""

from __future__ import annotations

import importlib.metadata
import asyncio
import json
import os
import re
import shutil
import subprocess
import tomllib
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, get_args, Iterable

import pyperclip

from openharness.autopilot import RepoAutopilotStore
from openharness.auth.manager import AuthManager
from openharness.config.paths import (
    get_config_dir,
    get_data_dir,
    get_feedback_log_path,
    get_project_config_dir,
    get_project_issue_file,
    get_project_pr_comments_file,
)
from openharness.bridge import get_bridge_manager
from openharness.bridge.types import WorkSecret
from openharness.bridge.work_secret import build_sdk_url, decode_work_secret, encode_work_secret
from openharness.api.client import ApiMessageCompleteEvent, ApiMessageRequest, ApiTextDeltaEvent
from openharness.api.provider import auth_status, detect_provider
from openharness.config.settings import Settings, display_model_setting, load_settings, save_settings
from openharness.engine.messages import ConversationMessage, sanitize_conversation_messages
from openharness.engine.query_engine import QueryEngine
from openharness.memory import (
    add_memory_entry,
    get_project_memory_dir,
    list_memory_files,
    remove_memory_entry,
)
from openharness.output_styles import load_output_styles
from openharness.permissions import PermissionChecker, PermissionMode
from openharness.plugins import load_plugins
from openharness.prompts import build_runtime_system_prompt
from openharness.plugins.installer import install_plugin_from_path, uninstall_plugin
from openharness.services import (
    build_post_compact_messages,
    compact_conversation,
    compact_messages,
    estimate_conversation_tokens,
    summarize_messages,
)
from openharness.services.session_backend import DEFAULT_SESSION_BACKEND, SessionBackend
from openharness.skills import load_skill_registry
from openharness.tasks import get_task_manager
from openharness.plugins.types import PluginCommandDefinition

if TYPE_CHECKING:
    from openharness.state import AppStateStore
    from openharness.tools.base import ToolRegistry


@dataclass
class CommandResult:
    """Result returned by a slash command."""

    message: str | None = None
    should_exit: bool = False
    clear_screen: bool = False
    replay_messages: list | None = None  # ConversationMessage list to replay in TUI
    continue_pending: bool = False
    continue_turns: int | None = None
    refresh_runtime: bool = False
    submit_prompt: str | None = None
    submit_model: str | None = None


@dataclass
class CommandContext:
    """Context available to command handlers."""

    engine: QueryEngine
    hooks_summary: str = ""
    mcp_summary: str = ""
    plugin_summary: str = ""
    cwd: str = "."
    tool_registry: ToolRegistry | None = None
    app_state: AppStateStore | None = None
    session_backend: SessionBackend = DEFAULT_SESSION_BACKEND
    session_id: str | None = None
    extra_skill_dirs: Iterable[str | Path] | None = None
    extra_plugin_roots: Iterable[str | Path] | None = None


CommandHandler = Callable[[str, CommandContext], Awaitable[CommandResult]]


@dataclass
class SlashCommand:
    """Definition of a slash command."""

    name: str
    description: str
    handler: CommandHandler
    remote_invocable: bool = True
    remote_admin_opt_in: bool = False


class CommandRegistry:
    """Map slash commands to handlers."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        """Register a command."""
        self._commands[command.name] = command

    def lookup(self, raw_input: str) -> tuple[SlashCommand, str] | None:
        """Parse a slash command and return its handler plus raw args."""
        if not raw_input.startswith("/"):
            return None
        name, _, args = raw_input[1:].partition(" ")
        command = self._commands.get(name)
        if command is None:
            return None
        return command, args.strip()

    def help_text(self) -> str:
        """Return a formatted summary of all registered commands."""
        lines = ["Available commands:"]
        for command in sorted(self._commands.values(), key=lambda item: item.name):
            lines.append(f"/{command.name:<12} {command.description}")
        return "\n".join(lines)

    def list_commands(self) -> list[SlashCommand]:
        """Return commands in registration order."""
        return list(self._commands.values())


def _run_git_command(cwd: str, *args: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",   # Explicit encoding to handle non-ASCII repo paths / commit messages
            errors="replace",   # Replace undecodable bytes rather than crashing
            check=False,
        )
    except FileNotFoundError:
        return False, "git is not installed."
    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        return False, output or f"git {' '.join(args)} failed"
    return True, output


def _read_text_excerpt(path: Path, max_chars: int = 2000) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(content) > max_chars:
        return content[:max_chars].rstrip() + "\n...[truncated]..."
    return content


def _first_markdown_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _load_pyproject(path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _generate_claude_md(cwd: Path) -> str:
    project_name = cwd.name
    readme = cwd / "README.md"
    pyproject = cwd / "pyproject.toml"
    package_json = cwd / "package.json"
    project_summary = None
    detected_dirs: list[str] = []
    commands: list[str] = []

    if readme.exists():
        readme_text = _read_text_excerpt(readme, max_chars=4000)
        project_summary = _first_markdown_heading(readme_text) or "Local project README found."

    pyproject_data = _load_pyproject(pyproject) if pyproject.exists() else {}
    project_data = pyproject_data.get("project")
    if isinstance(project_data, dict):
        name = project_data.get("name")
        if isinstance(name, str) and name.strip():
            project_name = name.strip()
        scripts = project_data.get("scripts")
        if isinstance(scripts, dict):
            for script_name, script_cmd in scripts.items():
                if isinstance(script_name, str) and isinstance(script_cmd, str):
                    commands.append(f"- `{script_name}`: `{script_cmd}`")

    if (cwd / "src").is_dir():
        detected_dirs.append("src")
    if (cwd / "tests").is_dir():
        detected_dirs.append("tests")
    if (cwd / "scripts").is_dir():
        detected_dirs.append("scripts")
    if (cwd / "frontend").is_dir():
        detected_dirs.append("frontend")
    if (cwd / "ohmo").is_dir():
        detected_dirs.append("ohmo")

    if pyproject.exists():
        commands.extend(
            [
                "- `uv sync --extra dev`",
                "- `uv run pytest -q`",
                "- `uv run ruff check src tests scripts`",
            ]
        )
        if (cwd / "frontend" / "terminal").is_dir():
            commands.append("- `cd frontend/terminal && npm ci`")
    elif package_json.exists():
        commands.extend(
            [
                "- `npm install`",
                "- `npm test`",
            ]
        )

    lines = [
        "# Project Instructions",
        "",
        "This file was generated from a local project scan. Review and refine it before relying on it.",
        "",
        "## Overview",
        f"- Project: `{project_name}`",
    ]
    if project_summary:
        lines.append(f"- README summary: {project_summary}")
    if detected_dirs:
        lines.append(f"- Detected directories: {', '.join(f'`{item}`' for item in detected_dirs)}")

    lines.extend(
        [
            "",
            "## Working Rules",
            "- Use OpenHarness tools deliberately.",
            "- Keep changes minimal and verify with tests when possible.",
            "- Read relevant files before editing.",
            "- Prefer editing existing files over adding new ones unless necessary.",
            "",
            "## Suggested Commands",
        ]
    )
    if commands:
        lines.extend(commands)
    else:
        lines.append("- Add project-specific commands here.")

    return "\n".join(lines) + "\n"


def _list_dir_names(path: Path) -> list[str]:
    try:
        return sorted(p.name for p in path.iterdir() if not p.name.startswith("."))
    except OSError:
        return []


def _build_claude_md_scan_summary(cwd: Path) -> str:
    parts: list[str] = []
    readme = cwd / "README.md"
    pyproject = cwd / "pyproject.toml"
    package_json = cwd / "package.json"

    # README excerpt (first 2000 chars)
    if readme.exists():
        excerpt = _read_text_excerpt(readme, max_chars=2000)
        if excerpt:
            parts.append(f"README excerpt:\n{excerpt}")

    # pyproject.toml full project metadata + tool config
    if pyproject.exists():
        pyproject_data = _load_pyproject(pyproject)
        project_data = pyproject_data.get("project")
        if isinstance(project_data, dict):
            name = project_data.get("name")
            if isinstance(name, str) and name.strip():
                parts.append(f"Project name: {name.strip()}")
            version = project_data.get("version")
            if isinstance(version, str):
                parts.append(f"Version: {version}")
            requires_python = project_data.get("requires-python")
            if isinstance(requires_python, str):
                parts.append(f"Requires Python: {requires_python}")
            dependencies = project_data.get("dependencies")
            if isinstance(dependencies, list) and dependencies:
                preview = ", ".join(str(item) for item in dependencies)
                parts.append(f"Dependencies: {preview}")
            optional_deps = project_data.get("optional-dependencies")
            if isinstance(optional_deps, dict):
                for group, deps in optional_deps.items():
                    if isinstance(deps, list):
                        parts.append(f"Optional [{group}]: {', '.join(str(d) for d in deps)}")
            scripts = project_data.get("scripts")
            if isinstance(scripts, dict) and scripts:
                script_lines = ", ".join(f"{k}={v}" for k, v in scripts.items())
                parts.append(f"CLI scripts: {script_lines}")

    # package.json scripts
    if package_json.exists():
        try:
            import json as _json
            pkg = _json.loads(package_json.read_text(encoding="utf-8"))
            pkg_scripts = pkg.get("scripts", {})
            if pkg_scripts:
                s = ", ".join(f"{k}: {v}" for k, v in list(pkg_scripts.items())[:10])
                parts.append(f"npm scripts: {s}")
        except Exception:
            pass

    # Top-level directory listing
    top_dirs = [p.name for p in sorted(cwd.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    if top_dirs:
        parts.append(f"Top-level directories: {', '.join(top_dirs)}")

    # Source package structure
    src_dir = cwd / "src"
    if src_dir.is_dir():
        for pkg_dir in sorted(src_dir.iterdir()):
            if pkg_dir.is_dir() and not pkg_dir.name.startswith("."):
                submodules = _list_dir_names(pkg_dir)
                parts.append(f"src/{pkg_dir.name}/ modules: {', '.join(submodules)}")
                # Read __init__.py or main entry file for public API hints
                for candidate in ("__init__.py", "main.py", "app.py", "cli.py"):
                    entry = pkg_dir / candidate
                    if entry.exists():
                        excerpt = _read_text_excerpt(entry, max_chars=1500)
                        if excerpt:
                            parts.append(f"src/{pkg_dir.name}/{candidate} excerpt:\n{excerpt}")
                        break

    # ohmo package (top-level sibling package)
    ohmo_dir = cwd / "ohmo"
    if ohmo_dir.is_dir():
        ohmo_modules = _list_dir_names(ohmo_dir)
        parts.append(f"ohmo/ modules: {', '.join(ohmo_modules)}")
        ohmo_cli = ohmo_dir / "cli.py"
        if ohmo_cli.exists():
            parts.append(f"ohmo/cli.py excerpt:\n{_read_text_excerpt(ohmo_cli, max_chars=800)}")

    # Test directory layout
    tests_dir = cwd / "tests"
    if tests_dir.is_dir():
        test_groups = _list_dir_names(tests_dir)
        parts.append(f"Test groups: {', '.join(test_groups)}")

    # Frontend structure
    frontend_dir = cwd / "frontend"
    if frontend_dir.is_dir():
        for subdir in sorted(frontend_dir.iterdir()):
            if subdir.is_dir():
                src = subdir / "src"
                if src.is_dir():
                    components = _list_dir_names(src)
                    parts.append(f"frontend/{subdir.name}/src/: {', '.join(components)}")
                pkg = subdir / "package.json"
                if pkg.exists():
                    try:
                        import json as _json2
                        p = _json2.loads(pkg.read_text(encoding="utf-8"))
                        s = p.get("scripts", {})
                        if s:
                            parts.append(
                                f"frontend/{subdir.name} npm scripts: "
                                + ", ".join(f"{k}: {v}" for k, v in list(s.items())[:6])
                            )
                    except Exception:
                        pass

    # Key config files
    for cfg in ("CLAUDE.md", ".env.example", "Makefile", "docker-compose.yml", "Dockerfile"):
        cfg_path = cwd / cfg
        if cfg_path.exists():
            excerpt = _read_text_excerpt(cfg_path, max_chars=1000)
            if excerpt:
                parts.append(f"{cfg} excerpt:\n{excerpt}")

    # scripts/ directory listing
    scripts_dir = cwd / "scripts"
    if scripts_dir.is_dir():
        script_files = _list_dir_names(scripts_dir)
        parts.append(f"scripts/: {', '.join(script_files)}")

    # CI/CD workflows
    workflows_dir = cwd / ".github" / "workflows"
    if workflows_dir.is_dir():
        workflows = _list_dir_names(workflows_dir)
        parts.append(f".github/workflows/: {', '.join(workflows)}")

    if not parts:
        return "No notable project files were found."
    return "\n\n".join(parts)


def _extract_text_from_assistant_message(message: ConversationMessage) -> str:
    return message.text.strip()


async def _draft_claude_md_with_model(cwd: Path, context: CommandContext) -> tuple[str | None, bool]:
    try:
        scan_summary = _build_claude_md_scan_summary(cwd)
        request_prompt = "\n".join(
            [
                "Write a CLAUDE.md file for this repository based solely on the project scan below.",
                "Return markdown only. Do not wrap the answer in code fences.",
                "Start with this exact header:",
                "# CLAUDE.md",
                "",
                "This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.",
                "",
                "Then include these sections (only if supported by the scan data):",
                "1. Commands — exact build/test/lint/run commands derived from scripts and config",
                "2. Architecture — high-level structure: entry points, request flow, key modules",
                "3. Key conventions — patterns specific to this codebase (tool registration, config, etc.)",
                "",
                "Rules:",
                "- Only use information present in the scan. Do not invent paths, commands, or module names.",
                "- Be specific: name actual files, modules, and commands found in the scan.",
                "- Omit obvious practices (write tests, use version control, etc.).",
                "- Keep it concise — prefer bullets over prose.",
                "",
                "Project scan:",
                scan_summary,
            ]
        )
        system_prompt = "\n".join(
            [
                "You are generating a CLAUDE.md project instruction file.",
                "Base every claim strictly on the provided project scan.",
                "Be specific and concrete — name actual files and commands from the scan.",
                "Do not add generic advice or invent anything not found in the scan.",
            ]
        )
        request = ApiMessageRequest(
            model=context.engine.model,
            messages=[ConversationMessage.from_user_text(request_prompt)],
            system_prompt=system_prompt,
            max_tokens=4096,
        )

        chunks: list[str] = []
        final_message: ConversationMessage | None = None
        stream = context.engine.api_client.stream_message(request)
        if not hasattr(stream, "__aiter__"):
            stream = await stream
        async for event in stream:
            if isinstance(event, ApiTextDeltaEvent):
                chunks.append(event.text)
            elif isinstance(event, ApiMessageCompleteEvent):
                final_message = event.message
                if not chunks:
                    chunks.append(_extract_text_from_assistant_message(event.message))

        draft = "".join(chunks).strip()
        if not draft and final_message is not None:
            draft = _extract_text_from_assistant_message(final_message)
        if not draft:
            return None, "empty model response"
        return draft, None
    except Exception as exc:
        reason = "model request failed"
        details = str(exc).strip()
        if details:
            reason = f"{reason}: {details}"
        return None, reason


def _copy_to_clipboard(text: str) -> tuple[bool, str]:
    try:
        pyperclip.copy(text)
        return True, "clipboard"
    except Exception:
        for command in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard"]):
            try:
                subprocess.run(command, input=text, text=True, check=True, capture_output=True)
                return True, "clipboard"
            except Exception:
                continue
    fallback = get_data_dir() / "last_copy.txt"
    fallback.write_text(text, encoding="utf-8")
    return False, str(fallback)


def _last_message_text(messages: list[ConversationMessage]) -> str:
    for message in reversed(messages):
        if message.text.strip():
            return message.text.strip()
    return ""


def _shorten_text(text: str, *, limit: int = 160) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _rewind_turns(messages: list[ConversationMessage], turns: int) -> list[ConversationMessage]:
    updated = list(messages)
    for _ in range(max(0, turns)):
        if not updated:
            break
        while updated:
            popped = updated.pop()
            if popped.role == "user" and popped.text.strip():
                break
    return updated


def _coerce_setting_value(settings: Settings, key: str, raw: str):
    field = Settings.model_fields.get(key)
    if field is None:
        raise KeyError(key)
    annotation = field.annotation
    if annotation is bool:
        lowered = raw.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"Invalid boolean value for {key}: {raw}")
    if annotation is int:
        return int(raw)
    if annotation is str:
        return raw
    if annotation is Literal or getattr(annotation, "__origin__", None) is Literal:
        allowed = get_args(annotation)
        if raw not in allowed:
            raise ValueError(f"Invalid value for {key}: {raw}")
        return raw
    return raw


def _is_real_tty() -> bool:
    """Return True when running in an interactive real terminal."""
    import os
    try:
        return os.isatty(0) and os.isatty(1)
    except Exception:
        return False


async def _qselect(prompt: str, choices: list, *, default=None):
    """Run questionary.select in a thread and return the result."""
    import asyncio
    import questionary
    def _run():
        return questionary.select(prompt, choices=choices, default=default).ask()
    return await asyncio.to_thread(_run)


async def _qtext(prompt: str, *, default: str = "") -> str | None:
    """Run questionary.text in a thread and return the result."""
    import asyncio
    import questionary
    def _run():
        return questionary.text(prompt, default=default).ask()
    return await asyncio.to_thread(_run)


def _render_plugin_command_prompt(command: PluginCommandDefinition, args: str, session_id: str | None = None) -> str:
    prompt = command.content
    raw_args = args.strip()
    if command.is_skill and command.base_dir:
        prompt = f"Base directory for this skill: {command.base_dir}\n\n{prompt}"
    prompt = prompt.replace("${ARGUMENTS}", raw_args).replace("$ARGUMENTS", raw_args)
    if session_id:
        prompt = prompt.replace("${CLAUDE_SESSION_ID}", session_id)
    if raw_args and "${ARGUMENTS}" not in command.content and "$ARGUMENTS" not in command.content:
        prompt = f"{prompt}\n\nArguments: {raw_args}"
    return prompt


def create_default_command_registry(
    plugin_commands: Iterable[PluginCommandDefinition] | None = None,
) -> CommandRegistry:
    """Create the built-in command registry."""
    registry = CommandRegistry()

    async def _help_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        return CommandResult(message=registry.help_text())

    async def _exit_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        return CommandResult(should_exit=True)

    async def _clear_handler(_: str, context: CommandContext) -> CommandResult:
        context.engine.clear()
        return CommandResult(message="Conversation cleared.", clear_screen=True)

    async def _status_handler(_: str, context: CommandContext) -> CommandResult:
        usage = context.engine.total_usage
        state = context.app_state.get() if context.app_state is not None else None
        manager = AuthManager()
        return CommandResult(
            message=(
                f"Messages: {len(context.engine.messages)}\n"
                f"Usage: input={usage.input_tokens} output={usage.output_tokens}\n"
                f"Profile: {manager.get_active_profile()}\n"
                f"Effort: {state.effort if state is not None else load_settings().effort}\n"
                f"Passes: {state.passes if state is not None else load_settings().passes}"
            )
        )

    async def _version_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        try:
            version = importlib.metadata.version("HanPlanet-CLI")
        except importlib.metadata.PackageNotFoundError:
            version = "0.1.7"
        return CommandResult(message=f"HanPlanet CLI {version}")

    async def _context_handler(_: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        prompt = build_runtime_system_prompt(settings, cwd=context.cwd)
        return CommandResult(message=prompt)

    async def _summary_handler(args: str, context: CommandContext) -> CommandResult:
        max_messages = 8
        if args:
            try:
                max_messages = max(1, int(args))
            except ValueError:
                return CommandResult(message="Usage: /summary [MAX_MESSAGES]")
        summary = summarize_messages(context.engine.messages, max_messages=max_messages)
        return CommandResult(message=summary or "No conversation content to summarize.")

    async def _compact_handler(args: str, context: CommandContext) -> CommandResult:
        preserve_recent = 6
        if args:
            try:
                preserve_recent = max(1, int(args))
            except ValueError:
                return CommandResult(message="Usage: /compact [PRESERVE_RECENT]")
        before = len(context.engine.messages)
        try:
            compacted_result = await compact_conversation(
                context.engine.messages,
                api_client=context.engine.api_client,
                model=context.engine.model,
                system_prompt=context.engine.system_prompt,
                preserve_recent=preserve_recent,
                trigger="manual",
            )
            compacted = build_post_compact_messages(compacted_result)
        except Exception:
            compacted = compact_messages(context.engine.messages, preserve_recent=preserve_recent)
        context.engine.load_messages(compacted)
        return CommandResult(
            message=f"Compacted conversation from {before} messages to {len(compacted)}."
        )

    async def _usage_handler(_: str, context: CommandContext) -> CommandResult:
        usage = context.engine.total_usage
        estimated = estimate_conversation_tokens(context.engine.messages)
        return CommandResult(
            message=(
                f"Actual usage: input={usage.input_tokens} output={usage.output_tokens}\n"
                f"Estimated conversation tokens: {estimated}\n"
                f"Messages: {len(context.engine.messages)}"
            )
        )

    async def _cost_handler(_: str, context: CommandContext) -> CommandResult:
        usage = context.engine.total_usage
        model = context.app_state.get().model if context.app_state is not None else load_settings().model
        estimated_cost = "unavailable"
        if model.startswith("claude-3-5-sonnet"):
            estimated = (usage.input_tokens * 3.0 + usage.output_tokens * 15.0) / 1_000_000
            estimated_cost = f"${estimated:.4f} (estimated)"
        elif model.startswith("claude-3-7-sonnet"):
            estimated = (usage.input_tokens * 3.0 + usage.output_tokens * 15.0) / 1_000_000
            estimated_cost = f"${estimated:.4f} (estimated)"
        elif model.startswith("claude-3-opus"):
            estimated = (usage.input_tokens * 15.0 + usage.output_tokens * 75.0) / 1_000_000
            estimated_cost = f"${estimated:.4f} (estimated)"
        return CommandResult(
            message=(
                f"Model: {model}\n"
                f"Input tokens: {usage.input_tokens}\n"
                f"Output tokens: {usage.output_tokens}\n"
                f"Total tokens: {usage.total_tokens}\n"
                f"Estimated cost: {estimated_cost}"
            )
        )

    async def _stats_handler(_: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        memory_count = len(list_memory_files(context.cwd))
        task_count = len(get_task_manager().list_tasks())
        tool_count = len(context.tool_registry.list_tools()) if context.tool_registry is not None else 0
        style = settings.output_style
        if context.app_state is not None:
            state = context.app_state.get()
            style = state.output_style
        return CommandResult(
            message=(
                "Session stats:\n"
                f"- messages: {len(context.engine.messages)}\n"
                f"- estimated_tokens: {estimate_conversation_tokens(context.engine.messages)}\n"
                f"- tools: {tool_count}\n"
                f"- memory_files: {memory_count}\n"
                f"- background_tasks: {task_count}\n"
                f"- output_style: {style}"
            )
        )

    async def _memory_handler(args: str, context: CommandContext) -> CommandResult:
        tokens = args.split(maxsplit=1)
        action = tokens[0] if tokens else ""
        rest = tokens[1] if len(tokens) == 2 else ""
        if action == "list":
            memory_files = list_memory_files(context.cwd)
            if not memory_files:
                return CommandResult(message="No memory files.")
            return CommandResult(message="\n".join(path.name for path in memory_files))
        if action == "show" and rest:
            memory_dir = get_project_memory_dir(context.cwd)
            path, invalid = _resolve_memory_entry_path(memory_dir, rest)
            if invalid:
                return CommandResult(message="Memory entry path must stay within the project memory directory.")
            if path is None:
                return CommandResult(message=f"Memory entry not found: {rest}")
            if not path.exists():
                return CommandResult(message=f"Memory entry not found: {rest}")
            return CommandResult(message=path.read_text(encoding="utf-8"))
        if action == "add" and rest:
            title, separator, content = rest.partition("::")
            if not separator or not title.strip() or not content.strip():
                return CommandResult(message="Usage: /memory add TITLE :: CONTENT")
            path = add_memory_entry(context.cwd, title.strip(), content.strip())
            return CommandResult(message=f"Added memory entry {path.name}")
        if action == "remove" and rest:
            if remove_memory_entry(context.cwd, rest.strip()):
                return CommandResult(message=f"Removed memory entry {rest.strip()}")
            return CommandResult(message=f"Memory entry not found: {rest.strip()}")
        return CommandResult(message="Usage: /memory [list|show NAME|add TITLE :: CONTENT|remove NAME]")

    async def _hooks_handler(_: str, context: CommandContext) -> CommandResult:
        return CommandResult(message=context.hooks_summary or "No hooks configured.")

    async def _resume_handler(args: str, context: CommandContext) -> CommandResult:
        tokens = args.strip().split()

        # /resume <session_id> — load a specific session
        if tokens:
            sid = tokens[0]
            snapshot = context.session_backend.load_by_id(context.cwd, sid)
            if snapshot is None:
                return CommandResult(message=f"Session not found: {sid}")
            messages = sanitize_conversation_messages(
                [ConversationMessage.model_validate(item) for item in snapshot.get("messages", [])]
            )
            context.engine.load_messages(messages)
            summary = snapshot.get("summary", "")[:60]
            return CommandResult(
                message=f"Restored {len(messages)} messages from session {sid}"
                + (f" ({summary})" if summary else ""),
                replay_messages=messages,
            )

        # /resume — list sessions (for the TUI to show a picker)
        sessions = context.session_backend.list_snapshots(context.cwd, limit=10)
        if not sessions:
            # Fall back to latest.json
            snapshot = context.session_backend.load_latest(context.cwd)
            if snapshot is None:
                return CommandResult(message="No saved sessions found for this project.")
            messages = sanitize_conversation_messages(
                [ConversationMessage.model_validate(item) for item in snapshot.get("messages", [])]
            )
            context.engine.load_messages(messages)
            return CommandResult(
                message=f"Restored {len(messages)} messages from the latest session.",
                replay_messages=messages,
            )

        # Format session list for display / picker
        import time
        lines = ["Saved sessions:"]
        for s in sessions:
            ts = time.strftime("%m/%d %H:%M", time.localtime(s["created_at"]))
            summary = s["summary"][:50] or "(no summary)"
            lines.append(f"  {s['session_id']}  {ts}  {s['message_count']}msg  {summary}")
        lines.append("")
        lines.append("Use /resume <session_id> to restore a specific session.")
        return CommandResult(message="\n".join(lines))

    async def _export_handler(_: str, context: CommandContext) -> CommandResult:
        path = context.session_backend.export_markdown(cwd=context.cwd, messages=context.engine.messages)
        return CommandResult(message=f"Exported transcript to {path}")

    async def _share_handler(_: str, context: CommandContext) -> CommandResult:
        path = context.session_backend.export_markdown(cwd=context.cwd, messages=context.engine.messages)
        return CommandResult(message=f"Created shareable transcript snapshot at {path}")

    async def _copy_handler(args: str, context: CommandContext) -> CommandResult:
        text = args.strip() or _last_message_text(context.engine.messages)
        if not text:
            return CommandResult(message="Nothing to copy.")
        copied, target = _copy_to_clipboard(text)
        if copied:
            return CommandResult(message=f"Copied {len(text)} characters to the clipboard.")
        return CommandResult(message=f"Clipboard unavailable. Saved copied text to {target}")

    async def _session_handler(args: str, context: CommandContext) -> CommandResult:
        session_dir = context.session_backend.get_session_dir(context.cwd)
        tokens = args.split()
        if not tokens or tokens[0] == "show":
            latest = session_dir / "latest.json"
            transcript = session_dir / "transcript.md"
            lines = [
                f"Session directory: {session_dir}",
                f"Latest snapshot: {'present' if latest.exists() else 'missing'}",
                f"Transcript export: {'present' if transcript.exists() else 'missing'}",
                f"Message count: {len(context.engine.messages)}",
            ]
            return CommandResult(message="\n".join(lines))
        if tokens[0] == "ls":
            files = sorted(path.name for path in session_dir.iterdir())
            return CommandResult(message="\n".join(files) if files else "(empty)")
        if tokens[0] == "path":
            return CommandResult(message=str(session_dir))
        if tokens[0] == "tag" and len(tokens) == 2:
            safe_name = "".join(character for character in tokens[1] if character.isalnum() or character in {"-", "_"})
            if not safe_name:
                return CommandResult(message="Usage: /session tag NAME")
            snapshot_path = context.session_backend.save_snapshot(
                cwd=context.cwd,
                model=context.app_state.get().model if context.app_state is not None else load_settings().model,
                system_prompt=build_runtime_system_prompt(load_settings(), cwd=context.cwd),
                messages=context.engine.messages,
                usage=context.engine.total_usage,
            )
            export_path = context.session_backend.export_markdown(cwd=context.cwd, messages=context.engine.messages)
            tagged_json = session_dir / f"{safe_name}.json"
            tagged_md = session_dir / f"{safe_name}.md"
            shutil.copy2(snapshot_path, tagged_json)
            shutil.copy2(export_path, tagged_md)
            return CommandResult(message=f"Tagged session as {safe_name}:\n- {tagged_json}\n- {tagged_md}")
        if tokens[0] == "clear":
            if session_dir.exists():
                shutil.rmtree(session_dir)
            session_dir.mkdir(parents=True, exist_ok=True)
            return CommandResult(message=f"Cleared session storage at {session_dir}")
        return CommandResult(message="Usage: /session [show|ls|path|tag NAME|clear]")

    async def _rewind_handler(args: str, context: CommandContext) -> CommandResult:
        turns = 1
        if args.strip():
            try:
                turns = max(1, int(args.strip()))
            except ValueError:
                return CommandResult(message="Usage: /rewind [TURNS]")
        before = len(context.engine.messages)
        updated = _rewind_turns(context.engine.messages, turns)
        context.engine.load_messages(updated)
        removed = before - len(updated)
        return CommandResult(message=f"Rewound {turns} turn(s); removed {removed} message(s).")

    async def _tag_handler(args: str, context: CommandContext) -> CommandResult:
        name = args.strip()
        if not name:
            return CommandResult(message="Usage: /tag NAME")
        return await _session_handler(f"tag {name}", context)

    async def _files_handler(args: str, context: CommandContext) -> CommandResult:
        raw = args.strip()
        root = Path(context.cwd)
        max_items = 30
        tokens = raw.split(maxsplit=1)
        if tokens and tokens[0] == "dirs":
            dirs = [
                path
                for path in sorted(root.rglob("*"))
                if path.is_dir() and ".git" not in path.parts and ".venv" not in path.parts
            ]
            lines = [str(path.relative_to(root)) for path in dirs[:max_items]]
            if len(dirs) > max_items:
                lines.append(f"... {len(dirs) - max_items} more")
            return CommandResult(message="\n".join(lines) if lines else "(no directories)")
        if tokens and tokens[0].isdigit():
            max_items = max(1, min(int(tokens[0]), 200))
            raw = tokens[1] if len(tokens) == 2 else ""
        needle = raw.lower()
        files = [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file() and ".git" not in path.parts and ".venv" not in path.parts
        ]
        if needle:
            files = [path for path in files if needle in str(path.relative_to(root)).lower()]
        lines = [str(path.relative_to(root)) for path in files[:max_items]]
        if len(files) > max_items:
            lines.append(f"... {len(files) - max_items} more")
        return CommandResult(
            message="\n".join(lines) if lines else "(no matching files)"
        )

    async def _agents_handler(args: str, context: CommandContext) -> CommandResult:
        tokens = args.split(maxsplit=1)
        guide = (
            "Subagent guide:\n"
            "- Ask the model to delegate with the `agent` tool when the task needs background work or parallel investigation.\n"
            '- The usual worker shape is subagent_type="worker".\n'
            "- /agents lists known worker tasks.\n"
            "- /agents show TASK_ID shows one worker's output and metadata.\n"
            "- send_message(task_id=..., message=...) can continue a spawned worker.\n"
            "- task_output(task_id=...) reads the worker's latest output."
        )
        if tokens and tokens[0] in {"help", "usage"}:
            return CommandResult(
                message=guide
            )
        if tokens and tokens[0] == "show" and len(tokens) == 2:
            task = get_task_manager().get_task(tokens[1])
            if task is None or task.type not in {"local_agent", "remote_agent", "in_process_teammate"}:
                return CommandResult(message=f"No agent found with ID: {tokens[1]}")
            output = get_task_manager().read_task_output(task.id)
            return CommandResult(
                message=(
                    f"{task.id} {task.type} {task.status} {task.description}\n"
                    f"metadata={task.metadata}\n"
                    f"output:\n{output or '(no output)'}"
                )
            )
        tasks = [
            task
            for task in get_task_manager().list_tasks()
            if task.type in {"local_agent", "remote_agent", "in_process_teammate"}
        ]
        if not tasks:
            return CommandResult(
                message=f"No active or recorded agents. Run /agents help for usage.\n\n{guide}"
            )
        lines = [
            f"{task.id} {task.type} {task.status} {task.description}"
            for task in tasks
        ]
        return CommandResult(message="\n".join(lines))

    async def _init_handler(args: str, context: CommandContext) -> CommandResult:
        tokens = args.split()
        force = any(token in {"--force", "-f"} for token in tokens)
        cwd = Path(context.cwd)
        project_dir = get_project_config_dir(cwd)
        created: list[str] = []

        claudemd = cwd / "CLAUDE.md"
        claudemd_existed = claudemd.exists()
        fallback_reason: str | None = None
        if force or not claudemd.exists():
            drafted, fallback_reason = await _draft_claude_md_with_model(cwd, context)
            claudemd.write_text(drafted or _generate_claude_md(cwd), encoding="utf-8")
            created.append(str(claudemd.relative_to(Path(context.cwd))))

        for relative, content in (
            (
                project_dir / "README.md",
                "# Project OpenHarness Config\n\nThis directory stores project-specific OpenHarness state.\n",
            ),
            (
                project_dir / "memory" / "MEMORY.md",
                "# Project Memory\n\nAdd reusable project knowledge here.\n",
            ),
            (
                project_dir / "plugins" / ".gitkeep",
                "",
            ),
            (
                project_dir / "skills" / ".gitkeep",
                "",
            ),
        ):
            relative.parent.mkdir(parents=True, exist_ok=True)
            if not relative.exists():
                relative.write_text(content, encoding="utf-8")
                created.append(str(relative.relative_to(Path(context.cwd))))

        if not created:
            return CommandResult(message="Project already initialized for OpenHarness.")
        header = "Initialized project files"
        if force and claudemd_existed:
            header = "Regenerated project files"
        message = f"{header}:\n" + "\n".join(f"- {item}" for item in created)
        if fallback_reason:
            message += f"\n\nAI draft failed. Used local scan. Reason: {fallback_reason}"
        return CommandResult(message=message)

    async def _bridge_handler(args: str, context: CommandContext) -> CommandResult:
        tokens = args.split()
        if not tokens or tokens[0] == "show":
            sessions = get_bridge_manager().list_sessions()
            lines = [
                "Bridge summary:",
                "- backend host: available",
                f"- cwd: {context.cwd}",
                f"- sessions: {len(sessions)}",
                "- utilities: encode, decode, sdk, spawn, list, output, stop",
            ]
            return CommandResult(message="\n".join(lines))
        if tokens[0] == "encode" and len(tokens) == 3:
            encoded = encode_work_secret(
                WorkSecret(version=1, session_ingress_token=tokens[2], api_base_url=tokens[1])
            )
            return CommandResult(message=encoded)
        if tokens[0] == "decode" and len(tokens) == 2:
            secret = decode_work_secret(tokens[1])
            return CommandResult(message=json.dumps(secret.__dict__, indent=2))
        if tokens[0] == "sdk" and len(tokens) == 3:
            return CommandResult(message=build_sdk_url(tokens[1], tokens[2]))
        if tokens[0] == "spawn" and len(tokens) >= 2:
            command = args[len("spawn ") :]
            handle = await get_bridge_manager().spawn(
                session_id=f"bridge-{datetime.now(timezone.utc).strftime('%H%M%S')}",
                command=command,
                cwd=context.cwd,
            )
            return CommandResult(
                message=f"Spawned bridge session {handle.session_id} pid={handle.process.pid}"
            )
        if tokens[0] == "list":
            sessions = get_bridge_manager().list_sessions()
            if not sessions:
                return CommandResult(message="No bridge sessions.")
            return CommandResult(
                message="\n".join(
                    f"{item.session_id} [{item.status}] pid={item.pid} {item.command}"
                    for item in sessions
                )
            )
        if tokens[0] == "output" and len(tokens) == 2:
            return CommandResult(message=get_bridge_manager().read_output(tokens[1]) or "(no output)")
        if tokens[0] == "stop" and len(tokens) == 2:
            try:
                await get_bridge_manager().stop(tokens[1])
            except ValueError as exc:
                return CommandResult(message=str(exc))
            return CommandResult(message=f"Stopped bridge session {tokens[1]}")
        return CommandResult(
            message="Usage: /bridge [show|encode API_BASE_URL TOKEN|decode SECRET|sdk API_BASE_URL SESSION_ID|spawn CMD|list|output SESSION_ID|stop SESSION_ID]"
        )

    async def _reload_plugins_handler(_: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        plugins = load_plugins(settings, context.cwd, extra_roots=context.extra_plugin_roots)
        if not plugins:
            return CommandResult(message="No plugins discovered.")
        lines = ["Reloaded plugins:"]
        for plugin in plugins:
            state = "enabled" if plugin.enabled else "disabled"
            lines.append(f"- {plugin.manifest.name} [{state}]")
        return CommandResult(message="\n".join(lines))

    async def _skills_handler(args: str, context: CommandContext) -> CommandResult:
        from openharness.config.settings import load_settings, save_settings

        skill_registry = load_skill_registry(
            context.cwd,
            extra_skill_dirs=context.extra_skill_dirs,
            extra_plugin_roots=context.extra_plugin_roots,
        )
        tokens = args.split(maxsplit=1)
        sub = tokens[0].lower() if tokens else ""
        rest = tokens[1].strip() if len(tokens) > 1 else ""

        # /skills preload <name|*>  — inject skill content into the system prompt permanently
        # (스킬 내용을 시스템 프롬프트에 항상 주입 — 로컬 모델처럼 tool 호출을 못하는 모델용)
        if sub == "preload":
            if not rest:
                return CommandResult(message="Usage: /skills preload <skill_name|*>")
            settings = load_settings()
            current: list[str] = list(getattr(settings, "preload_skills", None) or [])
            if rest not in current:
                current.append(rest)
                settings.preload_skills = current
                save_settings(settings)
            return CommandResult(
                message=f"✅ '{rest}' 스킬이 시스템 프롬프트에 항상 주입됩니다. (preload_skills: {current})",
                refresh_runtime=True,
            )

        # /skills unload <name|*>  — remove a skill from the permanent preload list
        # (preload 목록에서 해제)
        if sub == "unload":
            if not rest:
                return CommandResult(message="Usage: /skills unload <skill_name|*>")
            settings = load_settings()
            current = list(getattr(settings, "preload_skills", None) or [])
            if rest in current:
                current.remove(rest)
                settings.preload_skills = current
                save_settings(settings)
                return CommandResult(
                    message=f"✅ '{rest}' preload 해제됨. (preload_skills: {current})",
                    refresh_runtime=True,
                )
            return CommandResult(message=f"'{rest}'는 preload 목록에 없습니다.")

        # /skills list (or no args) — show all skills with preload status markers
        # (전체 스킬 목록 표시, preload된 항목은 * 마커)
        if not args or sub == "list":
            settings = load_settings()
            preloaded: list[str] = list(getattr(settings, "preload_skills", None) or [])
            skills = skill_registry.list_skills()
            if not skills:
                return CommandResult(message="No skills available.")
            lines = ["Available skills (* = preloaded into system prompt):"]
            for skill in skills:
                source = f" [{skill.source}]"
                marker = " *" if (skill.name in preloaded or "*" in preloaded) else ""
                lines.append(f"- {skill.name}{source}{marker}: {skill.description}")
            if preloaded:
                lines.append(f"\npreload_skills: {preloaded}")
            lines.append("\nUsage: /skills preload <name|*>  |  /skills unload <name>")
            return CommandResult(message="\n".join(lines))

        # /skills <skill_name>  — show the full content of a single skill
        # (스킬 내용 보기)
        skill = skill_registry.get(args)
        if skill is None:
            return CommandResult(message=f"Skill not found: {args}\nTip: /skills list")
        return CommandResult(message=skill.content)

    async def _config_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        tokens = args.split(maxsplit=2)

        # Explicit argument — handle directly (non-interactive)
        if tokens and tokens[0] not in ("show", ""):
            if tokens[0] == "show":
                return CommandResult(message=settings.model_dump_json(indent=2))
            if tokens[0] == "set" and len(tokens) == 3:
                key, value = tokens[1], tokens[2]
                if key not in Settings.model_fields:
                    return CommandResult(message=f"Unknown config key: {key}")
                try:
                    coerced = _coerce_setting_value(settings, key, value)
                except ValueError as exc:
                    return CommandResult(message=str(exc))
                setattr(settings, key, coerced)
                save_settings(settings)
                return CommandResult(message=f"Updated {key}", refresh_runtime=True)
            return CommandResult(message="Usage: /config [show|set KEY VALUE]")

        # Interactive menu
        if not _is_real_tty():
            return CommandResult(message=settings.model_dump_json(indent=2))

        try:
            import asyncio
            import questionary

            _LANGUAGES = [
                ("한국어 (Korean)", "Korean"),
                ("English", "English"),
                ("日本語 (Japanese)", "Japanese"),
                ("中文 (Chinese)", "Chinese"),
                ("Español (Spanish)", "Spanish"),
                ("Français (French)", "French"),
                ("Deutsch (German)", "German"),
                ("없음 (언어 설정 해제)", ""),
            ]

            _EFFORTS = [
                ("low — 빠른 응답, 얕은 추론", "low"),
                ("medium — 균형 (기본값)", "medium"),
                ("high — 느리지만 깊은 추론", "high"),
            ]

            _MENU_ITEMS = [
                ("language",   "🌐 응답 언어 설정"),
                ("model",      "🤖 모델 변경"),
                ("effort",     "⚡ 추론 깊이 (effort)"),
                ("fast_mode",  "🚀 빠른 모드 (fast_mode)"),
                ("vim_mode",   "⌨️  Vim 모드"),
                ("max_turns",  "🔄 최대 턴 수"),
                ("show",       "📄 전체 설정 보기"),
            ]

            def _run_menu() -> str | None:
                current_lang = settings.language or "없음"
                current_model = settings.model
                hints = {
                    "language":  f"현재: {current_lang}",
                    "model":     f"현재: {current_model}",
                    "effort":    f"현재: {settings.effort}",
                    "fast_mode": f"현재: {'켜짐' if settings.fast_mode else '꺼짐'}",
                    "vim_mode":  f"현재: {'켜짐' if settings.vim_mode else '꺼짐'}",
                    "max_turns": f"현재: {settings.max_turns}",
                    "show":      "",
                }
                choices = [
                    questionary.Choice(
                        title=[("", f"{label}  "), ("fg:#888888", hints[key])],
                        value=key,
                    )
                    for key, label in _MENU_ITEMS
                ]
                return questionary.select("설정 항목 선택:", choices=choices).ask()

            selected = await asyncio.to_thread(_run_menu)
            if selected is None:
                return CommandResult(message="취소됐습니다.")

            if selected == "show":
                return CommandResult(message=settings.model_dump_json(indent=2))

            # ── language ──────────────────────────────────────────────
            if selected == "language":
                def _pick_lang() -> str | None:
                    current = settings.language or ""
                    choices = [
                        questionary.Choice(title=label, value=val)
                        for label, val in _LANGUAGES
                    ]
                    return questionary.select(
                        "응답 언어를 선택하세요:",
                        choices=choices,
                        default=current if current else None,
                    ).ask()

                lang = await asyncio.to_thread(_pick_lang)
                if lang is None:
                    return CommandResult(message="취소됐습니다.")
                settings.language = lang
                save_settings(settings)
                label = lang if lang else "없음 (해제됨)"
                return CommandResult(message=f"응답 언어 → {label}", refresh_runtime=True)

            # ── effort ────────────────────────────────────────────────
            if selected == "effort":
                def _pick_effort() -> str | None:
                    choices = [
                        questionary.Choice(title=label, value=val)
                        for label, val in _EFFORTS
                    ]
                    return questionary.select(
                        "추론 깊이 선택:", choices=choices, default=settings.effort
                    ).ask()

                effort = await asyncio.to_thread(_pick_effort)
                if effort is None:
                    return CommandResult(message="취소됐습니다.")
                settings.effort = effort
                save_settings(settings)
                return CommandResult(message=f"effort → {effort}", refresh_runtime=True)

            # ── boolean toggles ───────────────────────────────────────
            if selected in ("fast_mode", "vim_mode"):
                current_val = getattr(settings, selected)
                new_val = not current_val
                setattr(settings, selected, new_val)
                save_settings(settings)
                state = "켜짐" if new_val else "꺼짐"
                return CommandResult(message=f"{selected} → {state}", refresh_runtime=True)

            # ── model (text input) ────────────────────────────────────
            if selected == "model":
                def _input_model() -> str | None:
                    return questionary.text(
                        "모델명 입력:", default=settings.model
                    ).ask()

                model = await asyncio.to_thread(_input_model)
                if model is None or not model.strip():
                    return CommandResult(message="취소됐습니다.")
                settings.model = model.strip()
                save_settings(settings)
                return CommandResult(message=f"model → {settings.model}", refresh_runtime=True)

            # ── max_turns (text input) ────────────────────────────────
            if selected == "max_turns":
                def _input_turns() -> str | None:
                    return questionary.text(
                        "최대 턴 수 입력:", default=str(settings.max_turns)
                    ).ask()

                turns_str = await asyncio.to_thread(_input_turns)
                if turns_str is None:
                    return CommandResult(message="취소됐습니다.")
                try:
                    settings.max_turns = int(turns_str)
                except ValueError:
                    return CommandResult(message="숫자를 입력해주세요.")
                save_settings(settings)
                return CommandResult(message=f"max_turns → {settings.max_turns}", refresh_runtime=True)

        except Exception as exc:
            return CommandResult(message=f"메뉴 오류: {exc}\n\n{settings.model_dump_json(indent=2)}")

        return CommandResult(message="설정이 완료됐습니다.", refresh_runtime=True)

    async def _language_handler(args: str, context: CommandContext) -> CommandResult:
        """Set the AI response language.  /language Korean  |  /language  (show current)"""
        del context
        settings = load_settings()
        lang = args.strip()
        if not lang:
            current = settings.language or "None (not set)"
            return CommandResult(message=f"Response language: {current}")
        # Accept "none" / "" to disable
        if lang.lower() in ("none", "off", "disable", "disabled", "없음"):
            settings.language = ""
            save_settings(settings)
            return CommandResult(message="Response language disabled.", refresh_runtime=True)
        settings.language = lang
        save_settings(settings)
        return CommandResult(message=f"Response language → {lang}", refresh_runtime=True)

    async def _login_handler(args: str, context: CommandContext) -> CommandResult:
        del context
        settings = load_settings()
        manager = AuthManager(settings)
        profile_name, profile = settings.resolve_profile()
        provider = detect_provider(settings)
        api_key = args.strip()
        if not api_key:
            masked = (
                f"{settings.api_key[:6]}...{settings.api_key[-4:]}"
                if settings.api_key
                else "(not configured)"
            )
            return CommandResult(
                message=(
                    f"Auth status:\n"
                    f"- profile: {profile_name}\n"
                    f"- provider: {provider.name}\n"
                    f"- auth_source: {profile.auth_source}\n"
                    f"- auth_status: {auth_status(settings)}\n"
                    f"- base_url: {settings.base_url or '(default)'}\n"
                    f"- model: {settings.model}\n"
                    f"- api_key: {masked}\n"
                    "Usage: /login API_KEY"
                )
            )
        manager.store_profile_credential(profile_name, "api_key", api_key)
        return CommandResult(message="Stored API key in ~/.openharness/settings.json")

    async def _logout_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        settings = load_settings()
        profile_name = settings.resolve_profile()[0]
        AuthManager(settings).clear_profile_credential(profile_name)
        return CommandResult(message="Cleared stored API key.")

    async def _feedback_handler(args: str, context: CommandContext) -> CommandResult:
        del context
        path = get_feedback_log_path()
        if not args.strip():
            return CommandResult(message=f"Feedback log: {path}\nUsage: /feedback TEXT")
        timestamp = datetime.now(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {args.strip()}\n")
        return CommandResult(message=f"Saved feedback to {path}")

    async def _onboarding_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        return CommandResult(
            message=(
                "OpenHarness quickstart:\n"
                "1. Ask for a coding task in plain language.\n"
                "2. Use /help to inspect commands.\n"
                "3. Use /doctor to inspect runtime state.\n"
                "4. Use /tasks for background work and /memory for project memory.\n"
                "5. Use /login to store an API key if needed."
            )
        )

    async def _fast_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        current = (
            context.app_state.get().fast_mode
            if context.app_state is not None
            else settings.fast_mode
        )
        action = args.strip()
        if action:
            if action == "show":
                return CommandResult(message=f"Fast mode: {'on' if current else 'off'}")
            enabled = {"on": True, "off": False, "toggle": not current}.get(action)
            if enabled is None:
                return CommandResult(message="Usage: /fast [show|on|off|toggle]")
            settings.fast_mode = enabled
            save_settings(settings)
            if context.app_state is not None:
                context.app_state.set(fast_mode=enabled)
            return CommandResult(message=f"Fast mode {'enabled' if enabled else 'disabled'}.")

        return CommandResult(message=f"Fast mode: {'on' if current else 'off'}\nUsage: /fast [on|off|toggle]")

    async def _effort_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        current = context.app_state.get().effort if context.app_state is not None else settings.effort
        value = args.strip()
        if value:
            if value == "show":
                return CommandResult(message=f"Reasoning effort: {current}")
            if value not in {"low", "medium", "high"}:
                return CommandResult(message="Usage: /effort [show|low|medium|high]")
            settings.effort = value
            save_settings(settings)
            context.engine.set_system_prompt(build_runtime_system_prompt(settings, cwd=context.cwd))
            if context.app_state is not None:
                context.app_state.set(effort=value)
            return CommandResult(message=f"Reasoning effort set to {value}.")

        return CommandResult(message=f"Reasoning effort: {current}\nUsage: /effort [low|medium|high]")

    async def _passes_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        current = context.app_state.get().passes if context.app_state is not None else settings.passes
        value = args.strip()
        if value and value != "show":
            try:
                passes = max(1, min(int(value), 8))
            except ValueError:
                return CommandResult(message="Usage: /passes [show|COUNT]")
            settings.passes = passes
            save_settings(settings)
            context.engine.set_system_prompt(build_runtime_system_prompt(settings, cwd=context.cwd))
            if context.app_state is not None:
                context.app_state.set(passes=passes)
            return CommandResult(message=f"Pass count set to {passes}.")

        return CommandResult(message=f"Passes: {current}\nUsage: /passes [COUNT]")

    async def _turns_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        engine_turns = "unlimited" if context.engine.max_turns is None else str(context.engine.max_turns)
        tokens = args.split()
        if tokens and tokens[0] != "show":
            if tokens[0] == "set" and len(tokens) == 2:
                raw = tokens[1]
            elif len(tokens) == 1:
                raw = tokens[0]
            else:
                return CommandResult(message="Usage: /turns [show|unlimited|COUNT]")
            if raw.lower() == "unlimited":
                context.engine.set_max_turns(None)
                return CommandResult(
                    message=(
                        "Max turns set to unlimited for this session. "
                        f"Saved config remains {settings.max_turns}."
                    )
                )
            try:
                turns = int(raw)
            except ValueError:
                return CommandResult(message="Usage: /turns [show|unlimited|COUNT]")
            turns = max(1, min(turns, 512))
            settings.max_turns = turns
            save_settings(settings)
            context.engine.set_max_turns(turns)
            return CommandResult(message=f"Max turns set to {turns}.")

        return CommandResult(
            message=(
                f"Max turns (engine): {engine_turns}\n"
                f"Max turns (config): {settings.max_turns}\n"
                "Usage: /turns [show|unlimited|COUNT]"
            )
        )

    async def _continue_handler(args: str, context: CommandContext) -> CommandResult:
        raw = args.strip()
        if not context.engine.has_pending_continuation():
            return CommandResult(message="Nothing to continue (no pending tool results).")

        turns: int | None = None
        if raw:
            tokens = raw.split()
            if tokens[0] == "set" and len(tokens) == 2:
                raw = tokens[1]
            try:
                turns = int(raw)
            except ValueError:
                return CommandResult(message="Usage: /continue [COUNT]")
            turns = max(1, min(turns, 512))

        return CommandResult(
            message="Continuing pending tool loop...",
            continue_pending=True,
            continue_turns=turns,
        )

    async def _issue_handler(args: str, context: CommandContext) -> CommandResult:
        path = get_project_issue_file(context.cwd)
        tokens = args.split(maxsplit=1)
        action = tokens[0] if tokens else "show"
        rest = tokens[1] if len(tokens) == 2 else ""
        if action == "show":
            if not path.exists():
                return CommandResult(message=f"No issue context. File path: {path}")
            return CommandResult(message=path.read_text(encoding="utf-8"))
        if action == "set" and rest:
            title, separator, body = rest.partition("::")
            if not separator or not title.strip() or not body.strip():
                return CommandResult(message="Usage: /issue set TITLE :: BODY")
            content = f"# {title.strip()}\n\n{body.strip()}\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return CommandResult(message=f"Saved issue context to {path}")
        if action == "clear":
            if path.exists():
                path.unlink()
                return CommandResult(message="Cleared issue context.")
            return CommandResult(message="No issue context to clear.")
        return CommandResult(message="Usage: /issue [show|set TITLE :: BODY|clear]")

    async def _pr_comments_handler(args: str, context: CommandContext) -> CommandResult:
        path = get_project_pr_comments_file(context.cwd)
        tokens = args.split(maxsplit=1)
        action = tokens[0] if tokens else "show"
        rest = tokens[1] if len(tokens) == 2 else ""
        if action == "show":
            if not path.exists():
                return CommandResult(message=f"No PR comments context. File path: {path}")
            return CommandResult(message=path.read_text(encoding="utf-8"))
        if action == "add" and rest:
            location, separator, comment = rest.partition("::")
            if not separator or not location.strip() or not comment.strip():
                return CommandResult(message="Usage: /pr_comments add FILE[:LINE] :: COMMENT")
            existing = path.read_text(encoding="utf-8") if path.exists() else "# PR Comments\n"
            if not existing.endswith("\n"):
                existing += "\n"
            existing += f"- {location.strip()}: {comment.strip()}\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(existing, encoding="utf-8")
            return CommandResult(message=f"Added PR comment to {path}")
        if action == "clear":
            if path.exists():
                path.unlink()
                return CommandResult(message="Cleared PR comments context.")
            return CommandResult(message="No PR comments context to clear.")
        return CommandResult(message="Usage: /pr_comments [show|add FILE[:LINE] :: COMMENT|clear]")

    async def _mcp_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        tokens = args.split()
        if tokens and tokens[0] == "auth" and len(tokens) >= 3:
            server_name = tokens[1]
            config = settings.mcp_servers.get(server_name)
            if config is None:
                return CommandResult(message=f"Unknown MCP server: {server_name}")

            if len(tokens) == 3:
                mode = "bearer"
                key = None
                value = tokens[2]
            elif len(tokens) == 4:
                mode = tokens[2]
                key = None
                value = tokens[3]
            elif len(tokens) == 5:
                mode = tokens[2]
                key = tokens[3]
                value = tokens[4]
            else:
                return CommandResult(
                    message="Usage: /mcp auth SERVER TOKEN | /mcp auth SERVER [bearer|env] VALUE | /mcp auth SERVER header KEY VALUE"
                )

            if hasattr(config, "headers"):
                if mode not in {"bearer", "header"}:
                    return CommandResult(message="HTTP/WS MCP auth supports bearer or header modes.")
                header_key = key or "Authorization"
                header_value = (
                    f"Bearer {value}" if mode == "bearer" and header_key == "Authorization" else value
                )
                headers = dict(getattr(config, "headers", {}) or {})
                headers[header_key] = header_value
                settings.mcp_servers[server_name] = config.model_copy(update={"headers": headers})
            elif hasattr(config, "env"):
                if mode not in {"bearer", "env"}:
                    return CommandResult(message="stdio MCP auth supports bearer or env modes.")
                env_key = key or "MCP_AUTH_TOKEN"
                env_value = f"Bearer {value}" if mode == "bearer" else value
                env = dict(getattr(config, "env", {}) or {})
                env[env_key] = env_value
                settings.mcp_servers[server_name] = config.model_copy(update={"env": env})
            else:
                return CommandResult(message=f"Server {server_name} does not support auth updates")
            save_settings(settings)
            return CommandResult(message=f"Saved MCP auth for {server_name}. Restart session to reconnect.")
        return CommandResult(message=context.mcp_summary or "No MCP servers configured.")

    async def _plugin_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        tokens = args.split()

        # Direct commands pass through
        if tokens and tokens[0] != "menu":
            if tokens[0] == "list":
                return CommandResult(message=context.plugin_summary or "No plugins discovered.")
            if tokens[0] == "enable" and len(tokens) == 2:
                settings.enabled_plugins[tokens[1]] = True
                save_settings(settings)
                return CommandResult(message=f"Enabled plugin '{tokens[1]}'. Restart session to reload.")
            if tokens[0] == "disable" and len(tokens) == 2:
                settings.enabled_plugins[tokens[1]] = False
                save_settings(settings)
                return CommandResult(message=f"Disabled plugin '{tokens[1]}'. Restart session to reload.")
            if tokens[0] == "install" and len(tokens) == 2:
                path = install_plugin_from_path(tokens[1])
                return CommandResult(message=f"Installed plugin to {path}")
            if tokens[0] == "uninstall" and len(tokens) == 2:
                if uninstall_plugin(tokens[1]):
                    return CommandResult(message=f"Uninstalled plugin '{tokens[1]}'")
                return CommandResult(message=f"Plugin '{tokens[1]}' not found")

        # Interactive menu
        if _is_real_tty():
            try:
                import questionary
                plugins = load_plugins(settings, context.cwd, extra_roots=context.extra_plugin_roots)
                plugin_names = [p.name for p in plugins]

                choices = [
                    questionary.Choice("📋 목록 보기 (list)", "list"),
                    questionary.Choice("✅ 플러그인 활성화 (enable)", "enable"),
                    questionary.Choice("🚫 플러그인 비활성화 (disable)", "disable"),
                    questionary.Choice("📥 설치 (install)", "install"),
                    questionary.Choice("🗑  제거 (uninstall)", "uninstall"),
                ]
                action = await _qselect("플러그인 작업 선택:", choices)
                if action is None:
                    return CommandResult(message="취소됐습니다.")

                if action == "list":
                    return CommandResult(message=context.plugin_summary or "No plugins discovered.")

                if action in ("enable", "disable") and plugin_names:
                    name = await _qselect(
                        f"{'활성화' if action == 'enable' else '비활성화'}할 플러그인:",
                        [questionary.Choice(n, n) for n in plugin_names],
                    )
                    if name is None:
                        return CommandResult(message="취소됐습니다.")
                    settings.enabled_plugins[name] = (action == "enable")
                    save_settings(settings)
                    verb = "Enabled" if action == "enable" else "Disabled"
                    return CommandResult(message=f"{verb} plugin '{name}'. Restart session to reload.")

                if action == "install":
                    path_str = await _qtext("설치할 플러그인 경로 입력:")
                    if not path_str or not path_str.strip():
                        return CommandResult(message="취소됐습니다.")
                    path = install_plugin_from_path(path_str.strip())
                    return CommandResult(message=f"Installed plugin to {path}")

                if action == "uninstall" and plugin_names:
                    name = await _qselect("제거할 플러그인:", [questionary.Choice(n, n) for n in plugin_names])
                    if name is None:
                        return CommandResult(message="취소됐습니다.")
                    if uninstall_plugin(name):
                        return CommandResult(message=f"Uninstalled plugin '{name}'")
                    return CommandResult(message=f"Plugin '{name}' not found")

                return CommandResult(message=context.plugin_summary or "No plugins discovered.")
            except ImportError:
                pass

        return CommandResult(message=context.plugin_summary or "Usage: /plugin [list|enable NAME|disable NAME|install PATH|uninstall NAME]")

    _MODE_LABELS = {"default": "Default", "plan": "Plan Mode", "full_auto": "Auto"}

    async def _permissions_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        tokens = args.split()

        # Explicit argument passed — handle directly
        if tokens and tokens[0] != "show":
            target_mode: str | None = None
            if tokens[0] == "set" and len(tokens) == 2:
                target_mode = tokens[1]
            elif len(tokens) == 1 and tokens[0] in _MODE_LABELS:
                target_mode = tokens[0]
            if target_mode is not None:
                settings.permission.mode = PermissionMode(target_mode)
                save_settings(settings)
                context.engine.set_permission_checker(PermissionChecker(settings.permission))
                if context.app_state is not None:
                    context.app_state.set(permission_mode=settings.permission.mode.value)
                label = _MODE_LABELS.get(target_mode, target_mode)
                return CommandResult(message=f"Permission mode set to {label}", refresh_runtime=True)
            return CommandResult(message="Usage: /permissions [show|default|auto|plan]")

        # Non-interactive fallback: show current status
        permission = settings.permission
        label = _MODE_LABELS.get(permission.mode.value, permission.mode.value)
        return CommandResult(
            message=(
                f"Mode: {label}\n"
                f"Allowed tools: {permission.allowed_tools}\n"
                f"Denied tools: {permission.denied_tools}\n"
                f"Usage: /permissions [default|auto|plan]"
            )
        )

    async def _plan_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        mode = args.strip() or "on"
        if mode in {"on", "enter"}:
            settings.permission.mode = PermissionMode.PLAN
            save_settings(settings)
            context.engine.set_permission_checker(PermissionChecker(settings.permission))
            if context.app_state is not None:
                context.app_state.set(permission_mode=settings.permission.mode.value)
            return CommandResult(message="Plan mode enabled.", refresh_runtime=True)
        if mode in {"off", "exit"}:
            settings.permission.mode = PermissionMode.DEFAULT
            save_settings(settings)
            context.engine.set_permission_checker(PermissionChecker(settings.permission))
            if context.app_state is not None:
                context.app_state.set(permission_mode=settings.permission.mode.value)
            return CommandResult(message="Plan mode disabled.", refresh_runtime=True)
        return CommandResult(message="Usage: /plan [on|off]")

    async def _model_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        manager = AuthManager(settings)
        active_profile = manager.get_active_profile()
        _, profile = settings.resolve_profile(active_profile)
        tokens = args.split(maxsplit=1)

        def _apply_model(model_name: str):
            if profile.allowed_models and model_name.lower() != "default" and model_name not in profile.allowed_models:
                allowed = ", ".join(profile.allowed_models)
                return CommandResult(message=f"Model '{model_name}' is not allowed for profile '{active_profile}'. Allowed: {allowed}")
            if model_name.lower() == "default":
                manager.update_profile(active_profile, last_model="")
                msg = "Model reset to default."
            else:
                manager.update_profile(active_profile, last_model=model_name)
                msg = f"Model set to {model_name}."
            updated = load_settings()
            context.engine.set_model(updated.model)
            if context.app_state is not None:
                context.app_state.set(model=display_model_setting(updated.resolve_profile()[1]))
            return CommandResult(message=msg, refresh_runtime=True)

        if tokens and tokens[0] != "show":
            if tokens[0] == "set" and len(tokens) == 2:
                return _apply_model(tokens[1].strip())
            if args.strip():
                return _apply_model(args.strip())

        return CommandResult(message=f"Model: {display_model_setting(profile)}\nProfile: {active_profile}\nUsage: /model [show|MODEL]")

    async def _provider_handler(args: str, context: CommandContext) -> CommandResult:
        manager = AuthManager()
        profiles = manager.get_profile_statuses()
        tokens = args.split()

        def _switch_to(target: str):
            if target not in profiles:
                return CommandResult(message=f"Unknown profile: {target}")
            manager.use_profile(target)
            updated = load_settings()
            profile = updated.resolve_profile()[1]
            context.engine.set_model(updated.model)
            if context.app_state is not None:
                context.app_state.set(
                    model=display_model_setting(profile),
                    provider=detect_provider(updated).name,
                    auth_status=auth_status(updated),
                    base_url=updated.base_url or "",
                )
            return CommandResult(message=f"Switched to {target} ({profile.label}).", refresh_runtime=True)

        if tokens:
            if tokens[0] == "show":
                active_name = manager.get_active_profile()
                active = profiles[active_name]
                lines = [
                    f"Active profile: {active_name}",
                    f"Label: {active['label']}",
                    f"Provider: {active['provider']}",
                    f"Configured: {'yes' if active['configured'] else 'no'}",
                    f"Base URL: {active['base_url'] or '(default)'}",
                    f"Model: {active['model']}",
                ]
                return CommandResult(message="\n".join(lines))
            if tokens[0] == "list":
                lines = ["Provider profiles:"]
                for name, info in profiles.items():
                    marker = "*" if info["active"] else " "
                    configured = "configured" if info["configured"] else "missing auth"
                    lines.append(f"{marker} {name} [{configured}] {info['label']} -> {info['model']}")
                return CommandResult(message="\n".join(lines))
            target = tokens[1] if tokens[0] == "use" and len(tokens) == 2 else tokens[0]
            return _switch_to(target)

        active_name = manager.get_active_profile()
        active = profiles[active_name]
        return CommandResult(
            message=(
                f"Active: {active_name} ({active['label']})\n"
                "Usage: /provider [show|list|PROFILE]"
            )
        )

    async def _theme_handler(args: str, context: CommandContext) -> CommandResult:
        from openharness.themes import list_themes, load_theme

        settings = load_settings()
        tokens = args.split(maxsplit=1)
        current = (
            context.app_state.get().theme
            if context.app_state is not None and hasattr(context.app_state.get(), "theme")
            else settings.theme
        )

        def _apply_theme(name: str):
            try:
                load_theme(name)
            except KeyError:
                available = list_themes()
                return CommandResult(message=f"Unknown theme: {name!r}. Available: {', '.join(available)}")
            settings.theme = name
            save_settings(settings)
            if context.app_state is not None:
                context.app_state.set(theme=name)
            return CommandResult(message=f"Theme set to {name}", refresh_runtime=True)

        if tokens:
            if tokens[0] == "show":
                try:
                    theme = load_theme(current)
                    lines = [
                        f"Theme: {theme.name}",
                        f"  Colors:  primary={theme.colors.primary}  secondary={theme.colors.secondary}"
                        f"  accent={theme.colors.accent}  error={theme.colors.error}  muted={theme.colors.muted}",
                        f"           background={theme.colors.background}  foreground={theme.colors.foreground}",
                        f"  Borders: style={theme.borders.style}",
                        f"  Icons:   spinner={theme.icons.spinner}  tool={theme.icons.tool}"
                        f"  error={theme.icons.error}  success={theme.icons.success}  agent={theme.icons.agent}",
                        f"  Layout:  compact={theme.layout.compact}"
                        f"  show_tokens={theme.layout.show_tokens}  show_time={theme.layout.show_time}",
                    ]
                    return CommandResult(message="\n".join(lines))
                except KeyError:
                    return CommandResult(message=f"Theme: {current} (not found)")
            if tokens[0] == "list":
                available = list_themes()
                lines = [f"{'*' if name == current else ' '} {name}" for name in available]
                return CommandResult(message="\n".join(lines))
            if tokens[0] == "preview" and len(tokens) == 2:
                try:
                    theme = load_theme(tokens[1])
                except KeyError:
                    return CommandResult(message=f"Unknown theme: {tokens[1]!r}. Available: {', '.join(list_themes())}")
                lines = [f"Preview: {theme.name}", f"  primary={theme.colors.primary}  secondary={theme.colors.secondary}  accent={theme.colors.accent}"]
                return CommandResult(message="\n".join(lines))
            if tokens[0] == "set" and len(tokens) == 2:
                return _apply_theme(tokens[1])
            if tokens[0] not in {"list", "preview", "show"}:
                return _apply_theme(tokens[0])

        available = list_themes()
        lines = [f"{'*' if name == current else ' '} {name}" for name in available]
        return CommandResult(message="\n".join(lines) + "\nUsage: /theme [list|show|NAME|preview NAME]")

    async def _output_style_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        tokens = args.split(maxsplit=1)
        styles = load_output_styles()
        available = {style.name: style for style in styles}
        current = (
            context.app_state.get().output_style
            if context.app_state is not None
            else settings.output_style
        )

        def _apply_style(style_name: str):
            if style_name not in available:
                return CommandResult(message=f"Unknown output style: {style_name}")
            settings.output_style = style_name
            save_settings(settings)
            if context.app_state is not None:
                context.app_state.set(output_style=style_name)
            return CommandResult(message=f"Output style set to {style_name}", refresh_runtime=True)

        if tokens:
            if tokens[0] == "show":
                return CommandResult(message=f"Output style: {current}")
            if tokens[0] == "list":
                return CommandResult(message="\n".join(f"{'*' if style.name == current else ' '} {style.name} [{style.source}]" for style in styles))
            if tokens[0] == "set" and len(tokens) == 2:
                return _apply_style(tokens[1])
            if tokens[0] not in {"list", "show"}:
                return _apply_style(tokens[0])

        return CommandResult(message=f"Output style: {current}\nUsage: /output-style [show|list|NAME]")

    async def _keybindings_handler(_: str, context: CommandContext) -> CommandResult:
        from openharness.keybindings import get_keybindings_path, load_keybindings

        bindings = (
            context.app_state.get().keybindings
            if context.app_state is not None and context.app_state.get().keybindings
            else load_keybindings()
        )
        lines = [f"Keybindings file: {get_keybindings_path()}"]
        lines.extend(f"{key} -> {command}" for key, command in sorted(bindings.items()))
        return CommandResult(message="\n".join(lines))

    async def _vim_handler(args: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        current = (
            context.app_state.get().vim_enabled
            if context.app_state is not None
            else settings.vim_mode
        )
        action = args.strip()
        if action:
            if action == "show":
                return CommandResult(message=f"Vim mode: {'on' if current else 'off'}")
            enabled = {"on": True, "off": False, "toggle": not current}.get(action)
            if enabled is None:
                return CommandResult(message="Usage: /vim [show|on|off|toggle]")
            settings.vim_mode = enabled
            save_settings(settings)
            if context.app_state is not None:
                context.app_state.set(vim_enabled=enabled)
            return CommandResult(message=f"Vim mode {'enabled' if enabled else 'disabled'}.")

        return CommandResult(message=f"Vim mode: {'on' if current else 'off'}\nUsage: /vim [on|off|toggle]")

    async def _voice_handler(args: str, context: CommandContext) -> CommandResult:
        from openharness.voice import extract_keyterms, inspect_voice_capabilities

        settings = load_settings()
        diagnostics = inspect_voice_capabilities(detect_provider(settings))
        current = (
            context.app_state.get().voice_enabled
            if context.app_state is not None
            else settings.voice_mode
        )
        tokens = args.split(maxsplit=1)
        if not tokens or tokens[0] == "show":
            return CommandResult(
                message=(
                    f"Voice mode: {'on' if current else 'off'}\n"
                    f"Available: {'yes' if diagnostics.available else 'no'}\n"
                    f"Recorder: {diagnostics.recorder or '(none)'}\n"
                    f"Reason: {diagnostics.reason}"
                )
            )
        if tokens[0] == "keyterms" and len(tokens) == 2:
            keyterms = extract_keyterms(tokens[1])
            return CommandResult(message="\n".join(keyterms) if keyterms else "(no keyterms)")
        enabled = {"on": True, "off": False, "toggle": not current}.get(tokens[0])
        if enabled is None:
            return CommandResult(message="Usage: /voice [show|on|off|toggle|keyterms TEXT]")
        settings.voice_mode = enabled
        save_settings(settings)
        if context.app_state is not None:
            context.app_state.set(
                voice_enabled=enabled,
                voice_available=diagnostics.available,
                voice_reason=diagnostics.reason,
            )
        return CommandResult(message=f"Voice mode {'enabled' if enabled else 'disabled'}.")

    async def _doctor_handler(_: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        manager = AuthManager(settings)
        active_profile_name, active_profile = settings.resolve_profile()
        memory_dir = get_project_memory_dir(context.cwd)
        state = context.app_state.get() if context.app_state is not None else None
        lines = [
            "Doctor summary:",
            f"- cwd: {context.cwd}",
            f"- active_profile: {active_profile_name}",
            f"- model: {settings.model}",
            f"- provider_workflow: {active_profile.label}",
            f"- auth_source: {active_profile.auth_source}",
            f"- permission_mode: {state.permission_mode if state is not None else settings.permission.mode}",
            f"- theme: {state.theme if state is not None else settings.theme}",
            f"- output_style: {state.output_style if state is not None else settings.output_style}",
            f"- vim_mode: {'on' if (state.vim_enabled if state is not None else settings.vim_mode) else 'off'}",
            f"- voice_mode: {'on' if (state.voice_enabled if state is not None else settings.voice_mode) else 'off'}",
            f"- effort: {state.effort if state is not None else settings.effort}",
            f"- passes: {state.passes if state is not None else settings.passes}",
            f"- memory_dir: {memory_dir}",
            f"- plugin_count: {max(len(context.plugin_summary.splitlines()) - 1, 0) if context.plugin_summary else 0}",
            f"- mcp_configured: {'yes' if context.mcp_summary and 'No MCP' not in context.mcp_summary else 'no'}",
            f"- auth_configured: {'yes' if manager.get_profile_statuses()[active_profile_name]['configured'] else 'no'}",
        ]
        return CommandResult(message="\n".join(lines))

    async def _privacy_settings_handler(_: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        session_dir = context.session_backend.get_session_dir(context.cwd)
        lines = [
            "Privacy settings:",
            f"- user_config_dir: {get_config_dir()}",
            f"- project_config_dir: {get_project_config_dir(context.cwd)}",
            f"- session_dir: {session_dir}",
            f"- feedback_log: {get_feedback_log_path()}",
            f"- api_base_url: {settings.base_url or '(default Anthropic-compatible endpoint)'}",
            "- network: enabled only for provider and explicit web/MCP calls",
            "- storage: local files under ~/.openharness and project .openharness",
        ]
        return CommandResult(message="\n".join(lines))

    async def _rate_limit_options_handler(_: str, context: CommandContext) -> CommandResult:
        settings = load_settings()
        provider = "moonshot-compatible" if (settings.base_url and "moonshot" in settings.base_url) else "anthropic-compatible"
        lines = [
            "Rate limit options:",
            f"- provider: {provider}",
            "- reduce /passes or switch /effort low for lighter requests",
            "- enable /fast for shorter responses and less tool churn",
            "- use /compact to shrink long transcripts before retrying",
            "- prefer background /tasks for long-running local work",
        ]
        return CommandResult(message="\n".join(lines))

    async def _release_notes_handler(_: str, context: CommandContext) -> CommandResult:
        path = Path(context.cwd) / "RELEASE_NOTES.md"
        if path.exists():
            return CommandResult(message=path.read_text(encoding="utf-8"))
        return CommandResult(
            message=(
                "# Release Notes\n\n"
                "- React TUI is now the default `oh` interface.\n"
                "- Added richer session, files, bridge, agent, copy, rewind, effort, passes, and privacy commands.\n"
                "- Expanded real-model validation across tools, MCP, tasks, plugins, notebook, LSP, cron, and worktree flows.\n"
            )
        )

    async def _upgrade_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        try:
            version = importlib.metadata.version("HanPlanet-CLI")
        except importlib.metadata.PackageNotFoundError:
            version = "0.1.7"
        return CommandResult(
            message=(
                f"Current version: {version}\n"
                "Upgrade instructions:\n"
                "- uv sync --extra dev\n"
                "- uv pip install -e .\n"
                "- npm --prefix frontend/terminal install"
            )
        )

    async def _update_handler(args: str, context: CommandContext) -> CommandResult:
        """Update HanPlanet CLI to the latest version via git pull.
        (git pull로 최신 소스를 받아 HanPlanet CLI를 업데이트한다.)
        """
        import sys
        from pathlib import Path

        force = args.strip().lower() == "force"
        settings = load_settings()
        git_timeout = max(60.0, float(settings.timeout))
        install_timeout = max(120.0, float(settings.timeout))

        # Resolve the repo root from the editable install location: src/openharness/ → repo root
        # (editable 설치 시 소스 위치: src/openharness/ → repo root)
        try:
            import openharness as _oh_pkg
            repo_root = Path(_oh_pkg.__file__).parents[2]
        except Exception:
            repo_root = Path(context.cwd)

        if not (repo_root / ".git").exists():
            return CommandResult(
                message=(
                    "⚠️  Git 저장소를 찾을 수 없습니다.\n"
                    "editable 설치(pipx install -e .) 환경에서만 /update를 사용할 수 있습니다.\n"
                    f"  감지된 경로: {repo_root}"
                )
            )

        update_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "PIP_NO_INPUT": "1",
        }

        async def _run_command(command: list[str], *, cwd: Path, timeout: float) -> tuple[int, str]:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=update_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return 124, f"Command timed out after {timeout:.0f}s: {' '.join(command)}"
            output = (
                stdout.decode("utf-8", errors="replace")
                + stderr.decode("utf-8", errors="replace")
            ).strip()
            return process.returncode or 0, output

        def _pip_failure_message(output: str) -> str:
            install_command = f"{sys.executable} -m pip install -e {repo_root}"
            if "WinError 32" in output or "oh.exe" in output.lower():
                return (
                    "❌ 재설치 실패: 현재 실행 중인 HanPlanet CLI가 pipx 실행 파일을 잠그고 있습니다.\n"
                    "Windows에서는 실행 중인 CLI가 자기 자신의 `oh.exe`를 덮어쓸 수 없습니다.\n\n"
                    "이 창을 `ctrl+c`로 완전히 종료한 뒤, 새 CMD/PowerShell에서 아래 명령을 실행하세요:\n"
                    f"  cd /d {repo_root}\n"
                    "  pipx install --force --editable .\n\n"
                    f"직접 pip 명령이 필요하면:\n  {install_command}\n\n"
                    f"원본 오류:\n```\n{output}\n```"
                )
            return f"❌ 재설치 실패:\n```\n{output}\n```"

        async def _pip_install() -> tuple[bool, str]:
            """Re-install in editable mode with the current interpreter to pick up dependency changes.
            (현재 인터프리터로 editable 재설치 — 의존성 변경 반영)
            """
            returncode, output = await _run_command(
                [sys.executable, "-m", "pip", "install", "-e", str(repo_root), "--quiet"],
                cwd=repo_root,
                timeout=install_timeout,
            )
            if returncode != 0:
                return False, output or f"pip install failed with exit code {returncode}"
            return True, output

        # Force-reinstall mode: skip git pull, just re-run pip install -e
        # (강제 재설치 모드: git pull 없이 pip install -e만 실행)
        if force:
            pip_ok, pip_out = await _pip_install()
            if not pip_ok:
                return CommandResult(message=_pip_failure_message(pip_out))
            return CommandResult(
                message=(
                    "✅ 강제 재설치 완료! 변경사항 적용을 위해 프로그램을 재시작하세요.\n"
                    "  (ctrl+c 후 다시 실행)\n"
                    + (f"\n```\n{pip_out}\n```" if pip_out else "")
                )
            )

        # git pull
        try:
            returncode, output = await _run_command(
                ["git", "pull", "--ff-only", "origin", "main"],
                cwd=repo_root,
                timeout=git_timeout,
            )
        except FileNotFoundError:
            return CommandResult(message="❌ git 명령어를 찾을 수 없습니다. git이 설치되어 있는지 확인하세요.")

        if returncode != 0:
            return CommandResult(message=f"❌ 업데이트 실패:\n```\n{output}\n```")

        if "Already up to date" in output:
            return CommandResult(
                message=(
                    "✅ 이미 최신 소스입니다.\n\n"
                    "새 패키지 의존성 반영 또는 강제 재설치가 필요하면:\n"
                    "  `/update force` 를 입력하세요.\n\n"
                    f"```\n{output}\n```"
                )
            )

        # 변경사항 있음 → pip 재설치 (새 의존성 반영)
        pip_ok, pip_out = await _pip_install()
        if not pip_ok:
            return CommandResult(
                message=(
                    "⚠️ 소스 업데이트는 완료됐지만 재설치에 실패했습니다.\n\n"
                    f"```\n{output}\n```\n\n"
                    f"{_pip_failure_message(pip_out)}"
                )
            )
        return CommandResult(
            message=(
                "✅ 업데이트 완료! 변경사항 적용을 위해 프로그램을 재시작하세요.\n"
                "  (ctrl+c 후 다시 실행)\n\n"
                f"```\n{output}\n```"
                + (f"\n\n패키지 재설치:\n```\n{pip_out}\n```" if pip_out else "")
            )
        )

    async def _diff_handler(args: str, context: CommandContext) -> CommandResult:
        if args.strip() == "full":
            ok, output = _run_git_command(context.cwd, "diff", "HEAD")
            return CommandResult(message=output or "(no diff)")
        ok, output = _run_git_command(context.cwd, "diff", "--stat")
        if not ok:
            return CommandResult(message=output)
        return CommandResult(message=output or "(no diff)")

    async def _branch_handler(args: str, context: CommandContext) -> CommandResult:
        action = args.strip() or "show"
        if action == "show":
            ok, current = _run_git_command(context.cwd, "branch", "--show-current")
            if not ok:
                return CommandResult(message=current)
            return CommandResult(message=f"Current branch: {current or '(detached HEAD)'}")
        if action == "list":
            ok, branches = _run_git_command(context.cwd, "branch", "--format", "%(refname:short)")
            return CommandResult(message=branches if ok else branches)
        return CommandResult(message="Usage: /branch [show|list]")

    async def _commit_handler(args: str, context: CommandContext) -> CommandResult:
        message = args.strip()
        if not message:
            ok, status = _run_git_command(context.cwd, "status", "--short")
            return CommandResult(message=status if ok and status else "(working tree clean)")
        ok, status = _run_git_command(context.cwd, "status", "--short")
        if not ok:
            return CommandResult(message=status)
        if not status.strip():
            return CommandResult(message="Nothing to commit.")
        ok, output = _run_git_command(context.cwd, "add", "-A")
        if not ok:
            return CommandResult(message=output)
        ok, output = _run_git_command(context.cwd, "commit", "-m", message)
        return CommandResult(message=output if ok else output)

    async def _tasks_handler(args: str, context: CommandContext) -> CommandResult:
        manager = get_task_manager()
        tokens = args.split(maxsplit=2)
        if not tokens or tokens[0] == "list":
            tasks = manager.list_tasks()
            if not tasks:
                return CommandResult(message="No background tasks.")
            return CommandResult(
                message="\n".join(f"{task.id} {task.type} {task.status} {task.description}" for task in tasks)
            )
        if tokens[0] == "run" and len(tokens) >= 2:
            command = args[len("run ") :]
            task = await manager.create_shell_task(
                command=command,
                description=command[:80],
                cwd=context.cwd,
            )
            return CommandResult(message=f"Started task {task.id}")
        if tokens[0] == "stop" and len(tokens) == 2:
            task = await manager.stop_task(tokens[1])
            return CommandResult(message=f"Stopped task {task.id}")
        if tokens[0] == "show" and len(tokens) == 2:
            task = manager.get_task(tokens[1])
            if task is None:
                return CommandResult(message=f"No task found with ID: {tokens[1]}")
            return CommandResult(message=str(task))
        if tokens[0] == "update" and len(tokens) == 3:
            task_id = tokens[1]
            rest = tokens[2]
            field, _, value = rest.partition(" ")
            if not value.strip():
                return CommandResult(
                    message="Usage: /tasks update ID [description TEXT|progress NUMBER|note TEXT]"
                )
            try:
                if field == "description":
                    task = manager.update_task(task_id, description=value)
                    return CommandResult(message=f"Updated task {task.id} description")
                if field == "progress":
                    try:
                        progress = int(value)
                    except ValueError:
                        return CommandResult(message="Progress must be an integer between 0 and 100.")
                    task = manager.update_task(task_id, progress=progress)
                    return CommandResult(message=f"Updated task {task.id} progress to {progress}%")
                if field == "note":
                    task = manager.update_task(task_id, status_note=value)
                    return CommandResult(message=f"Updated task {task.id} note")
            except ValueError as exc:
                return CommandResult(message=str(exc))
            return CommandResult(
                message="Usage: /tasks update ID [description TEXT|progress NUMBER|note TEXT]"
            )
        if tokens[0] == "output" and len(tokens) == 2:
            return CommandResult(message=manager.read_task_output(tokens[1]) or "(no output)")
        return CommandResult(
            message=(
                "Usage: /tasks "
                "[list|run CMD|stop ID|show ID|update ID description TEXT|update ID progress NUMBER|update ID note TEXT|output ID]"
            )
        )

    async def _autopilot_handler(args: str, context: CommandContext) -> CommandResult:
        store = RepoAutopilotStore(context.cwd)
        tokens = args.split()
        action = tokens[0].lower() if tokens else "status"

        def _render_card(card) -> str:
            lines = [
                f"{card.id} [{card.status}] score={card.score} {card.title}",
                f"source={card.source_kind} ref={card.source_ref or '-'}",
            ]
            if card.labels:
                lines.append(f"labels={', '.join(card.labels)}")
            if card.score_reasons:
                lines.append(f"reasons={', '.join(card.score_reasons[:4])}")
            if card.body:
                lines.append(_shorten_text(card.body, limit=220))
            return "\n".join(lines)

        if action == "status":
            counts = store.stats()
            active = store.pick_next_card()
            lines = ["Autopilot queue status:"]
            for status_name in (
                "queued",
                "accepted",
                "preparing",
                "running",
                "verifying",
                "pr_open",
                "waiting_ci",
                "repairing",
                "completed",
                "merged",
                "failed",
                "rejected",
                "superseded",
            ):
                lines.append(f"- {status_name}: {counts.get(status_name, 0)}")
            lines.append(f"- registry: {store.registry_path}")
            lines.append(f"- journal: {store.journal_path}")
            lines.append(f"- context: {store.context_path}")
            if active is not None:
                lines.append(f"- next: {active.id} {active.title} (score={active.score})")
            return CommandResult(message="\n".join(lines))

        if action == "list":
            status = tokens[1].lower() if len(tokens) >= 2 else None
            if status is not None and status not in {
                "queued",
                "accepted",
                "preparing",
                "running",
                "verifying",
                "pr_open",
                "waiting_ci",
                "repairing",
                "completed",
                "merged",
                "failed",
                "rejected",
                "superseded",
            }:
                return CommandResult(message=f"Unknown autopilot status: {status}")
            cards = store.list_cards(status=status)
            if not cards:
                return CommandResult(message="No autopilot cards.")
            return CommandResult(message="\n\n".join(_render_card(card) for card in cards[:12]))

        if action == "show" and len(tokens) >= 2:
            card = store.get_card(tokens[1])
            if card is None:
                return CommandResult(message=f"No autopilot card found with ID: {tokens[1]}")
            return CommandResult(message=_render_card(card))

        if action == "next":
            card = store.pick_next_card()
            if card is None:
                return CommandResult(message="No queued autopilot cards.")
            return CommandResult(message=_render_card(card))

        if action == "context":
            content = store.load_active_context()
            return CommandResult(message=content or "Active repo context is empty.")

        if action == "journal":
            limit = 8
            if len(tokens) >= 2:
                try:
                    limit = max(1, min(30, int(tokens[1])))
                except ValueError:
                    return CommandResult(message="Usage: /autopilot journal [LIMIT]")
            entries = store.load_journal(limit=limit)
            if not entries:
                return CommandResult(message="Repo journal is empty.")
            lines = []
            for entry in entries:
                timestamp = datetime.fromtimestamp(entry.timestamp, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                task_suffix = f" [{entry.task_id}]" if entry.task_id else ""
                lines.append(f"{timestamp} {entry.kind}{task_suffix}: {entry.summary}")
            return CommandResult(message="\n".join(lines))

        if action == "add":
            raw = args[len("add") :].strip()
            if not raw:
                return CommandResult(
                    message=(
                        "Usage: /autopilot add "
                        "[idea|ohmo|issue|pr|claude] TITLE :: DETAILS"
                    )
                )
            source_kind = "manual_idea"
            source_map = {
                "idea": "manual_idea",
                "manual": "manual_idea",
                "ohmo": "ohmo_request",
                "issue": "github_issue",
                "pr": "github_pr",
                "claude": "claude_code_candidate",
            }
            if " " in raw:
                first, remainder = raw.split(" ", 1)
                mapped = source_map.get(first.lower())
                if mapped is not None:
                    source_kind = mapped
                    raw = remainder.strip()
            title, _, body = raw.partition("::")
            if not title.strip():
                return CommandResult(
                    message=(
                        "Usage: /autopilot add "
                        "[idea|ohmo|issue|pr|claude] TITLE :: DETAILS"
                    )
                )
            card, created = store.enqueue_card(
                source_kind=source_kind,
                title=title.strip(),
                body=body.strip(),
            )
            status_word = "Queued" if created else "Refreshed"
            return CommandResult(
                message=f"{status_word} autopilot card {card.id} (score={card.score}): {card.title}"
            )

        if action in {"accept", "start", "complete", "reject", "fail"} and len(tokens) >= 2:
            status_map = {
                "accept": "accepted",
                "start": "running",
                "complete": "completed",
                "fail": "failed",
                "reject": "rejected",
            }
            note = ""
            if len(tokens) >= 3:
                note = args.split(maxsplit=2)[2]
            try:
                card = store.update_status(tokens[1], status=status_map[action], note=note or None)
            except ValueError as exc:
                return CommandResult(message=str(exc))
            return CommandResult(message=f"{card.id} -> {card.status}: {card.title}")

        if action == "run-next":
            try:
                result = await store.run_next()
            except ValueError as exc:
                return CommandResult(message=str(exc))
            return CommandResult(
                message=(
                    f"{result.card_id} -> {result.status}\n"
                    f"run report: {result.run_report_path}\n"
                    f"verification report: {result.verification_report_path}"
                )
            )

        if action == "tick":
            try:
                result = await store.tick()
            except ValueError as exc:
                return CommandResult(message=str(exc))
            if result is None:
                return CommandResult(message="Autopilot tick completed with no execution.")
            return CommandResult(
                message=(
                    f"Autopilot tick executed {result.card_id} -> {result.status}\n"
                    f"run report: {result.run_report_path}\n"
                    f"verification report: {result.verification_report_path}"
                )
            )

        if action == "install-cron":
            names = store.install_default_cron()
            return CommandResult(message="Installed autopilot cron jobs: " + ", ".join(names))

        if action == "export-dashboard":
            output = tokens[1] if len(tokens) >= 2 else None
            path = store.export_dashboard(output)
            return CommandResult(message=f"Exported autopilot dashboard: {path}")

        if action == "scan":
            if len(tokens) < 2:
                return CommandResult(
                    message="Usage: /autopilot scan [issues|prs|claude-code|all] [LIMIT]"
                )
            target = tokens[1].lower()
            limit = 10
            if len(tokens) >= 3:
                try:
                    limit = max(1, min(50, int(tokens[2])))
                except ValueError:
                    return CommandResult(
                        message="Usage: /autopilot scan [issues|prs|claude-code|all] [LIMIT]"
                    )
            try:
                if target == "issues":
                    cards = store.scan_github_issues(limit=limit)
                    return CommandResult(message=f"Scanned {len(cards)} GitHub issues into autopilot.")
                if target == "prs":
                    cards = store.scan_github_prs(limit=limit)
                    return CommandResult(message=f"Scanned {len(cards)} GitHub PRs into autopilot.")
                if target == "claude-code":
                    cards = store.scan_claude_code_candidates(limit=limit)
                    return CommandResult(
                        message=f"Scanned {len(cards)} claude-code candidates into autopilot."
                    )
                if target == "all":
                    counts = store.scan_all_sources(issue_limit=limit, pr_limit=limit)
                    return CommandResult(message=f"Scanned all sources: {json.dumps(counts)}")
            except ValueError as exc:
                return CommandResult(message=str(exc))
            return CommandResult(
                message="Usage: /autopilot scan [issues|prs|claude-code|all] [LIMIT]"
            )

        return CommandResult(
            message=(
                "Usage: /autopilot "
                "[status|list [STATUS]|show ID|next|context|journal [LIMIT]|"
                "add [idea|ohmo|issue|pr|claude] TITLE :: DETAILS|"
                "accept ID|start ID|complete ID [NOTE]|fail ID [NOTE]|reject ID [NOTE]|"
                "run-next|tick|install-cron|export-dashboard [OUTPUT]|"
                "scan [issues|prs|claude-code|all] [LIMIT]]"
            )
        )

    async def _ship_handler(args: str, context: CommandContext) -> CommandResult:
        raw = args.strip()
        if not raw:
            return CommandResult(message="Usage: /ship TITLE :: DETAILS")
        title, _, body = raw.partition("::")
        if not title.strip():
            return CommandResult(message="Usage: /ship TITLE :: DETAILS")
        store = RepoAutopilotStore(context.cwd)
        card, _ = store.enqueue_card(
            source_kind="ohmo_request",
            title=title.strip(),
            body=body.strip(),
        )
        try:
            result = await store.run_card(card.id)
        except ValueError as exc:
            return CommandResult(message=str(exc))
        return CommandResult(
            message=(
                f"{result.card_id} -> {result.status}\n"
                f"run report: {result.run_report_path}\n"
                f"verification report: {result.verification_report_path}"
            )
        )

    registry.register(SlashCommand("help", "Show available commands", _help_handler))
    registry.register(SlashCommand("exit", "Exit OpenHarness", _exit_handler))
    registry.register(SlashCommand("clear", "Clear conversation history", _clear_handler))
    registry.register(SlashCommand("version", "Show the installed HanPlanet CLI version", _version_handler))
    registry.register(SlashCommand("status", "Show session status", _status_handler))
    registry.register(SlashCommand("context", "Show the active runtime system prompt", _context_handler))
    registry.register(SlashCommand("summary", "Summarize conversation history", _summary_handler))
    registry.register(SlashCommand("compact", "Compact older conversation history", _compact_handler))
    registry.register(SlashCommand("cost", "Show token usage and estimated cost", _cost_handler))
    registry.register(SlashCommand("usage", "Show usage and token estimates", _usage_handler))
    registry.register(SlashCommand("stats", "Show session statistics", _stats_handler))
    registry.register(SlashCommand("memory", "Inspect and manage project memory", _memory_handler))
    registry.register(SlashCommand("hooks", "Show configured hooks", _hooks_handler))
    registry.register(SlashCommand("resume", "Restore the latest saved session", _resume_handler))
    registry.register(SlashCommand("session", "Inspect the current session storage", _session_handler))
    registry.register(SlashCommand("export", "Export the current transcript", _export_handler))
    registry.register(SlashCommand("share", "Create a shareable transcript snapshot", _share_handler))
    registry.register(SlashCommand("copy", "Copy the latest response or provided text", _copy_handler))
    registry.register(SlashCommand("tag", "Create a named snapshot of the current session", _tag_handler))
    registry.register(SlashCommand("rewind", "Remove the latest conversation turn(s)", _rewind_handler))
    registry.register(SlashCommand("files", "List files in the current workspace", _files_handler))
    registry.register(SlashCommand("init", "Initialize project OpenHarness files", _init_handler))
    registry.register(SlashCommand("bridge", "Inspect bridge helpers and spawn bridge sessions", _bridge_handler))
    registry.register(SlashCommand("login", "Show auth status or store an API key", _login_handler))
    registry.register(SlashCommand("logout", "Clear the stored API key", _logout_handler))
    registry.register(SlashCommand("feedback", "Save CLI feedback to the local feedback log", _feedback_handler))
    registry.register(SlashCommand("onboarding", "Show the quickstart guide", _onboarding_handler))
    registry.register(SlashCommand("skills", "List or show available skills", _skills_handler))
    registry.register(SlashCommand("config", "Show or update configuration", _config_handler))
    registry.register(SlashCommand("language", "Show or set AI response language", _language_handler))
    registry.register(SlashCommand("mcp", "Show MCP status", _mcp_handler))
    registry.register(
        SlashCommand(
            "plugin",
            "Manage plugins",
            _plugin_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )
    registry.register(
        SlashCommand(
            "reload-plugins",
            "Reload plugin discovery for this workspace",
            _reload_plugins_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )
    registry.register(
        SlashCommand(
            "permissions",
            "Show or update permission mode",
            _permissions_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )
    registry.register(
        SlashCommand(
            "plan",
            "Toggle plan permission mode",
            _plan_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )
    registry.register(SlashCommand("fast", "Show or update fast mode", _fast_handler))
    registry.register(SlashCommand("effort", "Show or update reasoning effort", _effort_handler))
    registry.register(SlashCommand("passes", "Show or update reasoning pass count", _passes_handler))
    registry.register(SlashCommand("turns", "Show or update maximum agentic turn count", _turns_handler))
    registry.register(SlashCommand("continue", "Continue the previous tool loop if it was interrupted", _continue_handler))
    registry.register(SlashCommand("provider", "Show or switch provider profiles", _provider_handler))
    registry.register(SlashCommand("model", "Show or update the default model", _model_handler))
    registry.register(SlashCommand("theme", "List, set, show or preview TUI themes", _theme_handler))
    registry.register(SlashCommand("output-style", "Show or update output style", _output_style_handler))
    registry.register(SlashCommand("keybindings", "Show resolved keybindings", _keybindings_handler))
    registry.register(SlashCommand("vim", "Show or update Vim mode", _vim_handler))
    registry.register(SlashCommand("voice", "Show or update voice mode", _voice_handler))
    registry.register(SlashCommand("doctor", "Show environment diagnostics", _doctor_handler))
    registry.register(SlashCommand("diff", "Show git diff output", _diff_handler))
    registry.register(SlashCommand("branch", "Show git branch information", _branch_handler))
    registry.register(SlashCommand("commit", "Show status or create a git commit", _commit_handler))
    registry.register(SlashCommand("issue", "Show or update project issue context", _issue_handler))
    registry.register(SlashCommand("pr_comments", "Show or update project PR comments context", _pr_comments_handler))
    registry.register(SlashCommand("privacy-settings", "Show local privacy and storage settings", _privacy_settings_handler))
    registry.register(SlashCommand("rate-limit-options", "Show ways to reduce provider rate pressure", _rate_limit_options_handler))
    registry.register(SlashCommand("release-notes", "Show recent OpenHarness release notes", _release_notes_handler))
    registry.register(SlashCommand("upgrade", "Show upgrade instructions", _upgrade_handler))
    registry.register(SlashCommand("update", "Pull latest changes from git and update HanPlanet CLI", _update_handler))
    registry.register(SlashCommand("agents", "List or inspect agent and teammate tasks", _agents_handler))
    registry.register(SlashCommand("subagents", "Show subagent usage and inspect worker tasks", _agents_handler))
    registry.register(SlashCommand("tasks", "Manage background tasks", _tasks_handler))
    registry.register(SlashCommand("autopilot", "Manage repo autopilot intake and context", _autopilot_handler))
    registry.register(SlashCommand("ship", "Queue and execute an ohmo-driven repo task", _ship_handler))

    for plugin_command in plugin_commands or ():
        if not plugin_command.user_invocable:
            continue

        async def _plugin_command_handler(
            args: str,
            context: CommandContext,
            *,
            command: PluginCommandDefinition = plugin_command,
        ) -> CommandResult:
            prompt = _render_plugin_command_prompt(
                command,
                args,
                getattr(context, "session_id", None),
            )
            if command.disable_model_invocation:
                return CommandResult(message=prompt)
            return CommandResult(
                submit_prompt=prompt,
                submit_model=command.model,
            )

        registry.register(
            SlashCommand(
                plugin_command.name,
                plugin_command.description,
                _plugin_command_handler,
            )
        )
    return registry


def _resolve_memory_entry_path(memory_dir: Path, candidate: str) -> tuple[Path | None, bool]:
    """Resolve a memory entry path while enforcing containment under ``memory_dir``."""

    base = memory_dir.resolve()
    resolved, invalid = _resolve_memory_candidate(base, candidate)
    if invalid:
        return None, True
    if resolved is not None and resolved.exists():
        return resolved, False
    fallback, invalid = _resolve_memory_candidate(base, f"{candidate}.md")
    if invalid:
        return None, True
    if fallback is not None and fallback.exists():
        return fallback, False
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", candidate.strip().lower()).strip("_")
    if slug and slug != candidate:
        slugged, invalid = _resolve_memory_candidate(base, f"{slug}.md")
        if invalid:
            return None, True
        if slugged is not None and slugged.exists():
            return slugged, False
    return None, False


def _resolve_memory_candidate(memory_dir: Path, candidate: str) -> tuple[Path | None, bool]:
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = memory_dir / path
    resolved = path.resolve()
    try:
        resolved.relative_to(memory_dir)
    except ValueError:
        return None, True
    return resolved, False
