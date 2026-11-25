import configparser
import json

import pytest

from core.config_service import ConfigLoadError, load_config, save_config
from core.site_factory import create_site_from_key


def _write_config(tmp_path, category_keywords_line: str = ""):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\n"
        "fid=9\n"
        "fenlei=../legacy-file-is-ignored.txt\n"
        "url=https://example.com\n"
        "jiekou=/api\n"
        "publish_dir=./articles/demo\n"
        "daily_publish_filename=./settings/demo_daily.txt\n"
        f"{category_keywords_line}",
        encoding="utf-8",
    )
    return config_path


def test_category_keywords_config_round_trip_preserves_order(tmp_path):
    site = create_site_from_key(
        "demo", url="https://example.com", jiekou="/api", fid=9, base_dir=tmp_path
    )
    site.category_keywords = {9: ("普通搬家",), 3: ("实验室搬家", "实验室")}
    config_path = tmp_path / "config.ini"

    save_config(config_path, [site])
    loaded = load_config(config_path)[0]

    assert loaded.category_keywords == site.category_keywords
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")
    assert json.loads(parser["demo"]["category_keywords"]) == {
        "9": ["普通搬家"],
        "3": ["实验室搬家", "实验室"],
    }
    assert "fenlei" not in parser["demo"]


def test_missing_category_keywords_defaults_to_empty_and_legacy_fenlei_is_ignored(tmp_path):
    site = load_config(_write_config(tmp_path))[0]

    assert site.category_keywords == {}
    assert not hasattr(site, "fenlei")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not-json", "不是有效 JSON"),
        ("[]", "必须是对象"),
        ('{"0":["词"]}', "正整数"),
        ('{"3":"词"}', "必须是数组"),
        ('{"3":[1]}', "必须是文本"),
        ('{"3":["词"],"4":["词"]}', "同时分配"),
    ],
)
def test_load_rejects_invalid_category_keywords(tmp_path, raw, message):
    config_path = _write_config(tmp_path, f"category_keywords={raw}\n")

    with pytest.raises(ConfigLoadError, match=message):
        load_config(config_path)


def test_save_rejects_cross_category_duplicate_keywords(tmp_path):
    site = create_site_from_key(
        "demo", url="https://example.com", jiekou="/api", fid=9, base_dir=tmp_path
    )
    site.category_keywords = {3: ("实验室",), 4: ("实验室",)}

    with pytest.raises(ConfigLoadError, match="同时分配"):
        save_config(tmp_path / "config.ini", [site])
