"""HMAC request signing: signature = HMAC_SHA256(secret, f"{timestamp}.{body}")."""

from __future__ import annotations

import hashlib
import hmac
import time


class SignatureError(Exception):
    """Raised when a request signature is missing, stale, or wrong."""


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` signature a sender must attach."""
    msg = timestamp.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    *,
    timestamp: str,
    signature: str,
    body: bytes,
    tolerance_s: int,
    now: float | None = None,
) -> None:
    """Raise SignatureError unless the signature is valid and the timestamp fresh."""
    try:
        ts = int(timestamp)
    except ValueError as err:
        raise SignatureError("timestamp must be an integer (unix seconds)") from err
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance_s:
        raise SignatureError("timestamp outside tolerance (possible replay)")
    if not hmac.compare_digest(sign(secret, timestamp, body), signature):
        raise SignatureError("signature mismatch")
