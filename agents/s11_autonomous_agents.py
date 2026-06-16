#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maggie.autonomy import AutonomousTeammateManager
from maggie.background import BackgroundManager
from maggie.compression import TOKEN_THRESHOLD, auto_compact, estimate_tokens, micro_compact
from maggie.config import load_settings
from maggie.langgraph_runtime import langgraph_is_available, run_agent_turn
from maggie.llm import ChatClient
from maggie.memory import MemoryManager
from maggie.prompts import build_system_prompt
from maggie.session_store import SessionStore
from maggie.skills import SkillLoader
from maggie.subagent import run_subagent
from maggie.tasks import TaskManager
from maggie.team import MessageBus, ProtocolRegistry
from maggie.todo import TodoManager
from maggie.tools import execute_tool, tools_with_autonomous_team_system


SYSTEM_SUFFIX = (
    'Before substantial tool use, briefly tell the user in one or two sentences what you are about to do. '
    'Use TodoWrite for short-horizon execution planning inside the current conversation. '
    'Use task_create, task_update, task_list, and task_get for persistent tasks that must survive compression and session changes. '
    'Use task_archive, task_delete, and task_prune_completed to keep the persistent task board tidy instead of letting stale tasks pile up forever. '
    'Use background_run for long-running shell commands when you do not need to block on the result immediately. '
    'Use check_background to inspect running or completed background tasks, and pay attention to injected background notifications before your next step. '
    'Use spawn_teammate to create a persistent autonomous teammate with its own inbox and identity. '
    'Use send_message, read_inbox, and broadcast to coordinate work across the team. '
    'Use list_teammates to inspect team status before delegating more work. '
    'Use shutdown_request and shutdown_response to gracefully stop teammates through an explicit approval flow. '
    'Use plan_approval and list_plan_requests to review teammate plans before major work begins. '
    'Autonomous teammates may idle, poll for inbox messages, and auto-claim open tasks from the persistent task board. '
    'Use task to delegate isolated one-off subtasks when a persistent teammate is unnecessary. '
    'Use load_skill to load specialized knowledge only when you need it. '
    'Use compact when the conversation is getting too large or when you want a clean summary. '
    'Before starting substantial work, inspect the persistent task board and keep it current.'
)

SUBAGENT_SYSTEM_TEMPLATE = (
    "You are Maggie's subagent working at {workdir}. "
    'You have fresh context and share the same workspace. '
    'You are running in a Windows workspace, so prefer Windows-compatible commands such as dir, type, cd, where, python, and PowerShell cmdlets over Unix commands like ls, cat, pwd, or which. '
    'Complete the given task using tools when needed, then return a concise summary to the parent agent.'
)


def log_info(message: str) -> None:
    print(f'[info] {message}')


def render_text(content: list[object]) -> str:
    parts: list[str] = []
    for block in content:
        text = getattr(block, 'text', '')
        if text:
            parts.append(text)
    return ''.join(parts).strip() or '(no text response)'


def emit_progress_text(content: list[object]) -> None:
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


def emit_tool_progress(name: str, tool_input: dict[str, Any]) -> None:
    summary = summarize_tool_use(name, tool_input)
    if summary:
        log_info(summary)


def build_system_with_skills(workdir: Path, skill_loader: SkillLoader, memory_context: str = '') -> str:
    parts = [
        f"{build_system_prompt(workdir)} {SYSTEM_SUFFIX}",
        'Skills available:',
        skill_loader.get_descriptions(),
    ]
    if memory_context.strip():
        parts.extend(['Relevant memory for this turn:', memory_context])
    return '\n\n'.join(parts)


def format_background_notifications(notifications: list[dict[str, str]]) -> str:
    lines = [
        f"[bg:{item['task_id']}] {item['status']} | {item['command']} | {item['result']}"
        for item in notifications
    ]
    return '<background-results>\n' + '\n'.join(lines) + '\n</background-results>'


def format_team_inbox(messages: list[dict[str, Any]]) -> str:
    return '<team-inbox>\n' + json.dumps(messages, ensure_ascii=False, indent=2) + '\n</team-inbox>'


def latest_user_input(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            return content
    return ''


def agent_loop(
    messages: list[dict[str, Any]],
    session_store: SessionStore,
    session_id: str,
    background_manager: BackgroundManager,
    message_bus: MessageBus,
    teammate_manager: AutonomousTeammateManager,
    protocol_registry: ProtocolRegistry,
) -> str:
    settings = load_settings()
    memory_manager = MemoryManager(settings.workdir, settings, log_info)
    current_query = latest_user_input(messages)
    memory_context = memory_manager.build_prompt_memory(session_id, current_query)
    skill_loader = SkillLoader(settings.workdir / 'skills')
    system = build_system_with_skills(settings.workdir, skill_loader, memory_context)
    subagent_system = SUBAGENT_SYSTEM_TEMPLATE.format(workdir=settings.workdir)

    if langgraph_is_available():
        return run_agent_turn(
            messages=messages,
            session_store=session_store,
            session_id=session_id,
            background_manager=background_manager,
            message_bus=message_bus,
            teammate_manager=teammate_manager,
            protocol_registry=protocol_registry,
            settings=settings,
            system=system,
            subagent_system=subagent_system,
            log_info=log_info,
            memory_manager=memory_manager,
            current_query=current_query,
        )

    log_info('LangGraph is unavailable. Falling back to the native runtime loop.')
    if not settings.api_key:
        raise RuntimeError('Missing API key. Set LLM_API_KEY or provider-specific env vars in .env')

    client = ChatClient(settings)
    todo = TodoManager()
    task_manager = TaskManager(settings.workdir)
    rounds_without_todo = 0

    while True:
        notifications = background_manager.drain_notifications()
        if notifications:
            messages.append({'role': 'user', 'content': format_background_notifications(notifications)})
            session_store.save_messages(session_id, messages)

        inbox_messages = message_bus.read_inbox('lead')
        if inbox_messages:
            messages.append({'role': 'user', 'content': format_team_inbox(inbox_messages)})
            session_store.save_messages(session_id, messages)

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

        response = client.create_message(
            system=system,
            messages=messages,
            tools=tools_with_autonomous_team_system(),
        )
        messages.append({'role': 'assistant', 'content': response.content})
        if response.stop_reason != 'tool_use':
            session_store.save_messages(session_id, messages)
            final_text = render_text(response.content)
            memory_manager.update_after_turn(
                session_id=session_id,
                user_input=current_query,
                final_text=final_text,
                messages=messages,
                todo=todo,
            )
            return final_text

        emit_progress_text(response.content)

        results: list[dict[str, str]] = []
        used_todo = False
        manual_compact = False
        manual_focus = ''
        for block in response.content:
            if getattr(block, 'type', None) != 'tool_use':
                continue

            emit_tool_progress(block.name, block.input)

            if block.name == 'task':
                prompt = str(block.input.get('prompt', '')).strip()
                output = run_subagent(settings, prompt, subagent_system)
            elif block.name == 'compact':
                manual_compact = True
                manual_focus = str(block.input.get('focus', '')).strip()
                output = 'Compressing conversation context.'
            else:
                output = execute_tool(
                    block.name,
                    block.input,
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
                if block.name == 'TodoWrite':
                    used_todo = True

            results.append({'type': 'tool_result', 'tool_use_id': block.id, 'content': str(output)})

        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if todo.has_open_items() and rounds_without_todo >= 3:
            results.append({'type': 'text', 'text': '<reminder>Update your todos.</reminder>'})
        messages.append({'role': 'user', 'content': results})
        session_store.save_messages(session_id, messages)

        if manual_compact:
            log_info('Manual compact triggered.')
            messages[:] = auto_compact(
                messages,
                client,
                settings,
                session_store,
                session_id,
                focus=manual_focus or 'Preserve current state, todos, persistent tasks, background jobs, team messages, protocol requests, autonomous work, and unfinished work.',
            )
            session_store.save_messages(session_id, messages)
            return '(context compacted)'


def start_session(
    session_store: SessionStore,
    resume_mode: str | None,
    current_session_id: str | None = None,
    target_session_id: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    if target_session_id:
        session_id, messages = session_store.load_session(target_session_id)
        session_store.set_latest_session(session_id)
        log_info(f'Resumed session: {session_id}')
        return session_id, messages

    if resume_mode == 'latest':
        restored = session_store.load_previous_session(current_session_id) if current_session_id else session_store.load_latest_session()
        if restored is not None:
            session_id, messages = restored
            session_store.set_latest_session(session_id)
            log_info(f'Resumed session: {session_id}')
            return session_id, messages
        log_info('No previous session found. Starting a new session.')

    session_id = session_store.create_session()
    log_info(f'New session: {session_id}')
    return session_id, []


def print_sessions(session_store: SessionStore) -> None:
    sessions = session_store.list_sessions()
    if not sessions:
        print('No sessions found.')
        return
    for session in sessions:
        print(f"{session['session_id']} | messages={session['message_count']} | transcripts={session['transcript_count']} | updated_at={int(session['updated_at'])}")


def parse_cleanup_keep(command: str) -> int:
    parts = command.split()
    if len(parts) < 2:
        return 1
    try:
        return max(int(parts[1]), 0)
    except ValueError:
        return 1


def parse_resume_target(command: str) -> str | None:
    parts = command.split()
    if len(parts) != 2:
        return None
    if parts[1].lower() == 'latest':
        return None
    return parts[1]


def main() -> None:
    settings = load_settings()
    session_store = SessionStore(Path.cwd())
    background_manager = BackgroundManager(Path.cwd())
    protocol_registry = ProtocolRegistry()
    message_bus = MessageBus(Path.cwd() / '.team')
    teammate_manager = AutonomousTeammateManager(settings, Path.cwd() / '.team', message_bus, protocol_registry)
    memory_manager = MemoryManager(settings.workdir, settings, log_info)
    session_id, history = start_session(
        session_store,
        'latest' if len(sys.argv) >= 3 and sys.argv[1:3] == ['--resume', 'latest'] else None,
        target_session_id=sys.argv[3] if len(sys.argv) >= 4 and sys.argv[1:3] == ['--resume', 'id'] else None,
    )

    while True:
        try:
            query = input('\033[36mMaggie >> \033[0m')
        except (EOFError, KeyboardInterrupt):
            break
        stripped = query.strip()
        command = stripped.lower()
        if command in ('q', 'exit', ''):
            break
        if command == '/resume latest':
            session_id, history = start_session(session_store, 'latest', current_session_id=session_id)
            continue
        if command.startswith('/resume '):
            target = parse_resume_target(stripped)
            if target:
                session_id, history = start_session(session_store, None, target_session_id=target)
            else:
                print('Usage: /resume <session_id> or /resume latest')
            continue
        if command.startswith('/cleanup'):
            keep_latest = parse_cleanup_keep(command)
            print(session_store.cleanup(keep_latest=keep_latest))
            continue
        if command == '/session':
            print(f'Current session: {session_id}')
            continue
        if command == '/sessions':
            print_sessions(session_store)
            continue
        if command == '/session export':
            export_path = session_store.export_session(session_id)
            print(f'Exported current session to: {export_path}')
            continue
        if command == '/team':
            print(teammate_manager.list_all())
            continue
        if command == '/inbox':
            inbox = message_bus.read_inbox('lead')
            print(json.dumps(inbox, ensure_ascii=False, indent=2))
            continue
        if command == '/memory':
            memories = memory_manager.list_recent_memories(limit=10)
            if not memories:
                print('No long-term memories.')
                continue
            for item in memories:
                print(f"[{item.type}] {item.summary} | importance={item.importance} | scope={item.scope}")
            continue
        if command == '/working-memory':
            snapshot = memory_manager.working_memory_snapshot(session_id)
            print(json.dumps(snapshot.__dict__, ensure_ascii=False, indent=2))
            continue

        history.append({'role': 'user', 'content': query})
        session_store.save_messages(session_id, history)
        reply = agent_loop(history, session_store, session_id, background_manager, message_bus, teammate_manager, protocol_registry)
        print(reply)


if __name__ == '__main__':
    main()
