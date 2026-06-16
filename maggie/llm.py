from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from .config import Settings


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class ModelResponse:
    content: list[Any]
    stop_reason: str


class ChatClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def create_message(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        if self.settings.provider == "anthropic":
            return self._call_anthropic(system=system, messages=messages, tools=tools)
        return self._call_openai(system=system, messages=messages, tools=tools)

    def _call_anthropic(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "system": system,
            "messages": messages,
            "max_tokens": self.settings.max_tokens,
        }
        if tools:
            payload["tools"] = tools
        data = _post_json(
            url=f"{self.settings.anthropic_base_url}/v1/messages",
            headers={
                "content-type": "application/json",
                "x-api-key": self.settings.api_key,
                "anthropic-version": "2023-06-01",
            },
            payload=payload,
        )
        content: list[Any] = []
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                content.append(ToolUseBlock(id=block["id"], name=block["name"], input=block.get("input", {})))
            else:
                content.append(TextBlock(text=block.get("text", "")))
        return ModelResponse(content=content, stop_reason=data.get("stop_reason", "end_turn"))

    def _call_openai(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": _to_openai_messages(system=system, messages=messages),
            "max_tokens": self.settings.max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    },
                }
                for tool in tools
            ]
            payload["tool_choice"] = "auto"
        data = _post_json(
            url=f"{self.settings.openai_base_url}/chat/completions",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.settings.api_key}",
            },
            payload=payload,
        )
        choice = data["choices"][0]
        message = choice["message"]
        content: list[Any] = []
        text = _extract_openai_text(message.get("content"))
        if text:
            content.append(TextBlock(text=text))
        for tool_call in message.get("tool_calls") or []:
            arguments = tool_call.get("function", {}).get("arguments") or "{}"
            try:
                parsed_arguments = json.loads(arguments)
            except json.JSONDecodeError:
                parsed_arguments = {"raw_arguments": arguments}
            content.append(
                ToolUseBlock(
                    id=tool_call["id"],
                    name=tool_call["function"]["name"],
                    input=parsed_arguments,
                )
            )
        # Some OpenAI-compatible providers return tool_calls with a non-tool finish_reason.
        # Treat any response containing tool calls as a tool round to avoid persisting
        # dangling assistant tool calls without matching tool results.
        stop_reason = "tool_use" if message.get("tool_calls") else choice.get("finish_reason", "stop")
        return ModelResponse(content=content, stop_reason=stop_reason)


class EmbeddingClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        payload = {
            "model": self.settings.embedding_model,
            "input": texts,
            "encoding_format": "float",
        }
        if self.settings.embedding_dimensions > 0:
            payload["dimensions"] = self.settings.embedding_dimensions
        data = _post_json(
            url=f"{self.settings.embedding_base_url}/embeddings",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.settings.embedding_api_key}",
            },
            payload=payload,
        )
        vectors: list[list[float]] = []
        for item in sorted(data.get("data", []), key=lambda part: part.get("index", 0)):
            embedding = item.get("embedding")
            if isinstance(embedding, list):
                vectors.append([float(value) for value in embedding])
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding response size mismatch")
        return vectors


def _to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        converted.extend(_convert_message_to_openai(message))
    return converted


def _convert_message_to_openai(message: dict[str, Any]) -> list[dict[str, Any]]:
    role = message["role"]
    content = message.get("content")

    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        return [{"role": role, "content": json.dumps(content, ensure_ascii=False, default=str)}]

    if role == "assistant":
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content:
            block_type = _block_value(block, "type")
            if block_type == "tool_use":
                tool_calls.append(
                    {
                        "id": _block_value(block, "id"),
                        "type": "function",
                        "function": {
                            "name": _block_value(block, "name"),
                            "arguments": json.dumps(_block_value(block, "input") or {}, ensure_ascii=False),
                        },
                    }
                )
            else:
                text_parts.append(_block_value(block, "text") or "")
        assistant_message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        return [assistant_message]

    converted: list[dict[str, Any]] = []
    buffered_text: list[str] = []
    for block in content:
        block_type = _block_value(block, "type")
        if block_type == "tool_result":
            if buffered_text:
                converted.append({"role": role, "content": "".join(buffered_text)})
                buffered_text.clear()
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": _block_value(block, "tool_use_id"),
                    "content": str(_block_value(block, "content") or ""),
                }
            )
        elif block_type == "text":
            buffered_text.append(str(_block_value(block, "text") or ""))
        else:
            buffered_text.append(json.dumps(block, ensure_ascii=False, default=str))
    if buffered_text:
        converted.append({"role": role, "content": "".join(buffered_text)})
    return converted


def _extract_openai_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def _block_value(block: Any, key: str) -> Any:
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Network error calling {url}: {exc.reason}") from exc
