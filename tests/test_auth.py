"""OIDC authentication and Swagger PKCE regression tests."""

import time
from collections.abc import AsyncIterator
from typing import Any, override
from unittest.mock import MagicMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from jwt import PyJWKClient, PyJWKClientConnectionError
from jwt.algorithms import RSAAlgorithm

from coder_manager import main as main_module
from coder_manager.auth import (
    AUTHENTICATION_ERROR,
    AUTHORIZATION_ERROR,
    PROVIDER_ERROR,
    OidcAuthenticator,
    OidcConfig,
    OidcConfigurationError,
    OidcProviderUnavailableError,
)
from coder_manager.config import Settings
from coder_manager.main import create_app

ISSUER = "https://auth.example.com/realms/coder"
CLIENT_ID = "coder-manager-swagger"
AUTHORIZATION_URL = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
MOCK_PROVIDER_UNAVAILABLE = "provider unavailable"


class FakeOidcProvider:
    """Serve deterministic discovery metadata and JWK client state."""

    def __init__(self, jwk_sets: list[list[dict[str, Any]]]) -> None:
        """Store the ordered JWK sets returned by successive requests."""

        self.issuer = ISSUER
        self.jwks_uri: object = JWKS_URL
        self.jwk_sets = jwk_sets
        self.jwks_calls = 0
        self.fail_discovery = False
        self.fail_jwks = False
        self.invalid_jwks = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Return the configured OIDC response for one mock request."""

        if request.url.path.endswith("/.well-known/openid-configuration"):
            if self.fail_discovery:
                raise httpx.ConnectError(MOCK_PROVIDER_UNAVAILABLE, request=request)
            return httpx.Response(
                200,
                json={"issuer": self.issuer, "jwks_uri": self.jwks_uri},
            )
        return httpx.Response(404)


class FakeJwkClient(PyJWKClient):
    """Feed PyJWKClient deterministic data without synchronous network calls."""

    def __init__(self, uri: str, provider: FakeOidcProvider) -> None:
        """Attach the fake client to one provider state."""

        super().__init__(uri, cache_keys=True)
        self.provider = provider

    @override
    def fetch_data(self) -> Any:
        """Return the next configured raw JWK set and update the real cache."""

        if self.provider.fail_jwks:
            raise PyJWKClientConnectionError(MOCK_PROVIDER_UNAVAILABLE)
        self.provider.jwks_calls += 1
        if self.provider.invalid_jwks:
            payload: object = {"keys": "invalid"}
        else:
            index = min(self.provider.jwks_calls - 1, len(self.provider.jwk_sets) - 1)
            payload = {"keys": self.provider.jwk_sets[index]}
        if self.jwk_set_cache is not None:
            self.jwk_set_cache.put(payload)
        return payload


def oidc_settings(**overrides: object) -> Settings:
    """Build complete OIDC settings with optional test overrides."""

    values: dict[str, object] = {
        "database_schema": "public",
        "oidc_issuer_url": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_authorization_url": AUTHORIZATION_URL,
        "oidc_token_url": TOKEN_URL,
        "oidc_scopes": "openid,profile",
        "oidc_username_claim": "preferred_username",
        "oidc_allowed_users": " Alice,BOB ",
    }
    values.update(overrides)
    return Settings(**values)


def rsa_signing_key(key_id: str) -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    """Generate one RSA private key and its public JWK representation."""

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"alg": "RS256", "kid": key_id, "use": "sig"})
    return private_key, jwk


def signed_token(
    private_key: rsa.RSAPrivateKey,
    key_id: str,
    *,
    removed_claims: tuple[str, ...] = (),
    **overrides: object,
) -> str:
    """Create one signed test access token."""

    claims: dict[str, object] = {
        "iss": ISSUER,
        "exp": int(time.time()) + 300,
        "preferred_username": "alice",
    }
    claims.update(overrides)
    for claim_name in removed_claims:
        claims.pop(claim_name, None)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": key_id})


async def initialized_authenticator(
    settings: Settings,
    provider: FakeOidcProvider,
) -> AsyncIterator[OidcAuthenticator]:
    """Yield an initialized authenticator backed by a fake OIDC provider."""

    config = OidcConfig.from_settings(settings)
    assert config is not None
    authenticator = authenticator_for(config, provider)
    await authenticator.initialize()
    yield authenticator


def authenticator_for(
    config: OidcConfig,
    provider: FakeOidcProvider,
) -> OidcAuthenticator:
    """Build an authenticator whose HTTP and JWK clients use one fake provider."""

    return OidcAuthenticator(
        config,
        transport=httpx.MockTransport(provider),
        jwk_client_factory=lambda uri: FakeJwkClient(uri, provider),
    )


def test_oidc_is_disabled_without_an_issuer() -> None:
    """Keep the API open when no OIDC issuer is configured."""

    assert OidcConfig.from_settings(Settings(oidc_issuer_url="")) is None


@pytest.mark.parametrize(
    ("field_name", "environment_name"),
    [
        ("oidc_client_id", "CODER_MANAGER_OIDC_CLIENT_ID"),
        ("oidc_authorization_url", "CODER_MANAGER_OIDC_AUTHORIZATION_URL"),
        ("oidc_token_url", "CODER_MANAGER_OIDC_TOKEN_URL"),
    ],
)
def test_enabled_oidc_requires_complete_settings(
    field_name: str,
    environment_name: str,
) -> None:
    """Reject partial OIDC configuration instead of starting an unusable API."""

    with pytest.raises(OidcConfigurationError, match=environment_name):
        OidcConfig.from_settings(oidc_settings(**{field_name: ""}))


@pytest.mark.parametrize(
    "field_name",
    ["oidc_issuer_url", "oidc_authorization_url", "oidc_token_url"],
)
def test_oidc_urls_require_https(field_name: str) -> None:
    """Reject clear-text OIDC endpoints."""

    with pytest.raises(OidcConfigurationError, match="must be a valid HTTPS URL"):
        OidcConfig.from_settings(
            oidc_settings(**{field_name: "http://auth.example.com/oidc"})
        )


def test_oidc_scopes_require_openid() -> None:
    """Require the OIDC scope in Swagger's OAuth2 request."""

    with pytest.raises(OidcConfigurationError, match="must contain openid"):
        OidcConfig.from_settings(oidc_settings(oidc_scopes="profile,email"))


def test_oidc_lists_reject_empty_items() -> None:
    """Reject ambiguous empty entries inside comma-separated OIDC settings."""

    with pytest.raises(OidcConfigurationError, match="contains an empty value"):
        OidcConfig.from_settings(oidc_settings(oidc_allowed_users="alice,,bob"))


@pytest.mark.asyncio
async def test_oidc_discovery_requires_the_exact_issuer() -> None:
    """Reject provider metadata for another issuer."""

    _, jwk = rsa_signing_key("key-1")
    provider = FakeOidcProvider([[jwk]])
    provider.issuer = "https://auth.example.com/realms/other"
    config = OidcConfig.from_settings(oidc_settings())
    assert config is not None
    authenticator = authenticator_for(config, provider)
    with pytest.raises(OidcConfigurationError, match="issuer does not match"):
        await authenticator.initialize()


@pytest.mark.asyncio
async def test_oidc_discovery_and_jwks_fail_closed() -> None:
    """Fail initialization when discovery or the initial JWK set is unavailable."""

    _, jwk = rsa_signing_key("key-1")
    config = OidcConfig.from_settings(oidc_settings())
    assert config is not None

    provider = FakeOidcProvider([[jwk]])
    provider.fail_discovery = True
    authenticator = authenticator_for(config, provider)
    with pytest.raises(OidcProviderUnavailableError, match=PROVIDER_ERROR):
        await authenticator.initialize()

    provider = FakeOidcProvider([[jwk]])
    provider.invalid_jwks = True
    authenticator = authenticator_for(config, provider)
    with pytest.raises(OidcConfigurationError, match="valid JWK set"):
        await authenticator.initialize()


@pytest.mark.asyncio
async def test_application_startup_initializes_oidc_before_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discover OIDC during application startup before exposing process metrics."""

    _, jwk = rsa_signing_key("key-1")
    provider = FakeOidcProvider([[jwk]])
    metrics_server = MagicMock()
    start_metrics_server = MagicMock(return_value=metrics_server)
    monkeypatch.setattr(main_module, "start_metrics_server", start_metrics_server)
    application = create_app(
        settings=oidc_settings(),
        oidc_authenticator_factory=lambda config: authenticator_for(config, provider),
    )

    async with application.router.lifespan_context(application):
        assert provider.jwks_calls == 1
        start_metrics_server.assert_called_once()

    metrics_server.stop.assert_called_once_with()


@pytest.mark.asyncio
async def test_valid_token_authorizes_case_insensitive_username() -> None:
    """Authorize a valid claim independently from username casing."""

    private_key, jwk = rsa_signing_key("key-1")
    provider = FakeOidcProvider([[jwk]])
    async for authenticator in initialized_authenticator(oidc_settings(), provider):
        token = signed_token(
            private_key,
            "key-1",
            aud="ignored-audience",
            preferred_username="aLiCe",
        )
        assert await authenticator.authorize(token) == "aLiCe"


@pytest.mark.asyncio
async def test_configurable_username_claim_is_used() -> None:
    """Authorize with the configured top-level username claim."""

    private_key, jwk = rsa_signing_key("key-1")
    provider = FakeOidcProvider([[jwk]])
    settings = oidc_settings(oidc_username_claim="login", oidc_allowed_users="carol")
    async for authenticator in initialized_authenticator(settings, provider):
        token = signed_token(private_key, "key-1", login="Carol")
        assert await authenticator.authorize(token) == "Carol"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        oidc_settings(oidc_allowed_users=""),
        oidc_settings(oidc_allowed_users="bob"),
    ],
)
async def test_valid_token_rejects_users_outside_the_allowlist(settings: Settings) -> None:
    """Return a stable forbidden response for empty and non-matching allowlists."""

    private_key, jwk = rsa_signing_key("key-1")
    provider = FakeOidcProvider([[jwk]])
    async for authenticator in initialized_authenticator(settings, provider):
        with pytest.raises(HTTPException) as caught:
            await authenticator.authorize(signed_token(private_key, "key-1"))
        assert caught.value.status_code == 403
        assert caught.value.detail == AUTHORIZATION_ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claims", "expected_status"),
    [
        ({"iss": "https://another.example.com"}, 401),
        ({"exp": 1}, 401),
        ({"preferred_username": 42}, 403),
        ({"removed_claims": ("preferred_username",)}, 403),
        ({"removed_claims": ("exp",)}, 401),
    ],
)
async def test_invalid_tokens_are_rejected(
    claims: dict[str, object],
    expected_status: int,
) -> None:
    """Reject invalid JWT claims and unusable usernames."""

    private_key, jwk = rsa_signing_key("key-1")
    provider = FakeOidcProvider([[jwk]])
    async for authenticator in initialized_authenticator(oidc_settings(), provider):
        with pytest.raises(HTTPException) as caught:
            await authenticator.authorize(signed_token(private_key, "key-1", **claims))
        assert caught.value.status_code == expected_status


@pytest.mark.asyncio
async def test_unknown_key_refreshes_jwks() -> None:
    """Refresh cached provider keys when a token contains a new key id."""

    _, first_jwk = rsa_signing_key("key-1")
    second_private_key, second_jwk = rsa_signing_key("key-2")
    provider = FakeOidcProvider([[first_jwk], [first_jwk, second_jwk]])
    async for authenticator in initialized_authenticator(oidc_settings(), provider):
        token = signed_token(second_private_key, "key-2")
        assert await authenticator.authorize(token) == "alice"
        assert provider.jwks_calls == 2


@pytest.mark.asyncio
async def test_unknown_key_returns_provider_error_when_refresh_fails() -> None:
    """Distinguish an unavailable JWKS refresh from an invalid token."""

    _, first_jwk = rsa_signing_key("key-1")
    second_private_key, _ = rsa_signing_key("key-2")
    provider = FakeOidcProvider([[first_jwk]])
    async for authenticator in initialized_authenticator(oidc_settings(), provider):
        provider.fail_jwks = True
        with pytest.raises(OidcProviderUnavailableError, match=PROVIDER_ERROR):
            await authenticator.authorize(signed_token(second_private_key, "key-2"))


@pytest.mark.asyncio
async def test_api_authentication_contract_and_provider_status() -> None:
    """Expose stable 401, 403, and 503 responses through protected API routes."""

    private_key, jwk = rsa_signing_key("key-1")
    unknown_private_key, _ = rsa_signing_key("key-2")
    provider = FakeOidcProvider([[jwk]])
    application = create_app(
        settings=oidc_settings(),
        oidc_authenticator_factory=lambda config: authenticator_for(config, provider),
    )
    authenticator = application.state.oidc_authenticator
    assert isinstance(authenticator, OidcAuthenticator)
    await authenticator.initialize()
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        missing = await client.get("/api/v1/jobs/not-a-uuid")
        assert missing.status_code == 401
        assert missing.json() == {"detail": AUTHENTICATION_ERROR}
        assert missing.headers["www-authenticate"] == "Bearer"

        forbidden_token = signed_token(
            private_key,
            "key-1",
            preferred_username="mallory",
        )
        forbidden = await client.get(
            "/api/v1/jobs/not-a-uuid",
            headers={"Authorization": f"Bearer {forbidden_token}"},
        )
        assert forbidden.status_code == 403
        assert forbidden.json() == {"detail": AUTHORIZATION_ERROR}

        allowed = await client.get(
            "/api/v1/jobs/not-a-uuid",
            headers={"Authorization": f"Bearer {signed_token(private_key, 'key-1')}"},
        )
        assert allowed.status_code == 422

        provider.fail_jwks = True
        unavailable = await client.get(
            "/api/v1/jobs/not-a-uuid",
            headers={
                "Authorization": f"Bearer {signed_token(unknown_private_key, 'key-2')}"
            },
        )
        assert unavailable.status_code == 503
        assert unavailable.json() == {"detail": PROVIDER_ERROR}


@pytest.mark.asyncio
async def test_swagger_is_open_and_has_no_security_when_oidc_is_disabled() -> None:
    """Keep existing open Swagger behavior when authentication is disabled."""

    application = create_app(settings=Settings(database_schema="public"))
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        document = (await client.get("/openapi.json")).json()
        assert "securitySchemes" not in document.get("components", {})
        assert (await client.get("/docs")).status_code == 200


@pytest.mark.asyncio
async def test_swagger_exposes_authorization_code_pkce_without_a_secret() -> None:
    """Describe OAuth2 and initialize Swagger as a public PKCE client."""

    application = create_app(settings=oidc_settings())
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        document = (await client.get("/openapi.json")).json()
        schemes = document["components"]["securitySchemes"]
        scheme_name, scheme = next(iter(schemes.items()))
        assert scheme == {
            "type": "oauth2",
            "flows": {
                "authorizationCode": {
                    "authorizationUrl": AUTHORIZATION_URL,
                    "tokenUrl": TOKEN_URL,
                    "scopes": {"openid": "openid", "profile": "profile"},
                }
            },
        }
        for path, operations in document["paths"].items():
            if not path.startswith("/api/v1"):
                continue
            for operation in operations.values():
                assert operation["security"] == [{scheme_name: []}]

        docs = (await client.get("/docs")).text
        assert f'"clientId": "{CLIENT_ID}"' in docs
        assert '"scopes": "openid profile"' in docs
        assert '"usePkceWithAuthorizationCodeGrant": true' in docs
        assert "clientSecret" not in docs
        assert (await client.get("/docs/oauth2-redirect")).status_code == 200
