import random

import httpx
import pytest

from hookdesk.config import Settings
from hookdesk.models import Event, Status
from hookdesk.store import EventStore
from hookdesk.worker import (
    HttpForwarder,
    PermanentDeliveryError,
    TransientDeliveryError,
    Worker,
    backoff_delay,
)


def test_backoff_is_bounded_and_grows() -> None:
    rng = random.Random(1)
    assert all(0 <= backoff_delay(1, 2, 300, rng) <= 2 for _ in range(50))
    assert all(0 <= backoff_delay(20, 2, 300, rng) <= 300 for _ in range(50))


async def test_success_marks_done(settings: Settings) -> None:
    store = EventStore(settings.db_path)
    store.insert_if_new("e1", "s", {"x": 1}, now=0)
    seen: list[str] = []

    async def ok(e: Event) -> None:
        seen.append(e.id)

    assert await Worker(store, ok, settings).run_once(now=1) == 1
    assert seen == ["e1"]
    assert store.get("e1").status is Status.DONE  # type: ignore[union-attr]


async def test_transient_retries_then_dies_at_max_attempts(settings: Settings) -> None:
    store = EventStore(settings.db_path)
    store.insert_if_new("e1", "s", {}, now=0)

    async def flaky(_: Event) -> None:
        raise TransientDeliveryError("503")

    w = Worker(store, flaky, settings)
    for i in range(settings.max_attempts):
        await w.run_once(now=1e12)  # far future: always due
        ev = store.get("e1")
        assert ev is not None
        assert ev.attempts == i + 1
    assert ev is not None
    assert ev.status is Status.DEAD
    assert ev.last_error == "503"


async def test_permanent_error_dies_immediately(settings: Settings) -> None:
    store = EventStore(settings.db_path)
    store.insert_if_new("e1", "s", {}, now=0)

    async def bad(_: Event) -> None:
        raise PermanentDeliveryError("HTTP 400")

    await Worker(store, bad, settings).run_once(now=1)
    ev = store.get("e1")
    assert ev is not None
    assert ev.status is Status.DEAD
    assert ev.attempts == 1


@pytest.mark.parametrize(
    ("status", "exc"),
    [(503, TransientDeliveryError), (429, TransientDeliveryError), (400, PermanentDeliveryError)],
)
async def test_http_forwarder_classifies_status(status: int, exc: type[Exception]) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(status))
    fwd = HttpForwarder("http://x.test/h", 1, httpx.AsyncClient(transport=transport))
    event = Event("e", "s", {}, Status.PROCESSING, 1, 0, None, 0, 0)
    with pytest.raises(exc):
        await fwd(event)


async def test_http_forwarder_timeout_is_transient() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    fwd = HttpForwarder(
        "http://x.test/h", 1, httpx.AsyncClient(transport=httpx.MockTransport(boom))
    )
    event = Event("e", "s", {}, Status.PROCESSING, 1, 0, None, 0, 0)
    with pytest.raises(TransientDeliveryError):
        await fwd(event)
