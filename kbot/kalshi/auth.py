"""Kalshi API request signing.

Kalshi authenticates each request with an RSA-PSS signature over
`timestamp_ms + HTTP_METHOD + path`, where `path` is the request path with the
query string stripped.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class InvalidPrivateKey(ValueError):
    """Raised when a user pastes something that is not an RSA private key."""


def load_private_key(pem: str) -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a message
        raise InvalidPrivateKey(str(exc)) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise InvalidPrivateKey("Kalshi requires an RSA private key.")
    return key


@dataclass
class Signer:
    key_id: str
    private_key: rsa.RSAPrivateKey

    @classmethod
    def from_pem(cls, key_id: str, pem: str) -> "Signer":
        return cls(key_id=key_id, private_key=load_private_key(pem))

    def headers(self, method: str, url_or_path: str) -> dict[str, str]:
        path = urlsplit(url_or_path).path
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path}".encode()
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }
