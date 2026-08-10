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


def test_environment_example_has_exact_service_categories_and_unique_variables() -> None:
    """Keep the environment template grouped without duplicate assignments."""

    project_root = Path(__file__).parents[1]
    lines = (project_root / ".env.example").read_text(encoding="utf-8").splitlines()
    categories = [
        line.removeprefix("# ")
        for line in lines
        if line.removeprefix("# ") in {"COMMUN", "API", "WORKER", "BEAT", "MIGRATE", "FLOWER"}
    ]
    variables = [
        line.partition("=")[0]
        for line in lines
        if line and not line.startswith("#") and "=" in line
    ]

    assert categories == ["COMMUN", "API", "WORKER", "BEAT", "MIGRATE", "FLOWER"]
    assert len(variables) == len(set(variables))
