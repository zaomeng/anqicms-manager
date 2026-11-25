"""Thread-safe asynchronous background job manager for the Local Web Console."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.redaction import redact_text

MAX_JOB_LOGS = 200


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    return str(value)


@dataclass(slots=True)
class JobRecord:
    """Snapshot of a background job's state, progress, logs, and outcome."""

    job_id: str
    kind: str
    title: str
    status: str = "pending"  # pending | running | completed | failed | cancelled
    done: int = 0
    total: int = 0
    message: str = ""
    logs: list[dict[str, Any]] = field(default_factory=list)
    result: Any = None
    error: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    cancel_requested: bool = False

    def copy(self) -> JobRecord:
        """Return an independent snapshot safe for cross-thread reads."""
        return JobRecord(
            job_id=self.job_id,
            kind=self.kind,
            title=self.title,
            status=self.status,
            done=self.done,
            total=self.total,
            message=self.message,
            logs=[dict(entry) for entry in self.logs],
            result=_to_jsonable(self.result),
            error=self.error,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            cancel_requested=self.cancel_requested,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize job record into a JSON-compatible dictionary."""
        if self.total > 0:
            percent = round(min(100.0, max(0.0, (self.done / self.total) * 100.0)), 1)
        elif self.status == "completed":
            percent = 100.0
        else:
            percent = 0.0
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "percent": percent,
            "message": self.message,
            "logs": [dict(entry) for entry in self.logs],
            "result": _to_jsonable(self.result),
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancel_requested": self.cancel_requested,
        }


class _ManagedJob:
    __slots__ = ("record", "logs", "cancel_event")

    def __init__(self, record: JobRecord) -> None:
        self.record = record
        self.logs: deque[dict[str, Any]] = deque(maxlen=MAX_JOB_LOGS)
        self.cancel_event = threading.Event()


class JobRunner:
    """Thread-safe async background job manager with progress tracking and cancellation."""

    def __init__(
        self,
        *,
        max_workers: int = 4,
        max_jobs: int = 200,
        redactor: Callable[[str], str] | None = None,
    ) -> None:
        self._max_workers = max(1, int(max_workers))
        self._max_jobs = max(10, int(max_jobs))
        self._redactor = redactor or redact_text
        self._lock = threading.RLock()
        self._jobs: dict[str, _ManagedJob] = {}
        self._order: deque[str] = deque()
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="web-job",
        )
        self._shutdown = False

    def _safe_redact(self, text: str) -> str:
        try:
            return self._redactor(text)
        except Exception:
            return redact_text(text)

    def _prune_unlocked(self) -> None:
        while len(self._order) > self._max_jobs:
            oldest_id = self._order[0]
            managed = self._jobs.get(oldest_id)
            if managed is not None and managed.record.status in {"pending", "running"}:
                break
            self._order.popleft()
            self._jobs.pop(oldest_id, None)

    def _snapshot_unlocked(self, managed: _ManagedJob) -> JobRecord:
        snap = managed.record.copy()
        snap.logs = [dict(entry) for entry in managed.logs]
        return snap

    def submit(
        self,
        kind: str,
        title: str,
        fn: Callable[[Callable[..., None], threading.Event], Any],
        *,
        total: int = 0,
    ) -> JobRecord:
        """Submit a background job ``fn(update_progress, cancel_event)`` and return its initial record."""
        job_id = uuid.uuid4().hex[:12]
        record = JobRecord(
            job_id=job_id,
            kind=str(kind or "job"),
            title=str(title or kind or "任务"),
            status="pending",
            done=0,
            total=max(0, int(total or 0)),
            message="等待执行",
            created_at=_now_iso(),
        )
        managed = _ManagedJob(record)

        with self._lock:
            if self._shutdown:
                raise RuntimeError("JobRunner 已关闭")
            self._jobs[job_id] = managed
            self._order.append(job_id)
            self._prune_unlocked()
            initial_snapshot = self._snapshot_unlocked(managed)

        def update_progress(
            done: int | None = None,
            total_count: int | None = None,
            message: str = "",
            item: Any = None,
        ) -> None:
            safe_msg = self._safe_redact(str(message)) if message else ""
            safe_item = _to_jsonable(item) if item is not None else None
            with self._lock:
                rec = managed.record
                if done is not None:
                    rec.done = max(0, int(done))
                if total_count is not None:
                    rec.total = max(0, int(total_count))
                if safe_msg:
                    rec.message = safe_msg
                if safe_msg or safe_item is not None:
                    entry: dict[str, Any] = {
                        "ts": _now_iso(),
                        "done": rec.done,
                        "total": rec.total,
                        "message": safe_msg,
                    }
                    if safe_item is not None:
                        entry["item"] = safe_item
                    managed.logs.append(entry)

        def _worker() -> None:
            with self._lock:
                if managed.cancel_event.is_set() or self._shutdown:
                    managed.record.status = "cancelled"
                    managed.record.message = managed.record.message or "已取消"
                    managed.record.finished_at = managed.record.finished_at or _now_iso()
                    return
                managed.record.status = "running"
                managed.record.started_at = _now_iso()
                managed.record.message = "正在执行..."

            try:
                result = fn(update_progress, managed.cancel_event)
            except Exception as exc:  # noqa: BLE001
                safe_error = self._safe_redact(str(exc)) or type(exc).__name__
                with self._lock:
                    if managed.cancel_event.is_set() and isinstance(exc, InterruptedError):
                        managed.record.status = "cancelled"
                        managed.record.message = "已取消"
                    else:
                        managed.record.status = "failed"
                        managed.record.error = safe_error
                        managed.record.message = f"失败：{safe_error}"
                    managed.record.finished_at = _now_iso()
                return

            with self._lock:
                managed.record.result = _to_jsonable(result)
                managed.record.finished_at = _now_iso()
                if managed.cancel_event.is_set():
                    managed.record.status = "cancelled"
                    if not managed.record.message or managed.record.message == "正在执行...":
                        managed.record.message = "已取消"
                else:
                    managed.record.status = "completed"
                    if managed.record.total > 0 and managed.record.done < managed.record.total:
                        managed.record.done = managed.record.total
                    if managed.record.message in {"", "正在执行...", "等待执行"}:
                        managed.record.message = "已完成"

        self._executor.submit(_worker)
        return initial_snapshot

    def get(self, job_id: str) -> JobRecord | None:
        """Return a thread-safe snapshot of ``job_id``, or ``None`` if not found."""
        with self._lock:
            managed = self._jobs.get(str(job_id or "").strip())
            if managed is None:
                return None
            return self._snapshot_unlocked(managed)

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        """Return newest-first job snapshots up to ``limit``."""
        cap = max(0, int(limit))
        with self._lock:
            ids = list(reversed(self._order))[:cap]
            return [
                self._snapshot_unlocked(self._jobs[jid])
                for jid in ids
                if jid in self._jobs
            ]

    def cancel(self, job_id: str) -> bool:
        """Request cancellation of ``job_id``; returns ``False`` if unknown."""
        with self._lock:
            managed = self._jobs.get(str(job_id or "").strip())
            if managed is None:
                return False
            managed.cancel_event.set()
            managed.record.cancel_requested = True
            if managed.record.status == "pending":
                managed.record.status = "cancelled"
                managed.record.message = "已取消"
                managed.record.finished_at = _now_iso()
            return True

    def shutdown(self, *, wait: bool = True, cancel_running: bool = True) -> None:
        """Cancel active jobs (if requested) and shut down the worker pool."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            if cancel_running:
                for managed in self._jobs.values():
                    if managed.record.status in {"pending", "running"}:
                        managed.cancel_event.set()
                        managed.record.cancel_requested = True
                        if managed.record.status == "pending":
                            managed.record.status = "cancelled"
                            managed.record.message = "服务关闭，已取消"
                            managed.record.finished_at = _now_iso()
        self._executor.shutdown(wait=wait, cancel_futures=True)
