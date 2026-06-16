from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maggie.config import load_settings
from maggie.memory import MemoryManager, MemoryRecord, WorkingMemorySnapshot


mcp = FastMCP("maggie-memory")

_MEMORY_OUTPUT_FIELDS = (
    "id",
    "type",
    "summary",
    "content",
    "scope",
    "tags",
    "importance",
    "session_id",
    "updated_at",
)
_manager: MemoryManager | None = None


def _silent_log_info(_: str) -> None:
    """Keep stdio transport clean; stdout is reserved for MCP JSON-RPC."""


def _get_memory_manager() -> MemoryManager:
    global _manager
    if _manager is None:
        settings = load_settings()
        _manager = MemoryManager(settings.workdir, settings, _silent_log_info)
    return _manager


def _clamp_int(value: int, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _memory_record_to_dict(record: MemoryRecord) -> dict[str, Any]:
    payload = asdict(record)
    return {field: payload.get(field) for field in _MEMORY_OUTPUT_FIELDS}


def _working_memory_to_dict(snapshot: WorkingMemorySnapshot) -> dict[str, Any]:
    return asdict(snapshot)


def build_memory_search_result(
    manager: MemoryManager,
    query: str,
    top_k: int = 4,
    session_id: str = "",
) -> dict[str, Any]:
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return {"memories": []}

    normalized_session_id = str(session_id or "").strip()
    clamped_top_k = _clamp_int(top_k, default=4, minimum=1, maximum=20)
    records = manager.store.search(
        normalized_query,
        top_k=clamped_top_k,
        session_id=normalized_session_id or None,
    )
    return {"memories": [_memory_record_to_dict(record) for record in records]}


def build_memory_recent_result(manager: MemoryManager, limit: int = 10) -> dict[str, Any]:
    clamped_limit = _clamp_int(limit, default=10, minimum=1, maximum=50)
    records = manager.list_recent_memories(limit=clamped_limit)
    return {"memories": [_memory_record_to_dict(record) for record in records]}


def build_working_memory_get_result(manager: MemoryManager, session_id: str) -> dict[str, Any]:
    normalized_session_id = str(session_id or "").strip()
    snapshot = manager.working_memory_snapshot(normalized_session_id)
    return {"working_memory": _working_memory_to_dict(snapshot)}


@mcp.tool()
def memory_search(query: str, top_k: int = 4, session_id: str = "") -> dict[str, Any]:
    """Search Maggie long-term memories."""
    return build_memory_search_result(_get_memory_manager(), query, top_k, session_id)


@mcp.tool()
def memory_recent(limit: int = 10) -> dict[str, Any]:
    """List recently updated Maggie long-term memories."""
    return build_memory_recent_result(_get_memory_manager(), limit)


@mcp.tool()
def working_memory_get(session_id: str) -> dict[str, Any]:
    """Get the working memory snapshot for a Maggie session."""
    return build_working_memory_get_result(_get_memory_manager(), session_id)


if __name__ == "__main__":
    mcp.run(transport="stdio")
