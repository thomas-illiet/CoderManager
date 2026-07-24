"""Coder template request and response schemas."""

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from coder_manager.models import TemplateScope
from coder_manager.schemas.application_identifier import ApplicationIdentifier

NonEmptyString = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
GitUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
CoderName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
SourcePath = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]
BranchName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
ModuleName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
ModuleList = Annotated[list[ModuleName], Field(min_length=1)]
PositiveInteger = Annotated[int, Field(gt=0)]
CODER_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
SCP_GIT_URL_PATTERN = re.compile(
    r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^\s]+)$"
)
INVALID_GIT_REF_CHARACTERS = frozenset(" ~^:?*[\\")
ASCII_CONTROL_LIMIT = 32
ASCII_DELETE = 127


class TemplateMutableFields(BaseModel):
    """Validated fields that can be replaced on an existing template."""

    model_config = ConfigDict(extra="forbid")

    name: NonEmptyString
    git_url: GitUrl
    source_path: SourcePath = "."
    branch: BranchName
    modules: ModuleList
    min_cpu_count: PositiveInteger
    max_cpu_count: PositiveInteger
    min_ram_gb: PositiveInteger
    max_ram_gb: PositiveInteger
    min_disk_gb: PositiveInteger
    max_disk_gb: PositiveInteger

    @field_validator("git_url")
    @classmethod
    def validate_git_url(cls, value: str) -> str:
        """Require an absolute HTTPS URL without contacting the repository."""

        parsed = urlsplit(value)
        is_https = (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
        )
        is_ssh = (
            parsed.scheme == "ssh"
            and parsed.hostname is not None
            and parsed.username is not None
            and parsed.password is None
            and bool(parsed.path.strip("/"))
        )
        is_scp = SCP_GIT_URL_PATTERN.fullmatch(value) is not None
        if not (is_https or is_ssh or is_scp):
            msg = "git_url must be an HTTPS, ssh://, or user@host:path Git URL"
            raise ValueError(msg)
        return value

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        """Normalize a repository-relative POSIX directory without traversal."""

        if "\\" in value:
            msg = "source_path must use POSIX separators"
            raise ValueError(msg)
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            msg = "source_path must remain inside the repository"
            raise ValueError(msg)
        normalized = str(path)
        if normalized in {"", "/"}:
            return "."
        return normalized

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        """Accept one exact Git branch name and reject option-like or unsafe refs."""

        components = value.split("/")
        invalid = (
            value.startswith(("-", "/"))
            or value.endswith(("/", ".", ".lock"))
            or "//" in value
            or ".." in value
            or "@{" in value
            or any(character in INVALID_GIT_REF_CHARACTERS for character in value)
            or any(
                not component or component.startswith(".") or component.endswith(".lock")
                for component in components
            )
            or any(
                ord(character) < ASCII_CONTROL_LIMIT or ord(character) == ASCII_DELETE
                for character in value
            )
        )
        if invalid:
            msg = "branch must be a valid Git branch name"
            raise ValueError(msg)
        return value

    @field_validator("modules")
    @classmethod
    def validate_modules_are_unique(cls, value: list[str]) -> list[str]:
        """Reject duplicate normalized module names while retaining their order."""

        if len(value) != len(set(value)):
            msg = "modules must not contain duplicates"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_resource_ranges(self) -> Self:
        """Require coherent inclusive resource ranges."""

        ranges = (
            ("cpu", self.min_cpu_count, self.max_cpu_count),
            ("ram", self.min_ram_gb, self.max_ram_gb),
            ("disk", self.min_disk_gb, self.max_disk_gb),
        )
        for resource, minimum, maximum in ranges:
            if minimum > maximum:
                msg = f"min_{resource} must be less than or equal to max_{resource}"
                raise ValueError(msg)
        return self


class TemplateCreate(TemplateMutableFields):
    """Payload accepted when creating a Coder template."""

    coder_name: CoderName
    scope: TemplateScope
    application: ApplicationIdentifier | None = None

    @field_validator("coder_name")
    @classmethod
    def normalize_coder_name(cls, value: str) -> str:
        """Normalize and validate the stable technical Coder template name."""

        normalized = value.lower()
        if CODER_NAME_PATTERN.fullmatch(normalized) is None:
            msg = "coder_name must be a lowercase Coder slug"
            raise ValueError(msg)
        return normalized

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        """Ensure the application reference agrees with the selected scope."""

        if self.scope is TemplateScope.GLOBAL and self.application is not None:
            msg = "application must be null for a global template"
            raise ValueError(msg)
        if self.scope is TemplateScope.APPLICATION and self.application is None:
            msg = "application is required for an application template"
            raise ValueError(msg)
        return self


class TemplateUpdate(TemplateMutableFields):
    """Complete replacement of a template's mutable fields."""


class TemplateRead(BaseModel):
    """Coder template representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    coder_name: str
    scope: TemplateScope
    application: str | None
    git_url: str
    source_path: str
    branch: str
    modules: list[str]
    min_cpu_count: int
    max_cpu_count: int
    min_ram_gb: int
    max_ram_gb: int
    min_disk_gb: int
    max_disk_gb: int
    created_at: datetime
    updated_at: datetime


class TemplateListQuery(BaseModel):
    """Validated filters and pagination for the template list."""

    model_config = ConfigDict(extra="forbid")

    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=100)] = 20
    scope: TemplateScope | None = None
    application: ApplicationIdentifier | None = None
    name: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ] = None


class TemplatePage(BaseModel):
    """A page of Coder templates with pagination metadata."""

    items: list[TemplateRead]
    page: int
    page_size: int
    total: int
    pages: int
