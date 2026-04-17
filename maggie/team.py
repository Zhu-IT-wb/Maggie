from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .llm import ChatClient
from .tools import BASE_TOOLS, execute_tool


VALID_MSG_TYPES = {
    'message',
    'broadcast',
    'shutdown_request',
    'shutdown_response',
    'plan_approval_response',
}


class MessageBus:
    """管理团队成员之间基于 JSONL 收件箱的消息通信。"""

    def __init__(self, team_dir: Path):
        # 每个成员一个 inbox 文件，消息采用追加写入，读取时再统一 drain。
        self.team_dir = team_dir.resolve()
        self.inbox_dir = self.team_dir / 'inbox'
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def send(
        self,
        sender: str,
        to: str,
        content: str,
        msg_type: str = 'message',
        extra: dict[str, Any] | None = None,
    ) -> str:
        # 写入单个成员收件箱；这里只保证投递，不负责更高层的协议语义。
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {sorted(VALID_MSG_TYPES)}"
        message = {
            'type': msg_type,
            'from': sender,
            'content': content,
            'timestamp': time.time(),
        }
        if extra:
            message.update(extra)
        inbox_path = self.inbox_dir / f'{to}.jsonl'
        line = json.dumps(message, ensure_ascii=False)
        with self._lock:
            with inbox_path.open('a', encoding='utf-8') as handle:
                handle.write(line + '\n')
        return f'Sent {msg_type} to {to}'

    def read_inbox(self, name: str) -> list[dict[str, Any]]:
        # 读取并清空指定成员的收件箱，模拟一次性投递到 agent 上下文。
        inbox_path = self.inbox_dir / f'{name}.jsonl'
        if not inbox_path.exists():
            return []
        with self._lock:
            raw = inbox_path.read_text(encoding='utf-8').strip()
            inbox_path.write_text('', encoding='utf-8')
        if not raw:
            return []
        messages: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            messages.append(json.loads(line))
        return messages

    def broadcast(self, sender: str, content: str, teammates: list[str]) -> str:
        # 向除自己之外的全部成员投递广播消息。
        count = 0
        for teammate in teammates:
            if teammate == sender:
                continue
            self.send(sender, teammate, content, msg_type='broadcast')
            count += 1
        return f'Broadcast to {count} teammates'


class ProtocolRegistry:
    """跟踪 shutdown 和 plan approval 两类协议请求。"""

    def __init__(self):
        # 两类协议都用 request_id 关联，便于主 agent 和 teammate 在多轮消息中对账。
        self.shutdown_requests: dict[str, dict[str, Any]] = {}
        self.plan_requests: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_shutdown_request(self, requester: str, target: str) -> str:
        # 生成 shutdown 请求并记录为 pending，等待对方响应。
        request_id = str(uuid.uuid4())[:8]
        with self._lock:
            self.shutdown_requests[request_id] = {
                'request_id': request_id,
                'requester': requester,
                'target': target,
                'status': 'pending',
                'created_at': time.time(),
                'updated_at': time.time(),
            }
        return request_id

    def resolve_shutdown_request(self, request_id: str, approve: bool, responder: str, reason: str = '') -> str:
        # 根据 teammate 的响应更新 shutdown 请求状态。
        with self._lock:
            request = self.shutdown_requests.get(request_id)
            if request is None:
                return f"Error: Unknown shutdown request '{request_id}'"
            request['status'] = 'approved' if approve else 'rejected'
            request['responder'] = responder
            request['reason'] = reason
            request['updated_at'] = time.time()
        return f"Shutdown {'approved' if approve else 'rejected'}"

    def get_shutdown_status(self, request_id: str) -> str:
        # 供 lead 查询指定 shutdown 请求的当前状态。
        with self._lock:
            request = self.shutdown_requests.get(request_id)
            if request is None:
                return json.dumps({'error': 'not found'}, ensure_ascii=False, indent=2)
            return json.dumps(request, ensure_ascii=False, indent=2)

    def create_plan_request(self, sender: str, plan: str) -> str:
        # teammate 提交计划后，先在注册表中生成 pending 记录，再等待 lead 审批。
        request_id = str(uuid.uuid4())[:8]
        with self._lock:
            self.plan_requests[request_id] = {
                'request_id': request_id,
                'from': sender,
                'plan': plan,
                'status': 'pending',
                'created_at': time.time(),
                'updated_at': time.time(),
            }
        return request_id

    def resolve_plan_request(self, request_id: str, approve: bool, reviewer: str, feedback: str = '') -> str:
        # lead 审批后，把计划请求改成 approved 或 rejected。
        with self._lock:
            request = self.plan_requests.get(request_id)
            if request is None:
                return f"Error: Unknown plan request '{request_id}'"
            request['status'] = 'approved' if approve else 'rejected'
            request['reviewer'] = reviewer
            request['feedback'] = feedback
            request['updated_at'] = time.time()
        return f"Plan {'approved' if approve else 'rejected'}"

    def get_plan_request(self, request_id: str) -> dict[str, Any] | None:
        # 读取计划请求详情，供发回审批结果时确定目标成员。
        with self._lock:
            request = self.plan_requests.get(request_id)
            if request is None:
                return None
            return dict(request)

    def list_plan_requests(self, status_filter: str = 'all') -> str:
        # 以紧凑文本列出当前计划审批请求，便于 lead 批量审阅。
        with self._lock:
            requests = list(self.plan_requests.values())
        if status_filter != 'all':
            requests = [request for request in requests if request.get('status') == status_filter]
        if not requests:
            return 'No plan requests.'
        lines: list[str] = []
        for request in requests:
            lines.append(
                f"{request['request_id']}: [{request['status']}] from={request['from']} plan={request['plan'][:80]}"
            )
        return '\n'.join(lines)


class TeammateManager:
    """管理持久化的团队成员配置与后台 teammate 线程。"""

    def __init__(self, settings: Settings, team_dir: Path, bus: MessageBus, protocols: ProtocolRegistry):
        # 团队配置持久化到 .team/config.json，线程本身只在当前进程中存活。
        self.settings = settings
        self.team_dir = team_dir.resolve()
        self.team_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.team_dir / 'config.json'
        self.bus = bus
        self.protocols = protocols
        self.config = self._load_config()
        self.threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def _load_config(self) -> dict[str, Any]:
        # 如果已有历史团队配置则复用，否则创建默认团队结构。
        if self.config_path.exists():
            return json.loads(self.config_path.read_text(encoding='utf-8'))
        return {'team_name': 'maggie-default', 'members': []}

    def _save_config(self) -> None:
        # 每次成员状态变化后都立即落盘，保证 /resume 之外也能查看团队状态。
        self.config_path.write_text(
            json.dumps(self.config, indent=2, ensure_ascii=False),
            encoding='utf-8',
        )

    def _find_member(self, name: str) -> dict[str, Any] | None:
        # 按成员名查找配置记录，便于更新状态和角色。
        for member in self.config['members']:
            if member['name'] == name:
                return member
        return None

    def _set_status(self, name: str, status: str) -> None:
        # 统一修改成员状态，避免不同代码路径分别写配置。
        member = self._find_member(name)
        if member is None:
            return
        member['status'] = status
        member['updated_at'] = time.time()
        self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        # 启动一个持久化 teammate；如果成员已存在且空闲，则复用其身份继续工作。
        with self._lock:
            member = self._find_member(name)
            if member is not None and member.get('status') not in {'idle', 'shutdown'}:
                return f"Error: '{name}' is currently {member['status']}"
            if member is None:
                member = {
                    'name': name,
                    'role': role,
                    'status': 'working',
                    'created_at': time.time(),
                    'updated_at': time.time(),
                }
                self.config['members'].append(member)
            else:
                member['role'] = role
                member['status'] = 'working'
                member['updated_at'] = time.time()
            self._save_config()

        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"Spawned teammate '{name}' (role: {role})"

    def _teammate_system_prompt(self, name: str, role: str) -> str:
        # teammate 除了本地执行外，还要知道如何走 shutdown / plan approval 协议。
        return (
            f"You are Maggie teammate '{name}' with role '{role}' working at {self.settings.workdir}. "
            'You have your own persistent identity and inbox. '
            'You are running in a Windows workspace, so prefer Windows-compatible commands such as dir, type, cd, where, python, and PowerShell cmdlets over Unix commands like ls, cat, pwd, or which. '
            'Use send_message to communicate progress or questions to the lead or other teammates. '
            'Use read_inbox when you need to consume new messages. '
            'Use plan_approval before substantial work that should be approved by the lead. '
            'Respond to shutdown_request with shutdown_response when the lead asks you to stop. '
            'Work on the assigned task, then go idle when finished.'
        )

    def _teammate_loop(self, name: str, role: str, prompt: str) -> None:
        # teammate 使用独立消息历史循环运行，但身份和 inbox 会在多次唤醒之间保留。
        client = ChatClient(self.settings)
        system = self._teammate_system_prompt(name, role)
        messages: list[dict[str, Any]] = [{'role': 'user', 'content': prompt}]
        should_shutdown = False

        for _ in range(50):
            inbox = self.bus.read_inbox(name)
            if inbox:
                messages.append(
                    {
                        'role': 'user',
                        'content': '<team-inbox>\n' + json.dumps(inbox, ensure_ascii=False, indent=2) + '\n</team-inbox>',
                    }
                )

            if should_shutdown:
                break

            try:
                response = client.create_message(
                    system=system,
                    messages=messages,
                    tools=self._teammate_tools(),
                )
            except Exception:
                self._set_status(name, 'idle')
                return

            messages.append({'role': 'assistant', 'content': response.content})
            if response.stop_reason != 'tool_use':
                break

            results: list[dict[str, str]] = []
            for block in response.content:
                if getattr(block, 'type', None) != 'tool_use':
                    continue
                output = execute_tool(
                    block.name,
                    block.input,
                    self.settings.workdir,
                    message_bus=self.bus,
                    protocol_registry=self.protocols,
                    current_agent_name=name,
                )
                print(f"  [{name}] {block.name}: {str(output)[:120]}")
                if block.name == 'shutdown_response' and bool(block.input.get('approve')):
                    should_shutdown = True
                results.append(
                    {
                        'type': 'tool_result',
                        'tool_use_id': block.id,
                        'content': str(output),
                    }
                )
            messages.append({'role': 'user', 'content': results})

        self._set_status(name, 'shutdown' if should_shutdown else 'idle')

    def _teammate_tools(self) -> list[dict[str, Any]]:
        # teammate 只暴露基础文件工具、团队通信工具和两类协议工具。
        return [
            *BASE_TOOLS,
            {
                'name': 'send_message',
                'description': 'Send a message to the lead or a teammate inbox.',
                'input_schema': {
                    'type': 'object',
                    'properties': {
                        'to': {'type': 'string'},
                        'content': {'type': 'string'},
                        'msg_type': {'type': 'string', 'enum': sorted(VALID_MSG_TYPES)},
                    },
                    'required': ['to', 'content'],
                },
            },
            {
                'name': 'read_inbox',
                'description': 'Read and drain your own inbox.',
                'input_schema': {
                    'type': 'object',
                    'properties': {},
                },
            },
            {
                'name': 'shutdown_response',
                'description': 'Approve or reject a shutdown request sent by the lead.',
                'input_schema': {
                    'type': 'object',
                    'properties': {
                        'request_id': {'type': 'string'},
                        'approve': {'type': 'boolean'},
                        'reason': {'type': 'string'},
                    },
                    'required': ['request_id', 'approve'],
                },
            },
            {
                'name': 'plan_approval',
                'description': 'Submit a work plan to the lead for approval before major work.',
                'input_schema': {
                    'type': 'object',
                    'properties': {
                        'plan': {'type': 'string'},
                    },
                    'required': ['plan'],
                },
            },
        ]

    def list_all(self) -> str:
        # 以紧凑文本格式返回当前团队成员清单和状态。
        if not self.config['members']:
            return 'No teammates.'
        lines = [f"Team: {self.config['team_name']}"]
        for member in self.config['members']:
            lines.append(f"  {member['name']} ({member['role']}): {member['status']}")
        return '\n'.join(lines)

    def member_names(self) -> list[str]:
        # 提供广播所需的当前成员名列表。
        return [member['name'] for member in self.config['members']]