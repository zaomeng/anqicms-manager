from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from core.content_service import parse_image_groups_content
from core.runtime_paths import app_dir
from core.state_store import atomic_write_text


class GalleryError(RuntimeError):
    pass


class GalleryRegistryError(GalleryError):
    pass


class GalleryValidationError(GalleryError):
    pass


@dataclass(frozen=True)
class Gallery:
    gallery_id: str
    name: str
    file_path: Path
    source_key: str | None = None


@dataclass(frozen=True)
class GalleryValidation:
    valid: bool
    url_count: int
    message: str = ""


class GalleryService:
    REGISTRY_VERSION = 1

    def __init__(
        self,
        settings_dir: str | Path | None = None,
        id_factory=None,
    ) -> None:
        self.settings_dir = (
            Path(settings_dir) if settings_dir is not None else app_dir() / "设置"
        )
        self.registry_path = self.settings_dir / "image_galleries.json"
        self.managed_dir = self.settings_dir / "图库"
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def list_galleries(self) -> list[Gallery]:
        payload = self._load_registry()
        return [self._record_to_gallery(record) for record in payload["galleries"]]

    def get_gallery(self, gallery_id: str) -> Gallery | None:
        return next(
            (
                gallery
                for gallery in self.list_galleries()
                if gallery.gallery_id == gallery_id
            ),
            None,
        )

    def require_gallery(self, gallery_id: str) -> Gallery:
        gallery = self.get_gallery(gallery_id)
        if gallery is None:
            raise GalleryRegistryError(f"图库不存在: {gallery_id}")
        return gallery

    def create_gallery(self, name: str, source_file: str | Path) -> Gallery:
        clean_name = self._validate_name(name)
        source = Path(source_file)
        content = self._read_and_validate_source(source)
        payload = self._load_registry()
        self._ensure_unique_name(payload, clean_name)

        gallery_id = self._new_gallery_id(payload)
        relative_file = f"图库/{gallery_id}.txt"
        destination = self.settings_dir / Path(relative_file)
        record = {"id": gallery_id, "name": clean_name, "file": relative_file}
        self._commit_new_gallery(payload, record, destination, content)
        return self._record_to_gallery(record)

    def migrate_legacy(
        self,
        legacy_file: str | Path,
        name: str,
        *,
        base_dir: str | Path | None = None,
    ) -> Gallery:
        source_key = normalize_legacy_path(legacy_file, base_dir=base_dir)
        payload = self._load_registry()
        for record in payload["galleries"]:
            if record.get("source_key") == source_key:
                return self._record_to_gallery(record)

        clean_name = self._unique_migration_name(payload, self._validate_name(name))
        source = Path(legacy_file)
        if not source.is_absolute() and base_dir is not None:
            source = Path(base_dir) / source
        content = self._read_and_validate_source(source)
        gallery_id = self._new_gallery_id(payload)
        relative_file = f"图库/{gallery_id}.txt"
        destination = self.settings_dir / Path(relative_file)
        record = {
            "id": gallery_id,
            "name": clean_name,
            "file": relative_file,
            "source_key": source_key,
        }
        self._commit_new_gallery(payload, record, destination, content)
        return self._record_to_gallery(record)

    def rename_gallery(self, gallery_id: str, new_name: str) -> Gallery:
        clean_name = self._validate_name(new_name)
        payload = self._load_registry()
        target = self._find_record(payload, gallery_id)
        self._ensure_unique_name(payload, clean_name, excluding_id=gallery_id)
        target["name"] = clean_name
        self._save_registry(payload)
        return self._record_to_gallery(target)

    def delete_gallery(
        self,
        gallery_id: str,
        *,
        referenced_gallery_ids: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        if gallery_id in referenced_gallery_ids:
            raise GalleryError("图库正在被站点使用，不能删除")
        payload = self._load_registry()
        target = self._find_record(payload, gallery_id)
        remaining = [
            record for record in payload["galleries"] if record["id"] != gallery_id
        ]
        payload["galleries"] = remaining
        self._save_registry(payload)
        self._record_to_gallery(target).file_path.unlink(missing_ok=True)

    def validate_gallery(self, gallery_id: str) -> GalleryValidation:
        gallery = self.require_gallery(gallery_id)
        try:
            content = gallery.file_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return GalleryValidation(False, 0, "图库文件无法读取")
        return validate_gallery_content(content)

    def _load_registry(self) -> dict:
        if not self.registry_path.exists():
            return {"version": self.REGISTRY_VERSION, "galleries": []}
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise GalleryRegistryError("图库注册表损坏") from exc
        self._validate_registry(payload)
        return payload

    def _validate_registry(self, payload: object) -> None:
        if (
            not isinstance(payload, dict)
            or payload.get("version") != self.REGISTRY_VERSION
            or not isinstance(payload.get("galleries"), list)
        ):
            raise GalleryRegistryError("图库注册表格式无效")
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        seen_sources: set[str] = set()
        for record in payload["galleries"]:
            if not isinstance(record, dict):
                raise GalleryRegistryError("图库注册表格式无效")
            gallery_id = record.get("id")
            name = record.get("name")
            relative_file = record.get("file")
            source_key = record.get("source_key")
            if (
                not isinstance(gallery_id, str)
                or not self._is_uuid(gallery_id)
                or not isinstance(name, str)
                or not name.strip()
                or not isinstance(relative_file, str)
                or relative_file != f"图库/{gallery_id}.txt"
                or (source_key is not None and not isinstance(source_key, str))
            ):
                raise GalleryRegistryError("图库注册表格式无效")
            folded_name = name.strip().casefold()
            if gallery_id in seen_ids or folded_name in seen_names:
                raise GalleryRegistryError("图库注册表包含重复记录")
            if source_key is not None and source_key in seen_sources:
                raise GalleryRegistryError("图库注册表包含重复来源")
            seen_ids.add(gallery_id)
            seen_names.add(folded_name)
            if source_key is not None:
                seen_sources.add(source_key)

    def _record_to_gallery(self, record: dict) -> Gallery:
        return Gallery(
            gallery_id=record["id"],
            name=record["name"],
            file_path=self.settings_dir / Path(record["file"]),
            source_key=record.get("source_key"),
        )

    def _save_registry(self, payload: dict) -> None:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        atomic_write_text(self.registry_path, serialized + "\n")

    def _commit_new_gallery(
        self,
        payload: dict,
        record: dict,
        destination: Path,
        content: str,
    ) -> None:
        atomic_write_text(destination, content)
        payload["galleries"].append(record)
        try:
            self._save_registry(payload)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def _read_and_validate_source(self, source: Path) -> str:
        try:
            content = source.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise GalleryValidationError("图库文件无法读取") from exc
        result = validate_gallery_content(content)
        if not result.valid:
            raise GalleryValidationError(result.message)
        return content

    def _new_gallery_id(self, payload: dict) -> str:
        existing = {record["id"] for record in payload["galleries"]}
        gallery_id = self.id_factory()
        if not isinstance(gallery_id, str) or not self._is_uuid(gallery_id):
            raise GalleryRegistryError("图库 ID 生成器返回了无效值")
        if gallery_id in existing:
            raise GalleryRegistryError("图库 ID 重复")
        return gallery_id

    @staticmethod
    def _find_record(payload: dict, gallery_id: str) -> dict:
        for record in payload["galleries"]:
            if record["id"] == gallery_id:
                return record
        raise GalleryRegistryError(f"图库不存在: {gallery_id}")

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str):
            raise GalleryValidationError("图库名称必须是文本")
        clean_name = name.strip()
        if not clean_name or any(ord(char) < 32 for char in clean_name):
            raise GalleryValidationError("图库名称不能为空或包含控制字符")
        if len(clean_name) > 80:
            raise GalleryValidationError("图库名称不能超过 80 个字符")
        return clean_name

    @staticmethod
    def _ensure_unique_name(
        payload: dict,
        name: str,
        *,
        excluding_id: str | None = None,
    ) -> None:
        folded = name.casefold()
        if any(
            record["id"] != excluding_id and record["name"].casefold() == folded
            for record in payload["galleries"]
        ):
            raise GalleryValidationError("图库名称已存在")

    @classmethod
    def _unique_migration_name(cls, payload: dict, name: str) -> str:
        existing = {record["name"].casefold() for record in payload["galleries"]}
        if name.casefold() not in existing:
            return name
        index = 2
        while f"{name}-{index}".casefold() in existing:
            index += 1
        return f"{name}-{index}"

    @staticmethod
    def _is_uuid(value: str) -> bool:
        try:
            return str(uuid.UUID(value)) == value
        except (ValueError, AttributeError):
            return False


def normalize_legacy_path(
    path: str | Path,
    *,
    base_dir: str | Path | None = None,
) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and base_dir is not None:
        candidate = Path(base_dir) / candidate
    return os.path.normcase(str(candidate.resolve(strict=False)))


def validate_gallery_content(content: str) -> GalleryValidation:
    groups = parse_image_groups_content(content)
    url_count = sum(len(urls) for urls in groups.values())
    if url_count == 0:
        return GalleryValidation(
            False,
            0,
            "未读取到图片链接：请每行填写一个 http(s) 或 /uploads/... 链接；[分组名] 可选",
        )
    return GalleryValidation(True, url_count)
