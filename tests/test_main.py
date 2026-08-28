"""FastAPI application configuration regression tests."""

import pytest

from coder_manager.config import Settings, get_settings
from coder_manager.main import create_app


@pytest.mark.parametrize(
    ("configured_domain", "expected_domain"),
    [
        ("EMEA.CODE-STUDIO.ECHONET", "emea.code-studio.echonet"),
        (" apac.coder-studio.echonet ", "apac.coder-studio.echonet"),
    ],
)
def test_instance_base_domain_is_normalized_for_public_hostnames(
    configured_domain: str,
    expected_domain: str,
) -> None:
    """Normalize the complete instance base domain."""

    settings = Settings(instance_base_domain=configured_domain)

    assert settings.require_instance_base_domain() == expected_domain


def test_instance_base_domain_accepts_the_longest_complete_public_hostname() -> None:
    """Reserve enough DNS hostname space for the fixed-length slug and separator."""

    base_domain = f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 48}"

    assert len(base_domain) == 240
    assert Settings(instance_base_domain=base_domain).require_instance_base_domain() == base_domain


@pytest.mark.parametrize("configured_domain", [None, "", "   "])
def test_instance_base_domain_is_required(configured_domain: str | None) -> None:
    """Reject a missing base domain before an instance URL can be built."""

    settings = Settings(instance_base_domain=configured_domain)

    with pytest.raises(ValueError, match="CODER_MANAGER_INSTANCE_BASE_DOMAIN is required"):
        settings.require_instance_base_domain()


@pytest.mark.parametrize(
    "configured_domain",
    [
        "single-label",
        "-emea.code-studio.echonet",
        "emea-.code-studio.echonet",
        "emea..code-studio.echonet",
        "emea_code-studio.echonet",
        "émea.code-studio.echonet",
        "https://emea.code-studio.echonet",
        "emea.code-studio.echonet/path",
        "emea.code-studio.echonet:443",
        f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 49}",
        f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 62}.echonet",
    ],
)
def test_instance_base_domain_must_be_a_dns_name(configured_domain: str) -> None:
    """Reject values that are not complete DNS names."""

    settings = Settings(instance_base_domain=configured_domain)

    with pytest.raises(
        ValueError,
        match="CODER_MANAGER_INSTANCE_BASE_DOMAIN must be a valid DNS name",
    ):
        settings.require_instance_base_domain()


@pytest.mark.parametrize(
    ("configured_domain", "expected_message"),
    [
        (None, "CODER_MANAGER_INSTANCE_BASE_DOMAIN is required"),
        (
            "invalid_domain",
            "CODER_MANAGER_INSTANCE_BASE_DOMAIN must be a valid DNS name",
        ),
    ],
)
def test_api_startup_rejects_an_invalid_instance_base_domain(
    configured_domain: str | None,
    expected_message: str,
) -> None:
    """Fail while constructing the API when its public base domain is invalid."""

    with pytest.raises(ValueError, match=expected_message):
        create_app(settings=Settings(instance_base_domain=configured_domain))


def test_api_startup_requires_a_deployment_environment() -> None:
    """Reject an API process that has no infrastructure environment."""

    with pytest.raises(ValueError, match="CODER_MANAGER_ENVIRONMENT is required"):
        create_app(
            settings=Settings(
                environment=None,
                instance_base_domain="emea.code-studio.echonet",
            )
        )


def test_create_app_injects_the_settings_it_validated() -> None:
    """Use one settings object consistently for startup and request dependencies."""

    settings = Settings(
        instance_base_domain="apac.coder-studio.echonet",
        allow_unauthenticated_api=True,
    )
    application = create_app(settings=settings)

    settings_dependency = application.dependency_overrides[get_settings]

    assert settings_dependency() is settings
