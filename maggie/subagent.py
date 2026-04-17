from __future__ import annotations

from typing import Any

from .config import Settings
from .llm import ChatClient
from .tools import BASE_TOOLS, execute_tool


def render_text(content: list[object]) -> str:
    # 从模型返回的内容块中提取最终文本，作为子 agent 的摘要输出。
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", "")
        if text:
            parts.append(text)
    return "".join(parts).strip() or "(no summary)"


def run_subagent(settings: Settings, prompt: str, system: str) -> str:
    # 子 agent 使用全新消息历史启动，只共享工作区和基础工具。
    client = ChatClient(settings)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    for _ in range(30):
        # 子 agent 不暴露 task / TodoWrite，避免递归委派或污染父级状态。
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
            # 将每个工具执行结果包装成 tool_result，继续反馈给子 agent。
            output = execute_tool(block.name, block.input, settings.workdir)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output)[:50000],
                }
            )
        messages.append({"role": "user", "content": results})

    # 用固定轮数限制子 agent 的执行预算，防止局部任务无限循环。
    return "(subagent stopped after 30 tool rounds)"