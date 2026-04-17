from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .llm import ChatClient
from .tasks import TaskManager
from .team import MessageBus, ProtocolRegistry, TeammateManager
from .tools import BASE_TOOLS, execute_tool


POLL_INTERVAL = 5
IDLE_TIMEOUT = 60


def make_identity_block(name: str, role: str, team_name: str) -> dict[str, str]:
    # 在 teammate 从 idle 恢复工作时重新注入身份，避免长时间运行后上下文漂移。
    return {
        'role': 'user',
        'content': f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


class AutonomousTeammateManager(TeammateManager):
    """在团队成员基础上加入 idle 轮询与自动认领任务能力。"""

    def __init__(self, settings: Settings, team_dir: Path, bus: MessageBus, protocols: ProtocolRegistry):
        # 自治 teammate 仍然沿用原有 team config 和 protocol 体系，只额外持有任务板访问能力。
        super().__init__(settings, team_dir, bus, protocols)
        self.task_manager = TaskManager(settings.workdir)
        self.claim_lock = threading.Lock()

    def _teammate_system_prompt(self, name: str, role: str) -> str:
        # 自治 teammate 除协议外，还要知道空闲时可以进入 idle 模式并自行寻找新任务。
        return (
            f"You are Maggie teammate '{name}' with role '{role}' working at {self.settings.workdir}. "
            'You have your own persistent identity and inbox. '
            'You are running in a Windows workspace, so prefer Windows-compatible commands such as dir, type, cd, where, python, and PowerShell cmdlets over Unix commands like ls, cat, pwd, or which. '
            'Use send_message to communicate progress or questions to the lead or other teammates. '
            'Use read_inbox when you need to consume new messages. '
            'Use plan_approval before substantial work that should be approved by the lead. '
            'Respond to shutdown_request with shutdown_response when the lead asks you to stop. '
            'Use idle when you have no more immediate work; you will then poll for inbox messages or unclaimed tasks. '
            'Use claim_task when you intentionally want to claim a specific task from the task board.'
        )

    def _teammate_loop(self, name: str, role: str, prompt: str) -> None:
        # teammate 在工作阶段结束后不会立刻退出，而是先进入 idle 轮询阶段等待新工作。
        client = ChatClient(self.settings)
        team_name = self.config.get('team_name', 'maggie-default')
        system = self._teammate_system_prompt(name, role)
        messages: list[dict[str, Any]] = [make_identity_block(name, role, team_name), {'role': 'user', 'content': prompt}]
        should_shutdown = False

        while True:
            idle_requested = False

            # 工作阶段：和普通 teammate 一样执行工具循环，但允许显式进入 idle。
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
                    idle_requested = True
                    break

                results: list[dict[str, str]] = []
                for block in response.content:
                    if getattr(block, 'type', None) != 'tool_use':
                        continue
                    if block.name == 'idle':
                        idle_requested = True
                        output = 'Entering idle phase. Will poll for inbox messages or unclaimed tasks.'
                    else:
                        output = execute_tool(
                            block.name,
                            block.input,
                            self.settings.workdir,
                            task_manager=self.task_manager,
                            message_bus=self.bus,
                            protocol_registry=self.protocols,
                            current_agent_name=name,
                        )
                        if block.name == 'shutdown_response' and bool(block.input.get('approve')):
                            should_shutdown = True
                    print(f"  [{name}] {block.name}: {str(output)[:120]}")
                    results.append(
                        {
                            'type': 'tool_result',
                            'tool_use_id': block.id,
                            'content': str(output),
                        }
                    )
                messages.append({'role': 'user', 'content': results})
                if idle_requested or should_shutdown:
                    break

            if should_shutdown:
                self._set_status(name, 'shutdown')
                return

            # 空闲阶段：等待 inbox 或自动认领可执行任务；超时后再真正 shutdown。
            self._set_status(name, 'idle')
            resumed = False
            poll_rounds = max(IDLE_TIMEOUT // max(POLL_INTERVAL, 1), 1)
            for _ in range(poll_rounds):
                time.sleep(POLL_INTERVAL)

                inbox = self.bus.read_inbox(name)
                if inbox:
                    messages.append(
                        {
                            'role': 'user',
                            'content': '<team-inbox>\n' + json.dumps(inbox, ensure_ascii=False, indent=2) + '\n</team-inbox>',
                        }
                    )
                    resumed = True
                    break

                with self.claim_lock:
                    unclaimed = self.task_manager.scan_unclaimed()
                    if unclaimed:
                        task = unclaimed[0]
                        result = self.task_manager.claim(int(task['id']), name)
                    else:
                        result = ''

                if result and not result.startswith('Error:'):
                    messages.insert(0, make_identity_block(name, role, team_name))
                    messages.append(
                        {
                            'role': 'user',
                            'content': (
                                f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                                f"{task.get('description', '')}</auto-claimed>"
                            ),
                        }
                    )
                    messages.append(
                        {
                            'role': 'assistant',
                            'content': f"Claimed task #{task['id']}. Continuing work as {name}.",
                        }
                    )
                    resumed = True
                    break

            if not resumed:
                self._set_status(name, 'shutdown')
                return

            self._set_status(name, 'working')

    def _teammate_tools(self) -> list[dict[str, Any]]:
        # 自治 teammate 在 s10 工具基础上，再增加 idle 和 claim_task 两个自治相关工具。
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
                        'msg_type': {'type': 'string', 'enum': ['message', 'broadcast', 'shutdown_request', 'shutdown_response', 'plan_approval_response']},
                    },
                    'required': ['to', 'content'],
                },
            },
            {
                'name': 'read_inbox',
                'description': 'Read and drain your own inbox.',
                'input_schema': {'type': 'object', 'properties': {}},
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
                    'properties': {'plan': {'type': 'string'}},
                    'required': ['plan'],
                },
            },
            {
                'name': 'idle',
                'description': 'Signal that you have no more immediate work and want to enter idle polling.',
                'input_schema': {'type': 'object', 'properties': {}},
            },
            {
                'name': 'claim_task',
                'description': 'Claim a specific task from the persistent task board by id.',
                'input_schema': {
                    'type': 'object',
                    'properties': {'task_id': {'type': 'integer'}},
                    'required': ['task_id'],
                },
            },
        ]