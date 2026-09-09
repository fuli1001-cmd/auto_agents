"""Ensure the src/ package wins over the root launcher shim on sys.path.

``python -m pytest`` from the repository root puts the root directory first on
``sys.path``; the root-level ``auto_agents.py`` launcher shim would then shadow
the real ``src/auto_agents`` package. Prefer an explicitly selected package root
when a verification driver supplies one (for example, a base revision), and
otherwise use this checkout's ``src`` for ordinary local pytest invocations.
"""

import sys
import site
from pathlib import Path

# A driver's explicit source precedes site-packages. An editable installation
# added by a .pth file is only a default and must not override this checkout.
_SITE_DIRS = {Path(entry).resolve() for entry in [*site.getsitepackages(), site.getusersitepackages()]}
_EXPLICIT_PATHS = []
for _ENTRY in sys.path:
    if Path(_ENTRY).resolve() in _SITE_DIRS:
        break
    _EXPLICIT_PATHS.append(_ENTRY)
_SRC = next(
    (entry for entry in _EXPLICIT_PATHS
     if (Path(entry) / "auto_agents" / "__init__.py").is_file()),
    str(Path(__file__).resolve().parent / "src"),
)
while _SRC in sys.path:
    sys.path.remove(_SRC)
sys.path.insert(0, _SRC)
