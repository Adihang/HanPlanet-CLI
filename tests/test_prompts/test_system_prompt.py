"""Tests for openharness.prompts.system_prompt."""

from __future__ import annotations

from openharness.prompts.environment import EnvironmentInfo
from openharness.prompts.system_prompt import build_system_prompt


def _make_env(**overrides) -> EnvironmentInfo:
    defaults = dict(
        os_name="Linux",
        os_version="5.15.0",
        platform_machine="x86_64",
        shell="bash",
        cwd="/home/user/project",
        home_dir="/home/user",
        date="2026-04-01",
        python_version="3.10.17",
        python_executable="/home/user/.openharness-venv/bin/python",
        virtual_env="/home/user/.openharness-venv",
        is_git_repo=True,
        git_branch="main",
        hostname="testhost",
    )
    defaults.update(overrides)
    return EnvironmentInfo(**defaults)


def test_build_system_prompt_contains_environment():
    env = _make_env()
    prompt = build_system_prompt(env=env)
    assert "Linux 5.15.0" in prompt
    assert "x86_64" in prompt
    assert "bash" in prompt
    assert "/home/user/project" in prompt
    assert "2026-04-01" in prompt
    assert "3.10.17" in prompt
    assert "/home/user/.openharness-venv/bin/python" in prompt
    assert "Virtual environment: /home/user/.openharness-venv" in prompt
    assert "branch: main" in prompt


def test_build_system_prompt_no_git():
    env = _make_env(is_git_repo=False, git_branch=None)
    prompt = build_system_prompt(env=env)
    assert "Git:" not in prompt


def test_build_system_prompt_git_no_branch():
    env = _make_env(is_git_repo=True, git_branch=None)
    prompt = build_system_prompt(env=env)
    assert "Git: yes" in prompt
    assert "branch:" not in prompt


def test_build_system_prompt_custom_prompt():
    env = _make_env()
    prompt = build_system_prompt(custom_prompt="You are a helpful bot.", env=env)
    assert prompt.startswith("You are a helpful bot.")
    assert "Linux 5.15.0" in prompt
    # Base prompt should not appear
    assert "HanHarness" not in prompt


def test_build_system_prompt_default_includes_base():
    env = _make_env()
    prompt = build_system_prompt(env=env)
    assert "HanHarness" in prompt


def test_build_system_prompt_allows_standard_developer_commands():
    env = _make_env()
    prompt = build_system_prompt(env=env)
    assert "It is acceptable to use Bash for standard developer commands" in prompt
    assert "`ls`" in prompt
    assert "`rg`" in prompt
    assert "`git status`" in prompt
    assert "`mv`" in prompt
    assert "Do NOT use Bash" not in prompt


def test_build_system_prompt_requires_inspection_before_modification():
    env = _make_env()
    prompt = build_system_prompt(env=env)
    assert "Do not guess project structure" in prompt
    assert "Before modifying a file, verify its current contents" in prompt


def test_build_system_prompt_tells_agent_to_edit_files_directly():
    env = _make_env()
    prompt = build_system_prompt(env=env)
    assert "make the file changes yourself with tools" in prompt
    assert "Do not answer with code blocks and ask the user to copy, paste, or apply them manually" in prompt
    assert "unless the user explicitly asks for instructions only" in prompt


def test_build_system_prompt_warns_edit_file_requires_exact_old_str():
    env = _make_env()
    prompt = build_system_prompt(env=env)
    assert "only when `old_str` is an exact block copied from the current file contents" in prompt
    assert "If `old_str` is not found, re-read the file before retrying" in prompt
