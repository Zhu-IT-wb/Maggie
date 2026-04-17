---
name: repo-map
description: Quickly map modules, responsibilities, and execution flow in an unfamiliar repository.
tags: analysis, onboarding
---
When mapping a repository:
1. Start from the entrypoint and list the modules it imports.
2. Separate orchestration code from state management and side-effect code.
3. Summarize which files own prompts, tool dispatch, and model I/O.
4. Keep the output compact enough that a parent agent can act on it immediately.