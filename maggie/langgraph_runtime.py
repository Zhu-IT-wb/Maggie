from __future__ import annotations

import json
from typing import Any, Callable, Literal, TypedDict

from .autonomy import AutonomousTeammateManager
from .background import BackgroundManager
from .compression import TOKEN_THRESHOLD, auto_compact, estimate_tokens, micro_compact
from .config import Settings
from .llm import ChatClient
from .memory import MemoryManager
from .session_store import SessionStore, normalize_messages
from .skills import SkillLoader
from .subagent import run_subagent
from .tasks import TaskManager
from .team import MessageBus, ProtocolRegistry
from .todo import TodoManager
from .tools import execute_tool, tools_with_autonomous_team_system

try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:
    InMemorySaver = None
    StateGraph = None
    START = None
    END = None
    LANGGRAPH_AVAILABLE = False


class AgentGraphState(TypedDict):
    messages: list[dict[str, Any]]
    final_text: str
    stop_reason: str
    manual_compact: bool
    manual_focus: str
    rounds_without_todo: int


def langgraph_is_available() -> bool:
    return LANGGRAPH_AVAILABLE


def _block_value(block: Any, key: str) -> Any:
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def render_text(content: list[object] | list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in content:
        text = _block_value(block, 'text') or ''
        if text:
            parts.append(str(text))
    return ''.join(parts).strip() or '(no text response)'


def emit_progress_text(content: list[dict[str, Any]]) -> None:
    text = render_text(content)
    if text and text != '(no text response)':
        print(text)


def summarize_tool_use(name: str, tool_input: dict[str, Any]) -> str | None:
    if name == 'TodoWrite':
        return 'Updating the current execution checklist.'
    if name == 'task':
        description = str(tool_input.get('description', '')).strip() or 'subtask'
        return f'Delegating subtask: {description}'
    if name == 'compact':
        return 'Compacting context while preserving the current state.'
    if name in {'write_file', 'read_file', 'edit_file'}:
        path = str(tool_input.get('path', '')).strip()
        action = {
            'write_file': 'Creating file',
            'read_file': 'Reading file',
            'edit_file': 'Editing file',
        }[name]
        return f'{action}: {path}' if path else action
    if name in {'shell', 'bash'}:
        command = str(tool_input.get('command', '')).strip().replace('\n', ' ')
        if len(command) > 80:
            command = command[:77] + '...'
        return f'Running command: {command}' if command else 'Running command.'
    if name == 'background_run':
        command = str(tool_input.get('command', '')).strip().replace('\n', ' ')
        if len(command) > 80:
            command = command[:77] + '...'
        return f'Starting background task: {command}' if command else 'Starting background task.'
    if name == 'load_skill':
        skill_name = str(tool_input.get('name', '')).strip()
        return f'Loading skill: {skill_name}' if skill_name else 'Loading skill.'
    if name in {'task_create', 'task_update', 'task_archive', 'task_delete', 'task_prune_completed', 'task_list', 'task_get', 'claim_task'}:
        return f'Running task-board operation: {name}'
    if name in {'spawn_teammate', 'list_teammates', 'send_message', 'read_inbox', 'broadcast'}:
        return f'Running team operation: {name}'
    if name in {'shutdown_request', 'shutdown_response', 'plan_approval', 'list_plan_requests', 'idle'}:
        return f'Running protocol action: {name}'
    return None


def emit_tool_progress(name: str, tool_input: dict[str, Any], log_info: Callable[[str], None]) -> None:
    summary = summarize_tool_use(name, tool_input)
    if summary:
        log_info(summary)


def format_background_notifications(notifications: list[dict[str, str]]) -> str:
    lines = [
        f"[bg:{item['task_id']}] {item['status']} | {item['command']} | {item['result']}"
        for item in notifications
    ]
    return '<background-results>\n' + '\n'.join(lines) + '\n</background-results>'


def format_team_inbox(messages: list[dict[str, Any]]) -> str:
    return '<team-inbox>\n' + json.dumps(messages, ensure_ascii=False, indent=2) + '\n</team-inbox>'


def build_agent_graph(
    *,
    settings: Settings,
    system: str,
    session_store: SessionStore,
    session_id: str,
    todo: TodoManager,
    task_manager: TaskManager,
    skill_loader: SkillLoader,
    background_manager: BackgroundManager,
    message_bus: MessageBus,
    teammate_manager: AutonomousTeammateManager,
    protocol_registry: ProtocolRegistry,
    subagent_system: str,
    log_info: Callable[[str], None],
):
    client = ChatClient(settings)

    def save_messages(messages: list[dict[str, Any]]) -> None:
        session_store.save_messages(session_id, messages)

    def prepare_node(state: AgentGraphState) -> dict[str, Any]:
        messages = list(state['messages'])
        micro_compact(messages)
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            log_info('Auto compact triggered.')
            messages[:] = auto_compact(
                messages,
                client,
                settings,
                session_store,
                session_id,
                focus='Preserve current state, open todos, persistent tasks, background jobs, team messages, protocol requests, autonomous work, loaded skills, and delegated work.',
            )
            save_messages(messages)
        return {'messages': messages}

    def model_node(state: AgentGraphState) -> dict[str, Any]:
        response = client.create_message(
            system=system,
            messages=state['messages'],
            tools=tools_with_autonomous_team_system(),
        )
        normalized_content = normalize_messages([{'role': 'assistant', 'content': response.content}])[0]['content']
        messages = state['messages'] + [{'role': 'assistant', 'content': normalized_content}]
        save_messages(messages)
        if response.stop_reason != 'tool_use':
            return {
                'messages': messages,
                'stop_reason': 'done',
                'final_text': render_text(normalized_content),
            }
        emit_progress_text(normalized_content)
        return {
            'messages': messages,
            'stop_reason': 'tool_use',
        }

    def tools_node(state: AgentGraphState) -> dict[str, Any]:
        assistant_content = state['messages'][-1]['content']
        results: list[dict[str, str]] = []
        used_todo = False
        manual_compact = False
        manual_focus = ''

        for block in assistant_content:
            if _block_value(block, 'type') != 'tool_use':
                continue

            name = str(_block_value(block, 'name') or '')
            tool_input = dict(_block_value(block, 'input') or {})
            emit_tool_progress(name, tool_input, log_info)

            if name == 'task':
                prompt = str(tool_input.get('prompt', '')).strip()
                output = run_subagent(settings, prompt, subagent_system)
            elif name == 'compact':
                manual_compact = True
                manual_focus = str(tool_input.get('focus', '')).strip()
                output = 'Compressing conversation context.'
            else:
                output = execute_tool(
                    name,
                    tool_input,
                    settings.workdir,
                    todo=todo,
                    skill_loader=skill_loader,
                    task_manager=task_manager,
                    background_manager=background_manager,
                    message_bus=message_bus,
                    teammate_manager=teammate_manager,
                    protocol_registry=protocol_registry,
                    current_agent_name='lead',
                )
                if name == 'TodoWrite':
                    used_todo = True

            results.append(
                {
                    'type': 'tool_result',
                    'tool_use_id': str(_block_value(block, 'id') or ''),
                    'content': str(output),
                }
            )

        rounds_without_todo = 0 if used_todo else state['rounds_without_todo'] + 1
        if todo.has_open_items() and rounds_without_todo >= 3:
            results.append({'type': 'text', 'text': '<reminder>Update your todos.</reminder>'})

        messages = state['messages'] + [{'role': 'user', 'content': results}]
        save_messages(messages)
        return {
            'messages': messages,
            'manual_compact': manual_compact,
            'manual_focus': manual_focus,
            'rounds_without_todo': rounds_without_todo,
        }

    def compact_node(state: AgentGraphState) -> dict[str, Any]:
        log_info('Manual compact triggered.')
        messages = list(state['messages'])
        messages[:] = auto_compact(
            messages,
            client,
            settings,
            session_store,
            session_id,
            focus=state['manual_focus'] or 'Preserve current state, todos, persistent tasks, background jobs, team messages, protocol requests, autonomous work, and unfinished work.',
        )
        save_messages(messages)
        return {
            'messages': messages,
            'stop_reason': 'done',
            'final_text': '(context compacted)',
            'manual_compact': False,
            'manual_focus': '',
        }

    def route_after_model(state: AgentGraphState) -> Literal['tools', 'done']:
        return 'tools' if state['stop_reason'] == 'tool_use' else 'done'

    def route_after_tools(state: AgentGraphState) -> Literal['compact', 'loop']:
        return 'compact' if state['manual_compact'] else 'loop'

    builder = StateGraph(AgentGraphState)
    builder.add_node('prepare', prepare_node)
    builder.add_node('model', model_node)
    builder.add_node('tools', tools_node)
    builder.add_node('compact', compact_node)
    builder.add_edge(START, 'prepare')
    builder.add_edge('prepare', 'model')
    builder.add_conditional_edges('model', route_after_model, {'tools': 'tools', 'done': END})
    builder.add_conditional_edges('tools', route_after_tools, {'compact': 'compact', 'loop': 'prepare'})
    builder.add_edge('compact', END)

    checkpointer = InMemorySaver() if InMemorySaver is not None else None
    return builder.compile(checkpointer=checkpointer)


def run_agent_turn(
    *,
    messages: list[dict[str, Any]],
    session_store: SessionStore,
    session_id: str,
    background_manager: BackgroundManager,
    message_bus: MessageBus,
    teammate_manager: AutonomousTeammateManager,
    protocol_registry: ProtocolRegistry,
    settings: Settings,
    system: str,
    subagent_system: str,
    log_info: Callable[[str], None],
    memory_manager: MemoryManager,
    current_query: str,
) -> str:
    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError('LangGraph is not installed.')

    working_messages = list(messages)
    notifications = background_manager.drain_notifications()
    if notifications:
        working_messages.append({'role': 'user', 'content': format_background_notifications(notifications)})
        session_store.save_messages(session_id, working_messages)

    inbox_messages = message_bus.read_inbox('lead')
    if inbox_messages:
        working_messages.append({'role': 'user', 'content': format_team_inbox(inbox_messages)})
        session_store.save_messages(session_id, working_messages)

    todo = TodoManager()
    task_manager = TaskManager(settings.workdir)
    skill_loader = SkillLoader(settings.workdir / 'skills')
    graph = build_agent_graph(
        settings=settings,
        system=system,
        session_store=session_store,
        session_id=session_id,
        todo=todo,
        task_manager=task_manager,
        skill_loader=skill_loader,
        background_manager=background_manager,
        message_bus=message_bus,
        teammate_manager=teammate_manager,
        protocol_registry=protocol_registry,
        subagent_system=subagent_system,
        log_info=log_info,
    )
    final_state = graph.invoke(
        {
            'messages': working_messages,
            'final_text': '',
            'stop_reason': '',
            'manual_compact': False,
            'manual_focus': '',
            'rounds_without_todo': 0,
        },
        config={'configurable': {'thread_id': session_id}},
    )
    messages[:] = final_state['messages']
    final_text = final_state['final_text'] or '(no text response)'
    memory_manager.update_after_turn(
        session_id=session_id,
        user_input=current_query,
        final_text=final_text,
        messages=messages,
        todo=todo,
    )
    return final_text
