"""JSON-lines backend host for the React terminal frontend."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from openharness.api.client import SupportsStreamingMessages
from openharness.auth.manager import AuthManager
from openharness.config.settings import CLAUDE_MODEL_ALIAS_OPTIONS, display_model_setting, resolve_model_setting
from openharness.bridge import get_bridge_manager
from openharness.memory import list_memory_files
from openharness.plugins import load_plugins
from openharness.skills import load_skill_registry
from openharness.themes import list_themes
from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    StatusEvent,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.output_styles import load_output_styles
from openharness.tasks import get_task_manager
from openharness.ui.protocol import BackendEvent, FrontendRequest, TranscriptItem
from openharness.ui.runtime import build_runtime, close_runtime, handle_line, start_runtime
from openharness.services.session_backend import SessionBackend

log = logging.getLogger(__name__)

log = logging.getLogger(__name__)

_PROTOCOL_PREFIX = "OHJSON:"


@dataclass(frozen=True)
class BackendHostConfig:
    """Configuration for one backend host session."""

    model: str | None = None
    max_turns: int | None = None
    base_url: str | None = None
    system_prompt: str | None = None
    api_key: str | None = None
    api_format: str | None = None
    active_profile: str | None = None
    api_client: SupportsStreamingMessages | None = None
    cwd: str | None = None
    restore_messages: list[dict] | None = None
    restore_tool_metadata: dict[str, object] | None = None
    enforce_max_turns: bool = True
    permission_mode: str | None = None
    session_backend: SessionBackend | None = None
    extra_skill_dirs: tuple[str, ...] = ()
    extra_plugin_roots: tuple[str, ...] = ()


class ReactBackendHost:
    """Drive the OpenHarness runtime over a structured stdin/stdout protocol."""

    def __init__(self, config: BackendHostConfig) -> None:
        self._config = config
        self._bundle = None
        self._write_lock = asyncio.Lock()
        self._request_queue: asyncio.Queue[FrontendRequest] = asyncio.Queue()
        self._permission_requests: dict[str, asyncio.Future[bool]] = {}
        self._question_requests: dict[str, asyncio.Future[str]] = {}
        self._permission_lock = asyncio.Lock()
        self._busy = False
        self._running = True
        # Track last tool input per name for rich event emission
        self._last_tool_inputs: dict[str, dict] = {}

    async def run(self) -> int:
        self._bundle = await build_runtime(
            model=self._config.model,
            max_turns=self._config.max_turns,
            base_url=self._config.base_url,
            system_prompt=self._config.system_prompt,
            api_key=self._config.api_key,
            api_format=self._config.api_format,
            active_profile=self._config.active_profile,
            api_client=self._config.api_client,
            cwd=self._config.cwd,
            restore_messages=self._config.restore_messages,
            restore_tool_metadata=self._config.restore_tool_metadata,
            permission_prompt=self._ask_permission,
            ask_user_prompt=self._ask_question,
            enforce_max_turns=self._config.enforce_max_turns,
            permission_mode=self._config.permission_mode,
            session_backend=self._config.session_backend,
            extra_skill_dirs=self._config.extra_skill_dirs,
            extra_plugin_roots=self._config.extra_plugin_roots,
        )
        await start_runtime(self._bundle)
        await self._emit(
            BackendEvent.ready(
                self._bundle.app_state.get(),
                get_task_manager().list_tasks(),
                _sorted_command_infos(self._bundle.commands.list_commands()),
            )
        )
        await self._emit(self._status_snapshot())

        # 인증이 설정되지 않은 경우 provider picker 자동 실행
        from openharness.api.provider import auth_status as _auth_status
        if _auth_status(self._bundle.current_settings()).startswith("missing"):
            await self._handle_select_command("provider")

        reader = asyncio.create_task(self._read_requests())
        try:
            while self._running:
                request = await self._request_queue.get()
                if request.type == "shutdown":
                    await self._emit(BackendEvent(type="shutdown"))
                    break
                if request.type in ("permission_response", "question_response"):
                    continue
                if request.type == "list_sessions":
                    await self._handle_list_sessions()
                    continue
                if request.type == "select_command":
                    await self._handle_select_command(request.command or "")
                    continue
                if request.type == "apply_select_command":
                    if self._busy:
                        await self._emit(BackendEvent(type="error", message="Session is busy"))
                        continue
                    self._busy = True
                    try:
                        should_continue = await self._apply_select_command(
                            request.command or "",
                            request.value or "",
                        )
                    finally:
                        self._busy = False
                    if not should_continue:
                        await self._emit(BackendEvent(type="shutdown"))
                        break
                    continue
                if request.type != "submit_line":
                    await self._emit(BackendEvent(type="error", message=f"Unknown request type: {request.type}"))
                    continue
                if self._busy:
                    await self._emit(BackendEvent(type="error", message="Session is busy"))
                    continue
                line = (request.line or "").strip()
                if not line:
                    continue
                self._busy = True
                try:
                    should_continue = await self._process_line(line)
                finally:
                    self._busy = False
                if not should_continue:
                    await self._emit(BackendEvent(type="shutdown"))
                    break
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            if self._bundle is not None:
                await close_runtime(self._bundle)
        return 0

    async def _read_requests(self) -> None:
        while True:
            raw = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not raw:
                await self._request_queue.put(FrontendRequest(type="shutdown"))
                return
            payload = raw.decode("utf-8").strip()
            if not payload:
                continue
            try:
                request = FrontendRequest.model_validate_json(payload)
            except Exception as exc:  # pragma: no cover - defensive protocol handling
                await self._emit(BackendEvent(type="error", message=f"Invalid request: {exc}"))
                continue
            if request.type == "permission_response" and request.request_id in self._permission_requests:
                future = self._permission_requests[request.request_id]
                if not future.done():
                    future.set_result(bool(request.allowed))
                continue
            if request.type == "question_response" and request.request_id in self._question_requests:
                future = self._question_requests[request.request_id]
                if not future.done():
                    future.set_result(request.answer or "")
                continue
            await self._request_queue.put(request)

    async def _process_line(self, line: str, *, transcript_line: str | None = None) -> bool:
        assert self._bundle is not None
        await self._emit(
            BackendEvent(type="transcript_item", item=TranscriptItem(role="user", text=transcript_line or line))
        )

        async def _print_system(message: str) -> None:
            await self._emit(
                BackendEvent(type="transcript_item", item=TranscriptItem(role="system", text=message))
            )

        async def _render_event(event: StreamEvent) -> None:
            if isinstance(event, AssistantTextDelta):
                await self._emit(BackendEvent(type="assistant_delta", message=event.text))
                return
            if isinstance(event, CompactProgressEvent):
                await self._emit(
                    BackendEvent(
                        type="compact_progress",
                        compact_phase=event.phase,
                        compact_trigger=event.trigger,
                        attempt=event.attempt,
                        compact_checkpoint=event.checkpoint,
                        compact_metadata=event.metadata,
                        message=event.message,
                    )
                )
                return
            if isinstance(event, AssistantTurnComplete):
                await self._emit(
                    BackendEvent(
                        type="assistant_complete",
                        message=event.message.text.strip(),
                        item=TranscriptItem(role="assistant", text=event.message.text.strip()),
                    )
                )
                await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
                return
            if isinstance(event, ToolExecutionStarted):
                self._last_tool_inputs[event.tool_name] = event.tool_input or {}
                await self._emit(
                    BackendEvent(
                        type="tool_started",
                        tool_name=event.tool_name,
                        tool_input=event.tool_input,
                        item=TranscriptItem(
                            role="tool",
                            text=f"{event.tool_name} {json.dumps(event.tool_input, ensure_ascii=True)}",
                            tool_name=event.tool_name,
                            tool_input=event.tool_input,
                        ),
                    )
                )
                return
            if isinstance(event, ToolExecutionCompleted):
                await self._emit(
                    BackendEvent(
                        type="tool_completed",
                        tool_name=event.tool_name,
                        output=event.output,
                        is_error=event.is_error,
                        item=TranscriptItem(
                            role="tool_result",
                            text=event.output,
                            tool_name=event.tool_name,
                            is_error=event.is_error,
                        ),
                    )
                )
                await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
                await self._emit(self._status_snapshot())
                # Emit todo_update when TodoWrite tool runs
                if event.tool_name in ("TodoWrite", "todo_write"):
                    tool_input = self._last_tool_inputs.get(event.tool_name, {})
                    # TodoWrite input may have 'todos' list or markdown content field
                    todos = tool_input.get("todos") or tool_input.get("content") or []
                    if isinstance(todos, list) and todos:
                        lines = []
                        for item in todos:
                            if isinstance(item, dict):
                                checked = item.get("status", "") in ("done", "completed", "x", True)
                                text = item.get("content") or item.get("text") or str(item)
                                lines.append(f"- [{'x' if checked else ' '}] {text}")
                        if lines:
                            await self._emit(BackendEvent(type="todo_update", todo_markdown="\n".join(lines)))
                    else:
                        await self._emit_todo_update_from_output(event.output)
                # Emit plan_mode_change when plan-related tools complete
                if event.tool_name in ("set_permission_mode", "plan_mode"):
                    assert self._bundle is not None
                    new_mode = self._bundle.app_state.get().permission_mode
                    await self._emit(BackendEvent(type="plan_mode_change", plan_mode=new_mode))
                return
            if isinstance(event, ErrorEvent):
                await self._emit(BackendEvent(type="error", message=event.message))
                await self._emit(
                    BackendEvent(type="transcript_item", item=TranscriptItem(role="system", text=event.message))
                )
                return
            if isinstance(event, StatusEvent):
                await self._emit(
                    BackendEvent(type="transcript_item", item=TranscriptItem(role="system", text=event.message))
                )
                return

        async def _clear_output() -> None:
            await self._emit(BackendEvent(type="clear_transcript"))

        await self._maybe_refresh_hanplanet_token()

        should_continue = await handle_line(
            self._bundle,
            line,
            print_system=_print_system,
            render_event=_render_event,
            clear_output=_clear_output,
        )
        await self._emit(self._status_snapshot())
        await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
        await self._emit(BackendEvent(type="line_complete"))
        return should_continue

    async def _apply_select_command(self, command_name: str, value: str) -> bool:
        command = command_name.strip().lstrip("/").lower()
        selected = value.strip()

        # /config top-level picker: re-dispatch to the chosen sub-command's selector
        if command == "config":
            await self._handle_select_command(selected)
            return True

        # model group picker → sub-level model list
        if command == "model-group":
            await self._handle_select_command(f"model-for-{selected}")
            return True

        # Hanplanet auth method picker
        if command == "hanplanet-auth":
            if selected == "apikey":
                await self._hanplanet_apikey_flow()
            elif selected == "oauth":
                await self._hanplanet_oauth_flow()
            return True

        # /memory: action picker → optional file picker
        if command == "memory":
            if selected == "add-hint":
                await self._emit(BackendEvent(type="info", message="To add a memory entry, type:\n/memory add TITLE :: CONTENT"))
                await self._emit(BackendEvent(type="line_complete"))
                return True
            if selected in ("show", "remove"):
                await self._handle_select_command(f"memory-{selected}")
                return True
            # "list" falls through to _build_select_command_line

        # /plugin: action picker → optional plugin picker
        if command == "plugin":
            if selected == "install-hint":
                await self._emit(BackendEvent(type="info", message="To install a plugin, type:\n/plugin install PATH"))
                await self._emit(BackendEvent(type="line_complete"))
                return True
            if selected in ("enable", "disable", "uninstall"):
                await self._handle_select_command(f"plugin-{selected}")
                return True
            # "list" falls through to _build_select_command_line

        # /tasks: action picker → optional task picker
        if command == "tasks":
            if selected in ("stop", "show", "output"):
                await self._handle_select_command(f"tasks-{selected}")
                return True
            # "list" falls through to _build_select_command_line

        # /provider __custom_add__ → 인증 방식 멀티선택 후 커스텀 API 추가 플로우
        if command == "provider" and selected == "__custom_add__":
            options = [
                {
                    "value": "oauth",
                    "label": "🌐  OAuth 설정",
                    "description": "브라우저 로그인으로 토큰 자동 발급",
                },
                {
                    "value": "apikey",
                    "label": "🔑  API 키 직접 입력",
                    "description": "API 키를 직접 입력해서 등록",
                },
            ]
            await self._emit(BackendEvent(
                type="select_request",
                modal={"kind": "multiselect", "title": "커스텀 API 추가 — 인증 방식 선택 (복수 가능)", "command": "provider-add-auth"},
                select_options=options,
            ))
            await self._emit(BackendEvent(type="line_complete"))
            return True

        # provider-add-auth: 멀티선택 결과 ("oauth", "apikey", "oauth,apikey" 등)
        if command == "provider-add-auth":
            auth_methods = {m.strip() for m in selected.split(",") if m.strip()}
            await self._custom_provider_add_flow(auth_methods=auth_methods)
            return True

        # /provider 사용자 추가 커스텀 프로바이더 → 관리 메뉴 (builtin/special/__ 가 아닌 경우)
        if command == "provider" and not selected.startswith("__"):
            from openharness.config.settings import builtin_provider_profile_names
            _builtin_names = builtin_provider_profile_names()
            _always_special = {"hanplanet", "ollama"}
            if selected not in _builtin_names and selected not in _always_special:
                await self._handle_select_command(f"custom-provider-{selected}")
                return True

        # custom-provider-manage: action::profile_name
        if command == "custom-provider-manage":
            parts = selected.split("::", 1)
            if len(parts) == 2:
                action, profile_name = parts
                if action == "oauth":
                    await self._custom_provider_oauth_reauth_flow(profile_name)
                elif action == "apikey":
                    await self._custom_provider_apikey_change_flow(profile_name)
                elif action == "delete":
                    await self._custom_provider_delete(profile_name)
            else:
                await self._emit(BackendEvent(type="error", message=f"잘못된 관리 커맨드: {selected}"))
                await self._emit(BackendEvent(type="line_complete"))
            return True

        # /provider hanplanet → always show auth method picker (allows re-auth / account switch)
        if command == "provider" and selected == "hanplanet":
            options = [
                {"value": "oauth",  "label": "🌐  브라우저로 로그인 (OAuth)", "description": "hanplanet.com 계정으로 자동 인증", "active": False},
                {"value": "apikey", "label": "🔑  API 키 직접 입력",          "description": "hanplanet.com에서 발급받은 키",    "active": False},
            ]
            await self._emit(BackendEvent(
                type="select_request",
                modal={"kind": "select", "title": "🏔 Hanplanet 인증 방법 선택", "command": "hanplanet-auth"},
                select_options=options,
            ))
            await self._emit(BackendEvent(type="line_complete"))
            return True

        line = self._build_select_command_line(command, selected)
        if line is None:
            await self._emit(BackendEvent(type="error", message=f"Unknown select command: {command_name}"))
            await self._emit(BackendEvent(type="line_complete"))
            return True
        result = await self._process_line(line, transcript_line=f"/{command}")
        # 프로바이더 전환 후 자동으로 모델 선택 화면 표시
        if command == "provider":
            await self._handle_select_command("model")
        return result

    def _build_select_command_line(self, command: str, value: str) -> str | None:
        if command == "provider":
            return f"/provider {value}"
        if command == "resume":
            return f"/resume {value}" if value else "/resume"
        if command == "permissions":
            return f"/permissions {value}"
        if command == "theme":
            return f"/theme {value}"
        if command == "output-style":
            return f"/output-style {value}"
        if command == "effort":
            return f"/effort {value}"
        if command == "passes":
            return f"/passes {value}"
        if command == "turns":
            return f"/turns {value}"
        if command == "fast":
            return f"/fast {value}"
        if command == "vim":
            return f"/vim {value}"
        if command == "voice":
            return f"/voice {value}"
        if command == "model":
            return f"/model {value}"
        if command == "language":
            return f"/language {value}"
        if command == "memory":
            if value == "list":
                return "/memory list"
        if command == "memory-show":
            return f"/memory show {value}"
        if command == "memory-remove":
            return f"/memory remove {value}"
        if command == "plugin":
            if value == "list":
                return "/plugin list"
        if command == "plugin-enable":
            return f"/plugin enable {value}"
        if command == "plugin-disable":
            return f"/plugin disable {value}"
        if command == "plugin-uninstall":
            return f"/plugin uninstall {value}"
        if command == "rewind":
            return f"/rewind {value}"
        if command == "tasks":
            if value == "list":
                return "/tasks list"
        if command == "tasks-stop":
            return f"/tasks stop {value}"
        if command == "tasks-show":
            return f"/tasks show {value}"
        if command == "tasks-output":
            return f"/tasks output {value}"
        if command == "agents":
            return f"/agents show {value}"
        if command == "skills":
            return f"/skills {value}"
        return None

    def _status_snapshot(self) -> BackendEvent:
        assert self._bundle is not None
        return BackendEvent.status_snapshot(
            state=self._bundle.app_state.get(),
            mcp_servers=self._bundle.mcp_manager.list_statuses(),
            bridge_sessions=get_bridge_manager().list_sessions(),
        )

    async def _emit_todo_update_from_output(self, output: str) -> None:
        """Emit a todo_update event by extracting markdown checklist from tool output."""
        # TodoWrite tools typically echo back the written content
        # We look for markdown checklist patterns in the output
        lines = output.splitlines()
        checklist_lines = [line for line in lines if line.strip().startswith("- [")]
        if checklist_lines:
            markdown = "\n".join(checklist_lines)
            await self._emit(BackendEvent(type="todo_update", todo_markdown=markdown))

    def _emit_swarm_status(self, teammates: list[dict], notifications: list[dict] | None = None) -> None:
        """Emit a swarm_status event synchronously (schedule as coroutine)."""
        import asyncio
        loop = asyncio.get_event_loop()
        loop.create_task(
            self._emit(BackendEvent(type="swarm_status", swarm_teammates=teammates, swarm_notifications=notifications))
        )

    async def _handle_list_sessions(self) -> None:
        import time as _time

        assert self._bundle is not None
        sessions = self._bundle.session_backend.list_snapshots(self._bundle.cwd, limit=20)
        if not sessions:
            await self._emit(BackendEvent(type="info", message="저장된 세션이 없습니다."))
            await self._emit(BackendEvent(type="line_complete"))
            return
        options = []
        for s in sessions:
            ts = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(s["created_at"]))
            summary = s.get("summary", "")[:60] or "(내용 없음)"
            model = s.get("model", "")
            msg_count = s["message_count"]
            options.append({
                "value": s["session_id"],
                "label": summary,
                "description": f"{ts}  •  {msg_count}개 메시지  •  {model}",
                "active": False,
            })
        await self._emit(
            BackendEvent(
                type="select_request",
                modal={"kind": "select", "title": "세션 복구", "command": "resume"},
                select_options=options,
            )
        )

    async def _handle_select_command(self, command_name: str) -> None:
        assert self._bundle is not None
        command = command_name.strip().lstrip("/").lower()
        if command == "resume":
            await self._handle_list_sessions()
            return

        settings = self._bundle.current_settings()
        state = self._bundle.app_state.get()
        _, active_profile = settings.resolve_profile()
        current_model = settings.model

        if command == "provider":
            from openharness.config.settings import builtin_provider_profile_names
            statuses = AuthManager(settings).get_profile_statuses()
            builtin_names = builtin_provider_profile_names()

            # 설정됨/활성 우선, 그 다음 기본 내장, 마지막에 미설정 내장
            def _sort_key(item: tuple) -> tuple:
                name, info = item
                if info["active"]:
                    return (0, name)
                if info["configured"]:
                    return (1, name)
                if name not in builtin_names:
                    return (2, name)   # 사용자 추가 커스텀
                return (3, name)

            PROVIDER_EMOJI = {
                "hanplanet": "🏔", "ollama": "🦙",
                "claude-api": "🟣", "claude-subscription": "🟣",
                "openai-compatible": "🟢", "codex": "🟢", "copilot": "⚙️",
                "moonshot": "🌙", "gemini": "🔷",
            }
            _HIDDEN_PROFILES = {"openrouter"}
            options = []
            for name, info in sorted(statuses.items(), key=_sort_key):
                if name in _HIDDEN_PROFILES:
                    continue
                emoji = PROVIDER_EMOJI.get(name, "🔌")
                if info["configured"]:
                    status_tag = "✓ 설정됨"
                else:
                    status_tag = "미설정"
                if name == "hanplanet":
                    desc = f"Hanplanet oauth / key  [{status_tag}]"
                else:
                    desc = f"{info['provider']} / {info['auth_source']}  [{status_tag}]"
                options.append({
                    "value": name,
                    "label": f"{emoji}  {info['label']}",
                    "description": desc,
                    "active": info["active"],
                })
            # 커스텀 API 추가 옵션
            options.append({
                "value": "__custom_add__",
                "label": "➕  커스텀 API 추가",
                "description": "OpenAI-compatible URL + API 키로 직접 등록",
                "active": False,
            })
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Provider Profile", "command": "provider"},
                    select_options=options,
                )
            )
            return

        if command.startswith("custom-provider-"):
            profile_name = command[len("custom-provider-"):]
            # 등록 시 선택했던 인증 방식 목록 로드
            from openharness.auth.storage import load_credential
            stored_methods_raw = ""
            try:
                stored_methods_raw = load_credential(profile_name, "auth_methods") or ""
            except Exception:
                pass
            stored_methods = {m.strip() for m in stored_methods_raw.split(",") if m.strip()}
            # 하위 호환: auth_methods 없으면 oauth_auth_url 존재 여부로 판단
            if not stored_methods:
                try:
                    if load_credential(profile_name, "oauth_auth_url"):
                        stored_methods.add("oauth")
                except Exception:
                    pass
                try:
                    if load_credential(profile_name, "api_key"):
                        stored_methods.add("apikey")
                except Exception:
                    pass
                if not stored_methods:
                    stored_methods.add("apikey")  # 기본 fallback
            profile_label = profile_name
            if profile_name in (settings.provider_profiles or {}):
                profile_label = settings.provider_profiles[profile_name].label or profile_name
            options = []
            if "oauth" in stored_methods:
                options.append({
                    "value": f"oauth::{profile_name}",
                    "label": "🌐  OAuth 재인증",
                    "description": "브라우저로 다시 인증합니다",
                    "active": False,
                })
            if "apikey" in stored_methods:
                options.append({
                    "value": f"apikey::{profile_name}",
                    "label": "🔑  API 키 변경",
                    "description": "새 API 키로 교체합니다",
                    "active": False,
                })
            options.append({
                "value": f"delete::{profile_name}",
                "label": "🗑  프로바이더 삭제",
                "description": f"'{profile_label}' 프로파일을 완전히 삭제합니다",
                "active": False,
            })
            await self._emit(BackendEvent(
                type="select_request",
                modal={"kind": "select", "title": f"⚙️  {profile_label} 관리", "command": "custom-provider-manage"},
                select_options=options,
            ))
            await self._emit(BackendEvent(type="line_complete"))
            return

        if command == "permissions":
            options = [
                {
                    "value": "default",
                    "label": "Default",
                    "description": "Ask before write/execute operations",
                    "active": settings.permission.mode.value == "default",
                },
                {
                    "value": "full_auto",
                    "label": "Auto",
                    "description": "Allow all tools automatically",
                    "active": settings.permission.mode.value == "full_auto",
                },
                {
                    "value": "plan",
                    "label": "Plan Mode",
                    "description": "Block all write operations",
                    "active": settings.permission.mode.value == "plan",
                },
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Permission Mode", "command": "permissions"},
                    select_options=options,
                )
            )
            return

        if command == "theme":
            options = [
                {
                    "value": name,
                    "label": name,
                    "active": name == settings.theme,
                }
                for name in list_themes()
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Theme", "command": "theme"},
                    select_options=options,
                )
            )
            return

        if command == "output-style":
            options = [
                {
                    "value": style.name,
                    "label": style.name,
                    "description": style.source,
                    "active": style.name == settings.output_style,
                }
                for style in load_output_styles()
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Output Style", "command": "output-style"},
                    select_options=options,
                )
            )
            return

        if command == "effort":
            options = [
                {"value": "low", "label": "Low", "description": "Fastest responses", "active": settings.effort == "low"},
                {"value": "medium", "label": "Medium", "description": "Balanced reasoning", "active": settings.effort == "medium"},
                {"value": "high", "label": "High", "description": "Deepest reasoning", "active": settings.effort == "high"},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Reasoning Effort", "command": "effort"},
                    select_options=options,
                )
            )
            return

        if command == "passes":
            current = int(state.passes or settings.passes)
            options = [
                {"value": str(value), "label": f"{value} pass{'es' if value != 1 else ''}", "active": value == current}
                for value in range(1, 9)
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Reasoning Passes", "command": "passes"},
                    select_options=options,
                )
            )
            return

        if command == "turns":
            current = self._bundle.engine.max_turns
            values = {32, 64, 128, 200, 256, 512}
            if isinstance(current, int):
                values.add(current)
            options = [{"value": "unlimited", "label": "Unlimited", "description": "Do not hard-stop this session", "active": current is None}]
            options.extend(
                {"value": str(value), "label": f"{value} turns", "active": value == current}
                for value in sorted(values)
            )
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Max Turns", "command": "turns"},
                    select_options=options,
                )
            )
            return

        if command == "fast":
            current = bool(state.fast_mode)
            options = [
                {"value": "on", "label": "On", "description": "Prefer shorter, faster responses", "active": current},
                {"value": "off", "label": "Off", "description": "Use normal response mode", "active": not current},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Fast Mode", "command": "fast"},
                    select_options=options,
                )
            )
            return

        if command == "vim":
            current = bool(state.vim_enabled)
            options = [
                {"value": "on", "label": "On", "description": "Enable Vim keybindings", "active": current},
                {"value": "off", "label": "Off", "description": "Use standard keybindings", "active": not current},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Vim Mode", "command": "vim"},
                    select_options=options,
                )
            )
            return

        if command == "voice":
            current = bool(state.voice_enabled)
            options = [
                {"value": "on", "label": "On", "description": state.voice_reason or "Enable voice mode", "active": current},
                {"value": "off", "label": "Off", "description": "Disable voice mode", "active": not current},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Voice Mode", "command": "voice"},
                    select_options=options,
                )
            )
            return

        if command == "model-group":
            options = [
                {"value": "anthropic", "label": "🟣  Anthropic / Claude",  "description": "claude-sonnet, claude-opus …",         "active": active_profile.provider in {"anthropic", "anthropic_claude"}},
                {"value": "openai",    "label": "🟢  OpenAI",              "description": "gpt-5, o4-mini …",                     "active": active_profile.provider in {"openai", "openai_codex"}},
                {"value": "ollama",    "label": "🦙  Ollama / Local",       "description": "llama, gemma, qwen … (로컬 목록 자동 조회)", "active": active_profile.provider == "ollama"},
                {"value": "deepseek",  "label": "🔵  DeepSeek",            "description": "deepseek-chat, deepseek-reasoner",      "active": active_profile.provider == "deepseek"},
                {"value": "gemini",    "label": "🔷  Google Gemini",        "description": "gemini-2.5-pro, gemini-2.5-flash",      "active": active_profile.provider == "gemini"},
                {"value": "dashscope", "label": "🟡  DashScope / Qwen",    "description": "qwen3-max, qwen3.5-flash",              "active": active_profile.provider == "dashscope"},
                {"value": "moonshot",  "label": "🌙  Moonshot / Kimi",     "description": "kimi-k2.5, kimi-k2-turbo",             "active": active_profile.provider == "moonshot"},
                {"value": "groq",      "label": "⚡  Groq",                "description": "llama-3.3-70b, mixtral",               "active": active_profile.provider == "groq"},
                {"value": "mistral",     "label": "🌊  Mistral",             "description": "mistral-large, codestral",             "active": active_profile.provider == "mistral"},
                {"value": "hanplanet",   "label": "🏔  Hanplanet",            "description": "Hanplanet / Hanplanet oauth, key",  "active": active_profile.provider == "hanplanet"},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Model — Provider 선택", "command": "model-group"},
                    select_options=options,
                )
            )
            return

        if command == "model-for-hanplanet":
            from openharness.auth.storage import load_credential
            existing_key = load_credential("profile:hanplanet", "api_key") or ""
            if existing_key:
                models = await self._fetch_hanplanet_models(existing_key)
                if models:
                    hp_current = display_model_setting(active_profile)
                    options = [
                        {"value": m, "label": m, "description": "🏔 Hanplanet", "active": m == hp_current}
                        for m in models
                    ]
                    await self._emit(BackendEvent(
                        type="select_request",
                        modal={"kind": "select", "title": "🏔 Hanplanet 모델", "command": "model"},
                        select_options=options,
                    ))
                    return
            # No saved key — show auth method picker
            options = [
                {"value": "oauth",  "label": "🌐  브라우저로 로그인 (OAuth)", "description": "hanplanet.com 계정으로 자동 인증", "active": False},
                {"value": "apikey", "label": "🔑  API 키 직접 입력",          "description": "hanplanet.com에서 발급받은 키",    "active": False},
            ]
            await self._emit(BackendEvent(
                type="select_request",
                modal={"kind": "select", "title": "🏔 Hanplanet 인증 방법 선택", "command": "hanplanet-auth"},
                select_options=options,
            ))
            return

        if command.startswith("model-for-"):
            group = command[len("model-for-"):]
            options = self._model_options_for_group(group, current_model, active_profile)
            group_labels = {
                "anthropic": "Anthropic / Claude", "openai": "OpenAI", "ollama": "Ollama / Local",
                "deepseek": "DeepSeek", "gemini": "Gemini", "dashscope": "DashScope / Qwen",
                "moonshot": "Moonshot / Kimi", "groq": "Groq", "mistral": "Mistral",
            }
            title = f"Model — {group_labels.get(group, group.title())}"
            if not options:
                await self._emit(BackendEvent(type="error", message=f"No models for '{group}'"))
                await self._emit(BackendEvent(type="line_complete"))
                return
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": title, "command": "model"},
                    select_options=options,
                )
            )
            await self._emit(BackendEvent(type="line_complete"))
            return

        if command == "model":
            if getattr(active_profile, "credential_slot", None) == "hanplanet":
                from openharness.auth.storage import load_credential
                existing_key = load_credential("profile:hanplanet", "api_key") or ""
                hp_current = display_model_setting(active_profile)
                models = await self._fetch_hanplanet_models(existing_key) if existing_key else []
                if not models and hp_current:
                    models = [hp_current]
                options = [
                    {"value": m, "label": m, "description": "🏔 Hanplanet", "active": m == hp_current}
                    for m in models
                ]
                await self._emit(BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "🏔 Hanplanet 모델", "command": "model"},
                    select_options=options,
                ))
                return
            options = self._model_select_options(current_model, active_profile.provider, active_profile.allowed_models)
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Model", "command": "model"},
                    select_options=options,
                )
            )
            return

        if command == "language":
            current_lang = settings.language or ""
            lang_options = [
                ("Korean",   "한국어"),
                ("English",  "English"),
                ("Japanese", "日本語"),
                ("Chinese",  "中文"),
                ("Spanish",  "Español"),
                ("French",   "Français"),
                ("German",   "Deutsch"),
                ("",         "None (disable)"),
            ]
            options = [
                {
                    "value": val,
                    "label": label,
                    "active": val == current_lang,
                }
                for val, label in lang_options
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Response Language", "command": "language"},
                    select_options=options,
                )
            )
            return

        if command == "config":
            fast_state = "On" if state.fast_mode else "Off"
            vim_state = "On" if state.vim_enabled else "Off"
            options = [
                {"value": "language", "label": "🌐  Response Language",  "description": f"Current: {settings.language or 'None'}",  "active": False},
                {"value": "model",       "label": "🤖  Model",            "description": f"Current: {current_model}",                "active": False},
                {"value": "effort",   "label": "⚡  Reasoning Effort",   "description": f"Current: {settings.effort}",              "active": False},
                {"value": "fast",     "label": "🚀  Fast Mode",          "description": f"Current: {fast_state}",                   "active": False},
                {"value": "vim",      "label": "⌨️   Vim Mode",          "description": f"Current: {vim_state}",                    "active": False},
                {"value": "turns",    "label": "🔄  Max Turns",          "description": f"Current: {settings.max_turns}",           "active": False},
                {"value": "theme",    "label": "🎨  Theme",              "description": f"Current: {settings.theme}",               "active": False},
                {"value": "provider", "label": "🔌  Provider Profile",   "description": f"Current: {settings.active_profile}",      "active": False},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Settings", "command": "config"},
                    select_options=options,
                )
            )
            return

        if command == "memory":
            files = list_memory_files(self._bundle.cwd)
            count = len(files)
            options = [
                {"value": "list",     "label": "📋  목록 보기",  "description": f"{count}개 파일",        "active": False},
                {"value": "show",     "label": "👁   내용 보기",  "description": "파일을 선택하세요",      "active": False},
                {"value": "remove",   "label": "🗑   항목 삭제",  "description": "파일을 선택하세요",      "active": False},
                {"value": "add-hint", "label": "➕  항목 추가",   "description": "/memory add TITLE :: CONTENT", "active": False},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Memory", "command": "memory"},
                    select_options=options,
                )
            )
            return

        if command in ("memory-show", "memory-remove"):
            files = list_memory_files(self._bundle.cwd)
            if not files:
                await self._emit(BackendEvent(type="info", message="메모리 파일이 없습니다."))
                await self._emit(BackendEvent(type="line_complete"))
                return
            verb = "보기" if command == "memory-show" else "삭제"
            options = [{"value": f.name, "label": f.name, "active": False} for f in files]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": f"파일 {verb}", "command": command},
                    select_options=options,
                )
            )
            await self._emit(BackendEvent(type="line_complete"))
            return

        if command == "plugin":
            plugins = load_plugins(settings, self._bundle.cwd)
            count = len(plugins)
            options = [
                {"value": "list",         "label": "📋  목록 보기",    "description": f"{count}개 플러그인",           "active": False},
                {"value": "enable",       "label": "✅  활성화",        "description": "플러그인을 선택하세요",         "active": False},
                {"value": "disable",      "label": "🚫  비활성화",      "description": "플러그인을 선택하세요",         "active": False},
                {"value": "uninstall",    "label": "🗑   제거",          "description": "플러그인을 선택하세요",         "active": False},
                {"value": "install-hint", "label": "📥  설치 (경로 입력)", "description": "/plugin install PATH",       "active": False},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Plugins", "command": "plugin"},
                    select_options=options,
                )
            )
            return

        if command in ("plugin-enable", "plugin-disable", "plugin-uninstall"):
            plugins = load_plugins(settings, self._bundle.cwd)
            if not plugins:
                await self._emit(BackendEvent(type="info", message="플러그인이 없습니다."))
                await self._emit(BackendEvent(type="line_complete"))
                return
            verb_map = {"plugin-enable": "활성화", "plugin-disable": "비활성화", "plugin-uninstall": "제거"}
            verb = verb_map[command]
            options = [
                {
                    "value": p.name,
                    "label": p.name,
                    "active": bool(settings.enabled_plugins.get(p.name, True)),
                }
                for p in plugins
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": f"플러그인 {verb}", "command": command},
                    select_options=options,
                )
            )
            await self._emit(BackendEvent(type="line_complete"))
            return

        if command == "rewind":
            options = [
                {"value": "1",  "label": "1 turn",   "active": False},
                {"value": "2",  "label": "2 turns",  "active": False},
                {"value": "3",  "label": "3 turns",  "active": False},
                {"value": "5",  "label": "5 turns",  "active": False},
                {"value": "10", "label": "10 turns", "active": False},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Rewind: 몇 턴 되돌릴까요?", "command": "rewind"},
                    select_options=options,
                )
            )
            return

        if command == "tasks":
            manager = get_task_manager()
            task_count = len(manager.list_tasks())
            options = [
                {"value": "list",   "label": "📋  목록 보기",     "description": f"{task_count}개 태스크", "active": False},
                {"value": "stop",   "label": "🛑  태스크 중지",   "description": "태스크를 선택하세요",   "active": False},
                {"value": "show",   "label": "👁   상세 보기",     "description": "태스크를 선택하세요",   "active": False},
                {"value": "output", "label": "📄  출력 보기",      "description": "태스크를 선택하세요",   "active": False},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Background Tasks", "command": "tasks"},
                    select_options=options,
                )
            )
            return

        if command in ("tasks-stop", "tasks-show", "tasks-output"):
            manager = get_task_manager()
            tasks = manager.list_tasks()
            if not tasks:
                await self._emit(BackendEvent(type="info", message="실행 중인 태스크가 없습니다."))
                await self._emit(BackendEvent(type="line_complete"))
                return
            verb_map = {"tasks-stop": "중지", "tasks-show": "상세 보기", "tasks-output": "출력 보기"}
            verb = verb_map[command]
            options = [
                {
                    "value": t.id,
                    "label": f"{t.id}  [{t.status}]",
                    "description": (t.description or "")[:60],
                    "active": False,
                }
                for t in tasks
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": f"태스크 {verb}", "command": command},
                    select_options=options,
                )
            )
            await self._emit(BackendEvent(type="line_complete"))
            return

        if command == "agents":
            manager = get_task_manager()
            agent_tasks = [
                t for t in manager.list_tasks()
                if t.type in {"local_agent", "remote_agent", "in_process_teammate"}
            ]
            if not agent_tasks:
                await self._emit(BackendEvent(type="info", message="No active or recorded agents."))
                await self._emit(BackendEvent(type="line_complete"))
                return
            options = [
                {
                    "value": t.id,
                    "label": f"[{t.status}]  {t.id}",
                    "description": (t.description or "")[:60],
                    "active": False,
                }
                for t in agent_tasks
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Agents — 상세 보기", "command": "agents"},
                    select_options=options,
                )
            )
            return

        if command == "skills":
            skill_registry = load_skill_registry(self._bundle.cwd)
            skills = skill_registry.list_skills()
            if not skills:
                await self._emit(BackendEvent(type="info", message="No skills available."))
                await self._emit(BackendEvent(type="line_complete"))
                return
            options = [
                {
                    "value": s.name,
                    "label": s.name,
                    "description": s.description,
                    "active": False,
                }
                for s in skills
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Skills — 내용 보기", "command": "skills"},
                    select_options=options,
                )
            )
            return

        await self._emit(BackendEvent(type="error", message=f"No selector available for /{command}"))

    def _model_select_options(self, current_model: str, provider: str, allowed_models: list[str] | None = None) -> list[dict[str, object]]:
        if allowed_models:
            return [
                {
                    "value": value,
                    "label": value,
                    "description": "Allowed for this profile",
                    "active": value == current_model,
                }
                for value in allowed_models
            ]
        provider_name = provider.lower()
        if provider_name in {"anthropic", "anthropic_claude"}:
            resolved_current = resolve_model_setting(current_model, provider_name)
            return [
                {
                    "value": value,
                    "label": label,
                    "description": description,
                    "active": value == current_model
                    or resolve_model_setting(value, provider_name) == resolved_current,
                }
                for value, label, description in CLAUDE_MODEL_ALIAS_OPTIONS
            ]
        families: list[tuple[str, str]] = []
        if provider_name in {"openai-codex", "openai", "openai-compatible", "openrouter", "github_copilot"}:
            families.extend(
                [
                    ("gpt-5.4", "OpenAI flagship"),
                    ("gpt-5", "General GPT-5"),
                    ("gpt-4.1", "Stable GPT-4.1"),
                    ("o4-mini", "Fast reasoning"),
                ]
            )
        elif provider_name in {"moonshot", "moonshot-compatible"}:
            families.extend(
                [
                    ("kimi-k2.5", "Moonshot K2.5"),
                    ("kimi-k2-turbo-preview", "Faster Moonshot"),
                ]
            )
        elif provider_name == "dashscope":
            families.extend(
                [
                    ("qwen3.5-flash", "Fast Qwen"),
                    ("qwen3-max", "Strong Qwen"),
                    ("deepseek-r1", "Reasoning model"),
                ]
            )
        elif provider_name == "gemini":
            families.extend(
                [
                    ("gemini-2.5-pro", "Gemini Pro"),
                    ("gemini-2.5-flash", "Gemini Flash"),
                ]
            )

        seen: set[str] = set()
        options: list[dict[str, object]] = []
        for value, description in [(current_model, "Current model"), *families]:
            if not value or value in seen:
                continue
            seen.add(value)
            options.append(
                {
                    "value": value,
                    "label": value,
                    "description": description,
                    "active": value == current_model,
                }
            )
        return options

    def _model_options_for_group(
        self,
        group: str,
        current_model: str,
        active_profile: object,
    ) -> list[dict[str, object]]:
        """Return select options for a specific provider group."""

        def opt(value: str, description: str = "") -> dict[str, object]:
            return {"value": value, "label": value, "description": description, "active": value == current_model}

        if group == "anthropic":
            return [
                {"value": v, "label": l, "description": d, "active": v == current_model}
                for v, l, d in CLAUDE_MODEL_ALIAS_OPTIONS
            ]

        if group == "openai":
            return [
                opt("gpt-5.4",       "OpenAI flagship"),
                opt("gpt-5",         "General GPT-5"),
                opt("gpt-4.1",       "Stable GPT-4.1"),
                opt("gpt-4.1-mini",  "Smaller GPT-4.1"),
                opt("o4-mini",       "Fast reasoning"),
                opt("o3",            "Strong reasoning"),
            ]

        if group == "ollama":
            import httpx
            from openharness.config.settings import load_settings
            from urllib.parse import urlparse

            settings = load_settings()
            base_url = (getattr(active_profile, "base_url", None) or "http://localhost:11434/v1").rstrip("/")

            options: list[dict[str, object]] = []

            # 1) Local Ollama /api/tags → prefix "local/<model>"
            try:
                resp = httpx.get("http://localhost:11434/api/tags", timeout=3.0)
                for m in resp.json().get("models", []):
                    name = m["name"]
                    options.append({
                        "value": name,
                        "label": f"local/{name}",
                        "description": "로컬 Ollama",
                        "active": name == current_model,
                    })
            except Exception:
                pass

            # 2) Remote proxy /v1/models → prefix "<host>/<model>"
            try:
                api_key = settings.resolve_auth().value or "ollama"
                headers = {"Authorization": f"Bearer {api_key}"}
                resp = httpx.get(f"{base_url}/models", headers=headers, timeout=5.0)
                body = resp.json()
                data = body.get("data") or [{"id": m} for m in body.get("models", [])]
                host = urlparse(base_url).hostname or base_url
                for m in data:
                    name = m.get("id", "")
                    if not name:
                        continue
                    options.append({
                        "value": name,
                        "label": f"{host}/{name}",
                        "description": f"원격 프록시 ({host})",
                        "active": name == current_model,
                    })
            except Exception:
                pass

            if options:
                return options

            # Fallback suggestions
            return [
                opt("llama3.2",             "Meta Llama 3.2"),
                opt("llama3.1:8b",          "Meta Llama 3.1 8B"),
                opt("gemma3:latest",         "Google Gemma 3"),
                opt("qwen2.5-coder:latest", "Qwen 2.5 Coder"),
                opt("deepseek-r1:latest",   "DeepSeek R1"),
                opt("mistral:latest",       "Mistral 7B"),
                opt("phi4:latest",          "Microsoft Phi-4"),
            ]

        if group == "deepseek":
            return [
                opt("deepseek-chat",       "DeepSeek Chat V3"),
                opt("deepseek-reasoner",   "DeepSeek R1 (reasoning)"),
                opt("deepseek-coder-v2",   "DeepSeek Coder V2"),
            ]

        if group == "gemini":
            return [
                opt("gemini-2.5-pro",     "Gemini 2.5 Pro"),
                opt("gemini-2.5-flash",   "Gemini 2.5 Flash"),
                opt("gemini-2.0-flash",   "Gemini 2.0 Flash"),
                opt("gemini-1.5-pro",     "Gemini 1.5 Pro"),
            ]

        if group == "dashscope":
            return [
                opt("qwen3-max",         "Qwen3 Max"),
                opt("qwen3-plus",        "Qwen3 Plus"),
                opt("qwen3.5-flash",     "Qwen3.5 Flash"),
                opt("deepseek-r1",       "DeepSeek R1 (DashScope)"),
                opt("deepseek-v3",       "DeepSeek V3 (DashScope)"),
            ]

        if group == "moonshot":
            return [
                opt("kimi-k2.5",                "Kimi K2.5"),
                opt("kimi-k2-turbo-preview",    "Kimi K2 Turbo"),
            ]

        if group == "groq":
            return [
                opt("llama-3.3-70b-versatile",  "Llama 3.3 70B"),
                opt("llama-3.1-8b-instant",     "Llama 3.1 8B"),
                opt("mixtral-8x7b-32768",       "Mixtral 8x7B"),
                opt("gemma2-9b-it",             "Gemma 2 9B"),
                opt("deepseek-r1-distill-llama-70b", "DeepSeek R1 Distill"),
            ]

        if group == "mistral":
            return [
                opt("mistral-large-latest",  "Mistral Large"),
                opt("mistral-small-latest",  "Mistral Small"),
                opt("codestral-latest",      "Codestral"),
                opt("mistral-nemo",          "Mistral Nemo 12B"),
            ]

        return []

    # ── 커스텀 프로바이더 추가 ─────────────────────────────────────────────────

    async def _custom_provider_add_flow(self, auth_methods: set[str] | None = None) -> None:
        """커스텀 OpenAI-compatible API 프로바이더를 대화형으로 추가한다."""
        if auth_methods is None:
            auth_methods = {"apikey"}
        try:
            await self._custom_provider_add_flow_inner(auth_methods=auth_methods)
        except Exception as exc:
            await self._emit(BackendEvent(type="error", message=f"커스텀 프로바이더 추가 실패: {exc}"))
            await self._emit(BackendEvent(type="line_complete"))

    async def _custom_provider_add_flow_inner(self, auth_methods: set[str]) -> None:
        import re
        from urllib.parse import urlsplit

        from openharness.auth.manager import AuthManager
        from openharness.api.provider import detect_provider, auth_status
        from openharness.config.settings import ProviderProfile, load_settings

        use_oauth = "oauth" in auth_methods
        use_apikey = "apikey" in auth_methods

        # ① Base URL
        base_url_raw = await self._ask_question(
            "Base URL을 입력하세요\n예) https://api.openai.com/v1  |  http://localhost:11434/v1"
        )
        if not base_url_raw or not base_url_raw.strip():
            await self._emit(BackendEvent(type="line_complete"))
            return
        base_url = base_url_raw.strip().rstrip("/")

        # ② OAuth 정보 수집
        oauth_auth_url = ""
        oauth_token_url = ""
        if use_oauth:
            oauth_auth_url_raw = await self._ask_question(
                "OAuth 로그인 URL을 입력하세요\n예) https://example.com/oauth/authorize"
            )
            oauth_auth_url = (oauth_auth_url_raw or "").strip()
            if not oauth_auth_url:
                await self._emit(BackendEvent(type="line_complete"))
                return
            oauth_token_url_raw = await self._ask_question(
                "토큰 폴링 URL을 입력하세요 (없으면 Enter로 건너뜀):\n예) https://example.com/api/oauth/token"
            )
            oauth_token_url = (oauth_token_url_raw or "").strip()

        # ③ API 키 수집
        api_key = ""
        if use_apikey:
            api_key_raw = await self._ask_question(
                "API 키를 입력하세요 (없으면 Enter로 건너뜀):"
            )
            api_key = (api_key_raw or "").strip()

        # ④ 기본 모델명
        model_raw = await self._ask_question(
            "기본 모델명을 입력하세요\n예) gpt-4o  |  llama3.3:70b  |  gemini-2.5-flash"
        )
        if not model_raw or not model_raw.strip():
            await self._emit(BackendEvent(type="line_complete"))
            return
        model_name = model_raw.strip()

        # ⑤ 프로파일 이름 (자동 제안 → 사용자 확인)
        parsed = urlsplit(base_url)
        auto_name = re.sub(r"[^a-z0-9\-]", "-", parsed.netloc.split(":")[0].lower()).strip("-") or "custom-api"
        name_raw = await self._ask_question(
            f"프로파일 이름을 입력하세요 (비워두면 '{auto_name}' 사용):"
        )
        profile_name = re.sub(r"[^a-zA-Z0-9\-_]", "-", (name_raw or "").strip()).strip("-") or auto_name

        # ⑥ 표시 레이블
        label_raw = await self._ask_question(
            f"표시 이름을 입력하세요 (비워두면 '{profile_name}' 사용):"
        )
        label = (label_raw or "").strip() or profile_name

        # 프로파일 생성 & 저장
        settings = load_settings()
        manager = AuthManager(settings)
        profile = ProviderProfile(
            label=label,
            provider="openai",
            api_format="openai",
            auth_source="openai_api_key",
            default_model=model_name,
            base_url=base_url,
            credential_slot=profile_name,
        )
        manager.upsert_profile(profile_name, profile)
        # 선택한 인증 방식 목록 저장 (관리 메뉴에서 재사용)
        manager.store_profile_credential(profile_name, "auth_methods", ",".join(sorted(auth_methods)))
        if api_key:
            manager.store_profile_credential(profile_name, "api_key", api_key)
        if oauth_auth_url:
            manager.store_profile_credential(profile_name, "oauth_auth_url", oauth_auth_url)
        if oauth_token_url:
            manager.store_profile_credential(profile_name, "oauth_token_url", oauth_token_url)

        manager.use_profile(profile_name)

        # OAuth가 포함된 경우 즉시 브라우저 인증 플로우 실행
        if use_oauth:
            await self._emit(BackendEvent(
                type="info",
                message=f"✅ 커스텀 프로바이더 '{profile_name}' 등록 완료! OAuth 인증을 시작합니다.",
            ))
            await self._custom_provider_oauth_reauth_flow(profile_name, model_name=model_name, base_url=base_url)
            return

        # API 키 전용인 경우 런타임 상태 갱신 후 완료
        updated_settings = load_settings()
        assert self._bundle is not None
        self._bundle.app_state.set(
            model=model_name,
            provider=detect_provider(updated_settings).name,
            auth_status=auth_status(updated_settings),
            base_url=base_url,
        )
        if self._bundle.engine is not None:
            self._bundle.engine.set_model(model_name)

        await self._emit(BackendEvent(
            type="info",
            message=(
                f"✅ 커스텀 프로바이더 '{profile_name}' 추가 완료!\n"
                f"  URL   : {base_url}\n"
                f"  모델  : {model_name}\n"
                f"  키    : {'등록됨' if api_key else '없음 (로컬 서버용)'}\n"
                "설정을 변경하려면 /provider 에서 다시 선택하세요."
            ),
        ))
        await self._emit(BackendEvent(type="line_complete"))

    # ── 커스텀 프로바이더 관리 (재인증 / 키 변경 / 삭제) ─────────────────────

    async def _custom_provider_oauth_reauth_flow(
        self,
        profile_name: str,
        model_name: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """저장된 OAuth URL로 커스텀 프로바이더를 브라우저 인증한다."""
        try:
            await self._custom_provider_oauth_reauth_flow_inner(profile_name, model_name, base_url)
        except Exception as exc:
            await self._emit(BackendEvent(type="error", message=f"OAuth 인증 실패: {exc}"))
            await self._emit(BackendEvent(type="line_complete"))

    async def _custom_provider_oauth_reauth_flow_inner(
        self,
        profile_name: str,
        model_name: str | None = None,
        base_url: str | None = None,
    ) -> None:
        import asyncio
        import platform
        import subprocess

        from openharness.auth.storage import load_credential
        from openharness.auth.manager import AuthManager
        from openharness.config.settings import load_settings
        from openharness.api.provider import detect_provider, auth_status

        oauth_auth_url = load_credential(profile_name, "oauth_auth_url") or ""
        oauth_token_url = load_credential(profile_name, "oauth_token_url") or ""

        if not oauth_auth_url:
            await self._emit(BackendEvent(type="error", message=f"'{profile_name}'에 OAuth URL이 저장되어 있지 않습니다."))
            await self._emit(BackendEvent(type="line_complete"))
            return

        await self._emit(BackendEvent(
            type="info",
            message=f"OAuth 로그인 URL: {oauth_auth_url}",
        ))
        await self._emit(BackendEvent(
            type="oauth_pending",
            message="브라우저에서 위 URL을 열어 인증을 완료해 주세요…",
            timeout_seconds=300,
        ))

        # 브라우저 열기 시도
        try:
            plat = platform.system()
            if plat == "Darwin":
                subprocess.Popen(["open", oauth_auth_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif plat == "Windows":
                subprocess.Popen(["start", "", oauth_auth_url], shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", oauth_auth_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        access_token: str | None = None

        if oauth_token_url:
            # 폴링으로 토큰 획득
            try:
                import httpx
                deadline = asyncio.get_running_loop().time() + 300
                async with httpx.AsyncClient(timeout=10) as client:
                    while asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(2)
                        try:
                            resp = await client.get(oauth_token_url)
                        except Exception:
                            continue
                        if resp.status_code == 200:
                            data = resp.json()
                            if data.get("status") == "cancelled":
                                await self._emit(BackendEvent(type="info", message="인증이 취소됐습니다."))
                                await self._emit(BackendEvent(type="line_complete"))
                                return
                            access_token = data.get("access_token")
                            if access_token:
                                break
            except Exception as exc:
                await self._emit(BackendEvent(type="error", message=f"토큰 폴링 실패: {exc}"))
                await self._emit(BackendEvent(type="line_complete"))
                return
            if not access_token:
                await self._emit(BackendEvent(type="error", message="인증 시간 초과 (300초). 다시 시도해주세요."))
                await self._emit(BackendEvent(type="line_complete"))
                return
        else:
            # 폴링 URL 없음 → 사용자가 직접 토큰을 붙여넣기
            token_raw = await self._ask_question(
                "인증 완료 후 발급된 API 토큰을 여기에 붙여넣으세요:"
            )
            access_token = (token_raw or "").strip()
            if not access_token:
                await self._emit(BackendEvent(type="line_complete"))
                return

        settings = load_settings()
        manager = AuthManager(settings)
        manager.store_profile_credential(profile_name, "api_key", access_token)

        # 런타임 상태 갱신
        updated_settings = load_settings()
        assert self._bundle is not None
        _model = model_name or updated_settings.provider_profiles.get(profile_name, None)
        if _model is None:
            _model = ""
        elif hasattr(_model, "default_model"):
            _model = _model.default_model
        _base_url = base_url or ""
        if not _base_url:
            _prof = updated_settings.provider_profiles.get(profile_name)
            if _prof:
                _base_url = _prof.base_url or ""
        self._bundle.app_state.set(
            provider=detect_provider(updated_settings).name,
            auth_status=auth_status(updated_settings),
            base_url=_base_url,
        )

        await self._emit(BackendEvent(type="info", message=f"✅ '{profile_name}' OAuth 인증 완료!"))
        await self._emit(BackendEvent(type="line_complete"))

    async def _custom_provider_apikey_change_flow(self, profile_name: str) -> None:
        """커스텀 프로바이더의 API 키를 변경한다."""
        try:
            new_key_raw = await self._ask_question(
                f"'{profile_name}'의 새 API 키를 입력하세요:"
            )
            new_key = (new_key_raw or "").strip()
            if not new_key:
                await self._emit(BackendEvent(type="info", message="취소됐습니다."))
                await self._emit(BackendEvent(type="line_complete"))
                return

            from openharness.auth.manager import AuthManager
            from openharness.config.settings import load_settings
            from openharness.api.provider import auth_status

            settings = load_settings()
            manager = AuthManager(settings)
            manager.store_profile_credential(profile_name, "api_key", new_key)

            updated_settings = load_settings()
            assert self._bundle is not None
            self._bundle.app_state.set(auth_status=auth_status(updated_settings))

            await self._emit(BackendEvent(type="info", message=f"✅ '{profile_name}' API 키가 변경됐습니다."))
            await self._emit(BackendEvent(type="line_complete"))
        except Exception as exc:
            await self._emit(BackendEvent(type="error", message=f"API 키 변경 실패: {exc}"))
            await self._emit(BackendEvent(type="line_complete"))

    async def _custom_provider_delete(self, profile_name: str) -> None:
        """커스텀 프로바이더 프로파일과 크리덴셜을 삭제한다."""
        try:
            from openharness.auth.manager import AuthManager
            from openharness.config.settings import load_settings
            from openharness.api.provider import detect_provider, auth_status

            settings = load_settings()
            _, active_prof = settings.resolve_profile()
            manager = AuthManager(settings)

            # 현재 활성 프로파일이면 기본으로 전환 후 삭제
            if active_prof == profile_name:
                try:
                    manager.use_profile("claude-api")
                except Exception:
                    pass

            manager.clear_profile_credential(profile_name)
            manager.remove_profile(profile_name)

            updated_settings = load_settings()
            assert self._bundle is not None
            self._bundle.app_state.set(
                provider=detect_provider(updated_settings).name,
                auth_status=auth_status(updated_settings),
            )

            await self._emit(BackendEvent(type="info", message=f"✅ '{profile_name}' 프로바이더가 삭제됐습니다."))
            await self._emit(BackendEvent(type="line_complete"))
        except Exception as exc:
            await self._emit(BackendEvent(type="error", message=f"프로바이더 삭제 실패: {exc}"))
            await self._emit(BackendEvent(type="line_complete"))

    # ── Hanplanet auth flows ────────────────────────────────────────────────

    async def _hanplanet_apikey_flow(self) -> None:
        api_key = await self._ask_question(
            "Hanplanet API 키를 입력하세요 (hanplanet.com 에서 발급):"
        )
        if not api_key or not api_key.strip():
            await self._emit(BackendEvent(type="line_complete"))
            return
        await self._hanplanet_save_and_select(api_key.strip())

    async def _hanplanet_oauth_flow(self) -> None:
        import asyncio
        import platform
        import secrets
        import subprocess
        from urllib.parse import urlencode

        HANPLANET_BASE = "https://www.hanplanet.com"
        state = secrets.token_hex(16)

        login_url = (
            f"{HANPLANET_BASE}/login/handrive?"
            + urlencode({
                "state":       state,
                "client_name": "HanHarness",
            })
        )
        poll_url = (
            f"{HANPLANET_BASE}/api/sync/auth/handrive-callback?"
            + urlencode({"state": state})
        )

        # URL을 먼저 출력 (브라우저가 자동으로 안 열릴 경우 직접 열 수 있도록)
        await self._emit(BackendEvent(
            type="info",
            message=f"Hanplanet 로그인 URL: {login_url}",
        ))
        await self._emit(BackendEvent(
            type="oauth_pending",
            message="브라우저에서 위 URL을 열어 연결 버튼을 눌러주세요…",
            timeout_seconds=300,
        ))

        # 크로스플랫폼 브라우저 열기 시도
        try:
            plat = platform.system()
            if plat == "Darwin":
                subprocess.Popen(["open", login_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif plat == "Windows":
                subprocess.Popen(["start", "", login_url], shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", login_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        # 사용자가 브라우저에서 "연결" 버튼을 클릭할 때까지 폴링 (최대 300초)
        try:
            import httpx
            deadline = asyncio.get_running_loop().time() + 300
            tokens = None
            async with httpx.AsyncClient(timeout=10) as client:
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(2)
                    try:
                        resp = await client.get(poll_url)
                    except Exception:
                        continue
                    if resp.status_code == 200:
                        tokens = resp.json()
                        break
                    # 202 → pending, 계속 폴링
                    # cancelled 응답은 즉시 종료
            if tokens is None:
                raise asyncio.TimeoutError
        except asyncio.TimeoutError:
            await self._emit(BackendEvent(type="error", message="인증 시간 초과 (300초). 다시 시도해주세요."))
            await self._emit(BackendEvent(type="line_complete"))
            return
        except Exception as exc:
            await self._emit(BackendEvent(type="error", message=f"OAuth 실패: {exc}"))
            await self._emit(BackendEvent(type="line_complete"))
            return

        if tokens.get("status") == "cancelled":
            await self._emit(BackendEvent(type="info", message="연결이 취소됐습니다."))
            await self._emit(BackendEvent(type="line_complete"))
            return

        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if not access_token:
            await self._emit(BackendEvent(type="error", message="토큰 발급 실패: access_token 없음"))
            await self._emit(BackendEvent(type="line_complete"))
            return

        await self._hanplanet_save_and_select(access_token, refresh_token=refresh_token)

    async def _hanplanet_save_and_select(self, api_key: str, refresh_token: str | None = None) -> None:
        """Save Hanplanet API key, create/update profile, then show model picker."""
        try:
            await self._hanplanet_save_and_select_inner(api_key, refresh_token)
        except Exception as exc:
            await self._emit(BackendEvent(type="error", message=f"Hanplanet 설정 실패: {exc}"))
            await self._emit(BackendEvent(type="line_complete"))

    async def _hanplanet_save_and_select_inner(self, api_key: str, refresh_token: str | None = None) -> None:
        from openharness.auth.manager import AuthManager
        from openharness.config.settings import ProviderProfile, load_settings

        settings = load_settings()
        manager = AuthManager(settings)

        # Upsert a dedicated "hanplanet" profile
        profile = ProviderProfile(
            label="Hanplanet",
            provider="openai",
            api_format="openai",
            auth_source="openai_api_key",
            default_model="",
            base_url="https://hanplanet.com/ai/v1",
            credential_slot="hanplanet",
        )
        manager.upsert_profile("hanplanet", profile)
        manager.use_profile("hanplanet")
        manager.store_profile_credential("hanplanet", "api_key", api_key)
        if refresh_token:
            manager.store_profile_credential("hanplanet", "refresh_token", refresh_token)

        # Fetch models using the new key
        models = await self._fetch_hanplanet_models(api_key)
        if not models:
            await self._emit(BackendEvent(
                type="info",
                message="✅ Hanplanet 인증 완료! 모델 목록을 가져올 수 없어 기본 모델을 사용합니다.",
            ))
            await self._emit(BackendEvent(type="line_complete"))
            return

        assert self._bundle is not None
        current_model = display_model_setting(self._bundle.current_settings().resolve_profile()[1])
        options = [
            {"value": m, "label": m, "description": "🏔 Hanplanet", "active": m == current_model}
            for m in models
        ]
        await self._emit(BackendEvent(type="info", message="✅ Hanplanet 인증 완료!"))
        await self._emit(BackendEvent(
            type="select_request",
            modal={"kind": "select", "title": "🏔 Hanplanet 모델 선택", "command": "model"},
            select_options=options,
        ))
        await self._emit(BackendEvent(type="line_complete"))

    async def _maybe_refresh_hanplanet_token(self) -> None:
        """Hanplanet 프로파일 사용 중이면 access token 만료 여부를 확인하고 필요 시 refresh."""
        assert self._bundle is not None
        settings = self._bundle.current_settings()
        _, active_profile = settings.resolve_profile()
        if getattr(active_profile, "credential_slot", None) != "hanplanet":
            return
        try:
            import base64, json as _json, time
            from openharness.auth.storage import load_credential
            token = load_credential("profile:hanplanet", "api_key") or ""
            if not token:
                return
            # JWT payload는 두 번째 세그먼트
            padding = token.split(".")[1] if token.count(".") == 2 else ""
            padding += "=" * (-len(padding) % 4)
            payload = _json.loads(base64.urlsafe_b64decode(padding))
            exp = payload.get("exp", 0)
            # 만료까지 5분 이하 남았으면 미리 refresh
            if exp - time.time() < 300:
                new_token = await ReactBackendHost._hanplanet_refresh_token()
                if new_token:
                    from openharness.ui.runtime import refresh_runtime_client
                    refresh_runtime_client(self._bundle)
        except Exception:
            pass

    @staticmethod
    async def _hanplanet_refresh_token() -> str | None:
        """refresh token으로 새 access token 발급 후 저장. 실패 시 None 반환."""
        try:
            from openharness.auth.storage import load_credential, store_credential
            import httpx
            refresh_token = load_credential("profile:hanplanet", "refresh_token")
            if not refresh_token:
                return None
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://hanplanet.com/api/sync/auth/refresh",
                    json={"refresh_token": refresh_token},
                )
            if resp.status_code != 200:
                return None
            new_access = resp.json().get("access_token")
            if new_access:
                store_credential("profile:hanplanet", "api_key", new_access)
            return new_access
        except Exception:
            return None

    @staticmethod
    async def _fetch_hanplanet_models(api_key: str) -> list[str]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://hanplanet.com/ai/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if resp.status_code == 401:
                new_key = await ReactBackendHost._hanplanet_refresh_token()
                if not new_key:
                    return []
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        "https://hanplanet.com/ai/v1/models",
                        headers={"Authorization": f"Bearer {new_key}"},
                    )
            body = resp.json()
            data = body.get("data") or [{"id": m} for m in body.get("models", [])]
            return [m["id"] for m in data if m.get("id")]
        except Exception:
            return []

    async def _ask_permission(self, tool_name: str, reason: str) -> bool:
        async with self._permission_lock:
            request_id = uuid4().hex
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._permission_requests[request_id] = future
            await self._emit(
                BackendEvent(
                    type="modal_request",
                    modal={
                        "kind": "permission",
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "reason": reason,
                    },
                )
            )
            try:
                return await asyncio.wait_for(future, timeout=300)
            except asyncio.TimeoutError:
                log.warning("Permission request %s timed out after 300s, denying", request_id)
                return False
            finally:
                self._permission_requests.pop(request_id, None)

    async def _ask_question(self, question: str) -> str:
        request_id = uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._question_requests[request_id] = future
        await self._emit(
            BackendEvent(
                type="modal_request",
                modal={
                    "kind": "question",
                    "request_id": request_id,
                    "question": question,
                },
            )
        )
        try:
            return await future
        finally:
            self._question_requests.pop(request_id, None)

    async def _emit(self, event: BackendEvent) -> None:
        log.debug("emit event: type=%s tool=%s", event.type, getattr(event, "tool_name", None))
        async with self._write_lock:
            payload = _PROTOCOL_PREFIX + event.model_dump_json() + "\n"
            buffer = getattr(sys.stdout, "buffer", None)
            if buffer is not None:
                buffer.write(payload.encode("utf-8"))
                buffer.flush()
                return
            sys.stdout.write(payload)
            sys.stdout.flush()


_COMMAND_PRIORITY = [
    "clear", "config", "model", "provider", "memory", "help",
    "continue", "rewind", "tasks", "agents", "skills", "plugin",
    "theme", "language", "permissions", "plan",
    "compact", "summary", "commit", "diff", "branch",
    "status", "hooks", "mcp", "files",
    "cost", "usage", "stats",
    "login", "logout",
    "export", "share", "copy", "feedback",
    "session", "resume", "tag",
    "init", "bridge",
    "fast", "effort", "passes", "turns",
    "vim", "voice", "output-style", "keybindings",
    "version", "context",
    "doctor", "onboarding", "release-notes", "upgrade",
    "issue", "pr_comments",
    "privacy-settings", "rate-limit-options",
    "reload-plugins",
]


def _sorted_command_infos(commands: list) -> list[dict[str, str]]:
    """Return commands as {name, description} dicts sorted by usage frequency."""
    priority = {name: i for i, name in enumerate(_COMMAND_PRIORITY)}
    sorted_cmds = sorted(commands, key=lambda c: priority.get(c.name, len(_COMMAND_PRIORITY)))
    return [{"name": f"/{c.name}", "description": c.description} for c in sorted_cmds]


async def run_backend_host(
    *,
    model: str | None = None,
    max_turns: int | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    active_profile: str | None = None,
    cwd: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    restore_messages: list[dict] | None = None,
    restore_tool_metadata: dict[str, object] | None = None,
    enforce_max_turns: bool = True,
    permission_mode: str | None = None,
    session_backend: SessionBackend | None = None,
    extra_skill_dirs: tuple[str | Path, ...] = (),
    extra_plugin_roots: tuple[str | Path, ...] = (),
) -> int:
    """Run the structured React backend host."""
    if cwd:
        os.chdir(cwd)
    host = ReactBackendHost(
        BackendHostConfig(
            model=model,
            max_turns=max_turns,
            base_url=base_url,
            system_prompt=system_prompt,
            api_key=api_key,
            api_format=api_format,
            active_profile=active_profile,
            api_client=api_client,
            cwd=cwd,
            restore_messages=restore_messages,
            restore_tool_metadata=restore_tool_metadata,
            enforce_max_turns=enforce_max_turns,
            permission_mode=permission_mode,
            session_backend=session_backend,
            extra_skill_dirs=tuple(str(Path(path).expanduser().resolve()) for path in extra_skill_dirs),
            extra_plugin_roots=tuple(str(Path(path).expanduser().resolve()) for path in extra_plugin_roots),
        )
    )
    return await host.run()


__all__ = ["run_backend_host", "ReactBackendHost", "BackendHostConfig"]
