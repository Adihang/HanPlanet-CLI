from __future__ import annotations

from types import SimpleNamespace

import pytest

from openharness.ui.runtime import handle_line


@pytest.mark.asyncio
async def test_unknown_slash_command_is_not_sent_to_model():
    async def fail_submit(_line):
        raise AssertionError("unknown slash commands must not reach the model")

    async def print_system(message):
        messages.append(message)

    async def render_event(_event):
        return None

    async def clear_output():
        return None

    messages: list[str] = []
    bundle = SimpleNamespace(
        external_api_client=True,
        commands=SimpleNamespace(lookup=lambda _line: None),
        engine=SimpleNamespace(submit_message=fail_submit),
    )

    result = await handle_line(
        bundle,
        "/does-not-exist",
        print_system=print_system,
        render_event=render_event,
        clear_output=clear_output,
    )

    assert result is True
    assert messages == ["Unknown slash command: /does-not-exist"]
