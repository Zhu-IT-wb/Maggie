#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maggie.config import load_settings
from maggie.llm import ChatClient
from maggie.prompts import build_system_prompt
from maggie.skills import SkillLoader
from maggie.subagent import run_subagent
from maggie.todo import TodoManager
from maggie.tools import execute_tool, tools_with_todo_task_and_skills


# s05 在 s04 的基础上再增加一层“按需加载技能”，避免把全部知识塞进 system prompt。
SYSTEM_SUFFIX = (
    "Use TodoWrite for multi-step work. "
    "Use task to delegate isolated exploration or execution subtasks. "
    "Use load_skill to load specialized knowledge only when you need it. "
    "Keep parent context focused by loading full skill bodies on demand instead of assuming them."
)

# 子 agent 仍专注隔离执行，技能加载暂时只发生在父 agent 侧。
SUBAGENT_SYSTEM_TEMPLATE = (
    "You are Maggie's subagent working at {workdir}. "
    "You have fresh context and share the same workspace. "
    "You are running in a Windows workspace, so prefer Windows-compatible commands such as dir, type, cd, where, python, and PowerShell cmdlets over Unix commands like ls, cat, pwd, or which. "
    "Complete the given task using tools when needed, then return a concise summary to the parent agent."
)


def render_text(content: list[object]) -> str:
    # 终端展示只保留自然语言文本，不暴露内部块结构。
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", "")
        if text:
            parts.append(text)
    return "".join(parts).strip() or "(no text response)"


def build_system_with_skills(workdir: Path, skill_loader: SkillLoader) -> str:
    # system prompt 只放技能名称和简介，完整正文通过 load_skill 工具按需注入。
    return (
        f"{build_system_prompt(workdir)} {SYSTEM_SUFFIX}\n\n"
        "Skills available:\n"
        f"{skill_loader.get_descriptions()}"
    )


def agent_loop(messages: list[dict[str, Any]]) -> str:
    # 父 agent 同时管理会话历史、待办状态、子任务委派和技能加载。
    settings = load_settings()
    if not settings.api_key:
        raise RuntimeError("Missing API key. Set LLM_API_KEY or provider-specific env vars in .env")

    client = ChatClient(settings)
    todo = TodoManager()
    skill_loader = SkillLoader(settings.workdir / "skills")
    system = build_system_with_skills(settings.workdir, skill_loader)
    subagent_system = SUBAGENT_SYSTEM_TEMPLATE.format(workdir=settings.workdir)
    rounds_without_todo = 0

    while True:
        # s05 的父 agent 工具集合已经扩展到：文件工具 + TodoWrite + task + load_skill。
        response = client.create_message(
            system=system,
            messages=messages,
            tools=tools_with_todo_task_and_skills(),
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return render_text(response.content)

        results: list[dict[str, str]] = []
        used_todo = False
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue

            if block.name == "task":
                # 子任务依然走独立上下文，避免探索过程把父上下文撑得太大。
                description = str(block.input.get("description", "subtask"))
                prompt = str(block.input.get("prompt", "")).strip()
                print(f"> task ({description}):")
                print(prompt[:200])
                output = run_subagent(settings, prompt, subagent_system)
            else:
                # load_skill / TodoWrite / 文件工具统一走本地工具分发。
                output = execute_tool(
                    block.name,
                    block.input,
                    settings.workdir,
                    todo=todo,
                    skill_loader=skill_loader,
                )
                print(f"> {block.name}:")
                print(str(output)[:200])
                if block.name == "TodoWrite":
                    used_todo = True

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                }
            )

        # 如果还有未完成待办但模型迟迟不更新，就补一个轻量提醒。
        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if todo.has_open_items() and rounds_without_todo >= 3:
            results.append({"type": "text", "text": "<reminder>Update your todos.</reminder>"})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history: list[dict[str, Any]] = []
    while True:
        try:
            query = input("\033[36mMaggie s05 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        reply = agent_loop(history)
        print(reply)
        print()