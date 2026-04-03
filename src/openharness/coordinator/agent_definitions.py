"""Agent definition loading system for OpenHarness."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from openharness.config.paths import get_config_dir


class AgentDefinition(BaseModel):
    """Full agent definition with all configuration fields."""

    name: str
    description: str
    system_prompt: str | None = None
    tools: list[str] | None = None  # None means all tools allowed
    model: str | None = None  # model override; None means inherit
    permissions: list[str] = Field(default_factory=list)
    subagent_type: str = "general-purpose"
    source: Literal["builtin", "user", "plugin"] = "builtin"


# ---------------------------------------------------------------------------
# Built-in agent definitions
# ---------------------------------------------------------------------------

_BUILTIN_AGENTS: list[AgentDefinition] = [
    AgentDefinition(
        name="general-purpose",
        description=(
            "General-purpose agent for researching complex questions, searching for code,"
            " and executing multi-step tasks. Use this when no more specific agent type fits."
        ),
        tools=None,  # all tools
        system_prompt=None,
        subagent_type="general-purpose",
        source="builtin",
    ),
    AgentDefinition(
        name="Explore",
        description=(
            "Fast agent specialized for exploring codebases. Use this when you need to"
            " quickly find files by patterns, search code for keywords, or answer questions"
            " about the codebase. Specify thoroughness: 'quick', 'medium', or 'very thorough'."
        ),
        tools=["Read", "Glob", "Grep", "WebFetch", "WebSearch"],
        system_prompt=(
            "You are an expert code exploration agent. Your job is to thoroughly explore"
            " codebases and answer questions. You have read-only access — never edit or"
            " create files. Be concise and precise in your findings."
        ),
        subagent_type="Explore",
        source="builtin",
    ),
    AgentDefinition(
        name="Plan",
        description=(
            "Software architect agent for designing implementation plans. Use this when you"
            " need to plan the implementation strategy for a task. Returns step-by-step plans,"
            " identifies critical files, and considers architectural trade-offs."
        ),
        tools=["Read", "Glob", "Grep", "WebFetch", "WebSearch"],
        system_prompt=(
            "You are a software architect. Your job is to produce clear, actionable"
            " implementation plans. Explore the codebase with read-only tools, identify the"
            " key files and patterns, then return a structured plan. Do not edit files."
        ),
        subagent_type="Plan",
        source="builtin",
    ),
    AgentDefinition(
        name="worker",
        description=(
            "Implementation-focused worker agent. Use this for concrete coding tasks:"
            " writing features, fixing bugs, refactoring code, and running tests."
        ),
        tools=None,  # all tools
        system_prompt=(
            "You are an implementation-focused worker agent. Execute the assigned task"
            " precisely and efficiently. Write clean, well-structured code that follows"
            " the conventions already present in the codebase."
        ),
        subagent_type="worker",
        source="builtin",
    ),
    AgentDefinition(
        name="verifier",
        description=(
            "Testing and verification focused agent. Use this to run tests, check correctness,"
            " validate outputs, and verify that implementations meet requirements."
        ),
        tools=["Read", "Glob", "Grep", "Bash"],
        system_prompt=(
            "You are a verification agent. Your job is to rigorously test and validate code."
            " Run the test suite, inspect outputs, check edge cases, and report any failures"
            " with clear reproduction steps. Be thorough and skeptical."
        ),
        subagent_type="verifier",
        source="builtin",
    ),
]


def get_builtin_agent_definitions() -> list[AgentDefinition]:
    """Return the built-in agent definitions."""
    return list(_BUILTIN_AGENTS)


# ---------------------------------------------------------------------------
# Markdown / YAML-frontmatter loader
# ---------------------------------------------------------------------------

def _parse_agent_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Parse YAML frontmatter from a markdown file.

    Returns a (frontmatter_dict, body) tuple.  Only simple ``key: value``
    lines are handled — no nested YAML — which is consistent with the skills
    loader pattern used elsewhere in OpenHarness.
    """
    frontmatter: dict[str, str] = {}
    body = content

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return frontmatter, body

    end_index: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = i
            break

    if end_index is None:
        return frontmatter, body

    for fm_line in lines[1:end_index]:
        if ":" in fm_line:
            key, _, value = fm_line.partition(":")
            frontmatter[key.strip()] = value.strip().strip("'\"")

    # Body is everything after the closing ---
    body = "\n".join(lines[end_index + 1 :]).strip()
    return frontmatter, body


def load_agents_dir(directory: Path) -> list[AgentDefinition]:
    """Load agent definitions from .md files in *directory*.

    Each file should contain YAML frontmatter with at least ``name`` and
    ``description`` fields.  The markdown body becomes the ``system_prompt``.
    Additional optional frontmatter fields:

    * ``tools`` — comma-separated tool whitelist
    * ``model`` — model override string
    * ``permissions`` — comma-separated extra permission rules
    * ``subagent_type`` — subagent type string
    """
    agents: list[AgentDefinition] = []

    if not directory.is_dir():
        return agents

    for path in sorted(directory.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
            frontmatter, body = _parse_agent_frontmatter(content)

            name = frontmatter.get("name", "").strip() or path.stem
            description = frontmatter.get("description", "").strip()
            if not description:
                description = f"Agent: {name}"

            # Parse optional tools list
            tools: list[str] | None = None
            raw_tools = frontmatter.get("tools", "").strip()
            if raw_tools:
                tools = [t.strip() for t in raw_tools.split(",") if t.strip()]

            # Parse optional permissions list
            permissions: list[str] = []
            raw_perms = frontmatter.get("permissions", "").strip()
            if raw_perms:
                permissions = [p.strip() for p in raw_perms.split(",") if p.strip()]

            agents.append(
                AgentDefinition(
                    name=name,
                    description=description,
                    system_prompt=body or None,
                    tools=tools,
                    model=frontmatter.get("model") or None,
                    permissions=permissions,
                    subagent_type=frontmatter.get("subagent_type", "general-purpose"),
                    source="user",
                )
            )
        except Exception:
            # Skip files that cannot be parsed
            continue

    return agents


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _get_user_agents_dir() -> Path:
    """Return the user agent definitions directory."""
    return get_config_dir() / "agents"


def get_all_agent_definitions() -> list[AgentDefinition]:
    """Return all agent definitions: built-in + user + plugin.

    Merge order (last writer wins for same name):
    1. Built-in agents
    2. User agents (~/.openharness/agents/)
    3. Plugin agents (loaded from active plugins)

    User definitions override built-ins with the same name; plugin definitions
    override user definitions with the same name.
    """
    agent_map: dict[str, AgentDefinition] = {}

    # 1. Built-ins (lowest priority)
    for agent in get_builtin_agent_definitions():
        agent_map[agent.name] = agent

    # 2. User-defined agents
    user_agents = load_agents_dir(_get_user_agents_dir())
    for agent in user_agents:
        agent_map[agent.name] = agent

    # 3. Plugin agents — loaded lazily to avoid import cycles
    try:
        from openharness.plugins.loader import load_plugins  # noqa: PLC0415
        from openharness.config.settings import load_settings  # noqa: PLC0415

        settings = load_settings()
        import os  # noqa: PLC0415

        cwd = os.getcwd()
        for plugin in load_plugins(settings, cwd):
            if not plugin.enabled:
                continue
            for agent_def in getattr(plugin, "agents", []):
                if isinstance(agent_def, AgentDefinition):
                    agent_map[agent_def.name] = agent_def
    except Exception:
        pass

    return list(agent_map.values())


def get_agent_definition(name: str) -> AgentDefinition | None:
    """Return the agent definition for *name*, or ``None`` if not found."""
    for agent in get_all_agent_definitions():
        if agent.name == name:
            return agent
    return None
