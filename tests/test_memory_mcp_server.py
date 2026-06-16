from __future__ import annotations

from pathlib import Path

from maggie.memory import MemoryRecord, VectorMemoryStore, WorkingMemoryStore
from maggie.todo import TodoManager
from mcp_servers.memory_server import (
    _clamp_int,
    build_memory_recent_result,
    build_memory_search_result,
    build_working_memory_get_result,
)


class FakeMemoryManager:
    def __init__(self, workspace: Path):
        self.store = VectorMemoryStore(workspace)
        self.working = WorkingMemoryStore(workspace)

    def list_recent_memories(self, limit: int = 10) -> list[MemoryRecord]:
        return self.store.list_recent(limit)

    def working_memory_snapshot(self, session_id: str):
        return self.working.load(session_id)


def _record(
    *,
    summary: str,
    content: str,
    memory_type: str = "project_fact",
    importance: int = 4,
    session_id: str = "session-a",
    tags: list[str] | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        id="",
        type=memory_type,
        content=content,
        summary=summary,
        scope="workspace",
        source="unit_test",
        tags=tags or [],
        created_at=0.0,
        updated_at=0.0,
        importance=importance,
        session_id=session_id,
    )


def test_memory_search_returns_structured_long_term_memories(tmp_path: Path) -> None:
    manager = FakeMemoryManager(tmp_path)
    manager.store.upsert(
        [
            _record(
                summary="Prefer PowerShell in this workspace.",
                content="The project defaults to Windows and PowerShell commands.",
                tags=["windows", "powershell"],
            )
        ]
    )

    result = build_memory_search_result(
        manager,
        "Which shell should this project use on Windows?",
        top_k=3,
        session_id=" session-a ",
    )

    assert result["memories"]
    memory = result["memories"][0]
    assert set(memory) == {
        "id",
        "type",
        "summary",
        "content",
        "scope",
        "tags",
        "importance",
        "session_id",
        "updated_at",
    }
    assert memory["summary"] == "Prefer PowerShell in this workspace."
    assert memory["tags"] == ["windows", "powershell"]


def test_memory_search_empty_query_returns_empty_memories(tmp_path: Path) -> None:
    manager = FakeMemoryManager(tmp_path)
    manager.store.upsert(
        [
            _record(
                summary="Prefer PowerShell in this workspace.",
                content="The project defaults to Windows and PowerShell commands.",
            )
        ]
    )

    assert build_memory_search_result(manager, "   ", top_k=3) == {"memories": []}


def test_memory_recent_returns_recent_memories_and_clamps_limit(tmp_path: Path) -> None:
    manager = FakeMemoryManager(tmp_path)
    manager.store.upsert(
        [
            _record(summary="First memory.", content="A stable first project fact.", importance=2),
            _record(summary="Second memory.", content="A stable second project fact.", importance=5),
        ]
    )

    result = build_memory_recent_result(manager, limit=-10)

    assert len(result["memories"]) == 1
    assert result["memories"][0]["summary"] in {"First memory.", "Second memory."}
    assert _clamp_int(999, default=10, minimum=1, maximum=50) == 50
    assert _clamp_int("bad", default=10, minimum=1, maximum=50) == 10


def test_working_memory_get_returns_session_snapshot(tmp_path: Path) -> None:
    manager = FakeMemoryManager(tmp_path)
    todo = TodoManager()
    todo.update(
        [
            {
                "content": "Create Memory MCP server",
                "status": "in_progress",
                "activeForm": "Creating Memory MCP server",
            }
        ]
    )
    manager.working.update(
        session_id="session-a",
        user_input="Expose memory as MCP tools.",
        final_text="Implemented read-only Memory MCP server.",
        todo=todo,
        recent_tool_results=["Created mcp_servers/memory_server.py"],
    )

    result = build_working_memory_get_result(manager, " session-a ")

    snapshot = result["working_memory"]
    assert snapshot["session_id"] == "session-a"
    assert snapshot["current_goal"] == "Expose memory as MCP tools."
    assert snapshot["plan"] == ["Create Memory MCP server"]
