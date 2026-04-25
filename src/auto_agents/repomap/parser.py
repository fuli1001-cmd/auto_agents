"""Python AST-based code parser for the repo map.

Provides a small, dependency-free `BaseParser` interface and a
`PythonAstParser` implementation that extracts file-level summaries.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence


@dataclass
class Symbol:
    """A single top-level or nested symbol within a file."""

    kind: str               # "class" | "function" | "method"
    name: str
    signature: str          # rendered single-line signature
    docstring: str = ""     # first line of docstring, may be empty
    lineno: int = 0
    children: List["Symbol"] = field(default_factory=list)  # methods inside a class


@dataclass
class FileSummary:
    """Compact representation of one source file used by the ranker / builder."""

    path: str               # POSIX path relative to project root
    language: str = "python"
    symbols: List[Symbol] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)   # module names (best-effort)
    parse_error: Optional[str] = None
    sloc: int = 0           # naive line count for ranking signal


class BaseParser:
    """Pluggable parser interface. Future tree-sitter backends implement this."""

    extensions: Sequence[str] = ()

    def parse(self, project_root: Path, rel_path: str) -> FileSummary:
        raise NotImplementedError


def _format_arg(arg: ast.arg, default: Optional[ast.expr] = None) -> str:
    text = arg.arg
    if arg.annotation is not None:
        try:
            text += f": {ast.unparse(arg.annotation)}"
        except Exception:
            text += ": ?"
    if default is not None:
        try:
            text += f"={ast.unparse(default)}"
        except Exception:
            text += "=?"
    return text


def _format_arguments(args: ast.arguments) -> str:
    parts: List[str] = []
    posonly = list(getattr(args, "posonlyargs", []) or [])
    regular = list(args.args or [])
    all_pos = posonly + regular
    defaults = list(args.defaults or [])
    pad = len(all_pos) - len(defaults)
    for idx, arg in enumerate(all_pos):
        default = defaults[idx - pad] if idx >= pad else None
        parts.append(_format_arg(arg, default))
        if posonly and idx == len(posonly) - 1:
            parts.append("/")
    if args.vararg is not None:
        parts.append("*" + _format_arg(args.vararg))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs or [], args.kw_defaults or []):
        parts.append(_format_arg(arg, default))
    if args.kwarg is not None:
        parts.append("**" + _format_arg(args.kwarg))
    return ", ".join(parts)


def _format_returns(node: ast.AST) -> str:
    returns = getattr(node, "returns", None)
    if returns is None:
        return ""
    try:
        return f" -> {ast.unparse(returns)}"
    except Exception:
        return ""


def _first_doc_line(node: ast.AST) -> str:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
        return ""
    doc = ast.get_docstring(node, clean=True)
    if not doc:
        return ""
    return doc.splitlines()[0].strip()[:160]


def _function_signature(node: ast.AST, *, is_method: bool = False) -> str:
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    return f"{prefix}{node.name}({_format_arguments(node.args)}){_format_returns(node)}"


def _class_signature(node: ast.ClassDef) -> str:
    bases: List[str] = []
    for base in node.bases:
        try:
            bases.append(ast.unparse(base))
        except Exception:
            bases.append("?")
    base_text = f"({', '.join(bases)})" if bases else ""
    return f"class {node.name}{base_text}"


class PythonAstParser(BaseParser):
    extensions = (".py",)

    def parse(self, project_root: Path, rel_path: str) -> FileSummary:
        abs_path = (project_root / rel_path)
        summary = FileSummary(path=rel_path)
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            summary.parse_error = f"read_error: {exc}"
            return summary
        summary.sloc = text.count("\n") + 1
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            summary.parse_error = f"syntax_error: line {exc.lineno}: {exc.msg}"
            return summary

        # Collect imports (module names only, top-level statements)
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    summary.imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    summary.imports.append(node.module)

        # Top-level symbols
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                cls_sym = Symbol(
                    kind="class",
                    name=node.name,
                    signature=_class_signature(node),
                    docstring=_first_doc_line(node),
                    lineno=node.lineno,
                )
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # Skip private dunder noise except __init__
                        if child.name.startswith("_") and child.name not in ("__init__",):
                            continue
                        cls_sym.children.append(
                            Symbol(
                                kind="method",
                                name=child.name,
                                signature=_function_signature(child, is_method=True),
                                docstring=_first_doc_line(child),
                                lineno=child.lineno,
                            )
                        )
                summary.symbols.append(cls_sym)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                summary.symbols.append(
                    Symbol(
                        kind="function",
                        name=node.name,
                        signature=_function_signature(node),
                        docstring=_first_doc_line(node),
                        lineno=node.lineno,
                    )
                )

        return summary
