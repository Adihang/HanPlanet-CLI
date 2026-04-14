# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

# Project Instructions

- Use OpenHarness tools deliberately.
- Keep changes minimal and verify with tests when possible.

# Commands

```bash
# Install (development)
uv sync --extra dev
cd frontend/terminal && npm ci

# Run
uv run oh                        # Interactive TUI
uv run oh -p "prompt"            # Non-interactive

# Test
uv run pytest -q                 # All 114 tests
uv run pytest tests/test_engine/ # Single module
uv run pytest --cov=src/openharness

# Lint
uv run ruff check src tests scripts

# Frontend typecheck
cd frontend/terminal && npx tsc --noEmit
```

CLI entry points are `oh` and `ohmo`, mapped in `pyproject.toml` to `openharness.cli:app` and `ohmo.cli:app`.

# Architecture

**OpenHarness** is a Python agent harness — infrastructure wrapping an LLM with tool execution, permissions, hooks, and multi-agent coordination.

## Request Flow

```
User Input → CLI / React TUI
  → QueryEngine (engine/query_engine.py)
  → query loop (engine/query.py) — streaming tool-call cycle
  → PermissionChecker (permissions/checker.py)
  → Hooks: PreToolUse → execute → PostToolUse
  → Tool execution (44+ tools in tools/)
  → Results feed back into the loop
```

## Key Subsystems

| Subsystem | Path | Role |
|-----------|------|------|
| Engine | `src/openharness/engine/` | `QueryEngine` owns conversation history; `query.py` is the core streaming tool-call loop |
| Tools | `src/openharness/tools/` | 44+ tools (file I/O, shell, web, search, MCP, agents, tasks, scheduling). All inherit `BaseTool`, registered in `ToolRegistry` |
| Permissions | `src/openharness/permissions/` | Three modes: DEFAULT (ask), AUTO (allow), PLAN (block writes). Checks paths, denied commands, tool allow/block lists |
| Hooks | `src/openharness/hooks/` | PreToolUse/PostToolUse lifecycle events; supports hot-reload |
| Skills | `src/openharness/skills/` | On-demand `.md` knowledge injected into system prompt at runtime |
| MCP | `src/openharness/mcp/` | Model Context Protocol — wraps external MCP servers as native tools |
| Commands | `src/openharness/commands/registry.py` | 54+ slash commands (89 KB file) |
| Config | `src/openharness/config/settings.py` | 880-line Pydantic settings schema |
| UI | `src/openharness/ui/` | React+Ink TUI (primary) + Textual fallback; WebSocket protocol in `protocol.py` |
| Swarm | `src/openharness/swarm/` + `coordinator/` | Multi-agent spawning and team management |
| Auth | `src/openharness/auth/` | Multi-provider OAuth and credential flows |
| ohmo | `ohmo/` | Separate personal-agent CLI app with gateway and channel support |

## API Clients

Provider abstraction lives in `src/openharness/api/`. All clients implement `SupportsStreamingMessages`. Supported backends: Codex (Anthropic), OpenAI, Ollama, Kimi, GLM, MiniMax, GitHub Copilot, any OpenAI-compatible endpoint.

## Configuration

Runtime config is stored at `~/.openharness/settings.json`. Resolution order: CLI args → env vars → config file → defaults. Key env vars: `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`.

## Adding a Tool

1. Create a file in `src/openharness/tools/` inheriting `BaseTool`
2. Define a Pydantic input model
3. Implement `async def execute()`
4. Register in `src/openharness/tools/__init__.py`
