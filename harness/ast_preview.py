"""AST structural preview for hash edits (opt-in, Python-only, stdlib ast).

Before/after a hash edit is applied to a Python file, this module reports
what changed structurally: functions/classes added, removed, or with a
changed argument signature. It never raises -- unparseable input on either
side yields {"available": False}. Enabled with HARNESS_AST_PREVIEW=1.
"""
from __future__ import annotations

import ast
import os
from typing import Any, Dict


def ast_preview_enabled() -> bool:
    """Feature flag: AST previews are opt-in via HARNESS_AST_PREVIEW."""
    return os.environ.get("HARNESS_AST_PREVIEW", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _signature_of(node: ast.AST) -> tuple:
    """Comparable signature for a def; classes compare by base-name list."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = node.args
        names = (
            [a.arg for a in getattr(args, "posonlyargs", [])]
            + [a.arg for a in args.args]
            + ([args.vararg.arg] if args.vararg else [])
            + [a.arg for a in args.kwonlyargs]
            + ([args.kwarg.arg] if args.kwarg else [])
        )
        return (
            "func",
            tuple(names),
            len(args.defaults),
            len([d for d in args.kw_defaults if d is not None]),
        )
    if isinstance(node, ast.ClassDef):
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(base.attr)
        return ("class", tuple(bases))
    return ("other",)


def _collect_symbols(tree: ast.AST) -> Dict[str, tuple]:
    """Dotted-path -> signature for every function/class def in the tree."""
    symbols: Dict[str, tuple] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                path = f"{prefix}.{child.name}" if prefix else child.name
                symbols[path] = _signature_of(child)
                walk(child, path)
            else:
                walk(child, prefix)

    walk(tree, "")
    return symbols


def structural_diff(before: str, after: str) -> Dict[str, Any]:
    """Structural difference between two Python sources. Never raises."""
    try:
        before_tree = ast.parse(before or "")
        after_tree = ast.parse(after or "")
    except Exception:
        return {"available": False}
    try:
        old = _collect_symbols(before_tree)
        new = _collect_symbols(after_tree)
    except Exception:
        return {"available": False}

    added = sorted(name for name in new if name not in old)
    removed = sorted(name for name in old if name not in new)
    changed = sorted(
        name for name in new if name in old and new[name] != old[name]
    )
    return {
        "available": True,
        "added": added,
        "removed": removed,
        "changed": changed,
    }
