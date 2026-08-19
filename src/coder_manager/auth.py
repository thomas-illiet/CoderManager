"""OIDC discovery, JWT validation, and role authorization."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
import jwt
from fastapi import HTTPException, Security, status
from fastapi.security import OAuth2AuthorizationCodeBearer
from jwt import (
    InvalidTokenError,
    PyJWKClient,
    PyJWKClientConnectionError,
    PyJWKClientError,
    PyJWKSetError,
)

from coder_manager.config import Settings

OIDC_TIMEOUT_SECONDS = 10.0
OIDC_ALGORITHM = "RS256"
OIDC_ROLES_CLAIM = "roles"
OIDC_REQUIRED_ROLE = "admin"
AUTHENTICATION_ERROR = "Invalid authentication credentials"
AUTHORIZATION_ERROR = "Access denied"
PROVIDER_ERROR = "OIDC provider unavailable"


class OidcConfigurationError(ValueError):
    """Report an invalid or incomplete OIDC configuration."""


class OidcProviderUnavailableError(RuntimeError):
    """Report an unavailable remote OIDC provider."""


@dataclass(frozen=True)
class OidcConfig:
    """Hold validated OIDC resource-server and Swagger settings."""

    issuer_url: str
    client_id: str
    authorization_url: str
    token_url: str
    scopes: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: Settings) -> "OidcConfig | None":
        """Return enabled OIDC configuration, or None when no issuer is configured."""

        issuer_url = _optional_value(settings.oidc_issuer_url)
        if issuer_url is None:
            return None
        return cls(
            issuer_url=_require_https_url(
                issuer_url,
                "CODER_MANAGER_OIDC_ISSUER_URL",
                allow_query=False,
            ),
            client_id=_required_value(settings.oidc_client_id, "CODER_MANAGER_OIDC_CLIENT_ID"),
            authorization_url=_require_https_url(
                _required_value(
                    settings.oidc_authorization_url,
                    "CODER_MANAGER_OIDC_AUTHORIZATION_URL",
                ),
                "CODER_MANAGER_OIDC_AUTHORIZATION_URL",
            ),
            token_url=_require_https_url(
                _required_value(settings.oidc_token_url, "CODER_MANAGER_OIDC_TOKEN_URL"),
                "CODER_MANAGER_OIDC_TOKEN_URL",
            ),
            scopes=_parse_scopes(settings.oidc_scopes),
        )


class OidcAuthenticator:
    """Discover an OIDC provider and authorize signed tokens by role."""

    def __init__(
        self,
        config: OidcConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        jwk_client_factory: Callable[[str], PyJWKClient] | None = None,
    ) -> None:
        """Create an uninitialized authenticator for one OIDC provider."""

        self.config = config
        self._transport = transport
        self._jwk_client_factory = jwk_client_factory or _create_jwk_client
        self._jwk_client: PyJWKClient | None = None

    async def initialize(self) -> None:
        """Discover the provider and preload its signing keys."""

        discovery_url = f"{self.config.issuer_url.rstrip('/')}/.well-known/openid-configuration"
        metadata = await self._get_json(discovery_url)
        if metadata.get("issuer") != self.config.issuer_url:
            msg = "OIDC discovery issuer does not match CODER_MANAGER_OIDC_ISSUER_URL"
            raise OidcConfigurationError(msg)
        jwks_uri = metadata.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            msg = "OIDC discovery metadata does not contain a valid jwks_uri"
            raise OidcConfigurationError(msg)
        client = self._jwk_client_factory(_require_https_url(jwks_uri, "OIDC discovery jwks_uri"))
        try:
            jwk_set = await asyncio.to_thread(client.get_jwk_set)
        except PyJWKClientConnectionError as error:
            raise OidcProviderUnavailableError(PROVIDER_ERROR) from error
        except (PyJWKClientError, PyJWKSetError) as error:
            msg = "OIDC jwks_uri did not return a valid JWK set"
            raise OidcConfigurationError(msg) from error
        if not any(
            key.key_id
            and key.algorithm_name == OIDC_ALGORITHM
            and key.public_key_use in {None, "sig"}
            for key in jwk_set.keys
        ):
            msg = "OIDC JWK set does not contain an RS256 signing key with a kid"
            raise OidcConfigurationError(msg)
        self._jwk_client = client

    async def authorize(self, token: str | None) -> None:
        """Validate one bearer token and require the API administrator role."""

        if not token:
            raise _authentication_exception()
        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError as error:
            raise _authentication_exception() from error
        if header.get("alg") != OIDC_ALGORITHM or not header.get("kid"):
            raise _authentication_exception()

        signing_key = await self._signing_key(token)
        if signing_key.algorithm_name != OIDC_ALGORITHM or signing_key.public_key_use not in {
            None,
            "sig",
        }:
            raise _authentication_exception()

        claims = self._decode(token, signing_key)
        roles = claims.get(OIDC_ROLES_CLAIM)
        if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
            raise _authorization_exception()
        if not any(role.casefold() == OIDC_REQUIRED_ROLE.casefold() for role in roles):
            raise _authorization_exception()

    async def _signing_key(self, token: str) -> jwt.PyJWK:
        """Return the token signing key through PyJWT's cached JWKS client."""

        client = self._jwk_client
        if client is None:
            msg = "OIDC authenticator has not been initialized"
            raise OidcConfigurationError(msg)
        try:
            return await asyncio.to_thread(client.get_signing_key_from_jwt, token)
        except PyJWKClientConnectionError as error:
            raise OidcProviderUnavailableError(PROVIDER_ERROR) from error
        except PyJWKSetError as error:
            raise OidcProviderUnavailableError(PROVIDER_ERROR) from error
        except (InvalidTokenError, PyJWKClientError) as error:
            raise _authentication_exception() from error

    def _decode(self, token: str, signing_key: jwt.PyJWK) -> dict[str, Any]:
        """Verify one JWT and return its claims."""

        try:
            return jwt.decode(
                token,
                signing_key,
                algorithms=[OIDC_ALGORITHM],
                issuer=self.config.issuer_url,
                options={"require": ["exp", "iss"], "verify_aud": False},
            )
        except InvalidTokenError as error:
            raise _authentication_exception() from error

    async def _get_json(self, url: str) -> dict[str, Any]:
        """Fetch one provider JSON object or report provider unavailability."""

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(OIDC_TIMEOUT_SECONDS),
                transport=self._transport,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise OidcProviderUnavailableError(PROVIDER_ERROR) from error
        if not isinstance(payload, dict):
            raise OidcProviderUnavailableError(PROVIDER_ERROR)
        return payload


def build_oidc_dependency(
    authenticator: OidcAuthenticator,
    scheme: OAuth2AuthorizationCodeBearer,
) -> Callable[..., Awaitable[None]]:
    """Build the FastAPI dependency that validates the OAuth2 bearer token."""

    async def authorize_oidc_request(
        token: Annotated[str | None, Security(scheme)],
    ) -> None:
        """Authorize one API request with the configured OIDC provider."""

        try:
            await authenticator.authorize(token)
        except OidcProviderUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=PROVIDER_ERROR,
            ) from error

    return authorize_oidc_request


def _create_jwk_client(jwks_uri: str) -> PyJWKClient:
    """Create the cached synchronous JWKS client used outside the event loop."""

    return PyJWKClient(jwks_uri, cache_keys=True, timeout=OIDC_TIMEOUT_SECONDS)


def _required_value(value: str | None, environment_name: str) -> str:
    """Return one non-empty OIDC setting."""

    normalized = _optional_value(value)
    if normalized is None:
        msg = f"{environment_name} is required when OIDC is enabled"
        raise OidcConfigurationError(msg)
    return normalized


def _optional_value(value: str | None) -> str | None:
    """Normalize an optional string setting."""

    if value is None or not value.strip():
        return None
    return value.strip()


def _require_https_url(
    value: str,
    environment_name: str,
    *,
    allow_query: bool = True,
) -> str:
    """Require an absolute HTTPS URL with no fragment."""

    url = httpx.URL(value)
    if url.scheme != "https" or not url.host or url.fragment or (url.query and not allow_query):
        msg = f"{environment_name} must be a valid HTTPS URL"
        raise OidcConfigurationError(msg)
    return value


def _parse_scopes(raw_value: str) -> tuple[str, ...]:
    """Parse and validate the ordered Swagger OAuth2 scope list."""

    scopes = _parse_comma_list(raw_value, "CODER_MANAGER_OIDC_SCOPES")
    if "openid" not in scopes:
        msg = "CODER_MANAGER_OIDC_SCOPES must contain openid"
        raise OidcConfigurationError(msg)
    return scopes


def _parse_comma_list(raw_value: str, environment_name: str) -> tuple[str, ...]:
    """Parse, trim, and deduplicate one comma-separated configuration list."""

    values = raw_value.split(",")
    if any(not value.strip() for value in values):
        msg = f"{environment_name} contains an empty value"
        raise OidcConfigurationError(msg)
    return tuple(dict.fromkeys(value.strip() for value in values))


def _authentication_exception() -> HTTPException:
    """Return the stable authentication failure response."""

    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=AUTHENTICATION_ERROR,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authorization_exception() -> HTTPException:
    """Return the stable role authorization failure response."""

    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=AUTHORIZATION_ERROR)
