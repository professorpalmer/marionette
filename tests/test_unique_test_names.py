"""A second def with the same name replaces the first, so pytest never runs the first."""

import ast
from pathlib import Path


def test_no_test_file_defines_a_test_name_twice():
    shadowed = []
    for path in sorted(Path(__file__).parent.glob("**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        for scope in scopes:
            seen = set()
            for node in scope.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                    if node.name in seen:
                        shadowed.append(f"{path.name}:{node.lineno} {node.name}")
                    seen.add(node.name)
    assert shadowed == []
