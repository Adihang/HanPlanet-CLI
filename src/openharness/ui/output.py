"""Console rendering helpers with rich markdown, syntax highlighting, and spinners."""

from __future__ import annotations

import threading
import time

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)


class OutputRenderer:
    """Render model and tool events to the terminal with rich formatting."""

    def __init__(self, style_name: str = "default") -> None:
        self.console = Console()
        self._assistant_line_open = False
        self._assistant_buffer = ""
        self._style_name = style_name
        self._spinner_status = None
        self._last_tool_input: dict | None = None
        self._spinner_label: str = ""
        self._spinner_start_time: float | None = None
        self._spinner_stop_event: threading.Event | None = None
        self._spinner_update_thread: threading.Thread | None = None
        # 직전 턴까지의 누적 토큰 (스피너에 표시)
        self._session_input_tokens: int = 0
        self._session_output_tokens: int = 0

    def set_style(self, style_name: str) -> None:
        self._style_name = style_name

    def show_thinking(self) -> None:
        """Show a 'thinking' spinner before the first assistant token arrives."""
        if self._spinner_status is not None:
            return
        if self._style_name == "minimal":
            return
        self._spinner_label = "Thinking"
        self._spinner_start_time = time.monotonic()
        self._spinner_stop_event = threading.Event()
        self._spinner_status = self.console.status(
            self._make_spinner_text(), spinner="dots"
        )
        self._spinner_status.start()
        self._start_spinner_update_thread()

    def start_assistant_turn(self) -> None:
        self._stop_spinner()  # Stop the thinking spinner when output starts
        if self._assistant_line_open:
            self.console.print()
        self._assistant_buffer = ""
        self._assistant_line_open = True
        if self._style_name == "minimal":
            self.console.print("a> ", end="", style="green")
        else:
            self.console.print("[green bold]\u23fa[/green bold] ", end="")

    def render_event(self, event: StreamEvent) -> None:
        if isinstance(event, AssistantTextDelta):
            self._assistant_buffer += event.text
            # Stream raw text for responsiveness
            self.console.print(event.text, end="", markup=False, highlight=False)
            return

        if isinstance(event, AssistantTurnComplete):
            if self._assistant_line_open:
                self.console.print()
                # Re-render with markdown if the buffer contains markdown indicators
                if _has_markdown(self._assistant_buffer) and self._style_name != "minimal":
                    self.console.print()
                    self.console.print(Markdown(self._assistant_buffer.strip()))
                self._assistant_line_open = False
                self._assistant_buffer = ""
            return

        if isinstance(event, ToolExecutionStarted):
            self._stop_spinner()
            if self._assistant_line_open:
                self.console.print()
                self._assistant_line_open = False
            tool_name = event.tool_name
            summary = _summarize_tool_input(tool_name, event.tool_input)
            self._last_tool_input = event.tool_input
            if self._style_name == "minimal":
                self.console.print(f"  > {tool_name} {summary}")
            else:
                self.console.print(
                    f"  [bold cyan]\u23f5 {tool_name}[/bold cyan] [dim]{summary}[/dim]"
                )
                self._start_spinner(tool_name)
            return

        if isinstance(event, ToolExecutionCompleted):
            self._stop_spinner()
            tool_name = event.tool_name
            output = event.output
            is_error = event.is_error
            if self._style_name == "minimal":
                self.console.print(f"    {output}")
                return
            if is_error:
                self.console.print(Panel(output, title=f"{tool_name} error", border_style="red", padding=(0, 1)))
                return
            # Render tool output based on tool type
            tool_input = getattr(event, "tool_input", None) or self._last_tool_input
            self._render_tool_output(tool_name, tool_input, output)

    def print_system(self, message: str) -> None:
        self._stop_spinner()
        if self._assistant_line_open:
            self.console.print()
            self._assistant_line_open = False
        if self._style_name == "minimal":
            self.console.print(message)
        else:
            self.console.print(f"[yellow]\u2139 {message}[/yellow]")

    def print_status_line(
        self,
        *,
        model: str = "unknown",
        input_tokens: int = 0,
        output_tokens: int = 0,
        permission_mode: str = "default",
    ) -> None:
        """Print a compact status line after each turn."""
        # 다음 스피너에서 누적 토큰을 표시할 수 있도록 저장
        self._session_input_tokens = input_tokens
        self._session_output_tokens = output_tokens

        parts = [f"[cyan]model: {model}[/cyan]"]
        if input_tokens > 0 or output_tokens > 0:
            down = "\u2193"
            up = "\u2191"
            parts.append(f"tokens: {_fmt_num(input_tokens)}{down} {_fmt_num(output_tokens)}{up}")
        parts.append(f"mode: {permission_mode}")
        sep = " \u2502 "
        line = sep.join(parts)
        self.console.print(f"[dim]{line}[/dim]")

    def clear(self) -> None:
        self.console.clear()

    def _make_spinner_text(self) -> str:
        """스피너에 표시할 텍스트: 레이블 + 경과 시간 + 누적 토큰."""
        parts = [f"[cyan]{self._spinner_label}...[/cyan]"]
        if self._spinner_start_time is not None:
            elapsed = time.monotonic() - self._spinner_start_time
            parts.append(f"[dim]{_fmt_elapsed(elapsed)}[/dim]")
        if self._session_input_tokens > 0 or self._session_output_tokens > 0:
            down, up = "\u2193", "\u2191"
            parts.append(
                f"[dim]{_fmt_num(self._session_input_tokens)}{down} "
                f"{_fmt_num(self._session_output_tokens)}{up}[/dim]"
            )
        return "  ".join(parts)

    def _start_spinner_update_thread(self) -> None:
        """매초 스피너 텍스트를 갱신하는 데몬 스레드."""
        stop_event = self._spinner_stop_event

        def _tick() -> None:
            while stop_event is not None and not stop_event.wait(1.0):
                if self._spinner_status is not None:
                    try:
                        self._spinner_status.update(self._make_spinner_text())
                    except Exception:
                        break

        t = threading.Thread(target=_tick, daemon=True, name="spinner-tick")
        t.start()
        self._spinner_update_thread = t

    def _start_spinner(self, tool_name: str) -> None:
        if self._style_name == "minimal":
            return
        self._spinner_label = f"Running {tool_name}"
        self._spinner_start_time = time.monotonic()
        self._spinner_stop_event = threading.Event()
        self._spinner_status = self.console.status(
            self._make_spinner_text(), spinner="dots"
        )
        self._spinner_status.start()
        self._start_spinner_update_thread()

    def _stop_spinner(self) -> None:
        if self._spinner_stop_event is not None:
            self._spinner_stop_event.set()
            self._spinner_stop_event = None
        if self._spinner_status is not None:
            self._spinner_status.stop()
            self._spinner_status = None
        self._spinner_start_time = None
        self._spinner_update_thread = None

    def _render_tool_output(self, tool_name: str, tool_input: dict | None, output: str) -> None:
        lower = tool_name.lower()
        # Bash: show in a panel
        if lower == "bash":
            cmd = (tool_input or {}).get("command", "")
            title = f"$ {cmd[:80]}" if cmd else "Bash"
            self.console.print(Panel(output[:2000], title=title, border_style="dim", padding=(0, 1)))
            return
        # Read/FileRead: syntax highlight by file extension
        if lower in ("read", "fileread", "file_read"):
            file_path = str((tool_input or {}).get("file_path", ""))
            ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""
            lexer = _ext_to_lexer(ext)
            if lexer and len(output) < 5000:
                self.console.print(Syntax(output, lexer, theme="monokai", line_numbers=True, word_wrap=True))
            else:
                self.console.print(Panel(output[:2000], title=file_path, border_style="dim", padding=(0, 1)))
            return
        # Edit/FileEdit: show as diff-style
        if lower in ("edit", "fileedit", "file_edit"):
            file_path = str((tool_input or {}).get("file_path", ""))
            self.console.print(Panel(output[:2000], title=f"Edit: {file_path}", border_style="green", padding=(0, 1)))
            return
        # Grep: highlight results
        if lower in ("grep", "greptool"):
            self.console.print(Panel(output[:2000], title="Search results", border_style="cyan", padding=(0, 1)))
            return
        # Default: dimmed text with truncation
        lines = output.split("\n")
        if len(lines) > 15:
            display = "\n".join(lines[:12]) + f"\n... ({len(lines) - 12} more lines)"
        else:
            display = output
        self.console.print(f"    [dim]{display}[/dim]")


def _has_markdown(text: str) -> bool:
    """Check if text likely contains markdown formatting."""
    indicators = ["```", "## ", "### ", "- ", "* ", "1. ", "**", "__", "> "]
    return any(ind in text for ind in indicators)


def _summarize_tool_input(tool_name: str, tool_input: dict | None) -> str:
    if not tool_input:
        return ""
    lower = tool_name.lower()
    if lower == "bash" and "command" in tool_input:
        return str(tool_input["command"])[:120]
    if lower in ("read", "fileread", "file_read") and "file_path" in tool_input:
        return str(tool_input["file_path"])
    if lower in ("write", "filewrite", "file_write") and "file_path" in tool_input:
        return str(tool_input["file_path"])
    if lower in ("edit", "fileedit", "file_edit") and "file_path" in tool_input:
        return str(tool_input["file_path"])
    if lower in ("grep", "greptool") and "pattern" in tool_input:
        return f"/{tool_input['pattern']}/"
    if lower in ("glob", "globtool") and "pattern" in tool_input:
        return str(tool_input["pattern"])
    entries = list(tool_input.items())
    if entries:
        k, v = entries[0]
        return f"{k}={str(v)[:60]}"
    return ""


def _ext_to_lexer(ext: str) -> str | None:
    mapping = {
        "py": "python", "js": "javascript", "ts": "typescript", "tsx": "tsx",
        "jsx": "jsx", "rs": "rust", "go": "go", "rb": "ruby", "java": "java",
        "c": "c", "cpp": "cpp", "h": "c", "hpp": "cpp", "cs": "csharp",
        "sh": "bash", "bash": "bash", "zsh": "bash", "json": "json",
        "yaml": "yaml", "yml": "yaml", "toml": "toml", "xml": "xml",
        "html": "html", "css": "css", "sql": "sql", "md": "markdown",
        "txt": None,
    }
    return mapping.get(ext.lower())


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}m{s:02d}s"


def _fmt_num(n: int) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)
