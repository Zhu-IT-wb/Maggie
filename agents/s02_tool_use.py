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
from maggie.tools import BASE_TOOLS, execute_tool


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
    system = build_system_prompt(settings.workdir)

    while True:
        response = client.create_message(
            system=system,
            messages=messages,
            tools=BASE_TOOLS,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return render_text(response.content)

        results: list[dict[str, str]] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            output = execute_tool(block.name, block.input, settings.workdir)
            print(f"> {block.name}:")
            print(str(output)[:200])
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                }
            )
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history: list[dict[str, Any]] = []
    while True:
        try:
            query = input("\033[36mMaggie s02 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        reply = agent_loop(history)
        print(reply)
        print()