"""Tests for build_runtime auth failure handling."""

from __future__ import annotations

import pytest

from openharness.api.openai_client import OpenAICompatibleClient
from openharness.ui.runtime import build_runtime


@pytest.mark.asyncio
async def test_build_runtime_uses_dummy_client_when_auth_resolution_fails(monkeypatch):
    """HanHarness should start with a dummy client so the provider picker can recover auth."""

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
