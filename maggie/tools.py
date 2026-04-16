from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .todo import TodoManager


DANGEROUS_PATTERNS = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]


BASE_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents from the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text inside a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
]

TODO_TOOL = {
    "name": "TodoWrite",
    "description": "Update the short task checklist for the current job.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                        "activeForm": {"type": "string"},
                    },
                    "required": ["content", "status", "activeForm"],
                },
            }
        },
        "required": ["items"],
    },
}

TASK_TOOL = {
    "name": "task",
    "description": "Spawn a subagent with fresh context for isolated exploration or execution.",
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["prompt"],
    },
}


def tools_with_todo() -> list[dict[str, Any]]:
    return [*BASE_TOOLS, TODO_TOOL]


def tools_with_task() -> list[dict[str, Any]]:
    return [*BASE_TOOLS, TASK_TOOL]


def tools_with_todo_and_task() -> list[dict[str, Any]]:
    return [*BASE_TOOLS, TODO_TOOL, TASK_TOOL]


def safe_path(workdir: Path, raw_path: str) -> Path:
    path = (workdir / raw_path).resolve()
    if not path.is_relative_to(workdir.resolve()):
        raise ValueError(f"Path escapes workspace: {raw_path}")
    return path


def run_bash(command: str, workdir: Path) -> str:
    if any(pattern in command for pattern in DANGEROUS_PATTERNS):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as exc:
        return f"Error: {exc}"
    output = (result.stdout + result.stderr).strip()
    return output[:50000] if output else "(no output)"


def run_read(path: str, workdir: Path, limit: int | None = None) -> str:
    try:
        text = safe_path(workdir, path).read_text(encoding="utf-8")
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as exc:
        return f"Error: {exc}"


def run_write(path: str, content: str, workdir: Path) -> str:
    try:
        target = safe_path(workdir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_edit(path: str, old_text: str, new_text: str, workdir: Path) -> str:
    try:
        target = safe_path(workdir, path)
        content = target.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: Text not found in {path}"
        target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


def execute_tool(name: str, tool_input: dict[str, Any], workdir: Path, todo: TodoManager | None = None) -> str:
    handlers = {
        "bash": lambda: run_bash(str(tool_input["command"]), workdir),
        "read_file": lambda: run_read(str(tool_input["path"]), workdir, tool_input.get("limit")),
        "write_file": lambda: run_write(str(tool_input["path"]), str(tool_input["content"]), workdir),
        "edit_file": lambda: run_edit(
            str(tool_input["path"]),
            str(tool_input["old_text"]),
            str(tool_input["new_text"]),
            workdir,
        ),
        "TodoWrite": lambda: todo.update(tool_input["items"]) if todo is not None else "Error: Todo manager unavailable",
    }
    handler = handlers.get(name)
    if handler is None:
        return f"Unknown tool: {name}"
    return handler()