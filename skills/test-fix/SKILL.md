---
name: test-fix
description: Repair failing tests without papering over the underlying product behavior.
tags: testing, reliability
---
When fixing test failures:
1. Read the failing assertion and identify whether the product or the test is wrong.
2. Prefer fixing the implementation if the failure exposes a real regression.
3. Only update snapshots, assertions, or fixtures when the new behavior is intentional.
4. Keep the change set narrow. Avoid broad rewrites unless the failure proves the old structure is invalid.
5. End with a short note: what failed, what was changed, and whether residual test gaps remain.

