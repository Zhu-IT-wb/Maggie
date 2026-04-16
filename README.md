# Maggie

Maggie is a personal coding agent project built step by step in the same spirit as `learn-claude-code`, but with a cleaner project structure and a provider-agnostic model client.

## Scope

The project is intentionally staged:

1. `s01_agent_loop.py`: minimal agent loop
2. `s02_tool_use.py`: tool dispatch
3. `s03_todo_write.py`: task tracking
4. `s04_subagent.py`: subagents
5. `s05+`: context, skills, tasks, teamwork, isolation

## Run

1. Copy `.env.example` to `.env`
2. Fill in your API key and model
3. Run:

```powershell
python agents\s01_agent_loop.py
```
