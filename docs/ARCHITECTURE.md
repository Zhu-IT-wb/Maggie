# Maggie Architecture

## 1. Overview

Maggie is a local coding-agent runtime with a single public entrypoint: `agents/main.py`.

The runtime is built around one principle:

- **Control plane** coordinates long-lived state and collaboration.
- **Execution plane** performs concrete work through tools and delegated workers.

This split keeps multi-step tasks stable across long sessions, retries, and collaboration.

## 2. High-Level Design

```text
+------------------------+        +-----------------------------+
|      User (CLI)        |        |        External LLM         |
|   python agents/main   |<------>|   ChatClient (tool-calling) |
+-----------+------------+        +---------------+-------------+
            |                                     ^
            v                                     |
+-----------+-------------------------------------+-------------+
|                  Agent Loop (s11 runtime)                     |
|  - message history / tool dispatch / compact / resume         |
+-----------+-------------------------+--------------------------+
            |                         |
            v                         v
+-----------+------------+   +--------+--------------------------+
|      Control Plane     |   |          Execution Plane          |
|  - TaskManager         |   |  - shell/read/write/edit tools    |
|  - SessionStore        |   |  - subagent (one-off delegation)  |
|  - Team protocols      |   |  - background jobs                |
|  - teammate state      |   |  - autonomous teammate loops      |
+-----------+------------+   +--------+--------------------------+
            |                         |
            v                         v
+-----------+-------------------------------------+--------------+
|                 Persistent Local Storage                        |
|     .sessions/   .tasks/   .team/   .transcripts/             |
+---------------------------------------------------------------+
```

## 3. Runtime Flow

1. `agents/main.py` imports and runs `s11_autonomous_agents.main()`.
2. The loop initializes managers: session, tasks, background, team, and protocols.
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
- Supports dependencies, archive/delete/prune, and ownership tracking.

### MessageBus + ProtocolRegistry

- `MessageBus`: teammate inbox transport via JSONL files.
- `ProtocolRegistry`: structured state machine for approvals and shutdown flows.

### Teammate State

- `.team/config.json` stores persistent teammate identity, role, and status.
- `AutonomousTeammateManager` supports idle polling and auto-claim behavior.

## 5. Execution Plane Components

### Base Tooling

- `shell`, `read_file`, `write_file`, `edit_file`
- Windows-aware command normalization and basic safety guards.

### Delegation Modes

- `task`: one-off subagent with fresh context.
- `spawn_teammate`: persistent teammate with inbox and protocol support.

### Async Work

- `background_run` and `check_background` decouple long-running commands from the foreground loop.

## 6. Key Principle: Persistent State Outside the Prompt

Maggie treats the prompt as working memory, not the source of truth:

- Tasks define intent, ownership, dependencies, and lifecycle.
- Sessions preserve long-running dialogue state across restarts and compaction.
- Team inboxes and protocols keep collaboration state outside the context window.

This is the core mechanism that allows Maggie to move beyond single-turn chat behavior and act more like a local engineering runtime.

## 7. Current Entry and Recommended Usage

- **Public entrypoint**: `agents/main.py`
- **Runtime core**: `agents/s11_autonomous_agents.py`
- **Recommended mode**: run from repository root and keep task/session state persistent.

```powershell
python agents\main.py
```

## 8. Known Boundaries

- Policy enforcement is still model-assisted in several flows (not fully hard-gated).
- Safety controls are practical but not equivalent to OS-level sandboxing.
- Designed for local single-operator workflows, not a distributed multi-node orchestrator.
