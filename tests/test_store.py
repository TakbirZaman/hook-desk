from pathlib import Path

from hookdesk.models import Status
from hookdesk.store import EventStore


def make(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "s.db")


def test_insert_is_idempotent(tmp_path: Path) -> None:
    s = make(tmp_path)
    assert s.insert_if_new("e1", "src", {"a": 1}) is True
    assert s.insert_if_new("e1", "src", {"a": 2}) is False
    assert s.get("e1").payload == {"a": 1}  # type: ignore[union-attr]


def test_claim_respects_due_time_and_counts_attempts(tmp_path: Path) -> None:
    s = make(tmp_path)
    s.insert_if_new("e1", "src", {}, now=100)
    assert s.claim_due(10, now=99) == []
    (e,) = s.claim_due(10, now=100)
    assert e.attempts == 1
    assert s.claim_due(10, now=101) == []  # already processing


def test_retry_then_dead_then_requeue(tmp_path: Path) -> None:
    s = make(tmp_path)
    s.insert_if_new("e1", "src", {}, now=0)
    s.claim_due(1, now=0)
    s.mark_retry("e1", "boom", next_at=50, now=1)
    assert s.get("e1").status is Status.PENDING  # type: ignore[union-attr]
    assert s.claim_due(1, now=10) == []
    s.claim_due(1, now=50)
    s.mark_dead("e1", "gave up")
    assert s.requeue_dead("e1") is True
    assert s.requeue_dead("e1") is False  # no longer dead
    assert s.get("e1").attempts == 0  # type: ignore[union-attr]


def test_recover_stuck_and_stats(tmp_path: Path) -> None:
    s = make(tmp_path)
    s.insert_if_new("e1", "src", {}, now=0)
    s.claim_due(1, now=0)
    assert s.recover_stuck() == 1
    assert s.stats()["pending"] == 1
