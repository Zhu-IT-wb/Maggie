---
name: code-review
description: Review code for bugs, regressions, risky assumptions, and missing tests.
tags: review, quality
---
When reviewing code:
1. Prioritize correctness, regressions, and hidden edge cases over style.
2. Look for mismatches between tool behavior and prompt expectations.
3. Call out missing validation, unsafe defaults, and state inconsistencies.
4. If no concrete bug is found, say so explicitly and mention residual risk.