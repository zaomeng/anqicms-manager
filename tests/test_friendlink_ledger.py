"""Unit tests for core/friendlink_ledger.py."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core.friendlink_ledger import FriendLinkLedger, ledger_path
from models.sitegroup import (
    CROSSLINK_HUB,
    CROSSLINK_MESH,
    LINK_CREATED,
    LINK_FAILED,
    LINK_SKIPPED_EXISTS,
    CrossLinkItem,
    CrossLinkPlan,
    FriendLinkSpec,
    LedgerEntry,
)


def _sample_plan(plan_id: str = "plan-1", *, mode: str = CROSSLINK_MESH, created_at: str = "2026-11-21T10:00:00") -> CrossLinkPlan:
    return CrossLinkPlan(
        plan_id=plan_id,
        mode=mode,
        created_at=created_at,
        hub_site_id="",
        bidirectional=True,
        items=(
            CrossLinkItem(
                source_site_id="s1",
                source_name="站A",
                target_site_id="s2",
                target_name="站B",
                spec=FriendLinkSpec(title="站B", link="https://b.example.com/"),
            ),
            CrossLinkItem(
                source_site_id="s2",
                source_name="站B",
                target_site_id="s1",
                target_name="站A",
                spec=FriendLinkSpec(title="站A", link="https://a.example.com/"),
            ),
        ),
    )


def test_ledger_path_points_to_settings_dir(tmp_path: Path) -> None:
    cfg = tmp_path / "config.ini"
    assert ledger_path(cfg) == tmp_path / "设置" / "friendlink_ledger.json"


def test_ledger_empty_when_file_missing(tmp_path: Path) -> None:
    ledger = FriendLinkLedger(tmp_path / "设置" / "friendlink_ledger.json")
    assert ledger.list_plans() == []
    assert ledger.created_links("missing") == []


def test_ledger_record_list_created_and_mark_rolled_back(tmp_path: Path) -> None:
    path = tmp_path / "设置" / "friendlink_ledger.json"
    clock = [datetime(2026, 11, 21, 10, 0, 0)]
    ledger = FriendLinkLedger(path, now=lambda: clock[0])

    plan1 = _sample_plan("plan-old", mode=CROSSLINK_MESH, created_at="2026-11-21T09:00:00")
    plan2 = _sample_plan("plan-new", mode=CROSSLINK_HUB, created_at="2026-11-21T10:00:00")
    ledger.record_plan(plan1)
    ledger.record_plan(plan2)

    ledger.record_results(
        "plan-old",
        [
            LedgerEntry("plan-old", "s1", "站A", "s2", "https://b.example.com/", "站B", LINK_CREATED, "2026-11-21T09:00:01"),
            LedgerEntry("plan-old", "s2", "站B", "s1", "https://a.example.com/", "站A", LINK_SKIPPED_EXISTS, "2026-11-21T09:00:02"),
            LedgerEntry("plan-old", "s2", "站B", "s3", "https://c.example.com/", "站C", LINK_FAILED, "2026-11-21T09:00:03"),
        ],
    )
    ledger.record_results(
        "plan-new",
        [
            LedgerEntry("plan-new", "s1", "站A", "s2", "https://b.example.com/", "站B", LINK_CREATED, "2026-11-21T10:00:01"),
            LedgerEntry("plan-new", "s2", "站B", "s1", "https://a.example.com/", "站A", LINK_CREATED, "2026-11-21T10:00:02"),
        ],
    )

    # Newest plan comes first
    plans = ledger.list_plans()
    assert [p.plan_id for p in plans] == ["plan-new", "plan-old"]
    assert plans[0].created_count == 2
    assert plans[0].rolled_back_count == 0
    assert plans[0].site_names == ("站A", "站B")
    assert plans[1].created_count == 1
    assert plans[1].rolled_back_count == 0

    # created_links returns ONLY status == created and not rolled back
    created = ledger.created_links("plan-old")
    assert len(created) == 1
    assert created[0].link == "https://b.example.com/"

    # Mark rolled back (even if trailing slash differs)
    clock[0] = datetime(2026, 11, 21, 11, 30, 0)
    ledger.mark_rolled_back("plan-old", "s1", "https://b.example.com")
    assert ledger.created_links("plan-old") == []

    # Re-open from disk to verify persistence
    reloaded = FriendLinkLedger(path)
    plans_after = {p.plan_id: p for p in reloaded.list_plans()}
    assert plans_after["plan-old"].created_count == 1
    assert plans_after["plan-old"].rolled_back_count == 1
    assert reloaded.created_links("plan-old") == []
    assert len(reloaded.created_links("plan-new")) == 2


def test_ledger_quarantines_corrupt_json_and_recovers(tmp_path: Path) -> None:
    path = tmp_path / "设置" / "friendlink_ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    fixed_now = datetime(2026, 11, 21, 12, 34, 56)
    ledger = FriendLinkLedger(path, now=lambda: fixed_now)

    assert ledger.list_plans() == []
    backup = path.with_name("friendlink_ledger.json.corrupt-20261121-123456")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "{not valid json"
    assert not path.exists()

    # Subsequent writes recreate a clean v1 file
    ledger.record_plan(_sample_plan("fresh"))
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert "fresh" in data["plans"]


def test_ledger_quarantines_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / "设置" / "friendlink_ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 999, "plans": {}}), encoding="utf-8")

    fixed_now = datetime(2026, 11, 21, 8, 9, 10)
    ledger = FriendLinkLedger(path, now=lambda: fixed_now)

    assert ledger.list_plans() == []
    assert path.with_name("friendlink_ledger.json.corrupt-20261121-080910").exists()


def test_ledger_concurrent_writes_preserve_all_entries(tmp_path: Path) -> None:
    path = tmp_path / "设置" / "friendlink_ledger.json"
    ledger = FriendLinkLedger(path)
    ledger.record_plan(_sample_plan("concurrent-plan"))

    worker_count = 10
    entries_per_worker = 5
    barrier = threading.Barrier(worker_count)

    def _write(worker_idx: int) -> None:
        barrier.wait()
        batch = [
            LedgerEntry(
                plan_id="concurrent-plan",
                source_site_id=f"s{worker_idx}",
                source_name=f"站{worker_idx}",
                target_site_id=f"t{item_idx}",
                link=f"https://t{worker_idx}-{item_idx}.example.com/",
                title=f"目标{item_idx}",
                status=LINK_CREATED,
                created_at="2026-11-21T10:00:00",
            )
            for item_idx in range(entries_per_worker)
        ]
        ledger.record_results("concurrent-plan", batch)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(_write, idx) for idx in range(worker_count)]
        for future in futures:
            future.result()

    created = ledger.created_links("concurrent-plan")
    assert len(created) == worker_count * entries_per_worker
    plans = ledger.list_plans()
    assert len(plans) == 1
    assert plans[0].created_count == worker_count * entries_per_worker
