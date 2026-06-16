from __future__ import annotations

from pathlib import Path

from maggie.memory import (
    MemoryRecord,
    VectorMemoryStore,
    WorkingMemoryStore,
    _contains_inspection_language,
    _is_stable_project_fact,
)
from maggie.todo import TodoManager


def test_working_memory_store_persists_snapshot(tmp_path: Path) -> None:
    store = WorkingMemoryStore(tmp_path)
    todo = TodoManager()
    todo.update(
        [
            {'content': 'Inspect memory flow', 'status': 'in_progress', 'activeForm': 'Inspecting memory flow'},
            {'content': 'Add vector retrieval', 'status': 'pending', 'activeForm': 'Adding vector retrieval'},
            {'content': 'Create smoke test', 'status': 'completed', 'activeForm': 'Creating smoke test'},
        ]
    )

    snapshot = store.update(
        session_id='session-a',
        user_input='Please improve the memory system.',
        final_text='Implemented working memory persistence.',
        todo=todo,
        recent_tool_results=['Created maggie/memory.py', 'Updated agents/s11_autonomous_agents.py'],
    )

    loaded = store.load('session-a')
    assert loaded.session_id == 'session-a'
    assert loaded.current_goal == 'Please improve the memory system.'
    assert loaded.plan == ['Inspect memory flow', 'Add vector retrieval']
    assert loaded.next_step == 'Inspect memory flow'
    assert loaded.completed_steps == ['Create smoke test']
    assert loaded.recent_tool_results == snapshot.recent_tool_results
    assert 'maggie/memory.py' in loaded.important_artifacts


def test_vector_memory_store_upsert_and_search(tmp_path: Path) -> None:
    store = VectorMemoryStore(tmp_path)
    record = MemoryRecord(
        id='',
        type='project_fact',
        content='The project uses Windows-friendly commands and PowerShell by default.',
        summary='Project targets a Windows workspace.',
        scope='workspace',
        source='unit_test',
        tags=['windows', 'powershell'],
        created_at=0.0,
        updated_at=0.0,
        importance=4,
        session_id='session-a',
    )

    stored = store.upsert([record])
    assert len(stored) == 1
    assert stored[0].id

    results = store.search('Which shell environment should the agent prefer?', top_k=3)
    assert results
    assert any(item.summary == 'Project targets a Windows workspace.' for item in results)


def test_vector_memory_store_deduplicates_records(tmp_path: Path) -> None:
    store = VectorMemoryStore(tmp_path)
    first = MemoryRecord(
        id='',
        type='workflow_preference',
        content='The user prefers short progress updates before large edits.',
        summary='User wants short progress updates.',
        scope='user',
        source='unit_test',
        tags=['progress'],
        created_at=0.0,
        updated_at=0.0,
        importance=3,
        session_id='session-a',
    )
    second = MemoryRecord(
        id='',
        type='workflow_preference',
        content='The user prefers short progress updates before large edits and testing.',
        summary='User wants short progress updates.',
        scope='user',
        source='unit_test',
        tags=['progress', 'testing'],
        created_at=0.0,
        updated_at=0.0,
        importance=5,
        session_id='session-b',
    )

    store.upsert([first])
    store.upsert([second])

    recent = store.list_recent(limit=10)
    matching = [item for item in recent if item.summary == 'User wants short progress updates.']
    assert len(matching) == 1
    assert matching[0].importance == 5
    assert 'testing' in matching[0].tags


def test_vector_memory_store_reranks_relevant_results(tmp_path: Path) -> None:
    store = VectorMemoryStore(tmp_path)
    store.upsert(
        [
            MemoryRecord(
                id='',
                type='project_fact',
                content='The workspace runs on Windows and the agent should prefer PowerShell commands.',
                summary='Prefer PowerShell in this workspace.',
                scope='workspace',
                source='unit_test',
                tags=['windows', 'powershell'],
                created_at=0.0,
                updated_at=0.0,
                importance=5,
                session_id='session-a',
            ),
            MemoryRecord(
                id='',
                type='learned_lesson',
                content='Keep CSS files short and grouped by component.',
                summary='CSS should stay grouped by component.',
                scope='project',
                source='unit_test',
                tags=['css'],
                created_at=0.0,
                updated_at=0.0,
                importance=2,
                session_id='session-a',
            ),
        ]
    )

    results = store.search('Use Windows shell commands and PowerShell for this task.', top_k=2, session_id='session-a')
    assert results
    assert results[0].summary == 'Prefer PowerShell in this workspace.'


def test_inspection_language_is_rejected_by_local_filters() -> None:
    assert _contains_inspection_language('本次我检查了记忆系统代码，没有发现 Windows 兼容问题。')
    assert _contains_inspection_language('I reviewed the files and verified the behavior.')


def test_project_fact_requires_stable_environment_or_architecture_constraint() -> None:
    assert _is_stable_project_fact(
        '项目默认运行在 Windows 和 PowerShell 环境下。',
        'This project defaults to a Windows and PowerShell environment and should prefer Windows commands.',
    )
    assert not _is_stable_project_fact(
        'Windows兼容性已验证通过。',
        '本次检查了 memory.py 和 config.py，发现没有 Windows 不兼容问题。',
    )
