#!/usr/bin/env python3
"""Thin shell for the dependency gate (plan_no_venv_B, V4a).

All measurement logic lives in stt_button.deps_gate; this entry point
only forwards to it so the historical tools/ invocation keeps working.
"""

import sys

from stt_button.deps_gate import main

if __name__ == "__main__":
    sys.exit(main())
