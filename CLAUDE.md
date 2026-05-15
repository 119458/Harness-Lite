# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Harness-Lite is a lightweight LLM multi-agent orchestration framework that supports tool calling, conversation memory, and security interception.

## Development Commands

```bash
# Install package
cd /path/to/Harness-Lite
pip install -e .

# Run CLI
harness-lite -i                    # Interactive mode (auto-generates session)
harness-lite -i --session <id>    # Continue existing session
harness-lite "task"               # Single-turn with auto session

# Run tests
pytest tests/ -v
```

## Architecture

### Core Module Dependency Chain

```
Config → Memory → Registry → Security → Loop → CLI
                         ↓
                      Tools/Skills
```

### Key Components

| Module | Path | Responsibility |
|--------|------|----------------|
| **Config** | `src/harness_lite/config/` | Loads LLM settings from `.env` |
| **Memory** | `src/harness_lite/memory/` | JSON file-based session storage (`memory_store/{date}/{session-id}.json`) |
| **Registry** | `src/harness_lite/registry/` | Tool/Skill plugin registry with `register()`, `get()`, `list_all()` |
| **Tools** | `src/harness_lite/tools/` | Built-in tools: `calculator`, `current_time`, `search` |
| **Skills** | `src/harness_lite/skills/` | Extensible skill directory |
| **Security** | `src/harness_lite/security/` | Permission check, input validation, audit logging |
| **Loop** | `src/harness_lite/loop/engine.py` | Core LLM orchestration engine with tool call loop |
| **CLI** | `src/harness_lite/cli/app.py` | Typer-based command entry point |

### Tool/Skill Schema Format

All tools must use OpenAI function-calling format:

```python
{
    "type": "function",
    "function": {
        "name": "tool_name",
        "description": "...",
        "parameters": {...}
    }
}
```

### Session Management

- Sessions stored at `memory_store/{YYYY-MM-DD}/{session-id}.json`
- Each CLI invocation without `--session` creates a new `session-{timestamp}` ID
- Memory is loaded/saved automatically per session by `LoopEngine`

### LLM Integration

- Uses MiniMax API by default (configured in `.env`)
- API calls via `requests` to `{base_url}/chat/completions`
- Supports streaming responses via SSE

## Important Implementation Notes

- **Tool schemas**: Must use OpenAI function-calling format (nested under `"function"` key).
- **Stream callback**: Use `sys.stdout.write()` in callback, not `print()`.
- **Session recovery**: Specify exact session ID to continue existing session.
