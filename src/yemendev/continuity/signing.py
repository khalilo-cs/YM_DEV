"""Ed25519 signing for passports and sync bundles (the `cryptography` library; no custom crypto).

The installation's private key lives in ``data_dir/keys/ed25519.pem`` (0600). A signature proves the
document was not modified; *who* signed it is only trusted when the signer's public key is in
``data_dir/keys/trusted/`` (or is this installation's own key).
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from yemendev.core.contracts import InvalidInputError, sha256_bytes


def load_or_create_signing_key(keys_dir: Path) -> Ed25519PrivateKey:
    path = keys_dir / "ed25519.pem"
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise InvalidInputError("signing key file is not an Ed25519 key")
        return key
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, pem)
        os.fsync(fd)
    finally:
        os.close(fd)
    return key


def public_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def fingerprint(public_key_hex: str) -> str:
    return sha256_bytes(bytes.fromhex(public_key_hex))[:16]


def sign(key: Ed25519PrivateKey, message: bytes) -> str:
    return key.sign(message).hex()


def verify(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(bytes.fromhex(signature_hex),
                                                                                 message)
        return True
    except (InvalidSignature, ValueError):
        return False


def trusted_keys(keys_dir: Path, own: Ed25519PrivateKey) -> set[str]:
    out = {public_hex(own)}
    d = keys_dir / "trusted"
    if d.is_dir():
        for f in sorted(d.glob("*.pub")):
            val = f.read_text().strip()
            if len(val) == 64 and all(c in "0123456789abcdef" for c in val):
                out.add(val)
    return out
