import pytest

from hookdesk.security import SignatureError, sign, verify

KW = {"tolerance_s": 300}


def test_valid_signature_passes() -> None:
    sig = sign("k" * 16, "1000", b"{}")
    verify("k" * 16, timestamp="1000", signature=sig, body=b"{}", now=1100, **KW)


def test_tampered_body_rejected() -> None:
    sig = sign("k" * 16, "1000", b"{}")
    with pytest.raises(SignatureError, match="mismatch"):
        verify("k" * 16, timestamp="1000", signature=sig, body=b'{"x":1}', now=1000, **KW)


def test_wrong_secret_rejected() -> None:
    sig = sign("a" * 16, "1000", b"{}")
    with pytest.raises(SignatureError):
        verify("b" * 16, timestamp="1000", signature=sig, body=b"{}", now=1000, **KW)


def test_stale_timestamp_rejected() -> None:
    sig = sign("k" * 16, "1000", b"{}")
    with pytest.raises(SignatureError, match="replay"):
        verify("k" * 16, timestamp="1000", signature=sig, body=b"{}", now=2000, **KW)


def test_non_integer_timestamp_rejected() -> None:
    with pytest.raises(SignatureError, match="integer"):
        verify("k" * 16, timestamp="abc", signature="x", body=b"", now=0, **KW)
