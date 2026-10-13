"""FastAPI app: signed webhook ingest -> durable queue -> background delivery."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from hookdesk.config import Settings, get_settings
from hookdesk.models import Status
from hookdesk.security import SignatureError, verify
from hookdesk.store import EventStore
from hookdesk.worker import Forwarder, HttpForwarder, Worker, log_only_forwarder

log = logging.getLogger("hookdesk")
MAX_EVENT_ID_LEN = 128
_bearer = HTTPBearer(auto_error=False)  # module level so FastAPI can resolve annotations


async def _read_limited(request: Request, limit: int) -> bytes:
    """Read the body, aborting as soon as it exceeds ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "payload too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    settings: Settings | None = None,
    forwarder: Forwarder | None = None,
    run_worker: bool = True,
) -> FastAPI:
    cfg = settings or get_settings()
    store = EventStore(cfg.db_path)

    owned_forwarder: HttpForwarder | None = None
    if forwarder is None:
        if cfg.forward_url is not None:
            owned_forwarder = HttpForwarder(str(cfg.forward_url), cfg.forward_timeout_s)
            forwarder = owned_forwarder
        else:
            forwarder = log_only_forwarder
    worker = Worker(store, forwarder, cfg)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop)) if run_worker else None
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task
            if owned_forwarder is not None:
                await owned_forwarder.aclose()

    app = FastAPI(title="hookdesk", version="0.1.0", lifespan=lifespan)

    def require_admin(
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        expected = cfg.admin_token.get_secret_value()
        if creds is None or not hmac.compare_digest(creds.credentials, expected):
            raise HTTPException(401, "invalid or missing admin token")

    admin = Depends(require_admin)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/hooks/{source}", status_code=202)
    async def receive(
        source: Annotated[str, Path(pattern=r"^[a-z0-9_-]{1,40}$")],
        request: Request,
        response: Response,
        x_timestamp: Annotated[str, Header()],
        x_signature: Annotated[str, Header()],
        x_event_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        body = await _read_limited(request, cfg.max_body_bytes)
        try:
            verify(
                cfg.webhook_secret.get_secret_value(),
                timestamp=x_timestamp,
                signature=x_signature,
                body=body,
                tolerance_s=cfg.signature_tolerance_s,
            )
        except SignatureError as err:
            log.warning("rejected webhook for %s: %s", source, err)
            raise HTTPException(401, str(err)) from err

        try:
            payload: Any = json.loads(body)
        except json.JSONDecodeError as err:
            raise HTTPException(422, "body must be valid JSON") from err
        if not isinstance(payload, dict):
            raise HTTPException(422, "body must be a JSON object")

        if x_event_id is not None and not 0 < len(x_event_id) <= MAX_EVENT_ID_LEN:
            raise HTTPException(422, "X-Event-Id must be 1-128 characters")
        event_id = x_event_id or hashlib.sha256(body).hexdigest()

        created = await asyncio.to_thread(store.insert_if_new, event_id, source, payload)
        if not created:
            response.status_code = 200
            return {"status": "duplicate", "id": event_id}
        return {"status": "accepted", "id": event_id}

    @app.get("/events", dependencies=[admin])
    async def list_events(
        status: Status | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        events = await asyncio.to_thread(store.list, status, limit, offset)
        return [e.as_dict() for e in events]

    @app.get("/events/{event_id}", dependencies=[admin])
    async def get_event(event_id: str) -> dict[str, Any]:
        event = await asyncio.to_thread(store.get, event_id)
        if event is None:
            raise HTTPException(404, "event not found")
        return event.as_dict()

    @app.post("/events/{event_id}/retry", dependencies=[admin])
    async def retry_event(event_id: str) -> dict[str, str]:
        if await asyncio.to_thread(store.requeue_dead, event_id):
            return {"status": "requeued", "id": event_id}
        raise HTTPException(409, "only dead events can be requeued")

    @app.get("/stats", dependencies=[admin])
    async def stats() -> dict[str, int]:
        return await asyncio.to_thread(store.stats)

    return app
