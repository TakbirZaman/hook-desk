import json
import time

from fastapi.testclient import TestClient

from tests.conftest import ADMIN, signed_headers

ADMIN_H = {"Authorization": f"Bearer {ADMIN}"}


def post(client: TestClient, body: bytes, headers: dict[str, str], source: str = "stripe"):  # type: ignore[no-untyped-def]
    return client.post(f"/hooks/{source}", content=body, headers=headers)


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_accepts_signed_and_dedupes(client: TestClient) -> None:
    body = json.dumps({"order": 7}).encode()
    h = {**signed_headers(body), "X-Event-Id": "evt_1"}
    first = post(client, body, h)
    assert first.status_code == 202
    assert first.json() == {"status": "accepted", "id": "evt_1"}
    again = post(client, body, {**signed_headers(body), "X-Event-Id": "evt_1"})
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"
    assert client.get("/stats", headers=ADMIN_H).json()["pending"] == 1


def test_event_id_defaults_to_body_hash(client: TestClient) -> None:
    body = b'{"a":1}'
    a = post(client, body, signed_headers(body)).json()
    b = post(client, body, signed_headers(body)).json()
    assert a["id"] == b["id"]
    assert b["status"] == "duplicate"


def test_bad_signature_rejected(client: TestClient) -> None:
    body = b"{}"
    assert post(client, body, signed_headers(body, secret="z" * 32)).status_code == 401


def test_replayed_old_timestamp_rejected(client: TestClient) -> None:
    body = b"{}"
    assert post(client, body, signed_headers(body, ts=int(time.time()) - 10_000)).status_code == 401


def test_non_object_and_invalid_json_rejected(client: TestClient) -> None:
    for body in (b"[1,2]", b"not json"):
        assert post(client, body, signed_headers(body)).status_code == 422


def test_invalid_source_name_rejected(client: TestClient) -> None:
    body = b"{}"
    assert post(client, body, signed_headers(body), source="Bad Source!").status_code == 422


def test_oversize_body_rejected(client: TestClient) -> None:
    body = b'{"x":"' + b"a" * 1_100_000 + b'"}'
    assert post(client, body, signed_headers(body)).status_code == 413


def test_admin_endpoints_need_token(client: TestClient) -> None:
    assert client.get("/events").status_code == 401
    assert client.get("/events", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/events", headers=ADMIN_H).status_code == 200


def test_get_event_and_404(client: TestClient) -> None:
    body = b'{"k":1}'
    eid = post(client, body, signed_headers(body)).json()["id"]
    assert client.get(f"/events/{eid}", headers=ADMIN_H).json()["status"] == "pending"
    assert client.get("/events/missing", headers=ADMIN_H).status_code == 404


def test_retry_only_dead(client: TestClient) -> None:
    body = b'{"k":1}'
    eid = post(client, body, signed_headers(body)).json()["id"]
    assert client.post(f"/events/{eid}/retry", headers=ADMIN_H).status_code == 409
