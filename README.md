# hookdesk

A small, production-minded webhook gateway. It accepts **signed** webhooks, stores them durably,
and delivers them to a target (for example an **n8n Webhook node**) with retries, backoff and a
dead-letter queue.

```
sender --HMAC--> POST /hooks/{source} --202--> SQLite queue --> worker --retry/backoff--> target
                       |                                                   (n8n, any HTTP URL)
                       +-- duplicates (same X-Event-Id) -> 200, no second delivery
```

## Guarantees
- **Authenticated**: `X-Signature: sha256=HMAC(secret, "{X-Timestamp}.{body}")`, constant-time compare,
  5-minute replay window.
- **Fast ack**: returns `202` after the event is persisted; delivery is asynchronous.
- **Idempotent**: `X-Event-Id` (or SHA-256 of the body) is the key; redeliveries are ignored.
- **At-least-once delivery**: transient failures (timeout, 408/425/429/5xx) retry with exponential
  backoff + full jitter up to `HOOKDESK_MAX_ATTEMPTS`; permanent failures (other 4xx) go straight to `dead`.
- **Crash-safe**: events stuck in `processing` after a crash are re-queued on startup.
- **Locked-down admin API**: bearer token, constant-time compare. Body size capped (default 1 MB).

## Run
```bash
uv sync
cp .env.example .env        # fill in HOOKDESK_WEBHOOK_SECRET and HOOKDESK_ADMIN_TOKEN (>=16 chars)
uv run uvicorn hookdesk.main:create_app --factory --port 8000
```

Send a test event:
```bash
export HOOKDESK_WEBHOOK_SECRET=...   # same value as the server
uv run hookdesk-send stripe '{"order": 42}' --event-id evt_42
```

Inspect (admin token required):
```bash
curl -H "Authorization: Bearer $HOOKDESK_ADMIN_TOKEN" localhost:8000/stats
curl -H "Authorization: Bearer $HOOKDESK_ADMIN_TOKEN" "localhost:8000/events?status=dead"
curl -X POST -H "Authorization: Bearer $HOOKDESK_ADMIN_TOKEN" localhost:8000/events/evt_42/retry
```

Docker: `docker build -t hookdesk . && docker run -p 8000:8000 --env-file .env -v hookdesk-data:/data hookdesk`

## API
| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/hooks/{source}` | HMAC | Ingest (202 accepted, 200 duplicate) |
| GET | `/events` | admin | List, filter by `status`, paginate |
| GET | `/events/{id}` | admin | One event with attempts and last error |
| POST | `/events/{id}/retry` | admin | Requeue a `dead` event |
| GET | `/stats` | admin | Counts per status |
| GET | `/health` | none | Liveness |

## Using it with n8n
Set `HOOKDESK_FORWARD_URL` to your n8n Webhook node URL. n8n receives
`{"id", "source", "payload"}` plus an `X-Event-Id` header, so a Switch node can branch on `source`.

## Develop
```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=hookdesk
```

## Limits (by design)
- Single-process worker with SQLite: right for one instance. For multiple replicas, swap `EventStore`
  for PostgreSQL using `FOR UPDATE SKIP LOCKED`; the `Worker` and API do not change.
- Delivery is at-least-once, so the target must tolerate duplicates (use the `id`).
