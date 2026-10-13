# hookdesk

[![ci](https://github.com/TakbirZaman/hook-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/TakbirZaman/hook-desk/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-%3E%3D3.11-blue)

A small, production-minded **webhook gateway**. It accepts **signed** webhooks, stores them durably
in SQLite, and delivers them to a target (for example an **n8n Webhook node**) with retries,
exponential backoff with jitter, and a dead-letter queue.

```
sender --HMAC--> POST /hooks/{source} --202--> SQLite queue --> worker --retry/backoff--> target
                       |                                                   (n8n, any HTTP URL)
                       +-- duplicates (same X-Event-Id) -> 200, no second delivery
```

## Guarantees

- **Authenticated**: `X-Signature: sha256=HMAC(secret, "{X-Timestamp}.{body}")`, constant-time
  compare, 5-minute replay window.
- **Fast ack**: returns `202` after the event is persisted; delivery is asynchronous.
- **Idempotent**: `X-Event-Id` (or SHA-256 of the body) is the dedupe key; redeliveries return
  `200` without a second delivery.
- **At-least-once delivery**: transient failures (timeout, 408/425/429/5xx) retry with exponential
  backoff + full jitter up to `HOOKDESK_MAX_ATTEMPTS`; permanent failures (other 4xx) go straight
  to `dead`.
- **Crash-safe**: events stuck in `processing` after a crash are re-queued on startup.
- **Locked-down admin API**: bearer token with constant-time compare. Body size capped
  (default 1 MB).

## Quickstart

Requirements: Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env        # fill in HOOKDESK_WEBHOOK_SECRET and HOOKDESK_ADMIN_TOKEN (>=16 chars)
uv run uvicorn hookdesk.main:create_app --factory --port 8000
```

With Docker:

```bash
docker build -t hookdesk .
docker run -p 8000:8000 --env-file .env -v hookdesk-data:/data hookdesk
```

## Sending webhooks

Every request to `POST /hooks/{source}` must carry three headers:

| Header | Value |
|---|---|
| `X-Timestamp` | Unix epoch seconds (must be within ±5 min) |
| `X-Signature` | `sha256=` + hex HMAC-SHA256 of `"{timestamp}.{raw_body}"` keyed with `HOOKDESK_WEBHOOK_SECRET` |
| `X-Event-Id` *(optional)* | Your idempotency key (1–128 chars); defaults to SHA-256 of the body |

The body must be a JSON object. `source` matches `[a-z0-9_-]{1,40}` (e.g. `stripe`, `github`).

For manual testing there is a built-in sender:

```bash
export HOOKDESK_WEBHOOK_SECRET=...   # same value as the server
uv run hookdesk-send stripe '{"order": 42}' --event-id evt_42
```

## Admin API

All admin routes need `Authorization: Bearer <HOOKDESK_ADMIN_TOKEN>`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/hooks/{source}` | Ingest (HMAC auth; `202` accepted, `200` duplicate) |
| GET | `/events` | List, filter by `status`, paginate (`limit`, `offset`) |
| GET | `/events/{id}` | One event with attempts and last error |
| POST | `/events/{id}/retry` | Requeue a `dead` event |
| GET | `/stats` | Counts per status |
| GET | `/health` | Liveness (no auth) |

```bash
curl -H "Authorization: Bearer $HOOKDESK_ADMIN_TOKEN" localhost:8000/stats
curl -H "Authorization: Bearer $HOOKDESK_ADMIN_TOKEN" "localhost:8000/events?status=dead"
curl -X POST -H "Authorization: Bearer $HOOKDESK_ADMIN_TOKEN" localhost:8000/events/evt_42/retry
```

## Configuration

All settings come from `HOOKDESK_*` environment variables (or a local `.env` file).

| Variable | Default | Purpose |
|---|---|---|
| `HOOKDESK_WEBHOOK_SECRET` | *(required, ≥16 chars)* | HMAC key for inbound signatures |
| `HOOKDESK_ADMIN_TOKEN` | *(required, ≥16 chars)* | Bearer token for the admin API |
| `HOOKDESK_FORWARD_URL` | — | Target URL (e.g. an n8n Webhook node); unset = log-only mode |
| `HOOKDESK_DB_PATH` | `hookdesk.db` | SQLite path (`/data/hookdesk.db` in Docker) |
| `HOOKDESK_MAX_ATTEMPTS` | `5` | Delivery attempts before an event goes `dead` |
| `HOOKDESK_FORWARD_TIMEOUT_S` | `10` | Per-attempt HTTP timeout |
| `HOOKDESK_BACKOFF_BASE_S` / `HOOKDESK_BACKOFF_CAP_S` | `2` / `300` | Exponential backoff base and cap |
| `HOOKDESK_SIGNATURE_TOLERANCE_S` | `300` | Replay window for `X-Timestamp` |
| `HOOKDESK_MAX_BODY_BYTES` | `1000000` | Ingest body size limit |
| `HOOKDESK_POLL_INTERVAL_S` / `HOOKDESK_BATCH_SIZE` | `1` / `10` | Worker polling |

Generate secrets with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Using it with n8n

Set `HOOKDESK_FORWARD_URL` to your n8n Webhook node URL. n8n receives
`{"id", "source", "payload"}` plus an `X-Event-Id` header, so a Switch node can branch on `source`.

## Develop

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=hookdesk
```

## Project structure

```
src/hookdesk/   main.py      # FastAPI app: ingest + admin API
                worker.py    # background delivery with backoff/jitter
                store.py     # SQLite event store (queue + DLQ)
                security.py  # HMAC sign/verify
                config.py    # HOOKDESK_* settings
                cli.py       # hookdesk-send test sender
tests/          # API, store, worker, security tests
```

## Limits (by design)

- Single-process worker with SQLite: right for one instance. For multiple replicas, swap `EventStore`
  for PostgreSQL using `FOR UPDATE SKIP LOCKED`; the `Worker` and API do not change.
- Delivery is at-least-once, so the target must tolerate duplicates (use the `id`).
