#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maggie.config import load_settings
from maggie.llm import ChatClient
from maggie.prompts import build_system_prompt
from maggie.subagent import run_subagent
from maggie.todo import TodoManager
from maggie.tools import execute_tool, tools_with_todo_and_task


SYSTEM_SUFFIX = (
    "Use TodoWrite for multi-step work. "
    "Use task to delegate isolated exploration or implementation subtasks. "
    "Keep parent context focused; use the subagent when a subtask can be completed independently."
)

SUBAGENT_SYSTEM_TEMPLATE = (
    "You are Maggie's subagent working at {workdir}. "
    "You have fresh context and share the same workspace. "
    "Complete the given task using tools when needed, then return a concise summary to the parent agent."
)


def render_text(content: list[object]) -> str:
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", "")
        if text:
            parts.append(text)
    return "".join(parts).strip() or "(no text response)"


def agent_loop(messages: list[dict[str, Any]]) -> str:
    settings = load_settings()
    if not settings.api_key:
        raise RuntimeError("Missing API key. Set LLM_API_KEY or provider-specific env vars in .env")

    client = ChatClient(settings)
    todo = TodoManager()
    system = f"{build_system_prompt(settings.workdir)} {SYSTEM_SUFFIX}"
    subagent_system = SUBAGENT_SYSTEM_TEMPLATE.format(workdir=settings.workdir)
    rounds_without_todo = 0

    while True:
        response = client.create_message(
            system=system,
            messages=messages,
            tools=tools_with_todo_and_task(),
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return render_text(response.content)

        results: list[dict[str, str]] = []
        used_todo = False
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue

            if block.name == "task":
                description = str(block.input.get("description", "subtask"))
                prompt = str(block.input.get("prompt", "")).strip()
                print(f"> task ({description}):")
                print(prompt[:200])
                output = run_subagent(settings, prompt, subagent_system)
            else:
                output = execute_tool(block.name, block.input, settings.workdir, todo=todo)
                print(f"> {block.name}:")
                print(str(output)[:200])
                if block.name == "TodoWrite":
                    used_todo = True

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                }
            )

        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if todo.has_open_items() and rounds_without_todo >= 3:
            results.append({"type": "text", "text": "<reminder>Update your todos.</reminder>"})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history: list[dict[str, Any]] = []
    while True:
        try:
            query = input("\033[36mMaggie s04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        reply = agent_loop(history)
        print(reply)
        print()