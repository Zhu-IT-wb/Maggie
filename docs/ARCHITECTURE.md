# Maggie Architecture

## 1. Overview

Maggie is a local coding-agent runtime with a single public entrypoint: `agents/main.py`.
The runtime is built around one principle:

- **Control plane** coordinates long-lived state and collaboration.
- **Execution plane** performs concrete work in isolated lanes.

This split keeps multi-step tasks stable across long sessions, retries, and parallel execution.

## 2. High-Level Design

```text
+------------------------+        +-----------------------------+
|      User (CLI)        |        |        External LLM         |
|   python agents/main   |<------>|   ChatClient (tool-calling) |
+-----------+------------+        +---------------+-------------+
            |                                     ^
            v                                     |
+-----------+-------------------------------------+-------------+
|                  Agent Loop (s12 runtime)                     |
|  - message history / tool dispatch / compact / resume         |
+-----------+-------------------------+--------------------------+
            |                         |
            v                         v
+-----------+------------+   +--------+--------------------------+
|      Control Plane     |   |          Execution Plane          |
|  - TaskManager         |   |  - shell/read/write/edit tools    |
|  - SessionStore        |   |  - subagent (one-off delegation)  |
|  - Team protocols      |   |  - background jobs                |
|  - Worktree index      |   |  - worktree_run in isolated dirs  |
+-----------+------------+   +--------+--------------------------+
            |                         |
            v                         v
+-----------+-------------------------------------+--------------+
|                 Persistent Local Storage                        |
|  .sessions/   .tasks/   .team/   .worktrees/   .transcripts/  |
+---------------------------------------------------------------+
```

## 3. Runtime Flow

1. `agents/main.py` imports and runs `s12_worktree_task_isolation.main()`.
2. The loop initializes managers: session, tasks, background, team, protocols, worktrees.
3. User input is appended to message history.
4. The model decides whether to answer directly or call tools.
5. Tool results are appended back into history as structured tool results.
6. Optional compaction runs when context grows large.
7. Session state is persisted each turn for `/resume` and export.

## 4. Control Plane Components

### SessionStore
- Persists conversation state in `.sessions/`.
- Supports `latest` tracking, explicit resume, export, and cleanup.

### TaskManager
- Persists long-horizon tasks in `.tasks/`.
- Supports dependencies, archive/delete/prune, and worktree binding.

### MessageBus + ProtocolRegistry
- `MessageBus`: teammate inbox transport via JSONL files.
- `ProtocolRegistry`: structured state machine for approvals and shutdown flows.

### Worktree Metadata
- `.worktrees/index.json` tracks lane identity, status, branch, and task binding.
- `.worktrees/events.jsonl` records lifecycle events for observability.

## 5. Execution Plane Components

### Base Tooling
- `shell`, `read_file`, `write_file`, `edit_file`
- Windows-aware command normalization and basic safety guards.

### Delegation Modes
- `task`: one-off subagent with fresh context.
- `spawn_teammate`: persistent teammate with inbox and protocol support.

### Async Work
- `background_run` and `check_background` decouple long-running commands from the foreground loop.

### Directory Isolation
- `worktree_create`, `worktree_run`, `worktree_status`, `worktree_keep`, `worktree_remove`
- Enables parallel or risky tasks to run in isolated directories.

## 6. Key Principle: Control by Task, Execute by Lane

Maggie treats persistent tasks as the source of truth and worktrees as isolated execution lanes:

- Tasks define intent, ownership, dependencies, and lifecycle.
- Worktrees provide safe parallel execution boundaries.
- Binding a task to a worktree links planning state with filesystem execution.

This is the core mechanism that allows Maggie to scale from single-task chat behavior to multi-task local engineering workflows.

## 7. Current Entry and Recommended Usage

- **Public entrypoint**: `agents/main.py`
- **Runtime core**: `agents/s12_worktree_task_isolation.py`
- **Recommended mode**: run from repository root and keep task/worktree state persistent.

```powershell
python agents\main.py
```

## 8. Known Boundaries

- Policy enforcement is still model-assisted in several flows (not fully hard-gated).
- Safety controls are practical but not equivalent to OS-level sandboxing.
- Designed for local single-operator workflows, not a distributed multi-node orchestrator.
