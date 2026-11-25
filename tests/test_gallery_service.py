import json
import uuid
from pathlib import Path

import pytest

from core.gallery_service import (
    GalleryError,
    GalleryRegistryError,
    GalleryService,
    GalleryValidationError,
    normalize_legacy_path,
    validate_gallery_content,
)
from core.content_service import parse_image_groups_content


GALLERY_ID_1 = "11111111-1111-4111-8111-111111111111"
GALLERY_ID_2 = "22222222-2222-4222-8222-222222222222"


def write_gallery(path: Path, url: str = "https://img.example/one.jpg") -> Path:
    path.write_text(f"[默认]\n{url}\n", encoding="utf-8")
    return path


def test_create_gallery_copies_file_and_writes_atomic_registry(tmp_path):
    source = write_gallery(tmp_path / "source.txt")
    service = GalleryService(tmp_path / "设置", id_factory=lambda: GALLERY_ID_1)

    gallery = service.create_gallery("主图库", source)

    assert gallery.gallery_id == GALLERY_ID_1
    assert gallery.file_path == tmp_path / "设置" / "图库" / f"{GALLERY_ID_1}.txt"
    assert gallery.file_path.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    payload = json.loads(service.registry_path.read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "galleries": [
            {
                "id": GALLERY_ID_1,
                "name": "主图库",
                "file": f"图库/{GALLERY_ID_1}.txt",
            }
        ],
    }
    assert list(service.settings_dir.rglob("*.tmp")) == []


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[默认]\n",
        "[默认]\nftp://img.example/a.jpg\n",
        "[默认]\nnot-a-url\n",
        "[默认]\nhttps://user:pass@img.example/a.jpg\n",
        "[默认]\nhttps://img.example:bad/a.jpg\n",
    ],
)
def test_gallery_requires_at_least_one_valid_image_link(tmp_path, content):
    source = tmp_path / "source.txt"
    source.write_text(content, encoding="utf-8")
    service = GalleryService(tmp_path / "设置")

    with pytest.raises(GalleryValidationError, match="每行填写一个"):
        service.create_gallery("无效图库", source)

    assert not service.registry_path.exists()


def test_validation_counts_only_valid_urls():
    content = (
        "[默认]\n"
        "https://img.example/a.jpg\n"
        "/uploads/b.webp?v=1\n"
        "invalid\n"
        "http://img.example/dynamic?id=2\n"
    )
    result = validate_gallery_content(content)

    assert result.valid is True
    assert result.url_count == 3

    # Validation and runtime parsing must expose the same URL candidates.
    groups = parse_image_groups_content(content)
    assert sum(map(len, groups.values())) == result.url_count


def test_create_gallery_accepts_root_relative_image_link(tmp_path):
    source = write_gallery(tmp_path / "source.txt", "/uploads/a.webp?v=1")
    service = GalleryService(tmp_path / "设置", id_factory=lambda: GALLERY_ID_1)

    gallery = service.create_gallery("相对路径图库", source)

    assert gallery.file_path.read_text(encoding="utf-8") == (
        "[默认]\n/uploads/a.webp?v=1\n"
    )


def test_create_gallery_accepts_one_link_per_line_without_group_header(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text(
        "```text\n"
        "/uploads/tu/ylgdzz0001.webp\n"
        "/uploads/tu/ylgdzz0002.webp\n"
        "```\n",
        encoding="utf-8",
    )
    service = GalleryService(tmp_path / "设置", id_factory=lambda: GALLERY_ID_1)

    gallery = service.create_gallery("逐行图库", source)

    validation = service.validate_gallery(gallery.gallery_id)
    assert validation.valid is True
    assert validation.url_count == 2
    assert parse_image_groups_content(source.read_text(encoding="utf-8")) == {
        "默认": [
            "/uploads/tu/ylgdzz0001.webp",
            "/uploads/tu/ylgdzz0002.webp",
        ]
    }


def test_names_are_unique_case_insensitively(tmp_path):
    source = write_gallery(tmp_path / "source.txt")
    ids = iter([GALLERY_ID_1, GALLERY_ID_2])
    service = GalleryService(tmp_path / "设置", id_factory=lambda: next(ids))
    service.create_gallery("Gallery", source)

    with pytest.raises(GalleryValidationError, match="已存在"):
        service.create_gallery("gallery", source)


def test_rename_preserves_id_and_managed_file(tmp_path):
    source = write_gallery(tmp_path / "source.txt")
    service = GalleryService(tmp_path / "设置", id_factory=lambda: GALLERY_ID_1)
    original = service.create_gallery("旧名称", source)

    renamed = service.rename_gallery(original.gallery_id, "新名称")

    assert renamed.gallery_id == original.gallery_id
    assert renamed.file_path == original.file_path
    assert service.require_gallery(original.gallery_id).name == "新名称"


def test_delete_rejects_referenced_gallery(tmp_path):
    source = write_gallery(tmp_path / "source.txt")
    service = GalleryService(tmp_path / "设置", id_factory=lambda: GALLERY_ID_1)
    gallery = service.create_gallery("主图库", source)

    with pytest.raises(GalleryError, match="正在被站点使用"):
        service.delete_gallery(
            gallery.gallery_id,
            referenced_gallery_ids={gallery.gallery_id},
        )

    assert gallery.file_path.exists()
    assert service.require_gallery(gallery.gallery_id) == gallery


def test_delete_removes_registry_record_and_managed_file(tmp_path):
    source = write_gallery(tmp_path / "source.txt")
    service = GalleryService(tmp_path / "设置", id_factory=lambda: GALLERY_ID_1)
    gallery = service.create_gallery("主图库", source)

    service.delete_gallery(gallery.gallery_id)

    assert gallery.file_path.exists() is False
    assert service.list_galleries() == []


def test_legacy_migration_is_idempotent_for_normalized_path(tmp_path):
    base = tmp_path / "应用"
    base.mkdir()
    legacy = write_gallery(base / "旧图库.txt")
    ids = iter([GALLERY_ID_1, GALLERY_ID_2])
    service = GalleryService(tmp_path / "设置", id_factory=lambda: next(ids))

    first = service.migrate_legacy("旧图库.txt", "旧站点-图库", base_dir=base)
    second = service.migrate_legacy(legacy, "另一个名称")

    assert first.gallery_id == second.gallery_id == GALLERY_ID_1
    assert len(service.list_galleries()) == 1
    assert first.source_key == normalize_legacy_path(legacy)


def test_legacy_migration_deduplicates_source_before_reading_it_again(tmp_path):
    legacy = write_gallery(tmp_path / "legacy.txt")
    service = GalleryService(tmp_path / "设置", id_factory=lambda: GALLERY_ID_1)
    first = service.migrate_legacy(legacy, "旧图库")
    legacy.unlink()

    second = service.migrate_legacy(legacy, "旧图库")

    assert second == first


def test_migration_uses_unique_display_name_for_different_sources(tmp_path):
    first_source = write_gallery(tmp_path / "first.txt")
    second_source = write_gallery(tmp_path / "second.txt", "https://img.example/two.jpg")
    ids = iter([GALLERY_ID_1, GALLERY_ID_2])
    service = GalleryService(tmp_path / "设置", id_factory=lambda: next(ids))

    first = service.migrate_legacy(first_source, "旧站点-图库")
    second = service.migrate_legacy(second_source, "旧站点-图库")

    assert first.name == "旧站点-图库"
    assert second.name == "旧站点-图库-2"


def test_corrupt_registry_fails_closed(tmp_path):
    service = GalleryService(tmp_path / "设置")
    service.registry_path.parent.mkdir(parents=True)
    service.registry_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(GalleryRegistryError, match="损坏"):
        service.list_galleries()


def test_registry_rejects_path_escape_and_duplicate_ids(tmp_path):
    service = GalleryService(tmp_path / "设置")
    service.registry_path.parent.mkdir(parents=True)
    service.registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "galleries": [
                    {"id": GALLERY_ID_1, "name": "one", "file": "../outside.txt"},
                    {"id": GALLERY_ID_1, "name": "two", "file": "../outside.txt"},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(GalleryRegistryError, match="格式|重复"):
        service.list_galleries()


def test_invalid_generated_id_does_not_write_files(tmp_path):
    source = write_gallery(tmp_path / "source.txt")
    service = GalleryService(tmp_path / "设置", id_factory=lambda: "not-a-uuid")

    with pytest.raises(GalleryRegistryError, match="ID"):
        service.create_gallery("主图库", source)

    assert not service.registry_path.exists()
    assert not service.managed_dir.exists()


def test_registry_write_failure_removes_managed_copy(tmp_path, monkeypatch):
    source = write_gallery(tmp_path / "source.txt")
    service = GalleryService(tmp_path / "设置", id_factory=lambda: GALLERY_ID_1)

    def fail_save(_payload):
        raise OSError("injected registry failure")

    monkeypatch.setattr(service, "_save_registry", fail_save)

    with pytest.raises(OSError, match="injected"):
        service.create_gallery("主图库", source)

    assert not (service.managed_dir / f"{GALLERY_ID_1}.txt").exists()


def test_registry_accepts_canonical_uuid_only(tmp_path):
    source = write_gallery(tmp_path / "source.txt")
    upper = str(uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")).upper()
    service = GalleryService(tmp_path / "设置", id_factory=lambda: upper)

    with pytest.raises(GalleryRegistryError, match="ID"):
        service.create_gallery("主图库", source)
