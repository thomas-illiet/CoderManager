"""Regression checks for project-wide documentation coverage."""

import ast
import re
from pathlib import Path

import yaml

from coder_manager.config import Settings


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


def test_environment_example_covers_settings_and_compose_consumers() -> None:
    """Keep every runtime setting documented and injected into its exact consumers."""

    project_root = Path(__file__).parents[1]
    example_text = (project_root / ".env.example").read_text(encoding="utf-8")
    compose_text = (project_root / "compose.yaml").read_text(encoding="utf-8")
    example_variables = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example_text, re.MULTILINE))
    settings_variables = {
        f"CODER_MANAGER_{field_name.upper()}" for field_name in Settings.model_fields
    }
    direct_runtime_variables = {"FLOWER_UNAUTHENTICATED_API", "PROMETHEUS_MULTIPROC_DIR"}
    compose_inputs = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_text))

    assert example_variables == settings_variables | direct_runtime_variables
    assert compose_inputs <= example_variables

    section_names = {"COMMUN", "API", "WORKER", "BEAT", "MIGRATE", "FLOWER"}
    current_section: str | None = None
    common_consumers: set[str] = set()
    expected_consumers: dict[str, set[str]] = {}
    for line in example_text.splitlines():
        if line.startswith("# "):
            comment = line.removeprefix("# ")
            if comment in section_names:
                current_section = comment
            elif current_section == "COMMUN" and re.fullmatch(
                r"(?:api|worker|beat|migrate|flower)(?:, (?:api|worker|beat|migrate|flower))*",
                comment,
            ):
                common_consumers = set(comment.split(", "))
            continue
        if not line or "=" not in line:
            continue
        variable = line.partition("=")[0]
        assert current_section is not None
        if current_section == "COMMUN":
            assert common_consumers
            expected_consumers[variable] = common_consumers.copy()
        else:
            expected_consumers[variable] = {current_section.lower()}

    compose = yaml.safe_load(compose_text)
    actual_consumers = {
        variable: {
            service_name
            for service_name, service in compose["services"].items()
            if variable in (service.get("environment") or {})
        }
        for variable in example_variables
    }
    assert actual_consumers == expected_consumers


def test_removed_environment_configuration_is_not_documented() -> None:
    """Keep the removed deployment environment out of runtime documentation."""

    project_root = Path(__file__).parents[1]
    documented_files = (
        project_root / ".env.example",
        project_root / "compose.yaml",
        project_root / "README.md",
    )

    for path in documented_files:
        text = path.read_text(encoding="utf-8")
        assert "CODER_MANAGER_ENVIRONMENT" not in text

    readme = (project_root / "README.md").read_text(encoding="utf-8")
    assert "removes the obsolete `environment` label" in readme
    assert "environment ownership" not in readme
    assert "environment owner" not in readme
