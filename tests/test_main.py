"""FastAPI application configuration regression tests."""

import pytest

from coder_manager.config import Settings, get_settings
from coder_manager.main import create_app


@pytest.mark.parametrize(
    ("configured_region", "expected_region"),
    [
        ("EMEA", "emea"),
        (" apac ", "apac"),
        ("AmEr", "amer"),
    ],
)
def test_instance_region_is_normalized_for_public_hostnames(
    configured_region: str,
    expected_region: str,
) -> None:
    """Normalize the deployment region to one lowercase DNS label."""

    settings = Settings(argocd_region=configured_region)

    assert settings.require_instance_region() == expected_region


@pytest.mark.parametrize("configured_region", [None, "", "   "])
def test_instance_region_is_required(configured_region: str | None) -> None:
    """Reject a missing deployment region before an instance URL can be built."""

    settings = Settings(argocd_region=configured_region)

    with pytest.raises(ValueError, match="CODER_MANAGER_ARGOCD_REGION is required"):
        settings.require_instance_region()


@pytest.mark.parametrize(
    "configured_region",
    ["-emea", "emea-", "emea_west", "emea.west", "émea", "a" * 64],
)
def test_instance_region_must_be_a_dns_label(configured_region: str) -> None:
    """Reject deployment regions that cannot form one hostname label."""

    settings = Settings(argocd_region=configured_region)

    with pytest.raises(
        ValueError,
        match="CODER_MANAGER_ARGOCD_REGION must be a valid DNS label",
    ):
        settings.require_instance_region()


@pytest.mark.parametrize(
    ("configured_region", "expected_message"),
    [
        (None, "CODER_MANAGER_ARGOCD_REGION is required"),
        ("invalid_region", "CODER_MANAGER_ARGOCD_REGION must be a valid DNS label"),
    ],
)
def test_api_startup_rejects_an_invalid_instance_region(
    configured_region: str | None,
    expected_message: str,
) -> None:
    """Fail while constructing the API when its public-hostname region is invalid."""

    with pytest.raises(ValueError, match=expected_message):
        create_app(settings=Settings(argocd_region=configured_region))


def test_create_app_injects_the_settings_it_validated() -> None:
    """Use one settings object consistently for startup and request dependencies."""

    settings = Settings(argocd_region="APAC", instance_domain="coder-studio")
    application = create_app(settings=settings)

    settings_dependency = application.dependency_overrides[get_settings]

    assert settings_dependency() is settings
