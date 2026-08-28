"""Ensure the src/ package wins over the root launcher shim on sys.path.

``python -m pytest`` from the repository root puts the root directory first on
``sys.path``; the root-level ``auto_agents.py`` launcher shim would then shadow
the real ``src/auto_agents`` package. Prepending ``src`` here makes the natural
``python -m pytest -q tests`` invocation work without an editable install.
"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent / "src")
while _SRC in sys.path:
    sys.path.remove(_SRC)
sys.path.insert(0, _SRC)
