from __future__ import annotations

import subprocess
import threading
import time
import uuid
from pathlib import Path


class BackgroundManager:
    """管理后台 shell 任务和完成通知队列。"""

    def __init__(self, workdir: Path):
        # 后台任务共享同一个工作区，但执行结果通过队列异步回到父 agent。
        self.workdir = workdir.resolve()
        self.tasks: dict[str, dict[str, str | float | None]] = {}
        self._notification_queue: list[dict[str, str]] = []
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        # 启动后台线程后立即返回 task_id，不阻塞当前 agent 循环。
        task_id = str(uuid.uuid4())[:8]
        with self._lock:
            self.tasks[task_id] = {
                'status': 'running',
                'result': None,
                'command': command,
                'created_at': time.time(),
            }
        thread = threading.Thread(target=self._execute, args=(task_id, command), daemon=True)
        thread.start()
        return f'Background task {task_id} started: {command[:80]}'

    def _decode_output(self, data: bytes) -> str:
        # Windows 下命令输出编码不稳定，按常见编码顺序兜底解码。
        for encoding in ('utf-8', 'gbk', 'cp936'):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode('utf-8', errors='replace')

    def _execute(self, task_id: str, command: str) -> None:
        # 在线程中执行命令，完成后把摘要放入通知队列供父 agent 下轮读取。
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.workdir,
                capture_output=True,
                timeout=300,
            )
            output = self._decode_output(result.stdout or b'') + self._decode_output(result.stderr or b'')
            status = 'completed'
        except subprocess.TimeoutExpired:
            output = 'Error: Timeout (300s)'
            status = 'timeout'
        except (FileNotFoundError, OSError) as exc:
            output = f'Error: {exc}'
            status = 'error'
        except Exception as exc:
            output = f'Error: {exc}'
            status = 'error'

        final_output = (output.strip() or '(no output)')[:50000]
        with self._lock:
            task = self.tasks.get(task_id)
            if task is not None:
                task['status'] = status
                task['result'] = final_output
            self._notification_queue.append(
                {
                    'task_id': task_id,
                    'status': status,
                    'command': command[:80],
                    'result': final_output[:500],
                }
            )

    def check(self, task_id: str | None = None) -> str:
        # 可查看单个任务详情，也可查看当前所有后台任务概览。
        with self._lock:
            if task_id:
                task = self.tasks.get(task_id)
                if task is None:
                    return f'Error: Unknown background task {task_id}'
                result = task.get('result') or '(running)'
                return f"[{task['status']}] {str(task['command'])[:80]}\n{result}"

            if not self.tasks:
                return 'No background tasks.'

            lines: list[str] = []
            for current_id, task in self.tasks.items():
                lines.append(f"{current_id}: [{task['status']}] {str(task['command'])[:80]}")
            return '\n'.join(lines)

    def drain_notifications(self) -> list[dict[str, str]]:
        # 在每轮 LLM 调用前取走所有完成通知，并清空队列。
        with self._lock:
            notifications = list(self._notification_queue)
            self._notification_queue.clear()
        return notifications