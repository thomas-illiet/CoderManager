"""Secure Git branch retrieval and Coder-compatible USTAR creation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from coder_manager.domains.template_source.errors import TemplateSourceError

TEMPLATE_ARCHIVE_LIMIT = 1 << 20
SCP_GIT_URL_PATTERN = re.compile(
    r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^\s]+)$"
)
GIT_SSH_COMMAND = (
    "ssh -oBatchMode=yes -oStrictHostKeyChecking=yes -oIdentitiesOnly=yes -oForwardAgent=no"
)


@dataclass(frozen=True, slots=True)
class TemplateArchive:
    """One resolved branch commit and its upload-ready USTAR bytes."""

    commit: str
    content: bytes


def git_host(git_url: str) -> str:
    """Return the normalized host from one supported remote URL."""

    parsed = urlsplit(git_url)
    if parsed.scheme in {"https", "ssh"} and parsed.hostname is not None:
        return parsed.hostname.lower()
    match = SCP_GIT_URL_PATTERN.fullmatch(git_url)
    if match is not None:
        return match.group("host").lower()
    msg = "Template Git URL is unsupported"
    raise TemplateSourceError(msg)


def _git_environment() -> dict[str, str]:
    """Build a non-interactive Git environment with fixed SSH safety controls."""

    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_SSH_COMMAND": GIT_SSH_COMMAND,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _run_git(arguments: list[str], *, cwd: Path) -> str:
    """Run Git without a shell and without surfacing remote output or credentials."""

    executable = shutil.which("git")
    if executable is None:
        msg = "Git executable is unavailable"
        raise TemplateSourceError(msg)
    try:
        completed = subprocess.run(  # noqa: S603
            [executable, *arguments],
            cwd=cwd,
            env=_git_environment(),
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        msg = "Template Git operation failed"
        raise TemplateSourceError(msg) from error
    return completed.stdout.strip()


def _skip_archive_file(relative_path: Path) -> bool:
    """Mirror Coder CLI exclusions for hidden, state, and variable files."""

    name = relative_path.name
    if any(part.startswith(".") for part in relative_path.parts) and name != ".terraform.lock.hcl":
        return True
    relative = relative_path.as_posix()
    if ".tfstate" in relative:
        return True
    return name in {"terraform.tfvars", "terraform.tfvars.json"} or name.endswith(
        (".auto.tfvars", ".auto.tfvars.json")
    )


def _write_archive_entries(archive: tarfile.TarFile, directory: Path) -> None:
    """Write filtered directory entries to one open USTAR archive."""

    for current, directory_names, file_names in os.walk(
        directory,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        visible_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = current_path / name
            relative = candidate.relative_to(directory)
            if any(part.startswith(".") for part in relative.parts):
                continue
            archive.add(candidate, arcname=relative.as_posix(), recursive=False)
            if not candidate.is_symlink():
                visible_directories.append(name)
        directory_names[:] = visible_directories
        for name in sorted(file_names):
            candidate = current_path / name
            relative = candidate.relative_to(directory)
            if _skip_archive_file(relative):
                continue
            archive.add(candidate, arcname=relative.as_posix(), recursive=False)


def archive_template_directory(directory: Path) -> bytes:
    """Create a bounded USTAR archive from one checked-out template directory."""

    if not directory.is_dir():
        msg = "Template source_path is not a directory"
        raise TemplateSourceError(msg)
    has_terraform = any(
        child.is_file() and child.name.endswith((".tf", ".tf.json"))
        for child in directory.iterdir()
    )
    if not has_terraform:
        msg = "Template source_path has no root Terraform files"
        raise TemplateSourceError(msg)

    output = BytesIO()
    try:
        with tarfile.open(
            fileobj=output,
            mode="w",
            format=tarfile.USTAR_FORMAT,
            dereference=False,
        ) as archive:
            _write_archive_entries(archive, directory)
    except (OSError, tarfile.TarError, ValueError) as error:
        msg = "Template archive could not be created"
        raise TemplateSourceError(msg) from error

    content = output.getvalue()
    if len(content) > TEMPLATE_ARCHIVE_LIMIT:
        msg = "Template archive exceeds the 1 MiB limit"
        raise TemplateSourceError(msg)
    return content


def fetch_branch_archive(
    git_url: str,
    branch: str,
    source_path: str,
    allowed_hosts: str,
) -> TemplateArchive:
    """Fetch one exact branch HEAD and return its Coder-compatible archive."""

    allowed = {host.strip().lower() for host in allowed_hosts.split(",") if host.strip()}
    host = git_host(git_url)
    if host not in allowed:
        msg = "Template Git host is not allowed"
        raise TemplateSourceError(msg)

    with tempfile.TemporaryDirectory(prefix="coder-manager-template-") as temporary:
        repository = Path(temporary) / "repository"
        repository.mkdir(mode=0o700)
        _run_git(["init", "--quiet"], cwd=repository)
        _run_git(["remote", "add", "origin", git_url], cwd=repository)
        _run_git(
            [
                "fetch",
                "--quiet",
                "--depth=1",
                "--no-tags",
                "origin",
                f"refs/heads/{branch}",
            ],
            cwd=repository,
        )
        _run_git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=repository)
        commit = _run_git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=repository)
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            msg = "Template Git branch did not resolve to a commit"
            raise TemplateSourceError(msg)

        source = (repository / source_path).resolve()
        repository_root = repository.resolve()
        if not source.is_relative_to(repository_root):
            msg = "Template source_path escapes the repository"
            raise TemplateSourceError(msg)
        return TemplateArchive(
            commit=commit,
            content=archive_template_directory(source),
        )
