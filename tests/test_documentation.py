"""Regression checks for project-wide documentation coverage."""

import ast
from pathlib import Path


def test_every_class_and_function_has_a_docstring() -> None:
    """Require documentation blocks on source, migration, and test definitions."""

    project_root = Path(__file__).parents[1]
    missing: list[str] = []

    for relative_root in ("src", "migrations", "tests"):
        for path in sorted((project_root / relative_root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                    and ast.get_docstring(node, clean=False) is None
                ):
                    relative_path = path.relative_to(project_root)
                    missing.append(f"{relative_path}:{node.lineno} {node.name}")

    assert not missing, "Missing documentation blocks:\n" + "\n".join(missing)
