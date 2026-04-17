from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


VALID_TASK_STATUS = {'pending', 'in_progress', 'completed'}
VALID_LIST_FILTERS = {'all', 'open', 'pending', 'in_progress', 'completed', 'archived'}


class TaskManager:
    """管理持久化到 .tasks/ 的长期任务，并支持 worktree 绑定。"""

    def __init__(self, workspace: Path):
        # 任务目录放在工作区根目录下，保证压缩上下文或切换会话后任务状态仍可恢复。
        self.workspace = workspace.resolve()
        self.tasks_dir = self.workspace / '.tasks'
        self.archive_dir = self.tasks_dir / 'archive'
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        # 扫描活动任务和归档任务，推导下一个可用任务 ID。
        ids: list[int] = []
        for root in (self.tasks_dir, self.archive_dir):
            for path in root.glob('task_*.json'):
                try:
                    ids.append(int(path.stem.split('_', 1)[1]))
                except (IndexError, ValueError):
                    continue
        return max(ids) if ids else 0

    def _task_path(self, task_id: int, archived: bool = False) -> Path:
        # 统一生成任务文件路径，避免各处手写文件名规则。
        base_dir = self.archive_dir if archived else self.tasks_dir
        return base_dir / f'task_{task_id}.json'

    def _find_path(self, task_id: int) -> Path:
        # 先找活动任务，再找归档任务，让调用方不用关心任务当前在哪个目录。
        active_path = self._task_path(task_id, archived=False)
        if active_path.exists():
            return active_path
        archive_path = self._task_path(task_id, archived=True)
        if archive_path.exists():
            return archive_path
        raise ValueError(f'Task {task_id} not found')

    def _load(self, task_id: int) -> dict[str, Any]:
        # 读取单个任务详情，并补充 archived 标记供上层判断。
        path = self._find_path(task_id)
        task = json.loads(path.read_text(encoding='utf-8'))
        task['archived'] = path.parent == self.archive_dir
        task.setdefault('blocked_by', [])
        task.setdefault('owner', '')
        task.setdefault('description', '')
        task.setdefault('worktree', '')
        return task

    def _save(self, task: dict[str, Any], archived: bool = False) -> None:
        # 保存任务前统一刷新 updated_at，并去掉运行期辅助字段。
        task = dict(task)
        task.pop('archived', None)
        task['blocked_by'] = [int(value) for value in task.get('blocked_by', [])]
        task['owner'] = str(task.get('owner', ''))
        task['description'] = str(task.get('description', ''))
        task['worktree'] = str(task.get('worktree', ''))
        task['updated_at'] = time.time()
        self._task_path(int(task['id']), archived=archived).write_text(
            json.dumps(task, indent=2, ensure_ascii=False),
            encoding='utf-8',
        )

    def _read_tasks(self, archived: bool = False) -> list[dict[str, Any]]:
        # 读取一组任务文件，并统一补上 archived 标记。
        base_dir = self.archive_dir if archived else self.tasks_dir
        tasks: list[dict[str, Any]] = []
        for path in sorted(base_dir.glob('task_*.json'), key=lambda item: int(item.stem.split('_', 1)[1])):
            task = json.loads(path.read_text(encoding='utf-8'))
            task['archived'] = archived
            task.setdefault('blocked_by', [])
            task.setdefault('owner', '')
            task.setdefault('description', '')
            task.setdefault('worktree', '')
            tasks.append(task)
        return tasks

    def create(self, subject: str, description: str = '', owner: str = '') -> str:
        # 创建新的长期任务，默认从 pending 开始，未绑定 owner 和 worktree。
        now = time.time()
        task = {
            'id': self._next_id,
            'subject': subject,
            'description': description,
            'status': 'pending',
            'blocked_by': [],
            'owner': owner,
            'worktree': '',
            'created_at': now,
            'updated_at': now,
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(self._load(int(task['id'])), indent=2, ensure_ascii=False)

    def get(self, task_id: int) -> str:
        # 返回单个任务的完整 JSON，方便模型读取细节后继续操作。
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def exists(self, task_id: int) -> bool:
        # 判断任务是否存在，供 worktree 绑定等场景快速校验。
        try:
            self._find_path(task_id)
            return True
        except ValueError:
            return False

    def update(
        self,
        task_id: int,
        status: str | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
        owner: str | None = None,
        description: str | None = None,
        worktree: str | None = None,
    ) -> str:
        # 更新任务状态、依赖、owner、说明或 worktree 绑定；归档任务不允许直接修改。
        task = self._load(task_id)
        if task.get('archived'):
            raise ValueError(f'Task {task_id} is archived; restore it before updating')
        if status is not None:
            if status not in VALID_TASK_STATUS:
                raise ValueError(f'Invalid status: {status}')
            task['status'] = status
        if add_blocked_by:
            merged = set(int(value) for value in task.get('blocked_by', []))
            merged.update(int(value) for value in add_blocked_by if int(value) != task_id)
            task['blocked_by'] = sorted(merged)
        if remove_blocked_by:
            remove_set = {int(value) for value in remove_blocked_by}
            task['blocked_by'] = [value for value in task.get('blocked_by', []) if int(value) not in remove_set]
        if owner is not None:
            task['owner'] = owner
        if description is not None:
            task['description'] = description
        if worktree is not None:
            task['worktree'] = worktree
        self._save(task, archived=False)
        if status == 'completed':
            self._clear_dependency(task_id)
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def bind_worktree(self, task_id: int, worktree: str, owner: str = '') -> str:
        # 把任务绑定到某个 worktree，并在需要时顺便推进到 in_progress。
        task = self._load(task_id)
        if task.get('archived'):
            raise ValueError(f'Task {task_id} is archived and cannot bind a worktree')
        task['worktree'] = worktree
        if owner:
            task['owner'] = owner
        if task.get('status') == 'pending':
            task['status'] = 'in_progress'
        self._save(task, archived=False)
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def unbind_worktree(self, task_id: int) -> str:
        # 清空任务和 worktree 的绑定关系，但不自动改动 owner 或状态。
        task = self._load(task_id)
        if task.get('archived'):
            raise ValueError(f'Task {task_id} is archived and cannot unbind a worktree')
        task['worktree'] = ''
        self._save(task, archived=False)
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def _clear_dependency(self, completed_id: int) -> None:
        # 某个任务完成后，从其他活动任务的 blocked_by 中移除它。
        for task in self._read_tasks(archived=False):
            blocked = [int(value) for value in task.get('blocked_by', [])]
            if completed_id not in blocked:
                continue
            task['blocked_by'] = [value for value in blocked if value != completed_id]
            self._save(task, archived=False)

    def archive(self, task_id: int) -> str:
        # 把任务移入 archive 目录，保留历史但默认不再出现在活动任务板中。
        task = self._load(task_id)
        if task.get('archived'):
            return json.dumps(task, indent=2, ensure_ascii=False)
        source = self._task_path(task_id, archived=False)
        self._save(task, archived=True)
        if source.exists():
            source.unlink()
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def delete(self, task_id: int) -> str:
        # 永久删除任务，用于明确确认不再需要保留的数据。
        path = self._find_path(task_id)
        path.unlink()
        self._remove_dependency_reference(task_id)
        return f'Deleted task {task_id}'

    def _remove_dependency_reference(self, removed_id: int) -> None:
        # 删除任务后，从其他活动任务中清理对它的依赖引用。
        for task in self._read_tasks(archived=False):
            blocked = [int(value) for value in task.get('blocked_by', [])]
            if removed_id not in blocked:
                continue
            task['blocked_by'] = [value for value in blocked if value != removed_id]
            self._save(task, archived=False)

    def prune_completed(self, older_than_days: int = 30, archive: bool = True) -> str:
        # 批量处理很久以前完成的任务，默认转入归档而不是直接删除。
        cutoff = time.time() - max(int(older_than_days), 0) * 86400
        candidates = [
            task for task in self._read_tasks(archived=False)
            if task.get('status') == 'completed' and float(task.get('updated_at', 0)) <= cutoff
        ]
        if not candidates:
            return 'No completed tasks matched the prune condition.'

        processed_ids: list[int] = []
        for task in candidates:
            task_id = int(task['id'])
            if archive:
                self.archive(task_id)
            else:
                self.delete(task_id)
            processed_ids.append(task_id)

        action = 'Archived' if archive else 'Deleted'
        return f"{action} completed tasks: {processed_ids}"

    def scan_unclaimed(self) -> list[dict[str, Any]]:
        # 扫描当前可自动认领的任务：pending、无 owner、无依赖，且尚未绑定 worktree。
        tasks = self._read_tasks(archived=False)
        return [
            task for task in tasks
            if task.get('status') == 'pending'
            and not task.get('owner')
            and not task.get('blocked_by')
        ]

    def claim(self, task_id: int, owner: str) -> str:
        # 将未认领任务标记为当前 owner 的 in_progress 任务，供自治 teammate 抢占工作。
        task = self._load(task_id)
        if task.get('archived'):
            raise ValueError(f'Task {task_id} is archived and cannot be claimed')
        if task.get('owner'):
            return f"Error: Task {task_id} has already been claimed by {task['owner']}"
        if task.get('status') != 'pending':
            return f"Error: Task {task_id} cannot be claimed because its status is '{task.get('status')}'"
        if task.get('blocked_by'):
            return f'Error: Task {task_id} is blocked by other task(s) and cannot be claimed yet'
        task['owner'] = owner
        task['status'] = 'in_progress'
        self._save(task, archived=False)
        return f'Claimed task #{task_id} for {owner}'

    def list_all(self, status_filter: str = 'all') -> str:
        # 按状态过滤列出任务；默认展示活动任务，归档任务可单独查看。
        status_filter = status_filter.lower().strip() or 'all'
        if status_filter not in VALID_LIST_FILTERS:
            raise ValueError(f'Invalid task filter: {status_filter}')

        if status_filter == 'archived':
            tasks = self._read_tasks(archived=True)
        else:
            tasks = self._read_tasks(archived=False)
            if status_filter == 'open':
                tasks = [task for task in tasks if task.get('status') != 'completed']
            elif status_filter != 'all':
                tasks = [task for task in tasks if task.get('status') == status_filter]

        if not tasks:
            return f'No tasks matched filter: {status_filter}.'

        lines: list[str] = []
        for task in tasks:
            marker = {
                'pending': '[ ]',
                'in_progress': '[>]',
                'completed': '[x]',
            }.get(task.get('status', ''), '[?]')
            blocked = task.get('blocked_by', [])
            owner = f" owner={task['owner']}" if task.get('owner') else ''
            blocked_text = f' blocked_by={blocked}' if blocked else ''
            worktree_text = f" wt={task['worktree']}" if task.get('worktree') else ''
            archived_text = ' archived=true' if task.get('archived') else ''
            lines.append(f"{marker} #{task['id']}: {task['subject']}{owner}{blocked_text}{worktree_text}{archived_text}")
        return '\n'.join(lines)
