from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import chromadb

from .config import Settings
from .llm import ChatClient, EmbeddingClient
from .todo import TodoManager


VALID_MEMORY_TYPES = {
    'user_preference',
    'project_fact',
    'workflow_preference',
    'learned_lesson',
}
VALID_MEMORY_SCOPES = {'user', 'workspace', 'project'}
EPHEMERAL_PATTERNS = (
    'this turn',
    'current request',
    'for now',
    'temporarily',
    'temporary',
    'one-off',
)
TASK_VERB_PREFIXES = (
    'please ',
    'help ',
    'create ',
    'write ',
    'implement ',
    'check ',
    'fix ',
    'build ',
)
INSPECTION_LANGUAGE_PATTERNS = (
    '检查了',
    '读取了',
    '发现了',
    '本次',
    '此次',
    '已验证',
    'reviewed',
    'inspected',
    'checked',
    'read files',
    'looked at',
    'verified',
    'found that',
    'this review',
    'this inspection',
)
STABLE_PROJECT_FACT_PATTERNS = (
    '默认运行在',
    '运行在',
    'windows',
    'powershell',
    'pathlib',
    'chromadb',
    'sqlite',
    'json',
    'langgraph',
    'openai-compatible',
    'openai compatible',
    'api',
    '环境',
    '架构',
    '持久化',
    '向量数据库',
    'embedding',
    'session',
    'workspace',
    'repo',
    '项目默认',
    '默认使用',
    '优先使用',
    'constraints',
    'architecture',
    'environment',
    'persistent',
    'vector',
)
ARTIFACT_PATTERN = re.compile(r'[\w./\\:-]+\.(?:py|md|txt|json|yaml|yml|html|css|js|ts|tsx|jsx|ps1|bat|sh)')


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]:
        ...


@dataclass
class MemoryRecord:
    id: str
    type: str
    content: str
    summary: str
    scope: str
    source: str
    tags: list[str]
    created_at: float
    updated_at: float
    importance: int
    session_id: str
    dedupe_key: str = ''


@dataclass
class WorkingMemorySnapshot:
    session_id: str
    current_goal: str = ''
    latest_user_input: str = ''
    latest_assistant_summary: str = ''
    plan: list[str] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    next_step: str = ''
    open_questions: list[str] = field(default_factory=list)
    important_artifacts: list[str] = field(default_factory=list)
    recent_tool_results: list[str] = field(default_factory=list)
    active_risks: list[str] = field(default_factory=list)
    updated_at: float = 0.0


def _now() -> float:
    return time.time()


def _trim(text: str, limit: int = 280) -> str:
    value = str(text or '').strip().replace('\r', ' ').replace('\n', ' ')
    if len(value) <= limit:
        return value
    return value[: limit - 3] + '...'


def _normalize_text(text: str) -> str:
    lowered = str(text or '').strip().lower()
    lowered = re.sub(r'\s+', ' ', lowered)
    return lowered


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or '').strip()
    if not raw:
        return None
    if raw.startswith('```'):
        raw = raw.strip('`')
        if raw.startswith('json'):
            raw = raw[4:].strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start = raw.find('{')
        end = raw.rfind('}')
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def _tokenize_terms(text: str) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []
    tokens = re.findall(r'[\w./\\:-]+', normalized, flags=re.UNICODE)
    compact = ''.join(ch for ch in normalized if not ch.isspace())
    for index in range(max(len(compact) - 1, 0)):
        tokens.append(compact[index : index + 2])
    if len(compact) >= 3:
        for index in range(len(compact) - 2):
            tokens.append(compact[index : index + 3])
    return [token for token in tokens if token]


def _recent_tool_results(messages: list[dict[str, Any]], limit: int = 4) -> list[str]:
    results: list[str] = []
    for message in reversed(messages):
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'tool_result':
                continue
            result_text = _trim(str(part.get('content', '')), 220)
            if result_text:
                results.append(result_text)
            if len(results) >= limit:
                return results
    return results


def _extract_artifacts(*texts: str) -> list[str]:
    seen: set[str] = set()
    artifacts: list[str] = []
    for text in texts:
        for match in ARTIFACT_PATTERN.findall(str(text or '')):
            normalized = match.replace('\\', '/')
            if normalized in seen:
                continue
            seen.add(normalized)
            artifacts.append(normalized)
            if len(artifacts) >= 8:
                return artifacts
    return artifacts


def _extract_open_questions(text: str) -> list[str]:
    raw = str(text or '').strip()
    if '?' not in raw:
        return []
    parts = re.split(r'(?<=[?])\s+', raw)
    questions: list[str] = []
    for part in parts:
        part = _trim(part, 180)
        if '?' not in part or len(part) < 12:
            continue
        questions.append(part)
        if len(questions) >= 3:
            break
    return questions


def _extract_risks(final_text: str, recent_tool_results: list[str]) -> list[str]:
    candidates = [str(final_text or '')] + recent_tool_results
    risks: list[str] = []
    seen: set[str] = set()
    for text in candidates:
        lowered = text.lower()
        if not any(keyword in lowered for keyword in ('error', 'failed', 'exception', 'warning', 'timeout', 'missing')):
            continue
        risk = _trim(text, 220)
        if risk and risk not in seen:
            seen.add(risk)
            risks.append(risk)
        if len(risks) >= 4:
            break
    return risks


def _normalize_scope(scope: str) -> str:
    value = _normalize_text(scope)
    if value in {'user', 'workspace', 'project'}:
        return value
    if value in {'repo', 'repository'}:
        return 'project'
    return 'workspace'


def _looks_ephemeral(summary: str, content: str) -> bool:
    summary_norm = _normalize_text(summary)
    content_norm = _normalize_text(content)
    if not summary_norm or not content_norm:
        return True
    if any(summary_norm.startswith(prefix) for prefix in TASK_VERB_PREFIXES):
        return True
    if any(pattern in summary_norm or pattern in content_norm for pattern in EPHEMERAL_PATTERNS):
        return True
    if '```' in content or len(re.findall(r'[`{};]', content)) > 20:
        return True
    return False


def _contains_inspection_language(*texts: str) -> bool:
    combined = _normalize_text(' '.join(str(text or '') for text in texts))
    if not combined:
        return False
    return any(pattern in combined for pattern in INSPECTION_LANGUAGE_PATTERNS)


def _is_stable_project_fact(summary: str, content: str) -> bool:
    combined = _normalize_text(f'{summary} {content}')
    if not combined:
        return False
    if _contains_inspection_language(summary, content):
        return False
    if any(prefix in combined for prefix in ('我检查', '我读取', '我发现', 'i checked', 'i reviewed', 'i found')):
        return False
    if any(pattern in combined for pattern in STABLE_PROJECT_FACT_PATTERNS):
        return True
    return False


class LocalHashEmbedder:
    def __init__(self, dimension: int = 256):
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in _tokenize_terms(text) or ['<empty>']:
            digest = hashlib.blake2b(token.encode('utf-8'), digest_size=8).digest()
            value = int.from_bytes(digest, 'big', signed=False)
            index = value % self.dimension
            sign = 1.0 if ((value >> 8) & 1) == 0 else -1.0
            weight = 1.5 if len(token) > 3 else 1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(item * item for item in vector))
        if norm == 0:
            return vector
        return [item / norm for item in vector]


class OpenAICompatibleEmbedder:
    def __init__(self, settings: Settings):
        self.client = EmbeddingClient(settings)

    def embed(self, text: str) -> list[float]:
        return self.client.embed_texts([text])[0]


class HybridEmbedder:
    def __init__(self, primary: Embedder | None, fallback: Embedder, log_info: Callable[[str], None]):
        self.primary = primary
        self.fallback = fallback
        self.log_info = log_info
        self.primary_disabled = primary is None

    def embed(self, text: str) -> list[float]:
        if self.primary_disabled or self.primary is None:
            return self.fallback.embed(text)
        try:
            return self.primary.embed(text)
        except Exception as exc:
            self.primary_disabled = True
            self.log_info(f'Embedding API unavailable, falling back to local embeddings: {exc}')
            return self.fallback.embed(text)


class WorkingMemoryStore:
    def __init__(self, workspace: Path):
        self.root = workspace / '.memory' / 'working_memory'
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, session_id: str) -> WorkingMemorySnapshot:
        path = self.root / f'{session_id}.json'
        if not path.exists():
            return WorkingMemorySnapshot(session_id=session_id, updated_at=_now())
        payload = json.loads(path.read_text(encoding='utf-8'))
        return WorkingMemorySnapshot(**payload)

    def save(self, snapshot: WorkingMemorySnapshot) -> None:
        path = self.root / f'{snapshot.session_id}.json'
        path.write_text(_json_dumps(asdict(snapshot)), encoding='utf-8')

    def update(
        self,
        *,
        session_id: str,
        user_input: str,
        final_text: str,
        todo: TodoManager,
        recent_tool_results: list[str],
    ) -> WorkingMemorySnapshot:
        snapshot = self.load(session_id)
        plan: list[str] = []
        completed_steps: list[str] = []
        next_step = ''

        for item in todo.items:
            content = _trim(item.content, 160)
            if item.status == 'in_progress':
                next_step = content
                plan.insert(0, content)
            elif item.status == 'pending':
                plan.append(content)
            elif item.status == 'completed':
                completed_steps.append(content)

        if not next_step and plan:
            next_step = plan[0]

        snapshot.latest_user_input = _trim(user_input, 240)
        snapshot.current_goal = _trim(user_input, 240)
        snapshot.latest_assistant_summary = _trim(final_text, 320)
        snapshot.plan = plan[:6]
        snapshot.completed_steps = completed_steps[:6]
        snapshot.next_step = _trim(next_step, 160)
        snapshot.open_questions = _extract_open_questions(final_text)
        snapshot.important_artifacts = _extract_artifacts(user_input, final_text, *recent_tool_results)
        snapshot.recent_tool_results = recent_tool_results[:4]
        snapshot.active_risks = _extract_risks(final_text, recent_tool_results)
        snapshot.updated_at = _now()
        self.save(snapshot)
        return snapshot

    def render_for_prompt(self, snapshot: WorkingMemorySnapshot) -> str:
        sections: list[str] = []
        if snapshot.current_goal:
            sections.append(f'Current goal: {snapshot.current_goal}')
        if snapshot.plan:
            sections.append('Execution plan:\n' + '\n'.join(f'- {item}' for item in snapshot.plan[:4]))
        if snapshot.completed_steps:
            sections.append('Completed steps:\n' + '\n'.join(f'- {item}' for item in snapshot.completed_steps[:3]))
        if snapshot.next_step:
            sections.append(f'Next step: {snapshot.next_step}')
        if snapshot.important_artifacts:
            sections.append('Important artifacts:\n' + '\n'.join(f'- {item}' for item in snapshot.important_artifacts[:5]))
        if snapshot.open_questions:
            sections.append('Open questions:\n' + '\n'.join(f'- {item}' for item in snapshot.open_questions[:3]))
        if snapshot.recent_tool_results:
            sections.append('Recent tool results:\n' + '\n'.join(f'- {item}' for item in snapshot.recent_tool_results[:3]))
        if snapshot.active_risks:
            sections.append('Active risks:\n' + '\n'.join(f'- {item}' for item in snapshot.active_risks[:3]))
        if not sections:
            return ''
        return '<working-memory>\n' + '\n\n'.join(sections) + '\n</working-memory>'


class VectorMemoryStore:
    def __init__(self, workspace: Path, embedder: Embedder | None = None):
        self.workspace = workspace
        self.root = workspace / '.memory'
        self.root.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.root / 'catalog.json'
        self.client = chromadb.PersistentClient(path=str(self.root / 'chroma'))
        self.collection = self.client.get_or_create_collection(name='maggie_memories')
        self.embedder = embedder or LocalHashEmbedder()
        if not self.catalog_path.exists():
            self.catalog_path.write_text(_json_dumps({'records': {}, 'dedupe': {}}), encoding='utf-8')

    def _load_catalog(self) -> dict[str, Any]:
        return json.loads(self.catalog_path.read_text(encoding='utf-8'))

    def _save_catalog(self, catalog: dict[str, Any]) -> None:
        self.catalog_path.write_text(_json_dumps(catalog), encoding='utf-8')

    def _document(self, record: MemoryRecord) -> str:
        parts = [record.type, record.scope, record.summary, record.content, ' '.join(record.tags)]
        return '\n'.join(part for part in parts if part)

    def _metadata(self, record: MemoryRecord) -> dict[str, Any]:
        return {
            'type': record.type,
            'scope': record.scope,
            'source': record.source,
            'importance': int(record.importance),
            'session_id': record.session_id,
            'created_at': float(record.created_at),
            'updated_at': float(record.updated_at),
            'tags': json.dumps(record.tags, ensure_ascii=False),
        }

    def _record_from_catalog(self, payload: dict[str, Any]) -> MemoryRecord:
        return MemoryRecord(
            id=str(payload['id']),
            type=str(payload['type']),
            content=str(payload['content']),
            summary=str(payload['summary']),
            scope=str(payload['scope']),
            source=str(payload['source']),
            tags=list(payload.get('tags', [])),
            created_at=float(payload['created_at']),
            updated_at=float(payload['updated_at']),
            importance=int(payload['importance']),
            session_id=str(payload.get('session_id', '')),
            dedupe_key=str(payload.get('dedupe_key', '')),
        )

    def _dedupe_key(self, record: MemoryRecord) -> str:
        basis = f"{record.type}|{record.scope}|{_normalize_text(record.summary or record.content)}"
        return hashlib.sha1(basis.encode('utf-8')).hexdigest()

    def upsert(self, records: list[MemoryRecord]) -> list[MemoryRecord]:
        if not records:
            return []
        catalog = self._load_catalog()
        stored: list[MemoryRecord] = []
        for record in records:
            dedupe_key = record.dedupe_key or self._dedupe_key(record)
            existing_id = catalog['dedupe'].get(dedupe_key)
            if existing_id and existing_id in catalog['records']:
                existing = self._record_from_catalog(catalog['records'][existing_id])
                existing.summary = record.summary or existing.summary
                existing.content = record.content or existing.content
                existing.tags = sorted(set(existing.tags) | set(record.tags))
                existing.importance = max(existing.importance, record.importance)
                existing.updated_at = _now()
                record = existing
            else:
                record.id = record.id or str(uuid.uuid4())[:12]
                record.created_at = record.created_at or _now()
                record.updated_at = record.updated_at or record.created_at
                record.dedupe_key = dedupe_key
                catalog['dedupe'][dedupe_key] = record.id

            vector = self.embedder.embed(self._document(record))
            self.collection.upsert(
                ids=[record.id],
                embeddings=[vector],
                documents=[self._document(record)],
                metadatas=[self._metadata(record)],
            )
            catalog['records'][record.id] = asdict(record)
            stored.append(record)

        self._save_catalog(catalog)
        return stored

    def _score_result(self, *, query: str, record: MemoryRecord, distance: float | None, session_id: str | None) -> float:
        query_terms = set(_tokenize_terms(query))
        memory_terms = set(_tokenize_terms(' '.join([record.summary, record.content, ' '.join(record.tags)])))
        lexical_overlap = (len(query_terms & memory_terms) / max(len(query_terms), 1)) if query_terms else 0.0
        vector_score = 1.0 / (1.0 + max(distance or 0.0, 0.0))
        importance_score = min(max(record.importance, 1), 5) / 5.0
        age_days = max((_now() - record.updated_at) / 86400.0, 0.0)
        freshness_score = 1.0 / (1.0 + age_days / 30.0)
        scope_boost = 0.06 if record.scope == 'user' else 0.03 if record.scope in {'workspace', 'project'} else 0.0
        session_boost = 0.05 if session_id and record.session_id == session_id else 0.0
        exact_phrase_boost = 0.08 if _normalize_text(record.summary) in _normalize_text(query) else 0.0
        return (
            vector_score * 0.5
            + lexical_overlap * 0.25
            + importance_score * 0.12
            + freshness_score * 0.07
            + scope_boost
            + session_boost
            + exact_phrase_boost
        )

    def search(
        self,
        query: str,
        top_k: int = 4,
        *,
        session_id: str | None = None,
        allowed_types: set[str] | None = None,
    ) -> list[MemoryRecord]:
        if not query.strip():
            return []
        payload = self.collection.query(
            query_embeddings=[self.embedder.embed(query)],
            n_results=max(top_k * 3, 8),
            include=['distances', 'metadatas'],
        )
        ids = payload.get('ids', [[]])[0]
        distances = payload.get('distances', [[]])[0]
        catalog = self._load_catalog()
        scored: list[tuple[float, MemoryRecord]] = []
        seen_ids: set[str] = set()
        for index, memory_id in enumerate(ids):
            if memory_id in seen_ids:
                continue
            seen_ids.add(memory_id)
            record_payload = catalog['records'].get(memory_id)
            if not record_payload:
                continue
            record = self._record_from_catalog(record_payload)
            if allowed_types and record.type not in allowed_types:
                continue
            distance = float(distances[index]) if index < len(distances) else None
            score = self._score_result(query=query, record=record, distance=distance, session_id=session_id)
            if score < 0.18:
                continue
            if distance is not None and distance > 1.8 and score < 0.35:
                continue
            scored.append((score, record))

        scored.sort(key=lambda item: item[0], reverse=True)
        selected: list[MemoryRecord] = []
        per_type_counts: dict[str, int] = {}
        for _, record in scored:
            if per_type_counts.get(record.type, 0) >= 2:
                continue
            per_type_counts[record.type] = per_type_counts.get(record.type, 0) + 1
            selected.append(record)
            if len(selected) >= top_k:
                break
        return selected

    def list_recent(self, limit: int = 10) -> list[MemoryRecord]:
        catalog = self._load_catalog()
        items = [self._record_from_catalog(item) for item in catalog['records'].values()]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return items[:limit]


class MemoryExtractor:
    def __init__(self, settings: Settings):
        self.client = ChatClient(settings)

    def extract(
        self,
        *,
        session_id: str,
        user_input: str,
        assistant_text: str,
        recent_tool_results: list[str],
    ) -> list[MemoryRecord]:
        if not user_input.strip() and not assistant_text.strip():
            return []

        prompt = {
            'user_input': _trim(user_input, 1000),
            'assistant_response': _trim(assistant_text, 1000),
            'recent_tool_results': recent_tool_results[:3],
            'allowed_types': sorted(VALID_MEMORY_TYPES),
            'rules': [
                'Only extract durable facts or preferences likely useful in future turns.',
                'Do not store temporary tasks, transient plans, long code, logs, stack traces, or one-off outputs.',
                'Prefer normalized statements like "User prefers short progress updates."',
                'Project facts must reflect stable repo or environment constraints.',
                'Learned lessons should be reusable debugging or workflow guidance.',
            ],
        }
        response = self.client.create_message(
            system=(
                'You extract durable memories for a coding agent. '
                'Return strict JSON only in the format '
                '{"memories":[{"type":"","summary":"","content":"","scope":"","tags":[],"importance":1}]}. '
                'Allowed types: user_preference, project_fact, workflow_preference, learned_lesson. '
                'Allowed scopes: user, workspace, project. '
                'Summary must be short and declarative. Content must be concise and reusable. '
                'If nothing durable is present, return {"memories":[]}.'
            ),
            messages=[{'role': 'user', 'content': json.dumps(prompt, ensure_ascii=False)}],
        )
        parsed = _extract_json_object(_render_text(response.content)) or {'memories': []}
        memories = parsed.get('memories', [])
        if not isinstance(memories, list):
            return []

        extracted: list[MemoryRecord] = []
        for item in memories[:5]:
            if not isinstance(item, dict):
                continue
            memory_type = str(item.get('type', '')).strip()
            if memory_type not in VALID_MEMORY_TYPES:
                continue
            summary = _trim(str(item.get('summary', '')).strip(), 180)
            content = _trim(str(item.get('content', '')).strip(), 500)
            if not summary or not content:
                continue
            if _looks_ephemeral(summary, content):
                continue
            if _contains_inspection_language(summary, content):
                continue
            if memory_type == 'project_fact' and not _is_stable_project_fact(summary, content):
                continue
            scope = _normalize_scope(str(item.get('scope', 'workspace')).strip())
            tags = sorted({str(tag).strip() for tag in item.get('tags', []) if str(tag).strip()})[:6]
            try:
                importance = max(1, min(int(item.get('importance', 3)), 5))
            except (TypeError, ValueError):
                importance = 3
            extracted.append(
                MemoryRecord(
                    id='',
                    type=memory_type,
                    content=content,
                    summary=summary,
                    scope=scope,
                    source='turn_extraction',
                    tags=tags,
                    created_at=_now(),
                    updated_at=_now(),
                    importance=importance,
                    session_id=session_id,
                )
            )
        return extracted


class MemoryManager:
    def __init__(self, workspace: Path, settings: Settings, log_info: Callable[[str], None]):
        self.workspace = workspace
        self.settings = settings
        self.log_info = log_info
        self.working = WorkingMemoryStore(workspace)
        remote_embedder: Embedder | None = None
        if settings.embedding_model and settings.embedding_base_url and settings.embedding_api_key:
            remote_embedder = OpenAICompatibleEmbedder(settings)
        embedder = HybridEmbedder(remote_embedder, LocalHashEmbedder(), log_info)
        self.store = VectorMemoryStore(workspace, embedder=embedder)
        self.extractor = MemoryExtractor(settings)

    def build_prompt_memory(self, session_id: str, current_query: str) -> str:
        snapshot = self.working.load(session_id)
        retrieved = self.store.search(current_query, top_k=4, session_id=session_id)
        lines: list[str] = []
        working_block = self.working.render_for_prompt(snapshot)
        if working_block:
            lines.append(working_block)
        if retrieved:
            self.log_info(f'Retrieved {len(retrieved)} long-term memories.')
            lines.append('<long-term-memory>')
            for item in retrieved:
                lines.append(
                    f"- [{item.type} | scope={item.scope} | importance={item.importance}] {item.summary} :: {item.content}"
                )
            lines.append('</long-term-memory>')
        else:
            self.log_info('Retrieved 0 long-term memories.')
        return '\n\n'.join(part for part in lines if part.strip())

    def update_after_turn(
        self,
        *,
        session_id: str,
        user_input: str,
        final_text: str,
        messages: list[dict[str, Any]],
        todo: TodoManager,
    ) -> None:
        recent_tool_results = _recent_tool_results(messages)
        snapshot = self.working.update(
            session_id=session_id,
            user_input=user_input,
            final_text=final_text,
            todo=todo,
            recent_tool_results=recent_tool_results,
        )
        self.log_info(
            'Updated working memory.'
            + (f' Next step: {snapshot.next_step}' if snapshot.next_step else '')
        )
        try:
            extracted = self.extractor.extract(
                session_id=session_id,
                user_input=user_input,
                assistant_text=final_text,
                recent_tool_results=recent_tool_results,
            )
        except Exception as exc:
            self.log_info(f'Memory extraction skipped due to error: {exc}')
            return
        stored = self.store.upsert(extracted)
        if stored:
            self.log_info(f'Extracted {len(stored)} long-term memories.')
            for item in stored:
                self.log_info(f'  memory[{item.type}] {item.summary}')
        else:
            self.log_info('Extracted 0 long-term memories.')

    def list_recent_memories(self, limit: int = 10) -> list[MemoryRecord]:
        return self.store.list_recent(limit)

    def working_memory_snapshot(self, session_id: str) -> WorkingMemorySnapshot:
        return self.working.load(session_id)


def _render_text(content: list[object]) -> str:
    parts: list[str] = []
    for block in content:
        text = getattr(block, 'text', '')
        if text:
            parts.append(text)
    return ''.join(parts).strip()
