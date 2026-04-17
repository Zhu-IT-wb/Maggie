---
name: refactor-safe
description: Restructure code while preserving behavior, controlling risk, and making review easier.
tags: refactor, maintainability
---
When refactoring:
1. Preserve behavior first. Structural cleanup is the goal; silent behavior drift is a bug.
2. Split the work into mechanical moves and logic changes. Do not mix them unless necessary.
3. Keep names, boundaries, and ownership clearer after the change than before.
4. Call out risky areas explicitly: shared state, I/O boundaries, concurrency, serialization, and public interfaces.
5. If verification is incomplete, say exactly what was not validated.

