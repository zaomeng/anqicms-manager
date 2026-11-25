from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from core.runtime_paths import app_dir
from core.state_store import atomic_write_text


class SourceIdentityChangedError(OSError):
    pass


class ArchiveService:
    def __init__(
        self,
        archive_root: str | Path | None = None,
        cleanup_state_file: str | Path | None = None,
        retention_days: int = 7,
        now_func=datetime.now,
        identity_map_file: str | Path | None = None,
    ) -> None:
        self.archive_root = Path(archive_root) if archive_root is not None else app_dir() / "归档"
        self.cleanup_state_file = (
            Path(cleanup_state_file) if cleanup_state_file is not None else app_dir() / "设置" / "archive_cleanup_state.txt"
        )
        self.retention_days = retention_days
        self.now_func = now_func
        self.recovered_sources: set[Path] = set()
        self.identity_map_file = (
            Path(identity_map_file)
            if identity_map_file is not None
            else self.cleanup_state_file.with_name("archive_site_ids.json")
        )

    def migrate_site_identity(self, site_name: str, site_id: str) -> bool:
        if not site_name.strip() or not site_id.strip():
            raise OSError("站点名称和 site_id 不能为空")
        payload = self._load_identity_map()
        mapped_id = payload["sites"].get(site_name)
        if mapped_id is not None and mapped_id != site_id:
            return False
        changed = False
        if mapped_id is None:
            for bucket in ("发布成功", "发布失败", "发布中"):
                legacy = self.archive_root / bucket / site_name
                target = self.archive_root / bucket / site_id
                if not legacy.exists():
                    continue
                if target.exists():
                    if any(legacy.iterdir()):
                        raise OSError(f"归档身份迁移冲突: {bucket}/{site_name}")
                    legacy.rmdir()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    legacy.replace(target)
                changed = True
            payload["sites"][site_name] = site_id
            atomic_write_text(
                self.identity_map_file,
                json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n",
            )
            changed = True
        return changed

    def _load_identity_map(self) -> dict:
        if not self.identity_map_file.exists():
            return {"version": 1, "sites": {}}
        try:
            payload = json.loads(self.identity_map_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise OSError("归档站点身份映射损坏") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("sites"), dict)
            or any(
                not isinstance(name, str) or not isinstance(site_id, str)
                for name, site_id in payload["sites"].items()
            )
        ):
            raise OSError("归档站点身份映射损坏")
        return payload

    def cleanup_once_per_day(self) -> bool:
        today = self.now_func().strftime("%Y-%m-%d")
        if self.cleanup_state_file.exists() and self.cleanup_state_file.read_text(encoding="utf-8").strip() == today:
            return False
        self.cleanup_old_archives()
        self.cleanup_state_file.parent.mkdir(parents=True, exist_ok=True)
        self.cleanup_state_file.write_text(today, encoding="utf-8")
        return True

    def cleanup_old_archives(self) -> None:
        if not self.archive_root.exists():
            return
        cutoff = self.now_func() - timedelta(days=self.retention_days)
        for path in sorted(self.archive_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                if path.relative_to(self.archive_root).parts[0] == "发布中":
                    continue
            except (ValueError, IndexError):
                continue
            if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    def archive_success(
        self,
        article_path: str | Path,
        site_name: str,
        source_hash: str | None = None,
        attempt_marker: str | Path | None = None,
    ) -> Path:
        return self._move_to_archive(
            article_path,
            "发布成功",
            site_name,
            source_hash,
            attempt_marker,
        )

    def archive_failure(
        self,
        article_path: str | Path,
        site_name: str,
        source_hash: str | None = None,
    ) -> Path:
        return self._move_to_archive(article_path, "发布失败", site_name, source_hash)

    def success_dir(self, site_name: str) -> Path:
        return self.archive_root / "发布成功" / site_name

    def failure_dir(self, site_name: str) -> Path:
        return self.archive_root / "发布失败" / site_name

    def attempt_dir(self, site_name: str) -> Path:
        return self.archive_root / "发布中" / site_name

    def begin_publish_attempt(
        self,
        article_path: str | Path,
        site_name: str,
        source_hash: str,
        today: str | None = None,
    ) -> Path:
        source_name = Path(article_path).name
        self._validate_marker_name(source_name, ".txt")
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise OSError("发布请求凭证的源文件身份无效")
        attempt_dir = self.attempt_dir(site_name)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        attempt_id = uuid.uuid4().hex
        marker = attempt_dir / f".attempt-{attempt_id}.marker"
        atomic_write_text(
            marker,
            f"{today or self.now_func().strftime('%Y-%m-%d')}\n{source_name}\nsha256:{source_hash}\nattempt:{attempt_id}",
        )
        return marker

    @staticmethod
    def finish_publish_attempt(marker: str | Path) -> None:
        Path(marker).unlink(missing_ok=True)

    def _ensure_no_pending_attempt(self, site_name: str) -> None:
        attempt_dir = self.attempt_dir(site_name)
        if not attempt_dir.exists():
            return
        try:
            pending_entries = list(attempt_dir.iterdir())
        except OSError as exc:
            raise OSError(f"无法确认站点发布请求状态: {site_name}") from exc
        success_dir = self.success_dir(site_name)
        for pending in pending_entries:
            if not pending.is_file() or not pending.name.endswith(".marker"):
                raise OSError(f"站点存在未确认的发布请求: {site_name}")
            _, source_name, source_hash, attempt_id = self._read_attempt_marker(pending)
            superseded = False
            if success_dir.is_dir():
                for success_marker in success_dir.glob(".success-*.marker"):
                    try:
                        success_record = self._read_success_marker(success_marker)
                    except OSError:
                        continue
                    if (
                        success_record[2] == source_name
                        and success_record[3] == source_hash
                        and success_record[4] == attempt_id
                    ):
                        superseded = True
                        break
            if not superseded:
                raise OSError(f"站点存在未确认的发布请求: {site_name}")
            pending.unlink()

    def has_pending_attempt(self, site_name: str) -> bool:
        try:
            self._ensure_no_pending_attempt(site_name)
        except OSError:
            return True
        return False

    def success_count_today(
        self,
        site_name: str,
        publish_dir: str | Path | None = None,
        today: str | None = None,
    ) -> int:
        self._ensure_no_pending_attempt(site_name)
        target_date = today or self.now_func().strftime("%Y-%m-%d")
        candidates: list[Path] = []
        success_dir = self.success_dir(site_name)
        marked_targets: set[str] = set()
        marker_count = 0
        if success_dir.exists():
            for path in success_dir.iterdir():
                if not path.is_file():
                    continue
                if path.name.startswith(".success-") and ".marker." in path.name and path.name.endswith(".tmp"):
                    raise OSError(f"发现未完成的成功归档恢复标记: {path.name}")
                if path.name.startswith(".success-") and path.name.endswith(".marker"):
                    (
                        marker_date,
                        target_name,
                        source_name,
                        source_hash,
                        attempt_id,
                        recovered_name,
                        recovery_complete,
                    ) = self._read_success_marker(path)
                    target = success_dir / target_name
                    target_exists = target.is_file()
                    if target_exists and source_hash is not None and self._file_sha256(target) != source_hash:
                        raise OSError(f"成功归档目标文件身份不一致: {target_name}")
                    if target_exists:
                        marked_targets.add(target_name)
                    if publish_dir is not None:
                        self.recovered_sources.add((Path(publish_dir) / source_name).resolve())
                        recovered_name = self._quarantine_recovered_source(
                            path,
                            Path(publish_dir),
                            marker_date,
                            target_name,
                            source_name,
                            recovered_name,
                            target_exists,
                            recovery_complete,
                            source_hash,
                            attempt_id,
                        )
                        if recovered_name:
                            marked_targets.add(recovered_name)
                    if marker_date == target_date:
                        marker_count += 1
                    continue
                if path.suffix.casefold() == ".txt":
                    candidates.append(path)
        if publish_dir is not None:
            source_dir = Path(publish_dir)
            if source_dir.is_dir():
                candidates.extend(
                    path
                    for path in source_dir.iterdir()
                    if path.is_file() and path.suffix.casefold() in {".published", ".uncertain"}
                )
        return marker_count + sum(
            1
            for path in candidates
            if path.name not in marked_targets
            and datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d") == target_date
        )

    def was_recovered_source(self, article_path: str | Path) -> bool:
        path = Path(article_path)
        return not path.is_file() and path.resolve() in self.recovered_sources

    def has_pending_success_marker(
        self,
        article_path: str | Path,
        site_name: str,
        source_hash: str,
        attempt_marker: str | Path | None,
    ) -> bool:
        source_name = Path(article_path).name
        if attempt_marker is None:
            return False
        attempt_match = re.fullmatch(
            r"\.attempt-([0-9a-f]{32})\.marker",
            Path(attempt_marker).name,
        )
        if attempt_match is None:
            return False
        attempt_id = attempt_match.group(1)
        success_dir = self.success_dir(site_name)
        if not success_dir.is_dir():
            return False
        for marker in success_dir.iterdir():
            if not marker.is_file() or not marker.name.startswith(".success-"):
                continue
            if marker.name.endswith(".marker"):
                try:
                    success_record = self._read_success_marker(marker)
                except OSError:
                    continue
                if (
                    success_record[2] == source_name
                    and success_record[3] == source_hash
                    and success_record[4] == attempt_id
                ):
                    return True
        return False

    def _move_to_archive(
        self,
        article_path: str | Path,
        result_folder: str,
        site_name: str,
        expected_source_hash: str | None = None,
        attempt_marker: str | Path | None = None,
    ) -> Path:
        source = Path(article_path)
        target_dir = self.archive_root / result_folder / site_name
        target_dir.mkdir(parents=True, exist_ok=True)
        target = self._unique_path(target_dir / source.name)
        success_marker: Path | None = None
        source_hash = expected_source_hash
        if result_folder == "发布成功":
            marker_date = self.now_func().strftime("%Y-%m-%d")
            success_marker = target_dir / f".success-{target.name}.marker"
            source_hash = source_hash or self._file_sha256(source)
            attempt_id: str | None = None
            if attempt_marker is not None:
                attempt_match = re.fullmatch(
                    r"\.attempt-([0-9a-f]{32})\.marker",
                    Path(attempt_marker).name,
                )
                if attempt_match is None:
                    raise OSError("发布请求凭证名称无效")
                attempt_id = attempt_match.group(1)
            attempt_line = f"\nattempt:{attempt_id}" if attempt_id else ""
            atomic_write_text(
                success_marker,
                f"{marker_date}\n{target.name}\n{source.name}\nsha256:{source_hash}{attempt_line}",
            )
            if not source.is_file() or self._file_sha256(source) != source_hash:
                raise SourceIdentityChangedError(f"成功归档源文件身份已变化: {source.name}")
        elif expected_source_hash is not None:
            if not source.is_file() or self._file_sha256(source) != source_hash:
                raise SourceIdentityChangedError(f"失败归档源文件身份已变化: {source.name}")
        if source.exists():
            try:
                shutil.move(str(source), str(target))
            except Exception:
                raise
            if source_hash is not None and self._file_sha256(target) != source_hash:
                if not source.exists():
                    try:
                        shutil.move(str(target), str(source))
                    except OSError:
                        pass
                raise SourceIdentityChangedError(f"归档目标文件身份不一致: {target.name}")
            timestamp = self.now_func().timestamp()
            try:
                os.utime(target, (timestamp, timestamp))
            except OSError:
                return target
            if success_marker is not None:
                if attempt_marker is not None:
                    self.finish_publish_attempt(attempt_marker)
                success_marker.unlink(missing_ok=True)
        return target

    @staticmethod
    def _read_attempt_marker(marker: Path) -> tuple[str, str, str, str]:
        try:
            marker_date, source_name, hash_line, attempt_line = marker.read_text(
                encoding="utf-8"
            ).splitlines()
            datetime.strptime(marker_date, "%Y-%m-%d")
            ArchiveService._validate_marker_name(source_name, ".txt")
            source_hash = hash_line.removeprefix("sha256:")
            if hash_line == source_hash or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
                raise ValueError("invalid attempt source hash")
            attempt_id = attempt_line.removeprefix("attempt:")
            if attempt_line == attempt_id or not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
                raise ValueError("invalid attempt id")
            if marker.name != f".attempt-{attempt_id}.marker":
                raise ValueError("attempt marker filename mismatch")
        except (OSError, UnicodeError, ValueError) as exc:
            raise OSError(f"发布请求凭证损坏: {marker.name}") from exc
        return marker_date, source_name, source_hash, attempt_id

    @staticmethod
    def _read_success_marker(
        marker: Path,
    ) -> tuple[str, str, str, str | None, str | None, str | None, bool]:
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
            if len(lines) not in {2, 3, 4, 5, 6, 7}:
                raise ValueError("wrong field count")
            marker_date, target_name = lines[:2]
            source_name = lines[2] if len(lines) >= 3 else target_name
            source_hash: str | None = None
            attempt_id: str | None = None
            recovered_name: str | None = None
            recovery_complete = False
            if len(lines) >= 4 and lines[3].startswith("sha256:"):
                source_hash = lines[3].removeprefix("sha256:")
                if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
                    raise ValueError("invalid source hash")
                position = 4
                if position < len(lines) and lines[position].startswith("attempt:"):
                    attempt_id = lines[position].removeprefix("attempt:")
                    if not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
                        raise ValueError("invalid attempt id")
                    position += 1
                if position < len(lines) and lines[position] != "complete":
                    recovered_name = lines[position]
                    position += 1
                if position < len(lines) and lines[position] == "complete":
                    recovery_complete = True
                    position += 1
                if position != len(lines):
                    raise ValueError("invalid recovery marker fields")
            elif len(lines) >= 4:
                if len(lines) >= 6:
                    raise ValueError("legacy marker has too many fields")
                recovered_name = lines[3]
                recovery_complete = len(lines) == 5
                if recovery_complete and lines[4] != "complete":
                    raise ValueError("invalid completion state")
            if recovery_complete and recovered_name is None:
                raise ValueError("invalid completion state")
            datetime.strptime(marker_date, "%Y-%m-%d")
            ArchiveService._validate_marker_name(target_name, ".txt")
            ArchiveService._validate_marker_name(source_name, ".txt")
            if marker.name != f".success-{target_name}.marker":
                raise ValueError("marker filename mismatch")
            if recovered_name is not None:
                ArchiveService._validate_marker_name(recovered_name, ".published")
        except (OSError, UnicodeError, ValueError) as exc:
            raise OSError(f"成功归档恢复标记损坏: {marker.name}") from exc
        return (
            marker_date,
            target_name,
            source_name,
            source_hash,
            attempt_id,
            recovered_name,
            recovery_complete,
        )

    @staticmethod
    def _validate_marker_name(name: str, suffix: str) -> None:
        if (
            not name
            or name in {".", ".."}
            or Path(name).name != name
            or re.search(r"[\\/:\x00]", name)
            or Path(name).suffix.casefold() != suffix
        ):
            raise ValueError("unsafe recovery marker path")

    def _quarantine_recovered_source(
        self,
        marker: Path,
        publish_dir: Path,
        marker_date: str,
        target_name: str,
        source_name: str,
        recovered_name: str | None,
        archive_target_exists: bool,
        recovery_complete: bool,
        source_hash: str | None,
        attempt_id: str | None,
    ) -> str | None:
        source = publish_dir / source_name
        if recovered_name is not None:
            target = publish_dir / recovered_name
            if recovery_complete:
                if target.exists() and not target.is_file():
                    raise OSError(f"成功归档恢复目标异常: {recovered_name}")
                return recovered_name
            if not source.exists():
                if target.is_file() and not recovery_complete:
                    self._write_completed_recovery_marker(
                        marker,
                        marker_date,
                        target_name,
                        source_name,
                        recovered_name,
                        source_hash,
                        attempt_id,
                    )
                return recovered_name
            if target.exists():
                raise OSError(f"成功归档恢复目标冲突: {recovered_name}")
        elif archive_target_exists:
            return None
        elif source.exists():
            self._verify_recovery_source(source, source_hash)
            target = self._unique_path(source.with_suffix(".published"))
            recovered_name = target.name
            self._write_planned_recovery_marker(
                marker,
                marker_date,
                target_name,
                source_name,
                source_hash,
                attempt_id,
                recovered_name,
            )
        elif not archive_target_exists:
            pattern = re.compile(rf"^{re.escape(Path(source_name).stem)}(?:-\d+)?\.published$")
            recovered = [path for path in publish_dir.iterdir() if path.is_file() and pattern.fullmatch(path.name)]
            if len(recovered) > 1:
                raise OSError(f"成功归档恢复隔离文件不唯一: {source_name}")
            if not recovered:
                return None
            recovered_name = recovered[0].name
            self._write_completed_recovery_marker(
                marker,
                marker_date,
                target_name,
                source_name,
                recovered_name,
                source_hash,
                attempt_id,
            )
            return recovered_name
        else:
            return None
        if not source.is_file():
            raise OSError(f"成功归档恢复源文件异常: {source_name}")
        self._verify_recovery_source(source, source_hash)
        try:
            source.rename(target)
            timestamp = self.now_func().timestamp()
            os.utime(target, (timestamp, timestamp))
            self._write_completed_recovery_marker(
                marker,
                marker_date,
                target_name,
                source_name,
                recovered_name,
                source_hash,
                attempt_id,
            )
        except OSError as exc:
            raise OSError(f"成功归档恢复源文件隔离失败: {source_name}") from exc
        return recovered_name

    @staticmethod
    def _write_completed_recovery_marker(
        marker: Path,
        marker_date: str,
        target_name: str,
        source_name: str,
        recovered_name: str,
        source_hash: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        hash_line = f"\nsha256:{source_hash}" if source_hash else ""
        attempt_line = f"\nattempt:{attempt_id}" if attempt_id else ""
        atomic_write_text(
            marker,
            f"{marker_date}\n{target_name}\n{source_name}{hash_line}{attempt_line}\n{recovered_name}\ncomplete",
        )

    @staticmethod
    def _write_planned_recovery_marker(
        marker: Path,
        marker_date: str,
        target_name: str,
        source_name: str,
        source_hash: str | None,
        attempt_id: str | None,
        recovered_name: str,
    ) -> None:
        if source_hash is None:
            raise OSError(f"成功归档恢复无法确认源文件身份: {source_name}")
        attempt_line = f"\nattempt:{attempt_id}" if attempt_id else ""
        atomic_write_text(
            marker,
            f"{marker_date}\n{target_name}\n{source_name}\nsha256:{source_hash}"
            f"{attempt_line}\n{recovered_name}",
        )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()

    @classmethod
    def _verify_recovery_source(cls, source: Path, expected_hash: str | None) -> None:
        if expected_hash is None or cls._file_sha256(source) != expected_hash:
            raise OSError(f"成功归档恢复无法确认源文件身份: {source.name}")

    @staticmethod
    def _unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        index = 1
        while True:
            candidate = path.with_name(f"{stem}-{index}{suffix}")
            if not candidate.exists():
                return candidate
            index += 1
