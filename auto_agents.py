from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
src_path = str(SRC)
if src_path in sys.path:
    sys.path.remove(src_path)
sys.path.insert(0, src_path)

from auto_agents.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
