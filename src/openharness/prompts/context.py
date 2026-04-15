"""Higher-level system prompt assembly."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from openharness.config.paths import get_project_issue_file, get_project_pr_comments_file
from openharness.config.settings import Settings
from openharness.coordinator.coordinator_mode import get_coordinator_system_prompt, is_coordinator_mode
from openharness.memory import find_relevant_memories, load_memory_prompt
from openharness.personalization.rules import load_local_rules
from openharness.prompts.claudemd import load_claude_md_prompt
from openharness.prompts.system_prompt import build_system_prompt
from openharness.skills.loader import load_skill_registry


def _build_skills_section(
    cwd: str | Path,
    *,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    extra_plugin_roots: Iterable[str | Path] | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Build a system prompt section listing available skills."""
    registry = load_skill_registry(
        cwd,
        extra_skill_dirs=extra_skill_dirs,
        extra_plugin_roots=extra_plugin_roots,
        settings=settings,
    )
    skills = registry.list_skills()
    if not skills:
        return None

    # Determine which skills to preload (inject full content into the system prompt)
    # (어떤 스킬을 시스템 프롬프트에 직접 주입할지 결정)
    preload_names: set[str] = set()
    if settings is not None:
        raw = getattr(settings, "preload_skills", None) or []
        if raw == ["*"]:
            preload_names = {s.name for s in skills}
        else:
            preload_names = {n.strip() for n in raw if n.strip()}

    # Skills that are NOT preloaded → listed for on-demand tool invocation
    # (preload 대상이 아닌 스킬 → tool 호출로 온디맨드 로드)
    on_demand = [s for s in skills if s.name not in preload_names]
    preloaded = [s for s in skills if s.name in preload_names]

    sections: list[str] = []

    if on_demand:
        lines = [
            "# Available Skills",
            "",
            "The following skills are available via the `skill` tool. "
            "When a user's request matches a skill, invoke it with `skill(name=\"<skill_name>\")` "
            "to load detailed instructions before proceeding.",
            "",
        ]
        for skill in on_demand:
            lines.append(f"- **{skill.name}**: {skill.description}")
        sections.append("\n".join(lines))

    for skill in preloaded:
        sections.append(
            f"# Skill: {skill.name}\n\n"
            f"{skill.content.strip()}"
        )

    return "\n\n".join(sections) if sections else None


def _build_delegation_section() -> str:
    """Build a concise section describing delegation and worker usage."""
    return "\n".join(
        [
            "# Delegation And Subagents",
            "",
            "OpenHarness can delegate background work with the `agent` tool.",
            "Use it when the user explicitly asks for a subagent, background worker, or parallel investigation, "
            "or when the task clearly benefits from splitting off a focused worker.",
            "",
            "Default pattern:",
            '- Spawn with `agent(description=..., prompt=..., subagent_type=\"worker\")`.',
            "- Inspect running or recorded workers with `/agents`.",
            "- Inspect one worker in detail with `/agents show TASK_ID`.",
            "- Send follow-up instructions with `send_message(task_id=..., message=...)`.",
            "- Read worker output with `task_output(task_id=...)`.",
            "",
            "Prefer a normal direct answer for simple tasks. Use subagents only when they materially help.",
        ]
    )


def build_runtime_system_prompt(
    settings: Settings,
    *,
    cwd: str | Path,
    latest_user_prompt: str | None = None,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    extra_plugin_roots: Iterable[str | Path] | None = None,
) -> str:
    """Build the runtime system prompt with project instructions and memory."""
    if is_coordinator_mode():
        sections = [get_coordinator_system_prompt()]
    else:
        sections = [build_system_prompt(custom_prompt=settings.system_prompt, cwd=str(cwd))]

    if not is_coordinator_mode() and settings.system_prompt is None:
        sections[0] = build_system_prompt(cwd=str(cwd))

    # Inject a language instruction so the model responds in the configured language
    # (설정된 언어로 응답하도록 언어 지시문을 시스템 프롬프트에 추가)
    if settings.language:
        sections.append(
            f"# Language Instruction\n"
            f"Always respond in **{settings.language}**, regardless of what language the user writes in. "
            f"All explanations, code comments, and messages must be in {settings.language}."
        )

    if settings.fast_mode:
        sections.append(
            "# Session Mode\nFast mode is enabled. Prefer concise replies, minimal tool use, and quicker progress over exhaustive exploration."
        )

    sections.append(
        "# Reasoning Settings\n"
        f"- Effort: {settings.effort}\n"
        f"- Passes: {settings.passes}\n"
        "Adjust depth and iteration count to match these settings while still completing the task."
    )

    skills_section = _build_skills_section(
        cwd,
        extra_skill_dirs=extra_skill_dirs,
        extra_plugin_roots=extra_plugin_roots,
        settings=settings,
    )
    if skills_section and not is_coordinator_mode():
        sections.append(skills_section)

    if not is_coordinator_mode():
        sections.append(_build_delegation_section())

    claude_md = load_claude_md_prompt(cwd)
    if claude_md:
        sections.append(claude_md)

    local_rules = load_local_rules()
    if local_rules:
        sections.append(f"# Local Environment Rules\n\n{local_rules}")

    for title, path in (
        ("Issue Context", get_project_issue_file(cwd)),
        ("Pull Request Comments", get_project_pr_comments_file(cwd)),
    ):
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                sections.append(f"# {title}\n\n```md\n{content[:12000]}\n```")

    if settings.memory.enabled:
        memory_section = load_memory_prompt(
            cwd,
            max_entrypoint_lines=settings.memory.max_entrypoint_lines,
        )
        if memory_section:
            sections.append(memory_section)

        if latest_user_prompt:
            relevant = find_relevant_memories(
                latest_user_prompt,
                cwd,
                max_results=settings.memory.max_files,
            )
            if relevant:
                lines = ["# Relevant Memories"]
                for header in relevant:
                    content = header.path.read_text(encoding="utf-8", errors="replace").strip()
                    lines.extend(
                        [
                            "",
                            f"## {header.path.name}",
                            "```md",
                            content[:8000],
                            "```",
                        ]
                    )
                sections.append("\n".join(lines))

    return "\n\n".join(section for section in sections if section.strip())
