"""Background delivery worker: claim due events, forward them, retry with backoff, dead-letter."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable

import httpx

from hookdesk.config import Settings
from hookdesk.models import Event
from hookdesk.store import EventStore

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class DeliveryError(Exception):
    """Base class for delivery failures."""


class TransientDeliveryError(DeliveryError):
    """Worth retrying (timeouts, 5xx, 429)."""


class PermanentDeliveryError(DeliveryError):
    """Retrying cannot help (4xx other than 408/425/429)."""


Forwarder = Callable[[Event], Awaitable[None]]


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random | None = None) -> float:
    """Exponential backoff with full jitter: uniform(0, min(cap, base * 2**(attempt-1)))."""
    ceiling = min(cap, base * 2 ** max(attempt - 1, 0))
    return (rng or random).uniform(0, ceiling)


class HttpForwarder:
    """POSTs the event as JSON to ``url`` (e.g. an n8n Webhook node)."""

    def __init__(self, url: str, timeout_s: float, client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def __call__(self, event: Event) -> None:
        body = {"id": event.id, "source": event.source, "payload": event.payload}
        try:
            r = await self._client.post(self._url, json=body, headers={"X-Event-Id": event.id})
        except httpx.TransportError as err:  # timeouts, DNS, connection resets
            raise TransientDeliveryError(f"{type(err).__name__}: {err}") from err
        if r.is_success:
            return
        msg = f"target returned HTTP {r.status_code}"
        if r.status_code in RETRYABLE_STATUS:
            raise TransientDeliveryError(msg)
        raise PermanentDeliveryError(msg)

    async def aclose(self) -> None:
        await self._client.aclose()


async def log_only_forwarder(event: Event) -> None:
    log.info("event %s from %s processed (no forward_url configured)", event.id, event.source)


class Worker:
    def __init__(self, store: EventStore, forwarder: Forwarder, settings: Settings) -> None:
        self._store = store
        self._forward = forwarder
        self._s = settings

    async def run_once(self, now: float | None = None) -> int:
        """Process one batch of due events. Returns how many were handled."""
        t = time.time() if now is None else now
        events = await asyncio.to_thread(self._store.claim_due, self._s.batch_size, t)
        await asyncio.gather(*(self._handle(e) for e in events))
        return len(events)

    async def _handle(self, event: Event) -> None:
        try:
            await self._forward(event)
        except PermanentDeliveryError as err:
            log.warning("event %s dead (permanent): %s", event.id, err)
            await asyncio.to_thread(self._store.mark_dead, event.id, str(err))
        except Exception as err:  # transient or unexpected: bounded by max_attempts
            if not isinstance(err, TransientDeliveryError):
                log.exception("event %s unexpected failure", event.id)
            if event.attempts >= self._s.max_attempts:
                log.warning("event %s dead after %d attempts", event.id, event.attempts)
                await asyncio.to_thread(self._store.mark_dead, event.id, str(err))
                return
            delay = backoff_delay(event.attempts, self._s.backoff_base_s, self._s.backoff_cap_s)
            await asyncio.to_thread(self._store.mark_retry, event.id, str(err), time.time() + delay)
        else:
            await asyncio.to_thread(self._store.mark_done, event.id)

    async def run(self, stop: asyncio.Event) -> None:
        recovered = await asyncio.to_thread(self._store.recover_stuck)
        if recovered:
            log.info("recovered %d stuck events", recovered)
        while not stop.is_set():
            try:
                handled = await self.run_once()
            except Exception:
                log.exception("worker loop error")
                handled = 0
            if handled == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._s.poll_interval_s)
