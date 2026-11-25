"""Persistent JSON ledger for cross-link (站群互链) plans and rollbacks.

Stores plan metadata and per-link execution records under
``设置/friendlink_ledger.json`` using atomic writes and a process-local lock.
Corrupt or unknown-version ledger files are quarantined (renamed with a
``.corrupt-YYYYmmdd-HHMMSS`` suffix) so the UI never crashes on startup.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from core.state_store import atomic_write_text
from models.sitegroup import (
    LINK_CREATED,
    CrossLinkPlan,
    LedgerEntry,
    LedgerPlanSummary,
)

LEDGER_FILENAME = "friendlink_ledger.json"
LEDGER_DIRNAME = "设置"


def ledger_path(config_path: str | Path) -> Path:
    """Return the default ``设置/friendlink_ledger.json`` path beside ``config_path``."""
    return Path(config_path).parent / LEDGER_DIRNAME / LEDGER_FILENAME


def _canonical_match_key(url: str) -> str:
    return (url or "").strip().rstrip("/").casefold()


class FriendLinkLedger:
    """Thread-safe JSON ledger for cross-link plans."""

    VERSION = 1

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.path = Path(path)
        self._now = now
        self._lock = threading.Lock()

    def record_plan(self, plan: CrossLinkPlan) -> None:
        """Persist plan header metadata (idempotent if already recorded)."""
        site_names: list[str] = []
        seen_names: set[str] = set()
        for item in plan.items:
            for name in (item.source_name, item.target_name):
                if name and name not in seen_names:
                    seen_names.add(name)
                    site_names.append(name)

        with self._lock:
            payload = self._load_unlocked()
            plans = payload["plans"]
            existing = plans.get(plan.plan_id)
            existing_entries = (
                list(existing.get("entries", []))
                if isinstance(existing, dict) and isinstance(existing.get("entries"), list)
                else []
            )
            plans[plan.plan_id] = {
                "plan_id": plan.plan_id,
                "mode": plan.mode,
                "created_at": plan.created_at,
                "hub_site_id": plan.hub_site_id,
                "bidirectional": bool(plan.bidirectional),
                "site_names": site_names,
                "entries": existing_entries,
            }
            self._save_unlocked(payload)

    def record_results(self, plan_id: str, entries: Iterable[LedgerEntry]) -> None:
        """Append a batch of execution entries for ``plan_id`` in a single atomic write."""
        batch = list(entries)
        if not batch:
            return

        with self._lock:
            payload = self._load_unlocked()
            plans = payload["plans"]
            record = plans.get(plan_id)
            if not isinstance(record, dict):
                record = {
                    "plan_id": plan_id,
                    "mode": "",
                    "created_at": batch[0].created_at or self._now().isoformat(timespec="seconds"),
                    "hub_site_id": "",
                    "bidirectional": True,
                    "site_names": [],
                    "entries": [],
                }
                plans[plan_id] = record

            stored_entries = record.setdefault("entries", [])
            site_names = record.setdefault("site_names", [])
            seen_names = set(site_names)

            for entry in batch:
                if entry.source_name and entry.source_name not in seen_names:
                    seen_names.add(entry.source_name)
                    site_names.append(entry.source_name)
                stored_entries.append(self._serialize_entry(entry))

            self._save_unlocked(payload)

    def list_plans(self) -> list[LedgerPlanSummary]:
        """Return recorded plans sorted newest first."""
        with self._lock:
            payload = self._load_unlocked()

        summaries: list[tuple[str, int, LedgerPlanSummary]] = []
        for idx, record in enumerate(payload["plans"].values()):
            if not isinstance(record, dict):
                continue
            plan_id = str(record.get("plan_id") or "")
            if not plan_id:
                continue
            mode = str(record.get("mode") or "")
            created_at = str(record.get("created_at") or "")
            raw_entries = record.get("entries")
            entries = raw_entries if isinstance(raw_entries, list) else []

            created_count = 0
            rolled_back_count = 0
            fallback_names: list[str] = []
            seen_fallback: set[str] = set()
            for item in entries:
                if not isinstance(item, dict):
                    continue
                source_name = str(item.get("source_name") or "")
                if source_name and source_name not in seen_fallback:
                    seen_fallback.add(source_name)
                    fallback_names.append(source_name)
                if item.get("status") == LINK_CREATED:
                    created_count += 1
                    if str(item.get("rolled_back_at") or "").strip():
                        rolled_back_count += 1

            raw_site_names = record.get("site_names")
            if isinstance(raw_site_names, list) and all(isinstance(n, str) for n in raw_site_names):
                names = tuple(raw_site_names)
            else:
                names = tuple(fallback_names)

            summary = LedgerPlanSummary(
                plan_id=plan_id,
                mode=mode,
                created_at=created_at,
                site_names=names,
                created_count=created_count,
                rolled_back_count=rolled_back_count,
            )
            summaries.append((created_at, idx, summary))

        summaries.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in summaries]

    def created_links(self, plan_id: str) -> list[LedgerEntry]:
        """Return entries for ``plan_id`` with ``status == 'created'`` and not rolled back."""
        with self._lock:
            payload = self._load_unlocked()

        record = payload["plans"].get(plan_id)
        if not isinstance(record, dict):
            return []
        raw_entries = record.get("entries")
        if not isinstance(raw_entries, list):
            return []

        result: list[LedgerEntry] = []
        for item in raw_entries:
            entry = self._deserialize_entry(item, default_plan_id=plan_id)
            if entry is None:
                continue
            if entry.status == LINK_CREATED and not entry.rolled_back_at:
                result.append(entry)
        return result

    def mark_rolled_back(self, plan_id: str, source_site_id: str, link: str) -> None:
        """Mark matching created entry(ies) as rolled back."""
        with self._lock:
            payload = self._load_unlocked()
            record = payload["plans"].get(plan_id)
            if not isinstance(record, dict):
                return
            raw_entries = record.get("entries")
            if not isinstance(raw_entries, list):
                return

            stamp = self._now().isoformat(timespec="seconds")
            target_key = _canonical_match_key(link)
            changed = False

            for item in raw_entries:
                if not isinstance(item, dict):
                    continue
                if item.get("status") != LINK_CREATED:
                    continue
                if str(item.get("rolled_back_at") or "").strip():
                    continue
                if str(item.get("source_site_id") or "") != source_site_id:
                    continue
                entry_link = str(item.get("link") or "")
                if entry_link == link or (target_key and _canonical_match_key(entry_link) == target_key):
                    item["rolled_back_at"] = stamp
                    changed = True

            if changed:
                self._save_unlocked(payload)

    def _empty_payload(self) -> dict:
        return {"version": self.VERSION, "plans": {}}

    def _load_unlocked(self) -> dict:
        if not self.path.exists():
            return self._empty_payload()
        try:
            text = self.path.read_text(encoding="utf-8-sig")
            payload = json.loads(text)
        except (OSError, UnicodeError, ValueError):
            self._quarantine_corrupt_unlocked()
            return self._empty_payload()

        if not self._is_valid_payload(payload):
            self._quarantine_corrupt_unlocked()
            return self._empty_payload()
        return payload

    def _is_valid_payload(self, payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        if payload.get("version") != self.VERSION:
            return False
        plans = payload.get("plans")
        if not isinstance(plans, dict):
            return False
        for key, value in plans.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                return False
            entries = value.get("entries", [])
            if not isinstance(entries, list):
                return False
        return True

    def _quarantine_corrupt_unlocked(self) -> None:
        if not self.path.exists():
            return
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        candidate = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        suffix = 1
        while candidate.exists():
            candidate = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{suffix}")
            suffix += 1
        try:
            self.path.replace(candidate)
        except OSError:
            pass

    def _save_unlocked(self, payload: dict) -> None:
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        atomic_write_text(self.path, content)

    @staticmethod
    def _serialize_entry(entry: LedgerEntry) -> dict:
        return {
            "plan_id": entry.plan_id,
            "source_site_id": entry.source_site_id,
            "source_name": entry.source_name,
            "target_site_id": entry.target_site_id,
            "link": entry.link,
            "title": entry.title,
            "status": entry.status,
            "created_at": entry.created_at,
            "rolled_back_at": entry.rolled_back_at,
        }

    @staticmethod
    def _deserialize_entry(raw: object, *, default_plan_id: str) -> LedgerEntry | None:
        if not isinstance(raw, dict):
            return None
        return LedgerEntry(
            plan_id=str(raw.get("plan_id") or default_plan_id),
            source_site_id=str(raw.get("source_site_id") or ""),
            source_name=str(raw.get("source_name") or ""),
            target_site_id=str(raw.get("target_site_id") or ""),
            link=str(raw.get("link") or ""),
            title=str(raw.get("title") or ""),
            status=str(raw.get("status") or ""),
            created_at=str(raw.get("created_at") or ""),
            rolled_back_at=str(raw.get("rolled_back_at") or ""),
        )
