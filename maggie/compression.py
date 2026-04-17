from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .llm import ChatClient
from .session_store import SessionStore, normalize_messages


TOKEN_THRESHOLD = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PRESERVE_RESULT_TOOLS = {"read_file", "load_skill"}


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    # 用极粗略的方法估算 token 数，足够用于触发压缩阈值。
    return len(json.dumps(normalize_messages(messages), ensure_ascii=False)) // 4


def micro_compact(messages: list[dict[str, Any]]) -> None:
    # 静默清理较早的大工具结果，优先保留最近几次结果和参考性内容。
    tool_results: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "tool_result":
                tool_results.append(part)

    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return

    tool_name_map = _build_tool_name_map(messages)
    for result in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        content = result.get("content")
        if not isinstance(content, str) or len(content) <= 100:
            continue
        tool_name = tool_name_map.get(result.get("tool_use_id", ""), "unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue
        result["content"] = f"[Previous: used {tool_name}]"


def auto_compact(
    messages: list[dict[str, Any]],
    client: ChatClient,
    settings: Settings,
    session_store: SessionStore,
    session_id: str,
    focus: str | None = None,
) -> list[dict[str, Any]]:
    # 保存压缩前的完整历史，并生成一份足以继续工作的连续性摘要。
    summary_prompt = (
        "Summarize this conversation for continuity. Include: "
        "1) what has been accomplished, 2) current state, 3) key decisions, "
        "4) unresolved work."
    )
    if focus:
        summary_prompt += f" Preserve this focus: {focus}."
    conversation_text = json.dumps(normalize_messages(messages), ensure_ascii=False)[-80000:]

    response = client.create_message(
        system="You compress coding-agent conversations while preserving execution continuity.",
        messages=[
            {
                "role": "user",
                "content": f"{summary_prompt}\n\n{conversation_text}",
            }
        ],
    )
    summary = _render_text(response.content) or "No summary generated."
    transcript_path = session_store.save_transcript(session_id, messages, summary, focus or "")

    compressed = [
        {
            "role": "user",
            "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}",
        }
    ]
    session_store.save_messages(session_id, compressed)
    return compressed


def _build_tool_name_map(messages: list[dict[str, Any]]) -> dict[str, str]:
    # 通过 assistant 侧的 tool_use 块反查 tool_result 对应的工具名称。
    mapping: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if _block_value(block, "type") == "tool_use":
                mapping[_block_value(block, "id") or ""] = _block_value(block, "name") or "unknown"
    return mapping


def _render_text(content: list[object]) -> str:
    # 从模型响应里提取纯文本摘要。
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", "")
        if text:
            parts.append(text)
    return "".join(parts).strip()


def _block_value(block: Any, key: str) -> Any:
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)