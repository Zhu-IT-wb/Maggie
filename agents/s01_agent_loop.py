#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maggie.config import load_settings
from maggie.llm import ChatClient
from maggie.prompts import build_system_prompt


def render_text(content: list[object]) -> str:
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", "")
        if text:
            parts.append(text)
    return "".join(parts).strip() or "(no text response)"


def agent_loop(messages: list[dict[str, str]]) -> str:
    settings = load_settings()
    if not settings.api_key:
        raise RuntimeError("Missing API key. Set LLM_API_KEY or provider-specific env vars in .env")
    client = ChatClient(settings)
    response = client.create_message(
        system=build_system_prompt(settings.workdir),
        messages=messages,
    )
    print("大模型相应：",response)
    messages.append({"role": "assistant", "content": render_text(response.content)})
    return messages[-1]["content"]


if __name__ == "__main__":
    history: list[dict[str, str]] = []
    while True:
        try:
            query = input("\033[36mMaggie s01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        reply = agent_loop(history)
        print(reply)
        print()
