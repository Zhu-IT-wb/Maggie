from __future__ import annotations

from pathlib import Path


def build_system_prompt(workdir: Path) -> str:
    return (
        f"You are Maggie, a coding agent working at {workdir}. "
        "Chat normally. Only use tools when the user explicitly asks for actions, inspection, or file changes. "
        "When the user is just greeting or chatting, respond directly without calling tools."
    )
