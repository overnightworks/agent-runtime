"""Throwaway RSA keys for the capability suites; no private key is ever committed."""

from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generated_rsa_key(bits: int = 3072, exponent: int = 65537) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=exponent, key_size=bits)


def pem_of(private_key: Any) -> bytes:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
