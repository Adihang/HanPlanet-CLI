"""Tests for build_runtime auth failure handling."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openharness.api.openai_client import OpenAICompatibleClient
from openharness.engine.messages import ConversationMessage
from openharness.ui.runtime import build_runtime, refresh_runtime_client


@pytest.mark.asyncio
async def test_build_runtime_uses_dummy_client_when_auth_resolution_fails(monkeypatch):
    """HanPlanet CLI should start with a dummy client so the provider picker can recover auth."""

    def fake_resolve_auth(self):
        raise ValueError("No credentials found")

    monkeypatch.setattr("openharness.config.settings.Settings.resolve_auth", fake_resolve_auth)

    bundle = await build_runtime(active_profile="claude-api")
    assert isinstance(bundle.api_client, OpenAICompatibleClient)
    assert bundle.api_client._client.api_key == "__no_auth__"


@pytest.mark.asyncio
async def test_build_runtime_uses_dummy_client_for_openai_format_auth_failure(monkeypatch):
    """The same recovery path should apply to OpenAI-compatible profiles."""

    def fake_resolve_auth(self):
        raise ValueError("No credentials found")

    monkeypatch.setattr("openharness.config.settings.Settings.resolve_auth", fake_resolve_auth)

    bundle = await build_runtime(active_profile="openai-compatible", api_format="openai")
    assert isinstance(bundle.api_client, OpenAICompatibleClient)
    assert bundle.api_client._client.api_key == "__no_auth__"


def test_refresh_runtime_client_rebuilds_system_prompt_for_language_setting(monkeypatch):
    captured: dict[str, str] = {}

    class _Engine:
        def __init__(self) -> None:
            self.messages = [ConversationMessage.from_user_text("테스트해줘")]

        def set_api_client(self, _client):
            return None

        def set_max_turns(self, _value):
            return None

        def set_model(self, model):
            captured["model"] = model

        def set_system_prompt(self, prompt):
            captured["prompt"] = prompt

    settings = SimpleNamespace(
        model="gpt-5.4",
        max_turns=200,
        permission=SimpleNamespace(mode=SimpleNamespace(value="default")),
        theme="dark",
        base_url="",
        vim_mode=False,
        voice_mode=False,
        fast_mode=False,
        effort="medium",
        passes=1,
        output_style="default",
    )

    bundle = SimpleNamespace(
        external_api_client=True,
        engine=_Engine(),
        cwd="/tmp/demo",
        extra_skill_dirs=(),
        extra_plugin_roots=(),
        current_settings=lambda: settings,
        mcp_manager=SimpleNamespace(list_statuses=lambda: []),
        app_state=SimpleNamespace(set=lambda **_kwargs: None),
        enforce_max_turns=True,
    )

    monkeypatch.setattr("openharness.ui.runtime.detect_provider", lambda _settings: SimpleNamespace(name="openai", voice_supported=False, voice_reason=None))
    monkeypatch.setattr("openharness.ui.runtime.auth_status", lambda _settings: "ok")
    monkeypatch.setattr("openharness.ui.runtime.load_keybindings", lambda: {})
    monkeypatch.setattr("openharness.ui.runtime.get_bridge_manager", lambda: SimpleNamespace(list_sessions=lambda: []))
    monkeypatch.setattr(
        "openharness.ui.runtime.build_runtime_system_prompt",
        lambda *args, **kwargs: "Always respond in **Korean**.",
    )

    refresh_runtime_client(bundle)

    assert captured["model"] == "gpt-5.4"
    assert captured["prompt"] == "Always respond in **Korean**."
