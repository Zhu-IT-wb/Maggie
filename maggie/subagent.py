from __future__ import annotations

from typing import Any

from .config import Settings
from .llm import ChatClient
from .tools import BASE_TOOLS, execute_tool


def render_text(content: list[object]) -> str:
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", "")
        if text:
            parts.append(text)
    return "".join(parts).strip() or "(no summary)"


def run_subagent(settings: Settings, prompt: str, system: str) -> str:
    client = ChatClient(settings)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    for _ in range(30):
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
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output)[:50000],
                }
            )
        messages.append({"role": "user", "content": results})

    return "(subagent stopped after 30 tool rounds)"