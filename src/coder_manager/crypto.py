"""Authenticated encryption for stored credentials."""

from base64 import b64decode
from binascii import Error as Base64Error
from os import urandom
from typing import Final
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

ENVELOPE_VERSION: Final[int] = 1
NONCE_LENGTH: Final[int] = 12
KEY_LENGTH: Final[int] = 32


class CryptoConfigurationError(Exception):
    """Raised when the configured encryption key is missing or invalid."""


class PasswordDecryptionError(Exception):
    """Raised when an encrypted password cannot be authenticated."""


class KubernetesTokenDecryptionError(Exception):
    """Raised when an encrypted Kubernetes token cannot be authenticated."""


class PasswordCipher:
    """Encrypt and decrypt database passwords with a versioned AES-GCM envelope."""

    def __init__(self, encoded_key: SecretStr | None) -> None:
        """Initialize the cipher from a validated base64-encoded AES-256 key."""

        if encoded_key is None:
            raise CryptoConfigurationError
        try:
            key = b64decode(encoded_key.get_secret_value(), validate=True)
        except (Base64Error, ValueError) as error:
            raise CryptoConfigurationError from error
        if len(key) != KEY_LENGTH:
            raise CryptoConfigurationError
        self._cipher = AESGCM(key)

    @staticmethod
    def _associated_data(database_id: UUID) -> bytes:
        """Bind an encrypted password to its owning database identifier."""

        return b"coder-manager:database-password:" + database_id.bytes

    def encrypt(self, password: SecretStr, database_id: UUID) -> bytes:
        """Return version, random nonce, ciphertext, and authentication tag in one envelope."""

        nonce = urandom(NONCE_LENGTH)
        ciphertext = self._cipher.encrypt(
            nonce,
            password.get_secret_value().encode(),
            self._associated_data(database_id),
        )
        return bytes((ENVELOPE_VERSION,)) + nonce + ciphertext

    def decrypt(self, envelope: bytes, database_id: UUID) -> SecretStr:
        """Authenticate and decrypt one password envelope without exposing plaintext in errors."""

        if len(envelope) <= 1 + NONCE_LENGTH or envelope[0] != ENVELOPE_VERSION:
            raise PasswordDecryptionError
        nonce = envelope[1 : 1 + NONCE_LENGTH]
        ciphertext = envelope[1 + NONCE_LENGTH :]
        try:
            plaintext = self._cipher.decrypt(
                nonce,
                ciphertext,
                self._associated_data(database_id),
            )
        except (InvalidTag, ValueError) as error:
            raise PasswordDecryptionError from error
        try:
            return SecretStr(plaintext.decode())
        except UnicodeDecodeError as error:
            raise PasswordDecryptionError from error


class KubernetesTokenCipher:
    """Encrypt Kubernetes tokens with an instance-bound AES-GCM envelope."""

    def __init__(self, encoded_key: SecretStr | None) -> None:
        """Initialize the cipher from a validated base64-encoded AES-256 key."""

        if encoded_key is None:
            raise CryptoConfigurationError
        try:
            key = b64decode(encoded_key.get_secret_value(), validate=True)
        except (Base64Error, ValueError) as error:
            raise CryptoConfigurationError from error
        if len(key) != KEY_LENGTH:
            raise CryptoConfigurationError
        self._cipher = AESGCM(key)

    @staticmethod
    def _associated_data(instance_id: UUID) -> bytes:
        """Bind an encrypted token to its owning Coder instance."""

        return b"coder-manager:kubernetes-token:" + instance_id.bytes

    def encrypt(self, token: SecretStr, instance_id: UUID) -> bytes:
        """Return a versioned authenticated envelope for one Kubernetes token."""

        nonce = urandom(NONCE_LENGTH)
        ciphertext = self._cipher.encrypt(
            nonce,
            token.get_secret_value().encode(),
            self._associated_data(instance_id),
        )
        return bytes((ENVELOPE_VERSION,)) + nonce + ciphertext

    def decrypt(self, envelope: bytes, instance_id: UUID) -> SecretStr:
        """Authenticate and decrypt one Kubernetes token envelope."""

        if len(envelope) <= 1 + NONCE_LENGTH or envelope[0] != ENVELOPE_VERSION:
            raise KubernetesTokenDecryptionError
        nonce = envelope[1 : 1 + NONCE_LENGTH]
        ciphertext = envelope[1 + NONCE_LENGTH :]
        try:
            plaintext = self._cipher.decrypt(
                nonce,
                ciphertext,
                self._associated_data(instance_id),
            )
        except (InvalidTag, ValueError) as error:
            raise KubernetesTokenDecryptionError from error
        try:
            return SecretStr(plaintext.decode())
        except UnicodeDecodeError as error:
            raise KubernetesTokenDecryptionError from error
