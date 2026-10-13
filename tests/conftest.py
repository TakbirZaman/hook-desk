from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hookdesk.config import Settings
from hookdesk.main import create_app
from hookdesk.models import Event
from hookdesk.security import sign

SECRET = "s" * 32
ADMIN = "a" * 32


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        webhook_secret=SECRET,  # type: ignore[arg-type]
        admin_token=ADMIN,  # type: ignore[arg-type]
        db_path=tmp_path / "t.db",
        backoff_base_s=0.01,
        backoff_cap_s=0.02,
        max_attempts=3,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    # Worker disabled so tests drive delivery deterministically.
    async def noop(_: Event) -> None:
        return None

    with TestClient(create_app(settings, forwarder=noop, run_worker=False)) as c:
        yield c


def signed_headers(body: bytes, ts: int | None = None, secret: str = SECRET) -> dict[str, str]:
    stamp = str(int(time.time()) if ts is None else ts)
    return {"X-Timestamp": stamp, "X-Signature": sign(secret, stamp, body)}
