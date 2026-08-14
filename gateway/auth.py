"""Auth / crypto helpers. Pure functions (no FastAPI dependency).

ferrum protocol matches the Rust `stt_ferrum` client + mpv-stt-crypto:
  - auth token: hex(sha256(secret)), sent as x-auth-token
  - encryption: AES-256-GCM, key = sha256(passphrase), wire = [12B nonce][ct]
Translation gateways:
  - DeepL:        Authorization: DeepL-Auth-Key {key} header
  - LibreTranslate: api_key field in the request body
"""

import hashlib
import os


def ct_compare(a: str, b: str) -> bool:
    """Constant-time string comparison (length short-circuit + char-wise sum)."""
    if len(a) != len(b):
        return False
    return sum(x != y for x, y in zip(a, b)) == 0


def ferrum_auth_token(secret: str) -> str:
    """Same derivation as Rust AuthToken::from_secret: hex(sha256(secret))."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def ferrum_cipher(passphrase: str):
    """AES-256-GCM, key = sha256(passphrase); wire = [12B nonce][ciphertext]."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = hashlib.sha256(passphrase.encode("utf-8")).digest()
    return AESGCM(key)


def ferrum_encrypt(cipher, plaintext: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + cipher.encrypt(nonce, plaintext, None)


def ferrum_decrypt(cipher, wire: bytes) -> bytes:
    if len(wire) < 12:
        raise ValueError("encrypted payload too short")
    return cipher.decrypt(wire[:12], wire[12:], None)


def deepl_auth_ok(authorization: str, expected_key: str) -> bool:
    """Constant-time check of the `Authorization: DeepL-Auth-Key {key}` header."""
    expected = f"DeepL-Auth-Key {expected_key}"
    return ct_compare(authorization, expected)


def libretranslate_key_ok(provided: str, expected_key: str) -> bool:
    """Constant-time check of the body `api_key` field (LibreTranslate auth)."""
    return ct_compare(provided, expected_key)
