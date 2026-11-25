from dataclasses import replace
from datetime import datetime

import pytest

from core.automation_state import (
    AutomationStateStore,
    BatchState,
    StateRecoveryRequired,
)
from core.state_store import StateFileError


def test_batch_identity_does_not_depend_on_publish_time(tmp_path):
    store = AutomationStateStore(tmp_path / "automation_state.json")

    assert store.batch_key("2026-07-14", "site-123") == "2026-07-14|site-123"
    with pytest.raises(ValueError, match="site_id"):
        store.batch_key("2026-07-14", "")


def test_state_round_trip_and_inventory_persistence(tmp_path):
    now = datetime(2026, 7, 14, 8, 30, 0)
    store = AutomationStateStore(
        tmp_path / "automation_state.json", now_func=lambda: now
    )

    store.create_or_update("2026-07-14", "site-1", planned_count=60)
    running = store.transition("2026-07-14", "site-1", "running")
    completed = store.transition(
        "2026-07-14",
        "site-1",
        "completed",
        attempted_count=60,
        success_count=59,
        failed_count=1,
    )
    recorded = store.record_inventory(
        "2026-07-14", "site-1", count=499, threshold=500, unreadable=2
    )

    assert running.started_at == "2026-07-14T08:30:00"
    assert completed.finished_at == "2026-07-14T08:30:00"
    assert recorded.inventory_count == 499
    assert recorded.inventory_unreadable == 2
    assert AutomationStateStore(store.path).get("2026-07-14", "site-1") == recorded


def test_corrupt_state_is_quarantined_and_all_writes_fail_closed(tmp_path):
    path = tmp_path / "automation_state.json"
    path.write_text("{broken", encoding="utf-8")
    store = AutomationStateStore(
        path, now_func=lambda: datetime(2026, 7, 14, 8, 30, 0)
    )

    with pytest.raises(StateRecoveryRequired, match="重建"):
        store.load()
    assert store.recovery_required is True
    assert not path.exists()
    quarantined = list(tmp_path.glob("automation_state.json.corrupt-20260714-083000"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{broken"
    assert store.last_quarantined_path == quarantined[0]

    with pytest.raises(StateRecoveryRequired, match="重建"):
        store.create_or_update("2026-07-14", "site-1", planned_count=1)
    assert not path.exists()


def test_complete_recovery_atomically_replaces_state_and_reenables_writes(tmp_path):
    path = tmp_path / "automation_state.json"
    path.write_text("{broken", encoding="utf-8")
    store = AutomationStateStore(path)
    with pytest.raises(StateRecoveryRequired):
        store.load()
    key = store.batch_key("2026-07-14", "site-1")
    recovered = BatchState(
        batch_key=key,
        run_date="2026-07-14",
        site_id="site-1",
        status="queued",
        success_count=2,
        attempted_count=2,
        counts_trusted=False,
    )

    store.complete_recovery({key: recovered})

    assert store.recovery_required is False
    assert store.get("2026-07-14", "site-1") == recovered
    assert store.transition("2026-07-14", "site-1", "running").status == "running"


def test_recovery_lock_survives_process_restart(tmp_path):
    path = tmp_path / "automation_state.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(StateRecoveryRequired):
        AutomationStateStore(path).load()

    restarted = AutomationStateStore(path)

    with pytest.raises(StateRecoveryRequired, match="重建"):
        restarted.put(
            BatchState(
                batch_key="2026-07-14|site-1",
                run_date="2026-07-14",
                site_id="site-1",
            )
        )
    assert not path.exists()


def test_corrupt_state_can_fail_closed_without_quarantine(tmp_path):
    path = tmp_path / "automation_state.json"
    path.write_text('{"version":1,"batches":[]}', encoding="utf-8")

    with pytest.raises(StateFileError, match="自动化状态格式损坏"):
        AutomationStateStore(path).load(quarantine_corrupt=False)

    assert path.exists()


def test_recover_interrupted_requeues_only_running_batches(tmp_path):
    store = AutomationStateStore(tmp_path / "automation_state.json")
    running_key = store.batch_key("2026-07-14", "running-site")
    completed_key = store.batch_key("2026-07-14", "done-site")
    store.save(
        {
            running_key: BatchState(
                batch_key=running_key,
                run_date="2026-07-14",
                site_id="running-site",
                status="running",
                planned_count=60,
                attempted_count=7,
            ),
            completed_key: BatchState(
                batch_key=completed_key,
                run_date="2026-07-14",
                site_id="done-site",
                status="completed",
            ),
        }
    )

    recovered = store.recover_interrupted()

    assert recovered[running_key].status == "queued"
    assert recovered[running_key].attempted_count == 7
    assert "重新扫描" in recovered[running_key].stop_reason
    assert recovered[completed_key].status == "completed"


def test_attempt_counts_and_retry_backoff_are_persisted(tmp_path):
    now = datetime(2026, 7, 14, 8, 0, 0)
    store = AutomationStateStore(
        tmp_path / "automation_state.json", now_func=lambda: now
    )
    store.create_or_update("2026-07-14", "site-1", planned_count=2)

    first_attempt = store.record_attempt("2026-07-14", "site-1", success=True)
    second_attempt = store.record_attempt("2026-07-14", "site-1", success=False)
    first_retry = store.schedule_retry(
        "2026-07-14", "site-1", reason="HTTP 503"
    )
    second_retry = store.schedule_retry(
        "2026-07-14", "site-1", reason="HTTP 503"
    )
    third_retry = store.schedule_retry(
        "2026-07-14", "site-1", reason="HTTP 503"
    )
    exhausted = store.schedule_retry(
        "2026-07-14", "site-1", reason="HTTP 503"
    )

    assert first_attempt.success_count == 1
    assert second_attempt.attempted_count == 2
    assert second_attempt.failed_count == 1
    assert first_retry.next_retry_at == "2026-07-14T08:05:00"
    assert second_retry.next_retry_at == "2026-07-14T08:15:00"
    assert third_retry.next_retry_at == "2026-07-14T08:30:00"
    assert exhausted.status == "blocked"
    assert exhausted.retry_count == 3


def test_transition_many_updates_all_batches_with_one_atomic_save(tmp_path, monkeypatch):
    store = AutomationStateStore(tmp_path / "automation_state.json")
    store.create_or_update("2026-07-14", "site-1", planned_count=2)
    store.create_or_update("2026-07-14", "site-2", planned_count=3)
    writes = 0
    real_save = store._save_unlocked

    def counted_save(batches, *, completing_recovery=False):
        nonlocal writes
        writes += 1
        return real_save(batches, completing_recovery=completing_recovery)

    monkeypatch.setattr(store, "_save_unlocked", counted_save)

    transitioned = store.transition_many(
        [("2026-07-14", "site-1"), ("2026-07-14", "site-2")],
        "running",
    )

    assert writes == 1
    assert set(transitioned) == {"2026-07-14|site-1", "2026-07-14|site-2"}
    assert all(batch.status == "running" for batch in transitioned.values())


def test_rebuild_from_evidence_blocks_pending_attempt(tmp_path):
    now = datetime(2026, 7, 14, 8, 0, 0)
    store = AutomationStateStore(
        tmp_path / "automation_state.json", now_func=lambda: now
    )

    rebuilt = store.rebuild_from_evidence(
        "2026-07-14",
        "site-1",
        success_count=2,
        pending_count=4,
        daily_limit=60,
        pending_attempt=True,
    )

    assert rebuilt.status == "blocked"
    assert rebuilt.stop_code == "pending_attempt"
    assert rebuilt.attempted_count == 2
    assert rebuilt.counts_trusted is False
    assert rebuilt.recovered_at == "2026-07-14T08:00:00"
    assert rebuilt.recovery_note


def test_latest_inventory_keeps_previous_day_warning_visible(tmp_path):
    store = AutomationStateStore(tmp_path / "automation_state.json")
    store.record_inventory("2026-07-13", "site-1", count=20, threshold=500)

    latest = store.latest_inventory("site-1")

    assert latest is not None
    assert latest.run_date == "2026-07-13"
    assert latest.inventory_count == 20


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "unknown"},
        {"planned_count": -1},
        {"inventory_count": -1},
    ],
)
def test_invalid_persisted_batch_is_rejected(tmp_path, changes):
    store = AutomationStateStore(tmp_path / "automation_state.json")
    key = store.batch_key("2026-07-14", "site-1")
    batch = BatchState(
        batch_key=key,
        run_date="2026-07-14",
        site_id="site-1",
    )

    with pytest.raises(StateFileError):
        store.save({key: replace(batch, **changes)})
