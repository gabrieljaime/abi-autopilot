# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gabriel Jaime
"""Run ABI Autopilot from a source checkout: `python autopilot.py <command>`.

`config.local.json` and `runtime/` live next to this file. When installed with
pip/pipx, use the `abi-autopilot` command instead.
"""
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parent
sys.path.insert(0, str(HOME))

from abi_autopilot.cli import main  # noqa: E402

if __name__ == "__main__":
    main(home=HOME, prog="python autopilot.py")
