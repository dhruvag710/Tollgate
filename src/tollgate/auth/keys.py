"""API key generation, hashing and verification.

A key is `tg_live_` followed by 32 random bytes in base62 (43 characters).
Only HMAC-SHA256(pepper, key) is stored, so a database leak yields no usable
keys without the pepper, which lives only in the gateway's environment.
"""

import hashlib
import hmac
import re
import secrets

KEY_PREFIX = "tg_live_"
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SECRET_BYTES = 32
_SECRET_DIGITS = 43  # base62 digits needed for any 256-bit value
_KEY_RE = re.compile(rf"{KEY_PREFIX}[0-9A-Za-z]{{{_SECRET_DIGITS}}}")
# How much of a key is stored and shown to identify it, e.g. "tg_live_ab12cd".
DISPLAY_PREFIX_LEN = len(KEY_PREFIX) + 6


def generate_key() -> str:
    value = int.from_bytes(secrets.token_bytes(_SECRET_BYTES), "big")
    digits = []
    for _ in range(_SECRET_DIGITS):  # fixed width: leading zeros are kept
        value, digit = divmod(value, 62)
        digits.append(_ALPHABET[digit])
    return KEY_PREFIX + "".join(reversed(digits))


def is_well_formed(raw_key: str) -> bool:
    return _KEY_RE.fullmatch(raw_key) is not None


def hash_key(raw_key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), raw_key.encode(), hashlib.sha256).hexdigest()


def hashes_match(a: str, b: str) -> bool:
    """Constant-time comparison of two key hashes."""
    return hmac.compare_digest(a.encode(), b.encode())


def display_prefix(raw_key: str) -> str:
    return raw_key[:DISPLAY_PREFIX_LEN]
