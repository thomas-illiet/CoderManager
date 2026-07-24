"""Secure Git source and Coder-compatible template archive tests."""

import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from coder_manager.domains.template_source import (
    TEMPLATE_ARCHIVE_LIMIT,
    TemplateSourceError,
    archive_template_directory,
    fetch_branch_archive,
    service,
)


def test_archive_filters_sensitive_and_hidden_files(tmp_path: Path) -> None:
    """Match the Coder CLI exclusions while retaining the Terraform lockfile."""

    (tmp_path / "main.tf").write_text('terraform { required_version = ">= 1.0" }\n')
    (tmp_path / ".terraform.lock.hcl").write_text("lock")
    (tmp_path / ".secret").write_text("secret")
    (tmp_path / "terraform.tfvars").write_text('token = "secret"')
    (tmp_path / "local.auto.tfvars.json").write_text("{}")
    (tmp_path / "terraform.tfstate.backup").write_text("state")
    nested = tmp_path / "modules"
    nested.mkdir()
    (nested / "module.tf").write_text('resource "null_resource" "example" {}')
    hidden = tmp_path / ".terraform"
    hidden.mkdir()
    (hidden / "plugin").write_text("binary")

    content = archive_template_directory(tmp_path)

    with tarfile.open(fileobj=BytesIO(content), mode="r:") as archive:
        names = set(archive.getnames())
    assert "main.tf" in names
    assert ".terraform.lock.hcl" in names
    assert "modules/module.tf" in names
    assert ".secret" not in names
    assert "terraform.tfvars" not in names
    assert "local.auto.tfvars.json" not in names
    assert "terraform.tfstate.backup" not in names
    assert ".terraform/plugin" not in names
    assert len(content) <= TEMPLATE_ARCHIVE_LIMIT


def test_archive_requires_root_terraform_and_enforces_limit(tmp_path: Path) -> None:
    """Reject non-template directories and archives larger than Coder accepts."""

    (tmp_path / "README.md").write_text("not terraform")
    with pytest.raises(TemplateSourceError, match="no root Terraform"):
        archive_template_directory(tmp_path)

    (tmp_path / "main.tf").write_bytes(b"x" * TEMPLATE_ARCHIVE_LIMIT)
    with pytest.raises(TemplateSourceError, match="1 MiB"):
        archive_template_directory(tmp_path)


def test_fetch_targets_exact_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fetch only refs/heads and archive the configured repository subdirectory."""

    calls: list[list[str]] = []
    commit = "a" * 40

    def fake_git(arguments: list[str], *, cwd: Path) -> str:
        """Record Git arguments and materialize the checkout at the expected step."""

        calls.append(arguments)
        if arguments[0] == "checkout":
            source = cwd / "templates" / "python"
            source.mkdir(parents=True)
            (source / "main.tf").write_text("terraform {}")
        if arguments[0] == "rev-parse":
            return commit
        return ""

    monkeypatch.setattr(service, "_run_git", fake_git)
    archive = fetch_branch_archive(
        "git@git.example.com:group/template.git",
        "feature/python",
        "templates/python",
    )

    assert archive.commit == commit
    assert any("refs/heads/feature/python" in arguments for arguments in calls)
    assert all("refs/tags/" not in " ".join(arguments) for arguments in calls)
