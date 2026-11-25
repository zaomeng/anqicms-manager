from core import runtime_paths


def test_resolve_external_or_bundled_prefers_external(tmp_path, monkeypatch):
    external_dir = tmp_path / "external"
    bundled_dir = tmp_path / "bundled"
    external_dir.mkdir()
    bundled_dir.mkdir()
    (external_dir / "config.ini").write_text("external", encoding="utf-8")
    (bundled_dir / "config.ini").write_text("bundled", encoding="utf-8")
    monkeypatch.setattr(runtime_paths, "app_dir", lambda: external_dir)
    monkeypatch.setattr(runtime_paths, "bundled_dir", lambda: bundled_dir)
    result = runtime_paths.resolve_external_or_bundled("config.ini")
    assert result == external_dir / "config.ini"


def test_resolve_external_or_bundled_falls_back_to_bundled(tmp_path, monkeypatch):
    external_dir = tmp_path / "external"
    bundled_dir = tmp_path / "bundled"
    external_dir.mkdir()
    bundled_dir.mkdir()
    (bundled_dir / "jieba_data").mkdir()
    target = bundled_dir / "jieba_data" / "fenci.txt"
    target.write_text("词", encoding="utf-8")
    monkeypatch.setattr(runtime_paths, "app_dir", lambda: external_dir)
    monkeypatch.setattr(runtime_paths, "bundled_dir", lambda: bundled_dir)
    result = runtime_paths.resolve_external_or_bundled("jieba_data/fenci.txt")
    assert result == target
