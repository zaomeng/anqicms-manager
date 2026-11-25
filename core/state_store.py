from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path


class StateFileError(RuntimeError):
    pass

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def atomic_write_text(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_date(value: str, path: Path) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise StateFileError(f"状态文件日期损坏: {path.name}") from exc


def get_published_count(daily_publish_filename: str | Path, today: str | None = None) -> int:
    path = Path(daily_publish_filename)
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise StateFileError(f"无法读取发布计数: {path.name}") from exc
    if len(lines) < 2:
        raise StateFileError(f"发布计数格式损坏: {path.name}")
    stored_date = lines[0].strip()
    _validate_date(stored_date, path)
    if stored_date != (today or _today()):
        return 0
    try:
        count = int(lines[1].strip())
    except ValueError as exc:
        raise StateFileError(f"发布计数格式损坏: {path.name}") from exc
    if count < 0:
        raise StateFileError(f"发布计数不能为负数: {path.name}")
    return count


def update_published_count(daily_publish_filename: str | Path, count: int, today: str | None = None) -> None:
    if count < 0:
        raise StateFileError("发布计数不能为负数")
    atomic_write_text(daily_publish_filename, f"{today or _today()}\n{count}")


def ensure_daily_file(daily_publish_filename: str | Path, today: str | None = None) -> None:
    path = Path(daily_publish_filename)
    current = today or _today()
    if not path.exists():
        update_published_count(path, 0, today=current)
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise StateFileError(f"发布计数格式损坏: {path.name}") from exc
    if not lines:
        update_published_count(path, 0, today=current)
        return
    first_line = lines[0].strip()
    _validate_date(first_line, path)
    if first_line != current:
        update_published_count(path, 0, today=current)
    else:
        get_published_count(path, today=current)


def load_schedule_claims(path: str | Path, today: str | None = None) -> set[str]:
    state_path = Path(path)
    if not state_path.exists():
        return set()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateFileError(f"调度状态损坏: {state_path.name}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("date"), str):
        raise StateFileError(f"调度状态格式损坏: {state_path.name}")
    stored_date = payload["date"]
    _validate_date(stored_date, state_path)
    if stored_date != (today or _today()):
        return set()
    claims = payload.get("claims", [])
    if not isinstance(claims, list) or any(not isinstance(item, str) for item in claims):
        raise StateFileError(f"调度状态格式损坏: {state_path.name}")
    return set(claims)


def save_schedule_claims(path: str | Path, claims: set[str], today: str | None = None) -> None:
    payload = {"date": today or _today(), "claims": sorted(claims)}
    atomic_write_text(path, json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
