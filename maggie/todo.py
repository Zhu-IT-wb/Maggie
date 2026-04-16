from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_STATUSES = {"pending", "in_progress", "completed"}


@dataclass
class TodoItem:
    content: str
    status: str
    active_form: str


class TodoManager:
    def __init__(self) -> None:
        # 保存当前会话中的待办快照，供 agent 在多轮执行中持续更新。
        self.items: list[TodoItem] = []

    def update(self, items: list[dict[str, Any]]) -> str:
        # 校验模型写入的待办列表，替换当前快照，并返回渲染后的清单文本。
        if len(items) > 20:
            raise ValueError("Max 20 todos allowed")

        validated: list[TodoItem] = []
        in_progress_count = 0
        for index, item in enumerate(items, start=1):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).strip().lower()
            active_form = str(item.get("activeForm", "")).strip()

            if not content:
                raise ValueError(f"Item {index}: content required")
            if status not in VALID_STATUSES:
                raise ValueError(f"Item {index}: invalid status '{status}'")
            if not active_form:
                raise ValueError(f"Item {index}: activeForm required")
            if status == "in_progress":
                in_progress_count += 1

            validated.append(TodoItem(content=content, status=status, active_form=active_form))

        if in_progress_count > 1:
            raise ValueError("Only one todo may be in_progress at a time")

        self.items = validated
        return self.render()

    def render(self) -> str:
        # 将当前待办快照渲染成适合终端展示的紧凑清单。
        if not self.items:
            return "No todos."

        lines: list[str] = []
        for item in self.items:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }[item.status]
            suffix = f" <- {item.active_form}" if item.status == "in_progress" else ""
            lines.append(f"{marker} {item.content}{suffix}")

        done = sum(1 for item in self.items if item.status == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        # 告诉调用方当前清单里是否还有未完成项。
        return any(item.status != "completed" for item in self.items)