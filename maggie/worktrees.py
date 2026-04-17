from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .tasks import TaskManager


DANGEROUS_PATTERNS = ['rm -rf /', 'sudo', 'shutdown', 'reboot', '> /dev/']


def detect_repo_root(cwd: Path) -> Path | None:
    """检测当前目录所属的 Git 仓库根目录。"""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    root = Path(result.stdout.strip())
    return root if root.exists() else None


class EventBus:
    """记录 worktree 生命周期事件，便于排查和复盘。"""

    def __init__(self, log_path: Path):
        # 事件日志采用追加写入的 JSONL，避免复杂数据库依赖。
        self.log_path = log_path.resolve()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.write_text('', encoding='utf-8')

    def emit(
        self,
        event: str,
        task: dict[str, Any] | None = None,
        worktree: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        # 每个事件都记录时间、任务快照和 worktree 快照，方便后续查询。
        payload: dict[str, Any] = {
            'event': event,
            'ts': time.time(),
            'task': task or {},
            'worktree': worktree or {},
        }
        if error:
            payload['error'] = error
        with self.log_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + '\n')

    def list_recent(self, limit: int = 20) -> str:
        # 返回最近若干条事件，便于模型观察 worktree 生命周期。
        safe_limit = max(1, min(int(limit or 20), 200))
        lines = self.log_path.read_text(encoding='utf-8').splitlines()
        recent = lines[-safe_limit:]
        items: list[dict[str, Any]] = []
        for line in recent:
            try:
                items.append(json.loads(line))
            except Exception:
                items.append({'event': 'parse_error', 'raw': line})
        return json.dumps(items, indent=2, ensure_ascii=False)


class WorktreeManager:
    """管理 .worktrees/ 下的执行隔离目录，并与长期任务板联动。"""

    def __init__(self, workspace: Path, tasks: TaskManager):
        # worktree 索引和事件日志都挂在仓库根目录下，保证多次启动能看到同一份状态。
        self.workspace = workspace.resolve()
        self.repo_root = detect_repo_root(self.workspace) or self.workspace
        self.tasks = tasks
        self.dir = self.repo_root / '.worktrees'
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / 'index.json'
        if not self.index_path.exists():
            self.index_path.write_text(json.dumps({'worktrees': []}, indent=2), encoding='utf-8')
        self.events = EventBus(self.dir / 'events.jsonl')
        self.git_available = self._is_git_repo()

    def _is_git_repo(self) -> bool:
        # 使用 git 自检，避免在非仓库目录里暴露假可用的 worktree 能力。
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--is-inside-work-tree'],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            return False
        return result.returncode == 0

    def _run_git(self, args: list[str]) -> str:
        # 对所有 git worktree 操作做统一包装，失败时抛出可读错误。
        if not self.git_available:
            raise RuntimeError('Not in a git repository. worktree tools require git.')
        result = subprocess.run(
            ['git', *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            output = (result.stdout + result.stderr).strip()
            raise RuntimeError(output or f"git {' '.join(args)} failed")
        return (result.stdout + result.stderr).strip() or '(no output)'

    def _load_index(self) -> dict[str, Any]:
        # 读取 .worktrees/index.json 作为当前 worktree 真相源。
        return json.loads(self.index_path.read_text(encoding='utf-8'))

    def _save_index(self, data: dict[str, Any]) -> None:
        # 每次变更 worktree 生命周期后都立刻落盘索引。
        self.index_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding='utf-8',
        )

    def _find(self, name: str) -> dict[str, Any] | None:
        # 按名称查找 worktree 记录，供状态查询和关闭流程复用。
        for item in self._load_index().get('worktrees', []):
            if item.get('name') == name:
                return item
        return None

    def _validate_name(self, name: str) -> None:
        # 限制名称字符集，避免拼接到路径和分支名时产生歧义。
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,40}', name or ''):
            raise ValueError('Invalid worktree name. Use 1-40 chars: letters, numbers, ., _, -')

    def _normalize_command(self, command: str) -> str:
        # 把常见 Unix 只读命令映射到 Windows 等价命令，减少模型误判平台时的报错。
        stripped = command.strip()
        lowered = stripped.lower()
        if lowered in {'ls', 'ls -l', 'ls -la', 'ls -al'}:
            return 'dir'
        if lowered == 'pwd':
            return 'cd'
        if lowered.startswith('cat ') and '|' not in stripped and '>' not in stripped and '<' not in stripped:
            return f"type {stripped[4:]}"
        if lowered.startswith('which ') and '|' not in stripped and '>' not in stripped and '<' not in stripped:
            return f"where {stripped[6:]}"
        return command

    def _decode_output(self, data: bytes) -> str:
        # 兼容中文 Windows 终端常见编码，尽量避免乱码或异常。
        for encoding in ('utf-8', 'gbk', 'cp936'):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode('utf-8', errors='replace')

    def create(self, name: str, task_id: int | None = None, base_ref: str = 'HEAD') -> str:
        # 创建新的 git worktree，并可选地把它绑定到指定长期任务。
        self._validate_name(name)
        if self._find(name):
            raise ValueError(f"Worktree '{name}' already exists in index")
        if task_id is not None and not self.tasks.exists(task_id):
            raise ValueError(f'Task {task_id} not found')

        path = self.dir / name
        branch = f'wt/{name}'
        task_snapshot = {'id': task_id} if task_id is not None else {}
        self.events.emit(
            'worktree.create.before',
            task=task_snapshot,
            worktree={'name': name, 'base_ref': base_ref},
        )
        try:
            self._run_git(['worktree', 'add', '-b', branch, str(path), base_ref])
            entry = {
                'name': name,
                'path': str(path),
                'branch': branch,
                'task_id': task_id,
                'status': 'active',
                'created_at': time.time(),
            }
            index = self._load_index()
            index['worktrees'].append(entry)
            self._save_index(index)
            if task_id is not None:
                self.tasks.bind_worktree(task_id, name)
            self.events.emit(
                'worktree.create.after',
                task=task_snapshot,
                worktree={'name': name, 'path': str(path), 'branch': branch, 'status': 'active'},
            )
            return json.dumps(entry, indent=2, ensure_ascii=False)
        except Exception as exc:
            self.events.emit(
                'worktree.create.failed',
                task=task_snapshot,
                worktree={'name': name, 'base_ref': base_ref},
                error=str(exc),
            )
            raise

    def list_all(self) -> str:
        # 列出所有被 Maggie 跟踪的 worktree，而不是直接相信 git 原生命令输出。
        worktrees = self._load_index().get('worktrees', [])
        if not worktrees:
            return 'No worktrees in index.'
        lines: list[str] = []
        for item in worktrees:
            task_suffix = f" task={item['task_id']}" if item.get('task_id') is not None else ''
            lines.append(
                f"[{item.get('status', 'unknown')}] {item['name']} -> {item['path']} ({item.get('branch', '-')}){task_suffix}"
            )
        return '\n'.join(lines)

    def status(self, name: str) -> str:
        # 查看某个 worktree 的 git 状态，帮助模型判断是否有未提交改动。
        worktree = self._find(name)
        if not worktree:
            return f"Error: Unknown worktree '{name}'"
        path = Path(worktree['path'])
        if not path.exists():
            return f'Error: Worktree path missing: {path}'
        result = subprocess.run(
            ['git', 'status', '--short', '--branch'],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        text = (result.stdout + result.stderr).strip()
        return text or 'Clean worktree'

    def run(self, name: str, command: str) -> str:
        # 在指定 worktree 目录里执行命令，让不同任务天然隔离到不同目录车道。
        normalized = self._normalize_command(command)
        if any(pattern in normalized for pattern in DANGEROUS_PATTERNS):
            return 'Error: Dangerous command blocked'
        worktree = self._find(name)
        if not worktree:
            return f"Error: Unknown worktree '{name}'"
        path = Path(worktree['path'])
        if not path.exists():
            return f'Error: Worktree path missing: {path}'
        try:
            result = subprocess.run(
                normalized,
                shell=True,
                cwd=path,
                capture_output=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return 'Error: Timeout (300s)'
        except (FileNotFoundError, OSError) as exc:
            return f'Error: {exc}'
        stdout = self._decode_output(result.stdout or b'')
        stderr = self._decode_output(result.stderr or b'')
        output = (stdout + stderr).strip()
        return output[:50000] if output else '(no output)'

    def remove(self, name: str, force: bool = False, complete_task: bool = False) -> str:
        # 移除 worktree，并可选地把绑定任务收尾为 completed。
        worktree = self._find(name)
        if not worktree:
            return f"Error: Unknown worktree '{name}'"

        task_snapshot = {'id': worktree.get('task_id')} if worktree.get('task_id') is not None else {}
        self.events.emit(
            'worktree.remove.before',
            task=task_snapshot,
            worktree={'name': name, 'path': worktree.get('path')},
        )
        try:
            args = ['worktree', 'remove']
            if force:
                args.append('--force')
            args.append(worktree['path'])
            self._run_git(args)

            if worktree.get('task_id') is not None:
                if complete_task:
                    before = json.loads(self.tasks.get(int(worktree['task_id'])))
                    self.tasks.update(int(worktree['task_id']), status='completed')
                    self.tasks.unbind_worktree(int(worktree['task_id']))
                    self.events.emit(
                        'task.completed',
                        task={'id': before['id'], 'subject': before.get('subject', ''), 'status': 'completed'},
                        worktree={'name': name},
                    )
                else:
                    self.tasks.unbind_worktree(int(worktree['task_id']))

            index = self._load_index()
            for item in index.get('worktrees', []):
                if item.get('name') == name:
                    item['status'] = 'removed'
                    item['removed_at'] = time.time()
            self._save_index(index)
            self.events.emit(
                'worktree.remove.after',
                task=task_snapshot,
                worktree={'name': name, 'path': worktree.get('path'), 'status': 'removed'},
            )
            return f"Removed worktree '{name}'"
        except Exception as exc:
            self.events.emit(
                'worktree.remove.failed',
                task=task_snapshot,
                worktree={'name': name, 'path': worktree.get('path')},
                error=str(exc),
            )
            raise

    def keep(self, name: str) -> str:
        # 把 worktree 标记为 kept，表示当前保留这条执行车道而不是立即移除。
        worktree = self._find(name)
        if not worktree:
            return f"Error: Unknown worktree '{name}'"
        index = self._load_index()
        kept: dict[str, Any] | None = None
        for item in index.get('worktrees', []):
            if item.get('name') == name:
                item['status'] = 'kept'
                item['kept_at'] = time.time()
                kept = item
                break
        self._save_index(index)
        self.events.emit(
            'worktree.keep',
            task={'id': worktree.get('task_id')} if worktree.get('task_id') is not None else {},
            worktree={'name': name, 'path': worktree.get('path'), 'status': 'kept'},
        )
        if kept is None:
            return f"Error: Unknown worktree '{name}'"
        return json.dumps(kept, indent=2, ensure_ascii=False)

    def list_events(self, limit: int = 20) -> str:
        # 透传最近的 worktree 事件，供主 agent 观察整个生命周期。
        return self.events.list_recent(limit)
