#!/usr/bin/env python3
"""Launcher so you can run it by its name:  py vle.py "your question"

All the real code lives in agent.py — this file only forwards to it.
"""

import sys

from agent import main

if __name__ == "__main__":
    raise SystemExit(main())
