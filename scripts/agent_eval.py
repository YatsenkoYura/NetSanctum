#!/usr/bin/env python3
"""Print the agent cascade behavioural baseline from tests/eval_agent.py scenarios."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.eval_agent import main

if __name__ == "__main__":
    raise SystemExit(main())
