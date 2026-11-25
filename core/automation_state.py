from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from collections.abc import Iterable
from typing import Callable

from core.state_store import StateFileError, atomic_write_text


BATCH_STATUSES = frozenset(
    {"pending", "queued", "running", "retry_wait", "blocked", "stopped", "completed"}
)


class StateRecoveryRequired(StateFileError):
    """Raised until quarantined automation state is explicitly rebuilt."""


@dataclass(frozen=True, slots=True)
class BatchState:
    batch_key: str
    run_date: str
    site_id: str
    status: str = "pending"
    planned_count: int = 0
    attempted_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    retry_count: int = 0
    stop_code: str = ""
    stop_reason: str = ""
    config_fingerprint: str = ""
    started_at: str = ""
    finished_at: str = ""
    next_retry_at: str = ""
    inventory_count: int | None = None
    inventory_threshold: int = 500
    inventory_unreadable: int = 0
    inventory_checked_at: str = ""
    inventory_reason: str = ""
    recovered_at: str = ""
    recovery_note: str = ""
    counts_trusted: bool = True


class AutomationStateStore:
    """Thread-safe persistence for one durable daily batch per site."""

    VERSION = 3
    RETRY_DELAYS_MINUTES = (5, 15, 30)

    def __init__(
        self,
        path: str | Path,
        now_func: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.path = Path(path)
        self.now_func = now_func
        self._lock = threading.RLock()
        self.recovery_marker_path = self.path.with_name(
            f"{self.path.name}.recovery-required"
        )
        self.recovery_required = self.recovery_marker_path.exists()
        self.last_quarantined_path: Path | None = None
        if self.recovery_required:
            try:
                quarantined_name = self.recovery_marker_path.read_text(
                    encoding="utf-8"
                ).strip()
            except (OSError, UnicodeError):
                quarantined_name = ""
            if quarantined_name:
                self.last_quarantined_path = self.path.with_name(quarantined_name)

    @staticmethod
    def batch_key(run_date: str | date, site_id: str) -> str:
        normalized_date = run_date.isoformat() if isinstance(run_date, date) else str(run_date)
        try:
            date.fromisoformat(normalized_date)
        except ValueError as exc:
            raise ValueError("批次日期必须为 YYYY-MM-DD") from exc
        normalized_site_id = str(site_id).strip()
        if not normalized_site_id or "|" in normalized_site_id:
            raise ValueError("site_id 不能为空且不能包含 |")
        return f"{normalized_date}|{normalized_site_id}"

    def load(self, *, quarantine_corrupt: bool = True) -> dict[str, BatchState]:
        with self._lock:
            self._raise_if_recovery_required()
            if not self.path.exists():
                return {}
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                decoded = self._decode(payload)
                return decoded
            except (OSError, ValueError, TypeError, StateFileError) as exc:
                if quarantine_corrupt:
                    self.last_quarantined_path = self.quarantine_corrupt()
                    self.recovery_required = True
                    try:
                        atomic_write_text(
                            self.recovery_marker_path,
                            self.last_quarantined_path.name
                            if self.last_quarantined_path is not None
                            else "unknown",
                        )
                    except OSError as marker_exc:
                        raise StateFileError(
                            f"自动化状态损坏且无法标记待恢复: {self.path.name}"
                        ) from marker_exc
                    raise StateRecoveryRequired(
                        f"自动化状态已隔离，必须完成全站重建: {self.path.name}"
                    ) from exc
                if isinstance(exc, StateFileError):
                    raise
                raise StateFileError(f"自动化状态损坏: {self.path.name}") from exc

    def save(self, batches: dict[str, BatchState]) -> None:
        with self._lock:
            self._raise_if_recovery_required()
            self._save_unlocked(batches)

    def complete_recovery(self, batches: dict[str, BatchState]) -> None:
        """Atomically install a complete evidence-based rebuild and unlock writes."""
        with self._lock:
            if not self.recovery_required and not self.recovery_marker_path.exists():
                raise StateRecoveryRequired("自动化状态当前不处于待重建状态")
            self._save_unlocked(batches, completing_recovery=True)

    def _save_unlocked(
        self,
        batches: dict[str, BatchState],
        *,
        completing_recovery: bool = False,
    ) -> None:
        if not completing_recovery:
            self._raise_if_recovery_required()
        validated = self._decode(
            {
                "version": self.VERSION,
                "batches": {key: asdict(value) for key, value in batches.items()},
            }
        )
        payload = {
            "version": self.VERSION,
            "batches": {key: asdict(value) for key, value in sorted(validated.items())},
        }
        atomic_write_text(
            self.path,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        )
        if completing_recovery:
            try:
                self.recovery_marker_path.unlink(missing_ok=True)
            except OSError as exc:
                self.recovery_required = True
                raise StateFileError("自动化状态已重建，但无法解除恢复锁") from exc
            self.recovery_required = False

    def _raise_if_recovery_required(self) -> None:
        if self.recovery_required or self.recovery_marker_path.exists():
            self.recovery_required = True
            raise StateRecoveryRequired(
                f"自动化状态等待全站重建: {self.path.name}"
            )

    def get(self, run_date: str | date, site_id: str) -> BatchState | None:
        return self.load().get(self.batch_key(run_date, site_id))

    def put(self, batch: BatchState) -> BatchState:
        with self._lock:
            batches = self.load()
            batches[batch.batch_key] = batch
            self.save(batches)
        return batch

    def create_or_update(
        self,
        run_date: str | date,
        site_id: str,
        **changes: object,
    ) -> BatchState:
        key = self.batch_key(run_date, site_id)
        normalized_site_id = str(site_id).strip()
        with self._lock:
            batches = self.load()
            current = batches.get(
                key,
                BatchState(
                    batch_key=key,
                    run_date=key.split("|", 1)[0],
                    site_id=normalized_site_id,
                ),
            )
            updated = replace(current, **changes)
            batches[key] = updated
            self.save(batches)
            return updated

    def transition(
        self,
        run_date: str | date,
        site_id: str,
        status: str,
        **changes: object,
    ) -> BatchState:
        if status not in BATCH_STATUSES:
            raise ValueError(f"未知批次状态: {status}")
        now = self.now_func().isoformat(timespec="seconds")
        if status == "running":
            changes.setdefault("started_at", now)
            changes.setdefault("finished_at", "")
        elif status in {"blocked", "stopped", "completed"}:
            changes.setdefault("finished_at", now)
        return self.create_or_update(run_date, site_id, status=status, **changes)

    def transition_many(
        self,
        identities: Iterable[tuple[str | date, str]],
        status: str,
        **changes: object,
    ) -> dict[str, BatchState]:
        """Transition multiple batches with one read-modify-write transaction."""
        if status not in BATCH_STATUSES:
            raise ValueError(f"未知批次状态: {status}")
        normalized: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for run_date, site_id in identities:
            key = self.batch_key(run_date, site_id)
            if key in seen:
                raise ValueError(f"批次重复: {key}")
            seen.add(key)
            normalized.append((key, key.split("|", 1)[0], str(site_id).strip()))
        if not normalized:
            return {}
        now = self.now_func().isoformat(timespec="seconds")
        common_changes = dict(changes)
        if status == "running":
            common_changes.setdefault("started_at", now)
            common_changes.setdefault("finished_at", "")
        elif status in {"blocked", "stopped", "completed"}:
            common_changes.setdefault("finished_at", now)
        with self._lock:
            batches = self.load()
            transitioned: dict[str, BatchState] = {}
            for key, normalized_date, normalized_site_id in normalized:
                current = batches.get(
                    key,
                    BatchState(
                        batch_key=key,
                        run_date=normalized_date,
                        site_id=normalized_site_id,
                    ),
                )
                updated = replace(current, status=status, **common_changes)
                batches[key] = updated
                transitioned[key] = updated
            self._save_unlocked(batches)
            return transitioned

    def record_inventory(
        self,
        run_date: str | date,
        site_id: str,
        *,
        count: int,
        threshold: int,
        unreadable: int = 0,
        reason: str = "batch_complete",
    ) -> BatchState:
        if min(count, threshold, unreadable) < 0:
            raise ValueError("库存数量和阈值不能为负数")
        return self.create_or_update(
            run_date,
            site_id,
            inventory_count=count,
            inventory_threshold=threshold,
            inventory_unreadable=unreadable,
            inventory_checked_at=self.now_func().isoformat(timespec="seconds"),
            inventory_reason=reason,
        )

    def record_attempt(
        self,
        run_date: str | date,
        site_id: str,
        *,
        success: bool,
    ) -> BatchState:
        current = self.get(run_date, site_id)
        if current is None:
            current = self.create_or_update(run_date, site_id)
        return self.create_or_update(
            run_date,
            site_id,
            attempted_count=current.attempted_count + 1,
            success_count=current.success_count + int(success),
            failed_count=current.failed_count + int(not success),
        )

    def schedule_retry(
        self,
        run_date: str | date,
        site_id: str,
        *,
        reason: str,
    ) -> BatchState:
        current = self.get(run_date, site_id)
        if current is None:
            current = self.create_or_update(run_date, site_id)
        if current.retry_count >= len(self.RETRY_DELAYS_MINUTES):
            return self.transition(
                run_date,
                site_id,
                "blocked",
                stop_code="retry_exhausted",
                stop_reason=f"重试次数已用尽: {reason}",
                next_retry_at="",
            )
        delay = self.RETRY_DELAYS_MINUTES[current.retry_count]
        return self.transition(
            run_date,
            site_id,
            "retry_wait",
            retry_count=current.retry_count + 1,
            stop_code="transient",
            stop_reason=reason,
            next_retry_at=(self.now_func() + timedelta(minutes=delay)).isoformat(
                timespec="seconds"
            ),
        )

    def recover_interrupted(self) -> dict[str, BatchState]:
        """Return interrupted running batches to the queue for a safe rescan."""
        with self._lock:
            batches = self.load()
            changed = False
            for key, batch in tuple(batches.items()):
                if batch.status == "running":
                    batches[key] = replace(
                        batch,
                        status="queued",
                        stop_reason="程序中断，等待重新扫描未完成文章",
                        finished_at="",
                    )
                    changed = True
            if changed:
                self.save(batches)
            return batches

    def rebuild_from_evidence(
        self,
        run_date: str | date,
        site_id: str,
        *,
        success_count: int,
        pending_count: int,
        daily_limit: int,
        pending_attempt: bool = False,
        note: str = "自动化状态损坏后按本地证据重建",
        evidence_error: str = "",
    ) -> BatchState:
        if min(success_count, pending_count, daily_limit) < 0:
            raise ValueError("重建计数不能为负数")
        now = self.now_func().isoformat(timespec="seconds")
        if evidence_error:
            status = "blocked"
            stop_code = "scan_error"
            stop_reason = f"状态重建所需扫描失败: {evidence_error}"
        elif pending_attempt:
            status = "blocked"
            stop_code = "pending_attempt"
            stop_reason = "存在未确认的发布请求，已阻断自动恢复"
        elif success_count >= daily_limit:
            status = "completed"
            stop_code = "quota_exhausted"
            stop_reason = "已达到每日发布上限"
        elif pending_count > 0:
            status = "queued"
            stop_code = "state_rebuilt"
            stop_reason = "状态已重建，等待重新扫描未完成文章"
        else:
            status = "completed"
            stop_code = "empty"
            stop_reason = "没有可发布文章"
        key = self.batch_key(run_date, site_id)
        rebuilt = BatchState(
            batch_key=key,
            run_date=key.split("|", 1)[0],
            site_id=str(site_id).strip(),
            status=status,
            planned_count=pending_count,
            attempted_count=success_count,
            success_count=success_count,
            failed_count=0,
            stop_code=stop_code,
            stop_reason=stop_reason,
            recovered_at=now,
            recovery_note=note,
            finished_at=now if status in {"blocked", "completed"} else "",
            counts_trusted=False,
        )
        if self.recovery_required or self.recovery_marker_path.exists():
            return rebuilt
        return self.put(rebuilt)

    def latest_inventory(self, site_id: str) -> BatchState | None:
        candidates = [
            batch
            for batch in self.load().values()
            if batch.site_id == site_id and batch.inventory_checked_at
        ]
        return max(candidates, key=lambda batch: batch.inventory_checked_at, default=None)

    def quarantine_corrupt(self) -> Path | None:
        with self._lock:
            if not self.path.exists():
                return None
            timestamp = self.now_func().strftime("%Y%m%d-%H%M%S")
            target = self.path.with_name(f"{self.path.name}.corrupt-{timestamp}")
            index = 1
            while target.exists():
                target = self.path.with_name(
                    f"{self.path.name}.corrupt-{timestamp}-{index}"
                )
                index += 1
            try:
                self.path.replace(target)
            except OSError as exc:
                raise StateFileError(f"自动化状态损坏且无法隔离: {self.path.name}") from exc
            return target

    def _decode(self, payload: object) -> dict[str, BatchState]:
        if not isinstance(payload, dict) or payload.get("version") not in {
            1,
            2,
            self.VERSION,
        }:
            raise StateFileError(f"自动化状态格式损坏: {self.path.name}")
        raw_batches = payload.get("batches")
        if not isinstance(raw_batches, dict):
            raise StateFileError(f"自动化状态格式损坏: {self.path.name}")
        decoded: dict[str, BatchState] = {}
        for key, value in raw_batches.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise StateFileError(f"自动化状态格式损坏: {self.path.name}")
            try:
                batch = BatchState(**value)
            except TypeError as exc:
                raise StateFileError(f"自动化状态格式损坏: {self.path.name}") from exc
            self._validate_batch(key, batch)
            decoded[key] = batch
        return decoded

    def _validate_batch(self, key: str, batch: BatchState) -> None:
        if batch.status not in BATCH_STATUSES:
            raise StateFileError(f"自动化批次状态损坏: {self.path.name}")
        try:
            expected_key = self.batch_key(batch.run_date, batch.site_id)
        except ValueError as exc:
            raise StateFileError(f"自动化批次标识损坏: {self.path.name}") from exc
        if key != expected_key or batch.batch_key != expected_key:
            raise StateFileError(f"自动化批次标识损坏: {self.path.name}")
        counts = (
            batch.planned_count,
            batch.attempted_count,
            batch.success_count,
            batch.failed_count,
            batch.retry_count,
            batch.inventory_threshold,
            batch.inventory_unreadable,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
            raise StateFileError(f"自动化批次数量损坏: {self.path.name}")
        if batch.inventory_count is not None and (
            isinstance(batch.inventory_count, bool)
            or not isinstance(batch.inventory_count, int)
            or batch.inventory_count < 0
        ):
            raise StateFileError(f"自动化批次库存损坏: {self.path.name}")
        if not isinstance(batch.counts_trusted, bool):
            raise StateFileError(f"自动化批次计数可信标记损坏: {self.path.name}")
