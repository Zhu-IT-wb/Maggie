#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maggie.compression import TOKEN_THRESHOLD, auto_compact, estimate_tokens, micro_compact
from maggie.config import load_settings
from maggie.llm import ChatClient
from maggie.prompts import build_system_prompt
from maggie.session_store import SessionStore
from maggie.skills import SkillLoader
from maggie.subagent import run_subagent
from maggie.todo import TodoManager
from maggie.tools import execute_tool, tools_with_everything


# s06 的重点是“可持续对话”：在不丢关键状态的前提下压缩上下文，并把会话做成可恢复资产。
SYSTEM_SUFFIX = (
    "Use TodoWrite for multi-step work. "
    "Use task to delegate isolated exploration or execution subtasks. "
    "Use load_skill to load specialized knowledge only when you need it. "
    "Use compact when the conversation is getting too large or when you want a clean summary."
)

# 子 agent 继续保持简洁职责：只做隔离执行，不参与父级压缩决策。
SUBAGENT_SYSTEM_TEMPLATE = (
    "You are Maggie's subagent working at {workdir}. "
    "You have fresh context and share the same workspace. "
    "You are running in a Windows workspace, so prefer Windows-compatible commands such as dir, type, cd, where, python, and PowerShell cmdlets over Unix commands like ls, cat, pwd, or which. "
    "Complete the given task using tools when needed, then return a concise summary to the parent agent."
)


def render_text(content: list[object]) -> str:
    # 终端展示只保留自然语言文本，不直接暴露内部协议结构。
    parts: list[str] = []
    for block in content:
        text = getattr(block, 'text', '')
        if text:
            parts.append(text)
    return ''.join(parts).strip() or '(no text response)'


def build_system_with_skills(workdir: Path, skill_loader: SkillLoader) -> str:
    # system prompt 里只保留技能名称和简介，完整技能正文由 load_skill 按需注入。
    return (
        f"{build_system_prompt(workdir)} {SYSTEM_SUFFIX}\n\n"
        "Skills available:\n"
        f"{skill_loader.get_descriptions()}"
    )


def agent_loop(
    messages: list[dict[str, Any]],
    session_store: SessionStore,
    session_id: str,
) -> str:
    # 父 agent 负责会话历史、待办、技能、子任务，以及压缩策略本身。
    settings = load_settings()
    if not settings.api_key:
        raise RuntimeError("Missing API key. Set LLM_API_KEY or provider-specific env vars in .env")

    client = ChatClient(settings)
    todo = TodoManager()
    skill_loader = SkillLoader(settings.workdir / 'skills')
    system = build_system_with_skills(settings.workdir, skill_loader)
    subagent_system = SUBAGENT_SYSTEM_TEMPLATE.format(workdir=settings.workdir)
    rounds_without_todo = 0

    while True:
        # 第一层压缩：静默清理较早的大工具结果，减少上下文膨胀速度。
        micro_compact(messages)

        # 第二层压缩：一旦上下文估算超过阈值，就自动生成连续性摘要替换历史。
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            print('[auto compact triggered]')
            messages[:] = auto_compact(
                messages,
                client,
                settings,
                session_store,
                session_id,
                focus='Preserve current state, open todos, loaded skills, and delegated work.',
            )

        response = client.create_message(
            system=system,
            messages=messages,
            tools=tools_with_everything(),
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
                # 子任务仍然交给 fresh-context 子 agent，避免父上下文被探索细节塞满。
                description = str(block.input.get('description', 'subtask'))
                prompt = str(block.input.get('prompt', '')).strip()
                print(f"> task ({description}):")
                print(prompt[:200])
                output = run_subagent(settings, prompt, subagent_system)
            elif block.name == 'compact':
                # 第三层压缩：模型可以在认为时机合适时手动触发上下文压缩。
                manual_compact = True
                manual_focus = str(block.input.get('focus', '')).strip()
                output = 'Compressing conversation context.'
                print('> compact:')
                print(output)
            else:
                # 其余工具统一走本地分发，包括 load_skill 和 TodoWrite。
                output = execute_tool(
                    block.name,
                    block.input,
                    settings.workdir,
                    todo=todo,
                    skill_loader=skill_loader,
                )
                print(f"> {block.name}:")
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

        # 继续沿用待办提醒，避免压缩之后模型忘了同步短期计划。
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
                focus=manual_focus or 'Preserve current state, todos, and unfinished work.',
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
        print(f"[resumed session: {session_id}]")
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
            print(f"[resumed session: {session_id}]")
            return session_id, messages
        print('[no previous session found; starting new session]')

    session_id = session_store.create_session()
    print(f"[new session: {session_id}]")
    return session_id, []


def print_sessions(session_store: SessionStore) -> None:
    # 打印所有可恢复会话，方便手动检查恢复目标。
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
    # 解析 /cleanup N，未提供 N 时默认保留最近 1 个会话。
    parts = command.split()
    if len(parts) < 2:
        return 1
    try:
        return max(int(parts[1]), 0)
    except ValueError:
        return 1


def parse_resume_target(command: str) -> str | None:
    # 解析 /resume <session_id>，latest 交给单独分支处理。
    parts = command.split()
    if len(parts) != 2:
        return None
    if parts[1].lower() == 'latest':
        return None
    return parts[1]


if __name__ == '__main__':
    session_store = SessionStore(Path.cwd())
    resume_flag = 'latest' if len(sys.argv) >= 3 and sys.argv[1:3] == ['--resume', 'latest'] else None
    target_session_id = sys.argv[3] if len(sys.argv) >= 4 and sys.argv[1:3] == ['--resume', 'id'] else None
    session_id, history = start_session(session_store, resume_flag, target_session_id=target_session_id)

    while True:
        try:
            query = input('\033[36mMaggie s06 >> \033[0m')
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

        history.append({'role': 'user', 'content': query})
        session_store.save_messages(session_id, history)
        reply = agent_loop(history, session_store, session_id)
        print(reply)
        print()