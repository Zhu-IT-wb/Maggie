---
name: debug-root-cause
description: Diagnose failures by isolating reproduction steps, narrowing scope, and identifying the real cause before editing.
tags: debugging, diagnosis
---
When debugging:
1. Reproduce the failure first. If it is not reproducible, say that explicitly.
2. Identify the exact symptom: error message, wrong output, missing side effect, or bad state.
3. Narrow the scope before editing code. Prefer tracing the execution path, checking inputs, and comparing expected vs actual behavior.
4. Distinguish root cause from downstream breakage. Fix the earliest wrong assumption, not the loudest error.
5. Before changing code, summarize: reproduction, suspected root cause, and smallest safe fix.

