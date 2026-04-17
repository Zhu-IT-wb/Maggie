from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, workdir: Path):
        # 所有会话状态统一存放在 .sessions 目录下，便于恢复和清理。
        self.workdir = workdir
        self.root = workdir / '.sessions'
        self.root.mkdir(exist_ok=True)
        self.index_path = self.root / 'index.json'

    def create_session(self) -> str:
        # 创建一个新会话目录，并把它登记为最新会话。
        session_id = str(uuid.uuid4())[:8]
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        state = {
            'session_id': session_id,
            'created_at': time.time(),
            'updated_at': time.time(),
            'messages': [],
            'transcripts': [],
        }
        self._write_json(self._state_path(session_id), state)
        self.set_latest_session(session_id)
        return session_id

    def set_latest_session(self, session_id: str) -> None:
        # 更新 index.json 中记录的最新会话 id。
        index = self._load_index()
        index['latest_session_id'] = session_id
        self._write_json(self.index_path, index)

    def load_latest_session(self) -> tuple[str, list[dict[str, Any]]] | None:
        # 读取最近一次会话的快照；如果没有会话则返回 None。
        index = self._load_index()
        latest = index.get('latest_session_id', '')
        if not latest:
            return None
        return latest, self.load_messages(latest)

    def load_previous_session(self, current_session_id: str) -> tuple[str, list[dict[str, Any]]] | None:
        # 恢复最近一个“不是当前会话”的历史会话，避免被刚创建的空会话覆盖。
        for session in self.list_sessions():
            if session['session_id'] == current_session_id:
                continue
            return session['session_id'], self.load_messages(session['session_id'])
        return None

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        # 从指定会话的 state.json 恢复可继续运行的消息历史。
        state = self._load_state(session_id)
        return state.get('messages', [])

    def load_session(self, session_id: str) -> tuple[str, list[dict[str, Any]]]:
        # 按指定 session_id 恢复会话。
        return session_id, self.load_messages(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        # 返回按更新时间倒序排列的会话列表，供 /sessions 和恢复逻辑使用。
        sessions: list[dict[str, Any]] = []
        for state_path in self.root.glob('*/state.json'):
            state = json.loads(state_path.read_text(encoding='utf-8'))
            sessions.append(
                {
                    'session_id': state.get('session_id', state_path.parent.name),
                    'updated_at': state.get('updated_at', 0),
                    'created_at': state.get('created_at', 0),
                    'message_count': len(state.get('messages', [])),
                    'transcript_count': len(state.get('transcripts', [])),
                }
            )
        sessions.sort(key=lambda item: item['updated_at'], reverse=True)
        return sessions

    def save_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        # 将当前消息历史归一化后落盘，作为后续恢复点。
        state = self._load_state(session_id)
        state['messages'] = normalize_messages(messages)
        state['updated_at'] = time.time()
        self._write_json(self._state_path(session_id), state)
        self.set_latest_session(session_id)

    def save_transcript(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        summary: str,
        focus: str,
    ) -> Path:
        # 保存一次压缩前的完整转录，并把索引写进会话状态里。
        session_dir = self._session_dir(session_id)
        transcript_dir = session_dir / 'transcripts'
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = transcript_dir / f"transcript_{int(time.time())}.jsonl"

        normalized = normalize_messages(messages)
        with transcript_path.open('w', encoding='utf-8') as handle:
            for message in normalized:
                handle.write(json.dumps(message, ensure_ascii=False) + '\n')

        state = self._load_state(session_id)
        state.setdefault('transcripts', []).append(
            {
                'path': str(transcript_path),
                'created_at': time.time(),
                'focus': focus,
                'summary_preview': summary[:200],
            }
        )
        state['updated_at'] = time.time()
        self._write_json(self._state_path(session_id), state)
        self.set_latest_session(session_id)
        return transcript_path

    def export_session(self, session_id: str) -> Path:
        # 导出当前会话为可读 Markdown，方便复盘或归档。
        state = self._load_state(session_id)
        export_dir = self._session_dir(session_id) / 'exports'
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"session_{session_id}.md"

        lines = [
            f"# Session {session_id}",
            '',
            f"Created At: {int(state.get('created_at', 0))}",
            f"Updated At: {int(state.get('updated_at', 0))}",
            '',
            '## Messages',
            '',
        ]
        for message in state.get('messages', []):
            role = message.get('role', 'unknown')
            lines.append(f"### {role}")
            lines.append('')
            lines.append(_content_to_markdown(message.get('content')))
            lines.append('')

        transcripts = state.get('transcripts', [])
        if transcripts:
            lines.append('## Transcripts')
            lines.append('')
            for transcript in transcripts:
                lines.append(f"- {transcript.get('path', '')}")

        export_path.write_text('\n'.join(lines), encoding='utf-8')
        return export_path

    def cleanup(self, keep_latest: int = 1) -> str:
        # 删除旧会话目录，默认只保留最近 N 个会话，避免 transcript 无限堆积。
        state_files = sorted(
            self.root.glob('*/state.json'),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for state_path in state_files[max(keep_latest, 0):]:
            shutil.rmtree(state_path.parent, ignore_errors=True)
            removed += 1

        if removed == 0:
            return 'No old sessions to clean.'
        return f'Removed {removed} old session(s).'

    def _session_dir(self, session_id: str) -> Path:
        return self.root / session_id

    def _state_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / 'state.json'

    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {'latest_session_id': ''}
        return json.loads(self.index_path.read_text(encoding='utf-8'))

    def _load_state(self, session_id: str) -> dict[str, Any]:
        state_path = self._state_path(session_id)
        if not state_path.exists():
            raise ValueError(f"Unknown session: {session_id}")
        return json.loads(state_path.read_text(encoding='utf-8'))

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 将消息中的自定义对象转换成普通 dict，保证可以可靠落盘并再次加载。
    normalized: list[dict[str, Any]] = []
    for message in messages:
        normalized.append(
            {
                'role': message.get('role', 'user'),
                'content': _normalize_content(message.get('content')),
            }
        )
    return normalized


def _normalize_content(content: Any) -> Any:
    if isinstance(content, list):
        return [_normalize_part(part) for part in content]
    return content


def _normalize_part(part: Any) -> Any:
    if isinstance(part, dict):
        return part
    block_type = getattr(part, 'type', None)
    if block_type == 'text':
        return {'type': 'text', 'text': getattr(part, 'text', '')}
    if block_type == 'tool_use':
        return {
            'type': 'tool_use',
            'id': getattr(part, 'id', ''),
            'name': getattr(part, 'name', ''),
            'input': getattr(part, 'input', {}),
        }
    return str(part)


def _content_to_markdown(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        lines: list[str] = []
        for part in content:
            if isinstance(part, dict):
                lines.append(json.dumps(part, ensure_ascii=False, indent=2))
            else:
                lines.append(str(part))
        return '\n'.join(lines)
    return str(content)