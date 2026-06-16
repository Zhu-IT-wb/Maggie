#!/usr/bin/env python3
from __future__ import annotations

try:
    from agents.s11_autonomous_agents import main
except ModuleNotFoundError:
    from s11_autonomous_agents import main


if __name__ == '__main__':
    main()
