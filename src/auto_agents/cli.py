"""CLI entrypoint, with optional independent supervision."""

from .bootstrap import main
from .control.arguments import build_parser

__all__ = ["main", "build_parser"]
