"""Public CLI facade; no execution or recovery rules live here."""

from .control.cli import main
from .control.arguments import build_parser

__all__ = ["main", "build_parser"]
