"""Thread-safe SQLite cache & audit store for site-group operations.

Stored at ``设置/sitegroup_cache.db`` beside ``config.ini`` in WAL mode.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

DB_FILENAME = "sitegroup_cache.db"
DB_DIRNAME = "设置"

_UNSET = object()


def db_path(config_path: str | Path) -> Path:
    """Return the default ``设置/sitegroup_cache.db`` path beside ``config_path``."""
    return Path(config_path).parent / DB_DIRNAME / DB_FILENAME


def _coerce_optional_flag(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) else 0
    if isinstance(value, str):
        cleaned = value.strip().casefold()
        if not cleaned:
            return None
        if cleaned in {"1", "true", "yes", "y", "on"}:
            return 1
        if cleaned in {"0", "false", "no", "n", "off"}:
            return 0
    return 1 if bool(value) else 0


def _coerce_optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        return int(stripped)
    return int(value)  # type: ignore[arg-type]


def _serialize_json(value: object, *, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


class SiteGroupDB:
    """Thread-safe SQLite state store for site status cache and audit logs."""

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        raw_path = str(path)
        self._is_memory = raw_path == ":memory:"
        self.path = Path(raw_path)
        self._now = now
        self._lock = threading.RLock()
        if not self._is_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = self._open_connection()
        self._init_schema()

    def _open_connection(self) -> sqlite3.Connection:
        target = ":memory:" if self._is_memory else str(self.path)
        conn = sqlite3.connect(target, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _conn_unlocked(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SiteGroupDB 已关闭")
        return self._conn

    def _now_iso(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn_unlocked()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS site_status_cache (
                    site_name TEXT PRIMARY KEY,
                    site_url TEXT NOT NULL DEFAULT '',
                    site_title TEXT NOT NULL DEFAULT '',
                    current_version TEXT NOT NULL DEFAULT '',
                    latest_version TEXT NOT NULL DEFAULT '',
                    update_available INTEGER,
                    mcp_ok INTEGER,
                    mcp_tools_json TEXT NOT NULL DEFAULT '[]',
                    link_count INTEGER,
                    broken_link_count INTEGER,
                    last_checked_at TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS backlink_audit_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checked_at TEXT NOT NULL,
                    site_name TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    target_title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    http_status INTEGER,
                    message TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_backlink_audit_site_time
                    ON backlink_audit_history (site_name, checked_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_backlink_audit_status_time
                    ON backlink_audit_history (status, checked_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS operation_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    action TEXT NOT NULL,
                    site_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_operation_log_cat_time
                    ON operation_audit_log (category, created_at DESC, id DESC);
                """
            )
            conn.commit()

    # ------------------------------------------------------------------ site_status_cache

    def upsert_site_status(
        self,
        site_name: str | Mapping[str, Any],
        *,
        site_url: str | object = _UNSET,
        site_title: str | object = _UNSET,
        current_version: str | object = _UNSET,
        latest_version: str | object = _UNSET,
        update_available: bool | int | None | object = _UNSET,
        mcp_ok: bool | int | None | object = _UNSET,
        mcp_tools: Sequence[str] | object = _UNSET,
        mcp_tools_json: str | object = _UNSET,
        link_count: int | None | object = _UNSET,
        broken_link_count: int | None | object = _UNSET,
        last_checked_at: str | object = _UNSET,
        raw: Any = _UNSET,
        raw_json: str | object = _UNSET,
    ) -> None:
        """Insert or partially update cached status for ``site_name``."""
        if isinstance(site_name, Mapping):
            payload = dict(site_name)
            resolved_name = str(payload.get("site_name") or "").strip()
            site_url = payload.get("site_url", site_url)
            site_title = payload.get("site_title", site_title)
            current_version = payload.get("current_version", current_version)
            latest_version = payload.get("latest_version", latest_version)
            update_available = payload.get("update_available", update_available)
            mcp_ok = payload.get("mcp_ok", mcp_ok)
            if "mcp_tools" in payload and mcp_tools is _UNSET:
                mcp_tools = payload["mcp_tools"]
            if "mcp_tools_json" in payload and mcp_tools_json is _UNSET:
                mcp_tools_json = payload["mcp_tools_json"]
            link_count = payload.get("link_count", link_count)
            broken_link_count = payload.get("broken_link_count", broken_link_count)
            last_checked_at = payload.get("last_checked_at", last_checked_at)
            if "raw" in payload and raw is _UNSET:
                raw = payload["raw"]
            if "raw_json" in payload and raw_json is _UNSET:
                raw_json = payload["raw_json"]
        else:
            resolved_name = str(site_name or "").strip()

        if not resolved_name:
            raise ValueError("site_name 不能为空")

        provided: dict[str, Any] = {}
        if site_url is not _UNSET:
            provided["site_url"] = "" if site_url is None else str(site_url)
        if site_title is not _UNSET:
            provided["site_title"] = "" if site_title is None else str(site_title)
        if current_version is not _UNSET:
            provided["current_version"] = "" if current_version is None else str(current_version)
        if latest_version is not _UNSET:
            provided["latest_version"] = "" if latest_version is None else str(latest_version)
        if update_available is not _UNSET:
            provided["update_available"] = _coerce_optional_flag(update_available)
        if mcp_ok is not _UNSET:
            provided["mcp_ok"] = _coerce_optional_flag(mcp_ok)
        if mcp_tools_json is not _UNSET:
            provided["mcp_tools_json"] = _serialize_json(mcp_tools_json, default="[]")
        elif mcp_tools is not _UNSET:
            tools_list = [] if mcp_tools is None else list(mcp_tools)  # type: ignore[arg-type]
            provided["mcp_tools_json"] = json.dumps(tools_list, ensure_ascii=False, default=str)
        if link_count is not _UNSET:
            provided["link_count"] = _coerce_optional_int(link_count)
        if broken_link_count is not _UNSET:
            provided["broken_link_count"] = _coerce_optional_int(broken_link_count)
        if raw_json is not _UNSET:
            provided["raw_json"] = _serialize_json(raw_json, default="{}")
        elif raw is not _UNSET:
            provided["raw_json"] = _serialize_json(raw, default="{}")

        if last_checked_at is _UNSET or last_checked_at is None:
            provided["last_checked_at"] = self._now_iso()
        else:
            provided["last_checked_at"] = str(last_checked_at)

        insert_values: dict[str, Any] = {
            "site_name": resolved_name,
            "site_url": provided.get("site_url", ""),
            "site_title": provided.get("site_title", ""),
            "current_version": provided.get("current_version", ""),
            "latest_version": provided.get("latest_version", ""),
            "update_available": provided.get("update_available", None),
            "mcp_ok": provided.get("mcp_ok", None),
            "mcp_tools_json": provided.get("mcp_tools_json", "[]"),
            "link_count": provided.get("link_count", None),
            "broken_link_count": provided.get("broken_link_count", None),
            "last_checked_at": provided["last_checked_at"],
            "raw_json": provided.get("raw_json", "{}"),
        }

        columns = list(insert_values.keys())
        placeholders = ", ".join("?" for _ in columns)
        update_assignments = ", ".join(
            f"{col} = excluded.{col}" for col in provided.keys()
        )
        sql = (
            f"INSERT INTO site_status_cache ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(site_name) DO UPDATE SET {update_assignments}"
        )

        with self._lock:
            conn = self._conn_unlocked()
            conn.execute(sql, tuple(insert_values[col] for col in columns))
            conn.commit()

    def get_site_status(self, site_name: str) -> dict[str, Any] | None:
        """Fetch cached status row for ``site_name`` or ``None``."""
        key = str(site_name or "").strip()
        if not key:
            return None
        with self._lock:
            conn = self._conn_unlocked()
            row = conn.execute(
                "SELECT * FROM site_status_cache WHERE site_name = ?",
                (key,),
            ).fetchone()
        return _row_to_dict(row)

    def list_site_statuses(self) -> list[dict[str, Any]]:
        """List all cached site status rows ordered by ``site_name``."""
        with self._lock:
            conn = self._conn_unlocked()
            rows = conn.execute(
                "SELECT * FROM site_status_cache ORDER BY site_name ASC"
            ).fetchall()
        return [_row_to_dict(row) for row in rows if row is not None]

    def delete_site_status(self, site_name: str) -> bool:
        """Delete cached status for ``site_name``; return whether a row was removed."""
        key = str(site_name or "").strip()
        if not key:
            return False
        with self._lock:
            conn = self._conn_unlocked()
            cur = conn.execute(
                "DELETE FROM site_status_cache WHERE site_name = ?",
                (key,),
            )
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ backlink_audit_history

    def record_backlink_audit(
        self,
        site_name: str,
        target_url: str,
        target_title: str = "",
        status: str = "",
        http_status: int | None = None,
        message: str = "",
        *,
        checked_at: str | None = None,
    ) -> int:
        """Insert a backlink audit entry and return its row id."""
        stamp = checked_at if checked_at is not None else self._now_iso()
        http_val = _coerce_optional_int(http_status)
        with self._lock:
            conn = self._conn_unlocked()
            cur = conn.execute(
                """
                INSERT INTO backlink_audit_history (
                    checked_at, site_name, target_url, target_title, status, http_status, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(stamp),
                    str(site_name or ""),
                    str(target_url or ""),
                    str(target_title or ""),
                    str(status or ""),
                    http_val,
                    str(message or ""),
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def list_backlink_audits(
        self,
        site_name: str | None = None,
        status: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """List backlink audit history newest first with optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if site_name is not None and str(site_name).strip():
            clauses.append("site_name = ?")
            params.append(str(site_name).strip())
        if status is not None and str(status).strip():
            clauses.append("status = ?")
            params.append(str(status).strip())

        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        safe_limit = max(0, int(limit))
        sql = (
            "SELECT * FROM backlink_audit_history "
            f"{where}"
            "ORDER BY checked_at DESC, id DESC LIMIT ?"
        )
        params.append(safe_limit)

        with self._lock:
            conn = self._conn_unlocked()
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_dict(row) for row in rows if row is not None]

    # ------------------------------------------------------------------ operation_audit_log

    def record_operation(
        self,
        category: str,
        action: str,
        site_name: str = "",
        status: str = "",
        summary: str = "",
        detail: Any = None,
        *,
        detail_json: str | None = None,
        created_at: str | None = None,
    ) -> int:
        """Insert an operation audit log entry and return its row id."""
        stamp = created_at if created_at is not None else self._now_iso()
        if detail_json is not None:
            serialized_detail = str(detail_json)
        elif detail is None:
            serialized_detail = "{}"
        else:
            serialized_detail = _serialize_json(detail, default="{}")

        with self._lock:
            conn = self._conn_unlocked()
            cur = conn.execute(
                """
                INSERT INTO operation_audit_log (
                    created_at, category, action, site_name, status, summary, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(stamp),
                    str(category or ""),
                    str(action or ""),
                    str(site_name or ""),
                    str(status or ""),
                    str(summary or ""),
                    serialized_detail,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def list_operations(
        self,
        category: str | None = None,
        limit: int = 200,
        *,
        site_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List operation audit logs newest first with optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if category is not None and str(category).strip():
            clauses.append("category = ?")
            params.append(str(category).strip())
        if site_name is not None and str(site_name).strip():
            clauses.append("site_name = ?")
            params.append(str(site_name).strip())

        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        safe_limit = max(0, int(limit))
        sql = (
            "SELECT * FROM operation_audit_log "
            f"{where}"
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        params.append(safe_limit)

        with self._lock:
            conn = self._conn_unlocked()
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_dict(row) for row in rows if row is not None]

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Close the underlying SQLite connection (idempotent)."""
        with self._lock:
            conn = self._conn
            self._conn = None
        if conn is not None:
            conn.close()

    def __enter__(self) -> SiteGroupDB:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
