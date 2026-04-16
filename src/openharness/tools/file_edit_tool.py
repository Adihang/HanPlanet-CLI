"""String-based file editing tool."""

from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class FileEditToolInput(BaseModel):
    """Arguments for the file edit tool."""

    path: str = Field(description="Path of the file to edit")
    old_str: str = Field(description="Exact existing text to replace. Must be copied from the current file contents.")
    new_str: str = Field(description="Replacement text")
    replace_all: bool = Field(default=False)


class FileEditTool(BaseTool):
    """Replace text in an existing file."""

    name = "edit_file"
    description = (
        "Edit an existing file by replacing an exact string. Use only when old_str is copied "
        "from the current file contents; if it fails, read the file again before retrying."
    )
    input_model = FileEditToolInput

    async def execute(
        self,
        arguments: FileEditToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        path = _resolve_path(context.cwd, arguments.path)

        from openharness.sandbox.session import is_docker_sandbox_active

        if is_docker_sandbox_active():
            from openharness.sandbox.path_validator import validate_sandbox_path

            allowed, reason = validate_sandbox_path(path, context.cwd)
            if not allowed:
                return ToolResult(output=f"Sandbox: {reason}", is_error=True)

        if not path.exists():
            return ToolResult(output=f"File not found: {path}", is_error=True)

        original = path.read_text(encoding="utf-8")
        if arguments.old_str not in original:
            return ToolResult(
                output=_format_missing_old_str_error(path=path, original=original, old_str=arguments.old_str),
                is_error=True,
            )

        if arguments.replace_all:
            updated = original.replace(arguments.old_str, arguments.new_str)
        else:
            updated = original.replace(arguments.old_str, arguments.new_str, 1)

        path.write_text(updated, encoding="utf-8")
        return ToolResult(output=f"Updated {path}")


def _resolve_path(base: Path, candidate: str) -> Path:
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _format_missing_old_str_error(*, path: Path, original: str, old_str: str) -> str:
    lines = [
        "old_str was not found in the file.",
        f"File: {path}",
        f"old_str length: {len(old_str)} characters",
        "edit_file requires an exact byte-for-byte text match after UTF-8 decoding.",
    ]

    stripped = old_str.strip()
    if stripped and stripped in original:
        lines.append("Hint: old_str matches only after trimming leading/trailing whitespace. Re-read the file and copy the exact block.")
    elif _normalize_whitespace(old_str) and _normalize_whitespace(old_str) in _normalize_whitespace(original):
        lines.append("Hint: similar text exists, but whitespace or line breaks differ. Re-read the file and copy the exact block.")
    else:
        closest = _closest_line_hint(original=original, old_str=old_str)
        if closest:
            lines.append(f"Closest line in file: {closest}")

    lines.append("Next step: use read_file on this path, then retry with an exact old_str or use write_file/bash for intentional larger rewrites.")
    return "\n".join(lines)


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _closest_line_hint(*, original: str, old_str: str) -> str | None:
    probes = [line.strip() for line in old_str.splitlines() if line.strip()]
    probes.extend(part.strip() for part in old_str.replace("=", " ").split() if len(part.strip()) >= 8)
    if not probes:
        return None
    candidates = [line.strip() for line in original.splitlines() if line.strip()]
    best_match: str | None = None
    best_ratio = 0.0
    for probe in probes:
        matches = difflib.get_close_matches(probe, candidates, n=1, cutoff=0.3)
        if not matches:
            continue
        ratio = difflib.SequenceMatcher(None, probe, matches[0]).ratio()
        if ratio > best_ratio:
            best_match = matches[0]
            best_ratio = ratio
    if best_match is None:
        return None
    return best_match if len(best_match) <= 240 else f"{best_match[:240]}..."
