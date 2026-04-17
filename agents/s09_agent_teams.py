#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maggie.background import BackgroundManager
from maggie.compression import TOKEN_THRESHOLD, auto_compact, estimate_tokens, micro_compact
from maggie.config import load_settings
from maggie.llm import ChatClient
from maggie.prompts import build_system_prompt
from maggie.session_store import SessionStore
from maggie.skills import SkillLoader
from maggie.subagent import run_subagent
from maggie.tasks import TaskManager
from maggie.team import MessageBus, TeammateManager
from maggie.todo import TodoManager
from maggie.tools import execute_tool, tools_with_team_system


# s09 在 s08 基础上增加“持久化 teammate + inbox 通信”，让多个 agent 可以分工协作而不是只做一次性子任务。
SYSTEM_SUFFIX = (
    'Use TodoWrite for short-horizon execution planning inside the current conversation. '
    'Use task_create, task_update, task_list, and task_get for persistent tasks that must survive compression and session changes. '
    'Use task_archive, task_delete, and task_prune_completed to keep the persistent task board tidy instead of letting stale tasks pile up forever. '
    'Use background_run for long-running shell commands when you do not need to block on the result immediately. '
    'Use check_background to inspect running or completed background tasks, and pay attention to injected background notifications before your next step. '
    'Use spawn_teammate to create a persistent teammate with its own inbox and identity. '
    'Use send_message, read_inbox, and broadcast to coordinate work across the team. '
    'Use list_teammates to inspect team status before delegating more work. '
    'Use task to delegate isolated one-off subtasks when a persistent teammate is unnecessary. '
    'Use load_skill to load specialized knowledge only when you need it. '
    'Use compact when the conversation is getting too large or when you want a clean summary. '
    'Before starting substantial work, inspect the persistent task board and keep it current.'
)

# 一次性子 agent 仍然保留给隔离探索使用，但团队成员与它不同，会跨多轮保留身份与 inbox。
SUBAGENT_SYSTEM_TEMPLATE = (
    "You are Maggie's subagent working at {workdir}. "
    'You have fresh context and share the same workspace. '
    'You are running in a Windows workspace, so prefer Windows-compatible commands such as dir, type, cd, where, python, and PowerShell cmdlets over Unix commands like ls, cat, pwd, or which. '
    'Complete the given task using tools when needed, then return a concise summary to the parent agent.'
)


def render_text(content: list[object]) -> str:
    # 终端展示只保留自然语言文本，不直接暴露内部协议对象。
    parts: list[str] = []
    for block in content:
        text = getattr(block, 'text', '')
        if text:
            parts.append(text)
    return ''.join(parts).strip() or '(no text response)'


def build_system_with_skills(workdir: Path, skill_loader: SkillLoader) -> str:
    # system prompt 里只保留技能名称和简介，完整正文由 load_skill 按需加载。
    return (
        f"{build_system_prompt(workdir)} {SYSTEM_SUFFIX}\n\n"
        'Skills available:\n'
        f'{skill_loader.get_descriptions()}'
    )


def format_background_notifications(notifications: list[dict[str, str]]) -> str:
    # 把后台任务结果压成一段结构化文本，方便父 agent 在下一轮消费。
    lines = [
        f"[bg:{item['task_id']}] {item['status']} | {item['command']} | {item['result']}"
        for item in notifications
    ]
    return '<background-results>\n' + '\n'.join(lines) + '\n</background-results>'


def format_team_inbox(messages: list[dict[str, Any]]) -> str:
    # 把 lead inbox 中的新消息注入主对话，避免团队沟通脱离主上下文。
    return '<team-inbox>\n' + json.dumps(messages, ensure_ascii=False, indent=2) + '\n</team-inbox>'


def agent_loop(
    messages: list[dict[str, Any]],
    session_store: SessionStore,
    session_id: str,
    background_manager: BackgroundManager,
    message_bus: MessageBus,
    teammate_manager: TeammateManager,
) -> str:
    # 主 agent 统一管理会话、Todo、skills、一次性子 agent、后台任务和持久化团队成员。
    settings = load_settings()
    if not settings.api_key:
        raise RuntimeError('Missing API key. Set LLM_API_KEY or provider-specific env vars in .env')

    client = ChatClient(settings)
    todo = TodoManager()
    task_manager = TaskManager(settings.workdir)
    skill_loader = SkillLoader(settings.workdir / 'skills')
    system = build_system_with_skills(settings.workdir, skill_loader)
    subagent_system = SUBAGENT_SYSTEM_TEMPLATE.format(workdir=settings.workdir)
    rounds_without_todo = 0

    while True:
        # 每轮模型调用前先把后台完成通知注入上下文。
        notifications = background_manager.drain_notifications()
        if notifications:
            messages.append({'role': 'user', 'content': format_background_notifications(notifications)})
            session_store.save_messages(session_id, messages)

        # 再把 lead inbox 中的新团队消息注入上下文，确保主 agent 能看到队友反馈。
        inbox_messages = message_bus.read_inbox('lead')
        if inbox_messages:
            messages.append({'role': 'user', 'content': format_team_inbox(inbox_messages)})
            session_store.save_messages(session_id, messages)

        # 第一层压缩：静默清理较早的大工具结果，减缓上下文膨胀。
        micro_compact(messages)

        # 第二层压缩：上下文过大时，先落 transcript 再生成连续性摘要。
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            print('[auto compact triggered]')
            messages[:] = auto_compact(
                messages,
                client,
                settings,
                session_store,
                session_id,
                focus='Preserve current state, open todos, persistent tasks, background jobs, team messages, loaded skills, and delegated work.',
            )

        response = client.create_message(
            system=system,
            messages=messages,
            tools=tools_with_team_system(),
        )
        messages.append({'role': 'assistant', 'content': response.content})
        if response.stop_reason != 'tool_use':
            session_store.save_messages(session_id, messages)
            return render_text(response.content)

        results: list[dict[str, str]] = []
        used_todo = False
        manual_compact = False
        manual_focus = ''
        for block in response.content:
            if getattr(block, 'type', None) != 'tool_use':
                continue

            if block.name == 'task':
                # 一次性 task 仍然用于隔离探索；persistent teammate 用 spawn_teammate。
                description = str(block.input.get('description', 'subtask'))
                prompt = str(block.input.get('prompt', '')).strip()
                print(f'> task ({description}):')
                print(prompt[:200])
                output = run_subagent(settings, prompt, subagent_system)
            elif block.name == 'compact':
                # 手动 compact 由模型自己决定时机，但压缩前状态仍会先写回 session store。
                manual_compact = True
                manual_focus = str(block.input.get('focus', '')).strip()
                output = 'Compressing conversation context.'
                print('> compact:')
                print(output)
            else:
                # 其余工具统一走本地分发，包括团队成员管理和 inbox 通信。
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
                    current_agent_name='lead',
                )
                print(f'> {block.name}:')
                print(str(output)[:200])
                if block.name == 'TodoWrite':
                    used_todo = True

            results.append(
                {
                    'type': 'tool_result',
                    'tool_use_id': block.id,
                    'content': str(output),
                }
            )

        # Todo 仍然只负责短期执行提醒，不与团队通信或长期任务板混用。
        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if todo.has_open_items() and rounds_without_todo >= 3:
            results.append({'type': 'text', 'text': '<reminder>Update your todos.</reminder>'})
        messages.append({'role': 'user', 'content': results})
        session_store.save_messages(session_id, messages)

        if manual_compact:
            print('[manual compact]')
            messages[:] = auto_compact(
                messages,
                client,
                settings,
                session_store,
                session_id,
                focus=manual_focus or 'Preserve current state, todos, persistent tasks, background jobs, team messages, and unfinished work.',
            )
            session_store.save_messages(session_id, messages)
            return '(context compacted)'


def start_session(
    session_store: SessionStore,
    resume_mode: str | None,
    current_session_id: str | None = None,
    target_session_id: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    # 根据启动模式选择新建会话、恢复最近会话，或恢复指定会话。
    if target_session_id:
        session_id, messages = session_store.load_session(target_session_id)
        session_store.set_latest_session(session_id)
        print(f'[resumed session: {session_id}]')
        return session_id, messages

    if resume_mode == 'latest':
        restored = (
            session_store.load_previous_session(current_session_id)
            if current_session_id
            else session_store.load_latest_session()
        )
        if restored is not None:
            session_id, messages = restored
            session_store.set_latest_session(session_id)
            print(f'[resumed session: {session_id}]')
            return session_id, messages
        print('[no previous session found; starting new session]')

    session_id = session_store.create_session()
    print(f'[new session: {session_id}]')
    return session_id, []


def print_sessions(session_store: SessionStore) -> None:
    # 打印全部可恢复会话，方便用户手动挑选恢复目标。
    sessions = session_store.list_sessions()
    if not sessions:
        print('No sessions found.')
        return
    for session in sessions:
        print(
            f"{session['session_id']} | messages={session['message_count']} | "
            f"transcripts={session['transcript_count']} | updated_at={int(session['updated_at'])}"
        )


def parse_cleanup_keep(command: str) -> int:
    # 解析 /cleanup N；未提供 N 时默认保留最近 1 个会话。
    parts = command.split()
    if len(parts) < 2:
        return 1
    try:
        return max(int(parts[1]), 0)
    except ValueError:
        return 1


def parse_resume_target(command: str) -> str | None:
    # 解析 /resume <session_id>；latest 单独交给恢复最近会话的分支。
    parts = command.split()
    if len(parts) != 2:
        return None
    if parts[1].lower() == 'latest':
        return None
    return parts[1]


if __name__ == '__main__':
    settings = load_settings()
    session_store = SessionStore(Path.cwd())
    background_manager = BackgroundManager(Path.cwd())
    message_bus = MessageBus(Path.cwd() / '.team')
    teammate_manager = TeammateManager(settings, Path.cwd() / '.team', message_bus)
    resume_flag = 'latest' if len(sys.argv) >= 3 and sys.argv[1:3] == ['--resume', 'latest'] else None
    target_session_id = sys.argv[3] if len(sys.argv) >= 4 and sys.argv[1:3] == ['--resume', 'id'] else None
    session_id, history = start_session(session_store, resume_flag, target_session_id=target_session_id)

    while True:
        try:
            query = input('\033[36mMaggie s09 >> \033[0m')
        except (EOFError, KeyboardInterrupt):
            break
        stripped = query.strip()
        command = stripped.lower()
        if command in ('q', 'exit', ''):
            break
        if command == '/resume latest':
            session_id, history = start_session(session_store, 'latest', current_session_id=session_id)
            print()
            continue
        if command.startswith('/resume '):
            target = parse_resume_target(stripped)
            if target:
                session_id, history = start_session(session_store, None, target_session_id=target)
            else:
                print('Usage: /resume <session_id> or /resume latest')
            print()
            continue
        if command.startswith('/cleanup'):
            keep_latest = parse_cleanup_keep(command)
            print(session_store.cleanup(keep_latest=keep_latest))
            print()
            continue
        if command == '/session':
            print(f'Current session: {session_id}')
            print()
            continue
        if command == '/sessions':
            print_sessions(session_store)
            print()
            continue
        if command == '/session export':
            export_path = session_store.export_session(session_id)
            print(f'Exported current session to: {export_path}')
            print()
            continue
        if command == '/team':
            print(teammate_manager.list_all())
            print()
            continue
        if command == '/inbox':
            inbox = message_bus.read_inbox('lead')
            print(json.dumps(inbox, ensure_ascii=False, indent=2))
            print()
            continue

        history.append({'role': 'user', 'content': query})
        session_store.save_messages(session_id, history)
        reply = agent_loop(
            history,
            session_store,
            session_id,
            background_manager,
            message_bus,
            teammate_manager,
        )
        print(reply)
        print()