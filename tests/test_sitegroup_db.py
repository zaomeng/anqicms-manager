from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from core.sitegroup_db import SiteGroupDB, db_path


def test_db_path_helper():
    cfg = Path("/tmp/workspace/config.ini")
    assert db_path(cfg) == Path("/tmp/workspace/设置/sitegroup_cache.db")


def test_sqlite_wal_mode_and_schema(tmp_path):
    target = db_path(tmp_path / "config.ini")
    with SiteGroupDB(target) as db:
        assert target.exists()
        conn = sqlite3.connect(str(target))
        try:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            assert str(mode).lower() == "wal"
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert {
                "site_status_cache",
                "backlink_audit_history",
                "operation_audit_log",
            }.issubset(tables)
        finally:
            conn.close()


def test_site_status_cache_crud_and_partial_updates(tmp_path):
    stamp = [datetime(2026, 11, 25, 10, 0, 0)]
    with SiteGroupDB(tmp_path / "cache.db", now=lambda: stamp[0]) as db:
        assert db.get_site_status("missing") is None
        assert db.list_site_statuses() == []

        db.upsert_site_status(
            "site-b",
            site_url="https://b.example.com",
            site_title="站点B",
            current_version="3.6.0",
            latest_version="3.6.2",
            update_available=True,
            mcp_ok=True,
            mcp_tools=["check_update", "friendlink_list"],
            raw={"server": "anqicms"},
        )
        row = db.get_site_status("site-b")
        assert row is not None
        assert row["site_url"] == "https://b.example.com"
        assert row["site_title"] == "站点B"
        assert row["current_version"] == "3.6.0"
        assert row["latest_version"] == "3.6.2"
        assert row["update_available"] == 1
        assert row["mcp_ok"] == 1
        assert json.loads(row["mcp_tools_json"]) == ["check_update", "friendlink_list"]
        assert json.loads(row["raw_json"]) == {"server": "anqicms"}
        assert row["link_count"] is None
        assert row["last_checked_at"] == "2026-11-25T10:00:00"

        # Partial update should preserve untouched fields while updating explicit 0/False.
        stamp[0] = datetime(2026, 11, 25, 10, 5, 0)
        db.upsert_site_status(
            "site-b",
            link_count=12,
            broken_link_count=0,
            update_available=False,
        )
        updated = db.get_site_status("site-b")
        assert updated is not None
        assert updated["site_url"] == "https://b.example.com"
        assert updated["current_version"] == "3.6.0"
        assert updated["mcp_ok"] == 1
        assert updated["update_available"] == 0
        assert updated["link_count"] == 12
        assert updated["broken_link_count"] == 0
        assert updated["last_checked_at"] == "2026-11-25T10:05:00"

        # Mapping-based upsert
        db.upsert_site_status(
            {
                "site_name": "site-a",
                "site_url": "https://a.example.com",
                "mcp_ok": False,
            }
        )
        rows = db.list_site_statuses()
        assert [r["site_name"] for r in rows] == ["site-a", "site-b"]
        assert db.delete_site_status("site-a") is True
        assert db.delete_site_status("site-a") is False


def test_backlink_audit_history_and_filters(tmp_path):
    with SiteGroupDB(tmp_path / "cache.db") as db:
        id1 = db.record_backlink_audit(
            "site-a",
            "https://peer1.example.com",
            "友链1",
            "ok",
            200,
            "回链正常",
            checked_at="2026-11-25T08:00:00",
        )
        id2 = db.record_backlink_audit(
            "site-a",
            "https://peer2.example.com",
            "友链2",
            "broken",
            404,
            "未检测到反链",
            checked_at="2026-11-25T09:00:00",
        )
        id3 = db.record_backlink_audit(
            "site-b",
            "https://peer3.example.com",
            "友链3",
            "broken",
            500,
            "目标不可达",
            checked_at="2026-11-25T10:00:00",
        )
        assert id1 < id2 < id3

        all_rows = db.list_backlink_audits()
        assert [r["id"] for r in all_rows] == [id3, id2, id1]

        site_a_rows = db.list_backlink_audits(site_name="site-a")
        assert [r["id"] for r in site_a_rows] == [id2, id1]

        broken_rows = db.list_backlink_audits(status="broken")
        assert [r["id"] for r in broken_rows] == [id3, id2]

        filtered = db.list_backlink_audits(site_name="site-a", status="broken", limit=1)
        assert len(filtered) == 1
        assert filtered[0]["target_url"] == "https://peer2.example.com"


def test_operation_audit_log_and_close_lifecycle(tmp_path):
    db = SiteGroupDB(tmp_path / "cache.db")
    op1 = db.record_operation(
        "friendlink",
        "crosslink_mesh",
        site_name="site-a",
        status="ok",
        summary="添加 3 条互链",
        detail={"created": 3},
        created_at="2026-11-25T08:00:00",
    )
    op2 = db.record_operation(
        "upgrade",
        "batch_upgrade",
        site_name="site-b",
        status="upgraded",
        summary="升级到 3.6.2",
        detail={"from": "3.6.0", "to": "3.6.2"},
        created_at="2026-11-25T09:00:00",
    )
    assert op2 > op1

    all_ops = db.list_operations()
    assert [op["category"] for op in all_ops] == ["upgrade", "friendlink"]
    assert json.loads(all_ops[1]["detail_json"]) == {"created": 3}

    upgrade_ops = db.list_operations(category="upgrade", limit=10)
    assert len(upgrade_ops) == 1
    assert upgrade_ops[0]["site_name"] == "site-b"

    db.close()
    db.close()  # idempotent
    with pytest.raises(RuntimeError):
        db.list_site_statuses()


def test_concurrent_reads_and_writes(tmp_path):
    with SiteGroupDB(tmp_path / "concurrent.db") as db:
        def _worker(idx: int) -> None:
            name = f"site-{idx}"
            db.upsert_site_status(name, site_url=f"https://{name}.example.com", link_count=idx)
            db.record_backlink_audit(name, f"https://peer-{idx}.example.com", status="ok", http_status=200)
            db.record_operation("test", "write", site_name=name, status="ok", detail={"idx": idx})
            assert db.get_site_status(name) is not None

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_worker, range(40)))

        assert len(db.list_site_statuses()) == 40
        assert len(db.list_backlink_audits(limit=100)) == 40
        assert len(db.list_operations(limit=100)) == 40
