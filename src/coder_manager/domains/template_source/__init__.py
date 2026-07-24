"""Public API for retrieving and archiving Git-backed templates."""

from coder_manager.domains.template_source.errors import TemplateSourceError
from coder_manager.domains.template_source.service import (
    TEMPLATE_ARCHIVE_LIMIT,
    TemplateArchive,
    archive_template_directory,
    fetch_branch_archive,
    git_host,
)

__all__ = [
    "TEMPLATE_ARCHIVE_LIMIT",
    "TemplateArchive",
    "TemplateSourceError",
    "archive_template_directory",
    "fetch_branch_archive",
    "git_host",
]
