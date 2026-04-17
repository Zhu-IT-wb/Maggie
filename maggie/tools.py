from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .skills import SkillLoader
from .tasks import TaskManager
from .todo import TodoManager
from .worktrees import WorktreeManager


DANGEROUS_PATTERNS = ['rm -rf /', 'sudo', 'shutdown', 'reboot', '> /dev/']


BASE_TOOLS = [
    {
        'name': 'shell',
        'description': 'Run a shell command in the current Windows workspace. Prefer Windows-compatible commands such as dir, type, cd, where, python, or PowerShell cmdlets.',
        'input_schema': {
            'type': 'object',
            'properties': {'command': {'type': 'string'}},
            'required': ['command'],
        },
    },
    {
        'name': 'read_file',
        'description': 'Read file contents from the workspace.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
                'limit': {'type': 'integer'},
            },
            'required': ['path'],
        },
    },
    {
        'name': 'write_file',
        'description': 'Write content to a file in the workspace.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
                'content': {'type': 'string'},
            },
            'required': ['path', 'content'],
        },
    },
    {
        'name': 'edit_file',
        'description': 'Replace exact text inside a workspace file.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
                'old_text': {'type': 'string'},
                'new_text': {'type': 'string'},
            },
            'required': ['path', 'old_text', 'new_text'],
        },
    },
]

TODO_TOOL = {
    'name': 'TodoWrite',
    'description': 'Update the short task checklist for the current job.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'items': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'content': {'type': 'string'},
                        'status': {
                            'type': 'string',
                            'enum': ['pending', 'in_progress', 'completed'],
                        },
                        'activeForm': {'type': 'string'},
                    },
                    'required': ['content', 'status', 'activeForm'],
                },
            }
        },
        'required': ['items'],
    },
}

TASK_TOOL = {
    'name': 'task',
    'description': 'Spawn a subagent with fresh context for isolated exploration or execution.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'prompt': {'type': 'string'},
            'description': {'type': 'string'},
        },
        'required': ['prompt'],
    },
}

LOAD_SKILL_TOOL = {
    'name': 'load_skill',
    'description': 'Load specialized knowledge by skill name.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
        },
        'required': ['name'],
    },
}

COMPACT_TOOL = {
    'name': 'compact',
    'description': 'Trigger manual conversation compression while preserving the requested focus.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'focus': {'type': 'string'},
        },
    },
}

TASK_CREATE_TOOL = {
    'name': 'task_create',
    'description': 'Create a persistent task stored outside the chat history.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'subject': {'type': 'string'},
            'description': {'type': 'string'},
            'owner': {'type': 'string'},
        },
        'required': ['subject'],
    },
}

TASK_UPDATE_TOOL = {
    'name': 'task_update',
    'description': 'Update a persistent task status, metadata, dependency list, or worktree binding.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'task_id': {'type': 'integer'},
            'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed']},
            'addBlockedBy': {'type': 'array', 'items': {'type': 'integer'}},
            'removeBlockedBy': {'type': 'array', 'items': {'type': 'integer'}},
            'owner': {'type': 'string'},
            'description': {'type': 'string'},
            'worktree': {'type': 'string'},
        },
        'required': ['task_id'],
    },
}

TASK_LIST_TOOL = {
    'name': 'task_list',
    'description': 'List persistent tasks with optional filters: all, open, pending, in_progress, completed, archived.',
    'input_schema': {
        'type': 'object',
        'properties': {'filter': {'type': 'string', 'enum': ['all', 'open', 'pending', 'in_progress', 'completed', 'archived']}},
    },
}

TASK_GET_TOOL = {
    'name': 'task_get',
    'description': 'Get the full JSON details for one persistent task.',
    'input_schema': {
        'type': 'object',
        'properties': {'task_id': {'type': 'integer'}},
        'required': ['task_id'],
    },
}

TASK_ARCHIVE_TOOL = {
    'name': 'task_archive',
    'description': 'Move a persistent task into the archive while keeping its history.',
    'input_schema': {
        'type': 'object',
        'properties': {'task_id': {'type': 'integer'}},
        'required': ['task_id'],
    },
}

TASK_DELETE_TOOL = {
    'name': 'task_delete',
    'description': 'Permanently delete a persistent task when it is no longer useful.',
    'input_schema': {
        'type': 'object',
        'properties': {'task_id': {'type': 'integer'}},
        'required': ['task_id'],
    },
}

TASK_PRUNE_COMPLETED_TOOL = {
    'name': 'task_prune_completed',
    'description': 'Archive or delete completed tasks older than a given number of days.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'olderThanDays': {'type': 'integer'},
            'archive': {'type': 'boolean'},
        },
    },
}

TASK_BIND_WORKTREE_TOOL = {
    'name': 'task_bind_worktree',
    'description': 'Bind a persistent task to a worktree name without creating the worktree itself.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'task_id': {'type': 'integer'},
            'worktree': {'type': 'string'},
            'owner': {'type': 'string'},
        },
        'required': ['task_id', 'worktree'],
    },
}

BACKGROUND_RUN_TOOL = {
    'name': 'background_run',
    'description': 'Run a long shell command in the background and return a task id immediately.',
    'input_schema': {
        'type': 'object',
        'properties': {'command': {'type': 'string'}},
        'required': ['command'],
    },
}

CHECK_BACKGROUND_TOOL = {
    'name': 'check_background',
    'description': 'Check one background task by id, or list all background tasks when task_id is omitted.',
    'input_schema': {
        'type': 'object',
        'properties': {'task_id': {'type': 'string'}},
    },
}

SPAWN_TEAMMATE_TOOL = {
    'name': 'spawn_teammate',
    'description': 'Spawn a persistent teammate that keeps its own identity and inbox.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'role': {'type': 'string'},
            'prompt': {'type': 'string'},
        },
        'required': ['name', 'role', 'prompt'],
    },
}

LIST_TEAMMATES_TOOL = {
    'name': 'list_teammates',
    'description': 'List all teammates with their roles and statuses.',
    'input_schema': {'type': 'object', 'properties': {}},
}

SEND_MESSAGE_TOOL = {
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
}

READ_INBOX_TOOL = {
    'name': 'read_inbox',
    'description': 'Read and drain the current agent inbox.',
    'input_schema': {'type': 'object', 'properties': {}},
}

BROADCAST_TOOL = {
    'name': 'broadcast',
    'description': 'Broadcast a message to all teammates except the sender.',
    'input_schema': {
        'type': 'object',
        'properties': {'content': {'type': 'string'}},
        'required': ['content'],
    },
}

SHUTDOWN_REQUEST_TOOL = {
    'name': 'shutdown_request',
    'description': 'Ask a teammate to shut down gracefully and return a request id for tracking.',
    'input_schema': {
        'type': 'object',
        'properties': {'teammate': {'type': 'string'}},
        'required': ['teammate'],
    },
}

SHUTDOWN_RESPONSE_TOOL = {
    'name': 'shutdown_response',
    'description': 'Check the status of a shutdown request by request id, or respond to one as a teammate.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'request_id': {'type': 'string'},
            'approve': {'type': 'boolean'},
            'reason': {'type': 'string'},
        },
        'required': ['request_id'],
    },
}

PLAN_APPROVAL_TOOL = {
    'name': 'plan_approval',
    'description': 'Approve or reject a teammate plan request by request id, or submit a plan as a teammate.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'request_id': {'type': 'string'},
            'approve': {'type': 'boolean'},
            'feedback': {'type': 'string'},
            'plan': {'type': 'string'},
        },
    },
}

LIST_PLAN_REQUESTS_TOOL = {
    'name': 'list_plan_requests',
    'description': 'List pending or historical plan approval requests. Optional filter: all, pending, approved, rejected.',
    'input_schema': {
        'type': 'object',
        'properties': {'filter': {'type': 'string', 'enum': ['all', 'pending', 'approved', 'rejected']}},
    },
}

IDLE_TOOL = {
    'name': 'idle',
    'description': 'Signal that the current autonomous teammate has no immediate work and wants to enter idle polling.',
    'input_schema': {'type': 'object', 'properties': {}},
}

CLAIM_TASK_TOOL = {
    'name': 'claim_task',
    'description': 'Claim a persistent task by id and mark it in progress under the current agent owner.',
    'input_schema': {
        'type': 'object',
        'properties': {'task_id': {'type': 'integer'}},
        'required': ['task_id'],
    },
}

WORKTREE_CREATE_TOOL = {
    'name': 'worktree_create',
    'description': 'Create a git worktree lane and optionally bind it to a persistent task.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'task_id': {'type': 'integer'},
            'base_ref': {'type': 'string'},
        },
        'required': ['name'],
    },
}

WORKTREE_LIST_TOOL = {
    'name': 'worktree_list',
    'description': 'List worktrees tracked by Maggie under .worktrees/index.json.',
    'input_schema': {'type': 'object', 'properties': {}},
}

WORKTREE_STATUS_TOOL = {
    'name': 'worktree_status',
    'description': 'Show git status for one named worktree.',
    'input_schema': {
        'type': 'object',
        'properties': {'name': {'type': 'string'}},
        'required': ['name'],
    },
}

WORKTREE_RUN_TOOL = {
    'name': 'worktree_run',
    'description': 'Run a shell command inside a named worktree directory.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'command': {'type': 'string'},
        },
        'required': ['name', 'command'],
    },
}

WORKTREE_KEEP_TOOL = {
    'name': 'worktree_keep',
    'description': 'Mark a worktree as kept instead of removing it during closeout.',
    'input_schema': {
        'type': 'object',
        'properties': {'name': {'type': 'string'}},
        'required': ['name'],
    },
}

WORKTREE_REMOVE_TOOL = {
    'name': 'worktree_remove',
    'description': 'Remove a worktree lane and optionally mark its bound task completed.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'force': {'type': 'boolean'},
            'complete_task': {'type': 'boolean'},
        },
        'required': ['name'],
    },
}

WORKTREE_EVENTS_TOOL = {
    'name': 'worktree_events',
    'description': 'List recent worktree lifecycle events from .worktrees/events.jsonl.',
    'input_schema': {
        'type': 'object',
        'properties': {'limit': {'type': 'integer'}},
    },
}


def tools_with_todo() -> list[dict[str, Any]]:
    return [*BASE_TOOLS, TODO_TOOL]


def tools_with_task() -> list[dict[str, Any]]:
    return [*BASE_TOOLS, TASK_TOOL]


def tools_with_todo_and_task() -> list[dict[str, Any]]:
    return [*BASE_TOOLS, TODO_TOOL, TASK_TOOL]


def tools_with_todo_task_and_skills() -> list[dict[str, Any]]:
    return [*BASE_TOOLS, TODO_TOOL, TASK_TOOL, LOAD_SKILL_TOOL]


def tools_with_everything() -> list[dict[str, Any]]:
    return [*BASE_TOOLS, TODO_TOOL, TASK_TOOL, LOAD_SKILL_TOOL, COMPACT_TOOL]


def tools_with_task_system() -> list[dict[str, Any]]:
    return [
        *BASE_TOOLS,
        TODO_TOOL,
        TASK_TOOL,
        LOAD_SKILL_TOOL,
        COMPACT_TOOL,
        TASK_CREATE_TOOL,
        TASK_UPDATE_TOOL,
        TASK_LIST_TOOL,
        TASK_GET_TOOL,
        TASK_ARCHIVE_TOOL,
        TASK_DELETE_TOOL,
        TASK_PRUNE_COMPLETED_TOOL,
    ]


def tools_with_background_task_system() -> list[dict[str, Any]]:
    return [*tools_with_task_system(), BACKGROUND_RUN_TOOL, CHECK_BACKGROUND_TOOL]


def tools_with_team_system() -> list[dict[str, Any]]:
    return [*tools_with_background_task_system(), SPAWN_TEAMMATE_TOOL, LIST_TEAMMATES_TOOL, SEND_MESSAGE_TOOL, READ_INBOX_TOOL, BROADCAST_TOOL]


def tools_with_protocol_team_system() -> list[dict[str, Any]]:
    return [*tools_with_team_system(), SHUTDOWN_REQUEST_TOOL, SHUTDOWN_RESPONSE_TOOL, PLAN_APPROVAL_TOOL, LIST_PLAN_REQUESTS_TOOL]


def tools_with_autonomous_team_system() -> list[dict[str, Any]]:
    return [*tools_with_protocol_team_system(), IDLE_TOOL, CLAIM_TASK_TOOL]


def tools_with_worktree_system() -> list[dict[str, Any]]:
    return [
        *tools_with_autonomous_team_system(),
        TASK_BIND_WORKTREE_TOOL,
        WORKTREE_CREATE_TOOL,
        WORKTREE_LIST_TOOL,
        WORKTREE_STATUS_TOOL,
        WORKTREE_RUN_TOOL,
        WORKTREE_KEEP_TOOL,
        WORKTREE_REMOVE_TOOL,
        WORKTREE_EVENTS_TOOL,
    ]


def safe_path(workdir: Path, raw_path: str) -> Path:
    path = (workdir / raw_path).resolve()
    if not path.is_relative_to(workdir.resolve()):
        raise ValueError(f'Path escapes workspace: {raw_path}')
    return path


def normalize_shell_command(command: str) -> str:
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


def decode_command_output(data: bytes) -> str:
    for encoding in ('utf-8', 'gbk', 'cp936'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def run_shell(command: str, workdir: Path) -> str:
    normalized = normalize_shell_command(command)
    if any(pattern in normalized for pattern in DANGEROUS_PATTERNS):
        return 'Error: Dangerous command blocked'
    try:
        result = subprocess.run(normalized, shell=True, cwd=workdir, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return 'Error: Timeout (120s)'
    except (FileNotFoundError, OSError) as exc:
        return f'Error: {exc}'
    stdout = decode_command_output(result.stdout or b'')
    stderr = decode_command_output(result.stderr or b'')
    output = (stdout + stderr).strip()
    return output[:50000] if output else '(no output)'


def run_read(path: str, workdir: Path, limit: int | None = None) -> str:
    try:
        text = safe_path(workdir, path).read_text(encoding='utf-8')
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f'... ({len(lines) - limit} more lines)']
        return '\n'.join(lines)[:50000]
    except Exception as exc:
        return f'Error: {exc}'


def run_write(path: str, content: str, workdir: Path) -> str:
    try:
        target = safe_path(workdir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        return f'Wrote {len(content)} bytes to {path}'
    except Exception as exc:
        return f'Error: {exc}'


def run_edit(path: str, old_text: str, new_text: str, workdir: Path) -> str:
    try:
        target = safe_path(workdir, path)
        content = target.read_text(encoding='utf-8')
        if old_text not in content:
            return f'Error: Text not found in {path}'
        target.write_text(content.replace(old_text, new_text, 1), encoding='utf-8')
        return f'Edited {path}'
    except Exception as exc:
        return f'Error: {exc}'


def execute_tool(
    name: str,
    tool_input: dict[str, Any],
    workdir: Path,
    todo: TodoManager | None = None,
    skill_loader: SkillLoader | None = None,
    task_manager: TaskManager | None = None,
    background_manager: Any | None = None,
    message_bus: Any | None = None,
    teammate_manager: Any | None = None,
    protocol_registry: Any | None = None,
    worktree_manager: WorktreeManager | None = None,
    current_agent_name: str = 'lead',
) -> str:
    def handle_shutdown_request() -> str:
        if protocol_registry is None or message_bus is None:
            return 'Error: Shutdown protocol unavailable'
        teammate = str(tool_input['teammate'])
        request_id = protocol_registry.create_shutdown_request(current_agent_name, teammate)
        message_bus.send(
            current_agent_name,
            teammate,
            'Please shut down gracefully.',
            msg_type='shutdown_request',
            extra={'request_id': request_id},
        )
        return f"Shutdown request {request_id} sent to '{teammate}' (status: pending)"

    def handle_shutdown_status() -> str:
        if protocol_registry is None:
            return 'Error: Shutdown protocol unavailable'
        return protocol_registry.get_shutdown_status(str(tool_input['request_id']))

    def handle_plan_submit() -> str:
        if protocol_registry is None or message_bus is None:
            return 'Error: Plan approval protocol unavailable'
        plan = str(tool_input['plan'])
        request_id = protocol_registry.create_plan_request(current_agent_name, plan)
        message_bus.send(
            current_agent_name,
            'lead',
            plan,
            msg_type='plan_approval_response',
            extra={'request_id': request_id, 'plan': plan},
        )
        return f'Plan submitted (request_id={request_id}). Waiting for lead approval.'

    def handle_plan_review() -> str:
        if protocol_registry is None or message_bus is None:
            return 'Error: Plan approval protocol unavailable'
        request_id = str(tool_input['request_id'])
        approve = bool(tool_input['approve'])
        feedback = str(tool_input.get('feedback', ''))
        result = protocol_registry.resolve_plan_request(request_id, approve, current_agent_name, feedback)
        request = protocol_registry.get_plan_request(request_id)
        if request is not None:
            message_bus.send(
                current_agent_name,
                str(request['from']),
                feedback,
                msg_type='plan_approval_response',
                extra={'request_id': request_id, 'approve': approve, 'feedback': feedback},
            )
        return result

    def handle_shutdown_reply() -> str:
        if protocol_registry is None or message_bus is None:
            return 'Error: Shutdown protocol unavailable'
        request_id = str(tool_input['request_id'])
        approve = bool(tool_input['approve'])
        reason = str(tool_input.get('reason', ''))
        result = protocol_registry.resolve_shutdown_request(request_id, approve, current_agent_name, reason)
        message_bus.send(
            current_agent_name,
            'lead',
            reason,
            msg_type='shutdown_response',
            extra={'request_id': request_id, 'approve': approve, 'reason': reason},
        )
        return result

    handlers = {
        'shell': lambda: run_shell(str(tool_input['command']), workdir),
        'bash': lambda: run_shell(str(tool_input['command']), workdir),
        'read_file': lambda: run_read(str(tool_input['path']), workdir, tool_input.get('limit')),
        'write_file': lambda: run_write(str(tool_input['path']), str(tool_input['content']), workdir),
        'edit_file': lambda: run_edit(str(tool_input['path']), str(tool_input['old_text']), str(tool_input['new_text']), workdir),
        'TodoWrite': lambda: todo.update(tool_input['items']) if todo is not None else 'Error: Todo manager unavailable',
        'load_skill': lambda: skill_loader.get_content(str(tool_input['name'])) if skill_loader is not None else 'Error: Skill loader unavailable',
        'task_create': lambda: task_manager.create(
            str(tool_input['subject']),
            str(tool_input.get('description', '')),
            str(tool_input.get('owner', '')),
        ) if task_manager is not None else 'Error: Task manager unavailable',
        'task_update': lambda: task_manager.update(
            int(tool_input['task_id']),
            status=tool_input.get('status'),
            add_blocked_by=tool_input.get('addBlockedBy'),
            remove_blocked_by=tool_input.get('removeBlockedBy'),
            owner=tool_input.get('owner'),
            description=tool_input.get('description'),
            worktree=tool_input.get('worktree'),
        ) if task_manager is not None else 'Error: Task manager unavailable',
        'task_list': lambda: task_manager.list_all(str(tool_input.get('filter', 'all'))) if task_manager is not None else 'Error: Task manager unavailable',
        'task_get': lambda: task_manager.get(int(tool_input['task_id'])) if task_manager is not None else 'Error: Task manager unavailable',
        'task_archive': lambda: task_manager.archive(int(tool_input['task_id'])) if task_manager is not None else 'Error: Task manager unavailable',
        'task_delete': lambda: task_manager.delete(int(tool_input['task_id'])) if task_manager is not None else 'Error: Task manager unavailable',
        'task_prune_completed': lambda: task_manager.prune_completed(
            int(tool_input.get('olderThanDays', 30)),
            archive=bool(tool_input.get('archive', True)),
        ) if task_manager is not None else 'Error: Task manager unavailable',
        'task_bind_worktree': lambda: task_manager.bind_worktree(
            int(tool_input['task_id']),
            str(tool_input['worktree']),
            str(tool_input.get('owner', '')),
        ) if task_manager is not None else 'Error: Task manager unavailable',
        'claim_task': lambda: task_manager.claim(int(tool_input['task_id']), current_agent_name) if task_manager is not None else 'Error: Task manager unavailable',
        'background_run': lambda: background_manager.run(str(tool_input['command'])) if background_manager is not None else 'Error: Background manager unavailable',
        'check_background': lambda: background_manager.check(tool_input.get('task_id')) if background_manager is not None else 'Error: Background manager unavailable',
        'spawn_teammate': lambda: teammate_manager.spawn(str(tool_input['name']), str(tool_input['role']), str(tool_input['prompt'])) if teammate_manager is not None else 'Error: Teammate manager unavailable',
        'list_teammates': lambda: teammate_manager.list_all() if teammate_manager is not None else 'Error: Teammate manager unavailable',
        'send_message': lambda: message_bus.send(
            current_agent_name,
            str(tool_input['to']),
            str(tool_input['content']),
            str(tool_input.get('msg_type', 'message')),
        ) if message_bus is not None else 'Error: Message bus unavailable',
        'read_inbox': lambda: json.dumps(message_bus.read_inbox(current_agent_name), ensure_ascii=False, indent=2) if message_bus is not None else 'Error: Message bus unavailable',
        'broadcast': lambda: message_bus.broadcast(
            current_agent_name,
            str(tool_input['content']),
            teammate_manager.member_names() if teammate_manager is not None else [],
        ) if message_bus is not None and teammate_manager is not None else 'Error: Team broadcast unavailable',
        'shutdown_request': handle_shutdown_request,
        'shutdown_response': handle_shutdown_status if current_agent_name == 'lead' and 'approve' not in tool_input else handle_shutdown_reply,
        'plan_approval': handle_plan_review if current_agent_name == 'lead' and 'approve' in tool_input else handle_plan_submit,
        'list_plan_requests': lambda: protocol_registry.list_plan_requests(str(tool_input.get('filter', 'all'))) if protocol_registry is not None else 'Error: Plan approval protocol unavailable',
        'idle': lambda: 'Entering idle phase.' if current_agent_name != 'lead' else 'Lead does not idle.',
        'worktree_create': lambda: worktree_manager.create(
            str(tool_input['name']),
            int(tool_input['task_id']) if 'task_id' in tool_input and tool_input.get('task_id') is not None else None,
            str(tool_input.get('base_ref', 'HEAD')),
        ) if worktree_manager is not None else 'Error: Worktree manager unavailable',
        'worktree_list': lambda: worktree_manager.list_all() if worktree_manager is not None else 'Error: Worktree manager unavailable',
        'worktree_status': lambda: worktree_manager.status(str(tool_input['name'])) if worktree_manager is not None else 'Error: Worktree manager unavailable',
        'worktree_run': lambda: worktree_manager.run(str(tool_input['name']), str(tool_input['command'])) if worktree_manager is not None else 'Error: Worktree manager unavailable',
        'worktree_keep': lambda: worktree_manager.keep(str(tool_input['name'])) if worktree_manager is not None else 'Error: Worktree manager unavailable',
        'worktree_remove': lambda: worktree_manager.remove(
            str(tool_input['name']),
            force=bool(tool_input.get('force', False)),
            complete_task=bool(tool_input.get('complete_task', False)),
        ) if worktree_manager is not None else 'Error: Worktree manager unavailable',
        'worktree_events': lambda: worktree_manager.list_events(int(tool_input.get('limit', 20))) if worktree_manager is not None else 'Error: Worktree manager unavailable',
    }
    handler = handlers.get(name)
    if handler is None:
        return f'Unknown tool: {name}'
    return handler()
