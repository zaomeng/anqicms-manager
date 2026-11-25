import configparser
from pathlib import Path

import pytest

from core.config_service import ConfigLoadError, load_config, migrate_site_metadata, save_config
from core.gallery_service import GalleryService
from models.entities import SiteConfig


WINDOWS_RESERVED_SITE_KEYS = [
    "CON",
    "prn",
    "AuX",
    "nul",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
    "con.txt",
    "CoM1.log",
    "lPt9.backup",
    "CON .txt",
    "com1 .log",
]


def test_load_config_reads_sections(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "\n".join(
            [
                "[site-a]",
                "fid = 9",
                "fenlei = ./settings/a.txt",
                "url = https://example.com",
                "jiekou = /api/import",
                "publish_dir = ./articles/a",
                "daily_publish_filename = ./settings/a_daily.txt",
                "publish_time = 00:00:00",
                "interval_time = 10",
                "daily_publish_limit = 7",
                "image_file = ./a_images.txt",
            ]
        ),
        encoding="utf-8",
    )
    sites = load_config(config_path)
    assert len(sites) == 1
    assert sites[0].name == "site-a"
    assert sites[0].publish_dir == (tmp_path / "articles" / "a").resolve()


def test_save_config_round_trip(tmp_path):
    config_path = tmp_path / "config.ini"
    sites = [
        SiteConfig(
            name="demo",
            fid=1,
            url="https://example.com",
            jiekou="/api",
            publish_dir=(tmp_path / "articles" / "demo").resolve(),
            daily_publish_filename=(tmp_path / "settings" / "demo_daily.txt").resolve(),
            publish_time="01:02:03",
            interval_time=5,
            daily_publish_limit=9,
            image_file=(tmp_path / "demo_images.txt").resolve(),
        )
    ]
    save_config(config_path, sites)
    text = config_path.read_text(encoding="utf-8")
    assert "[demo]" in text
    loaded = load_config(config_path)
    assert loaded[0].interval_time == 5
    assert loaded[0].image_file == (tmp_path / "demo_images.txt").resolve()


@pytest.mark.parametrize("site_key", WINDOWS_RESERVED_SITE_KEYS)
def test_config_load_and_save_reject_windows_reserved_site_keys_before_write(
    tmp_path, site_key
):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        f"[{site_key}]\n"
        "url=https://example.com\n"
        "jiekou=/api\n"
        "fenlei=./settings/category.txt\n"
        "publish_dir=./articles/site\n"
        "daily_publish_filename=./settings/daily.txt\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="站点标识"):
        load_config(config_path)

    config_path.write_text("existing", encoding="utf-8")
    site = SiteConfig(
        name=site_key,
        fid=1,
        url="https://example.com",
        jiekou="/api",
        publish_dir=tmp_path / "articles" / "site",
        daily_publish_filename=tmp_path / "settings" / "daily.txt",
        publish_time="00:00:00",
        interval_time=1,
        daily_publish_limit=1,
    )
    with pytest.raises(ConfigLoadError, match="站点标识"):
        save_config(config_path, [site])
    assert config_path.read_text(encoding="utf-8") == "existing"


def test_save_config_preserves_relative_style_for_project_paths(tmp_path):
    config_path = tmp_path / "config.ini"
    sites = [
        SiteConfig(
            name="demo",
            fid=1,
            url="https://example.com",
            jiekou="/api",
            publish_dir=tmp_path / "articles" / "demo",
            daily_publish_filename=tmp_path / "settings" / "demo_daily.txt",
            publish_time="01:02:03",
            interval_time=5,
            daily_publish_limit=9,
            image_file=tmp_path / "demo_images.txt",
        )
    ]
    save_config(config_path, sites)
    text = config_path.read_text(encoding="utf-8")
    assert "publish_dir = ./articles/demo" in text


def test_save_config_uses_temp_file_then_replaces(tmp_path):
    config_path = tmp_path / "config.ini"
    sites = [
        SiteConfig(
            name="demo",
            fid=1,
            url="https://example.com",
            jiekou="/api",
            publish_dir=tmp_path / "articles" / "demo",
            daily_publish_filename=tmp_path / "settings" / "demo_daily.txt",
            publish_time="01:02:03",
            interval_time=5,
            daily_publish_limit=9,
            image_file=None,
        )
    ]

    save_config(config_path, sites)

    assert config_path.exists()
    assert not (tmp_path / "config.ini.tmp").exists()


def test_malformed_ini_raises_safe_config_error(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text("[broken\nfid=abc", encoding="utf-8")

    with pytest.raises(ConfigLoadError, match="配置文件格式错误"):
        load_config(config_path)


def test_invalid_numeric_value_raises_safe_config_error(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nfid=bad\nfenlei=./settings/demo.txt\nurl=https://example.com\njiekou=/api\n"
        "publish_dir=./articles/demo\ndaily_publish_filename=./settings/daily.txt\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="demo"):
        load_config(config_path)


def test_empty_critical_path_raises_safe_config_error(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nfid=1\nfenlei=\nurl=https://example.com\njiekou=/api\n"
        "publish_dir=\ndaily_publish_filename=\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="关键路径"):
        load_config(config_path)


def test_config_round_trip_preserves_exact_key_set(tmp_path):
    config_path = tmp_path / "config.ini"
    site = SiteConfig(
        name="demo",
        fid=1,
        url="https://example.com",
        jiekou="/api",
        publish_dir=tmp_path / "articles" / "demo",
        daily_publish_filename=tmp_path / "settings" / "daily.txt",
        publish_time="01:02:03",
        interval_time=5,
        daily_publish_limit=9,
        image_file=tmp_path / "images.txt",
    )
    save_config(config_path, [site])
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")

    assert set(parser["demo"]) == {
        "fid", "category_keywords", "url", "jiekou", "publish_dir", "daily_publish_filename",
        "publish_time", "publish_times", "interval_time", "daily_publish_limit", "enabled", "image_file",
        "site_id", "category_name", "gallery_id", "article_warning_threshold", "activation_date",
    }


def test_percent_characters_in_config_values_do_not_crash_interpolation(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nfid=1\nfenlei=./settings/%E5%88%86%E7%B1%BB.txt\n"
        "url=https://example.com/path%20with%20spaces\njiekou=/api?token=synthetic%2Bvalue\n"
        "publish_dir=./articles/%(literal)s\ndaily_publish_filename=./settings/daily.txt\n",
        encoding="utf-8",
    )

    sites = load_config(config_path)

    assert len(sites) == 1
    assert "%20" in sites[0].url
    assert "%(literal)s" in str(sites[0].publish_dir)


def test_unsafe_site_section_name_is_rejected(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[../outside]\nfid=1\nfenlei=./settings/demo.txt\nurl=https://example.com\n"
        "jiekou=/api\npublish_dir=./articles/demo\ndaily_publish_filename=./settings/daily.txt\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="站点标识"):
        load_config(config_path)


def test_url_with_embedded_credentials_is_rejected(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nfid=1\nfenlei=./category.txt\npublish_dir=./articles\n"
        "daily_publish_filename=./daily.txt\nurl=https://user:password@example.com\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="站点网址无效"):
        load_config(config_path)


def test_incomplete_enabled_site_is_loaded_disabled(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nfenlei=./category.txt\npublish_dir=./articles\n"
        "daily_publish_filename=./daily.txt\nurl=https://example.com\njiekou=\nenabled=true\n",
        encoding="utf-8",
    )

    sites = load_config(config_path)

    assert sites[0].enabled is False


@pytest.mark.parametrize("fid", [0, -1])
def test_legacy_site_with_non_positive_fid_loads_disabled(tmp_path, fid):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\n"
        f"fid={fid}\n"
        "fenlei=./settings/category.txt\n"
        "publish_dir=./articles/demo\n"
        "daily_publish_filename=./settings/daily.txt\n"
        "url=https://example.com\n"
        "jiekou=/api\n"
        "enabled=true\n",
        encoding="utf-8",
    )

    site = load_config(config_path)[0]

    assert site.fid == fid
    assert site.enabled is False


@pytest.mark.parametrize("fid", [0, -1])
def test_save_config_rejects_enabled_site_with_non_positive_fid_before_write(
    tmp_path, fid
):
    config_path = tmp_path / "config.ini"
    config_path.write_text("existing", encoding="utf-8")
    site = SiteConfig(
        name="demo",
        fid=fid,
        url="https://example.com",
        jiekou="/api",
        publish_dir=tmp_path / "articles" / "demo",
        daily_publish_filename=tmp_path / "settings" / "daily.txt",
        publish_time="00:00:00",
        interval_time=1,
        daily_publish_limit=1,
        enabled=True,
    )

    with pytest.raises(ConfigLoadError, match="栏目 ID.*正整数"):
        save_config(config_path, [site])

    assert config_path.read_text(encoding="utf-8") == "existing"


def test_http_site_loads_enabled(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nfid=1\nfenlei=./category.txt\npublish_dir=./articles\n"
        "daily_publish_filename=./daily.txt\nurl=http://example.com\njiekou=/api\nenabled=true\n",
        encoding="utf-8",
    )

    sites = load_config(config_path)

    assert sites[0].url == "http://example.com"
    assert sites[0].enabled is True


def test_invalid_legacy_endpoint_loads_disabled_but_cannot_be_resaved(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nfenlei=./category.txt\npublish_dir=./articles\n"
        "daily_publish_filename=./daily.txt\nurl=https://example.com\n"
        "jiekou=.attacker.test/api\nenabled=true\n",
        encoding="utf-8",
    )

    site = load_config(config_path)[0]

    assert site.enabled is False
    with pytest.raises(ConfigLoadError, match="发布接口无效"):
        save_config(config_path, [site])


def test_save_config_accepts_http_url(tmp_path):
    config_path = tmp_path / "config.ini"
    site = SiteConfig(
        name="demo",
        fid=1,
        url="http://example.com",
        jiekou="/api",
        publish_dir=tmp_path / "articles",
        daily_publish_filename=tmp_path / "daily.txt",
        publish_time="00:00:00",
        interval_time=1,
        daily_publish_limit=1,
    )

    save_config(config_path, [site])

    assert load_config(config_path)[0].url == "http://example.com"


@pytest.mark.parametrize("field", ["daily_publish_filename", "image_file"])
def test_load_config_rejects_paths_outside_config_directory(tmp_path, field):
    config_path = tmp_path / "config.ini"
    values = {
        "fenlei": "./settings/category.txt",
        "publish_dir": "./articles/demo",
        "daily_publish_filename": "./settings/daily.txt",
        "image_file": "./settings/images.txt",
    }
    values[field] = "../outside.txt"
    config_path.write_text(
        "[demo]\n"
        f"fenlei={values['fenlei']}\n"
        f"publish_dir={values['publish_dir']}\n"
        f"daily_publish_filename={values['daily_publish_filename']}\n"
        f"image_file={values['image_file']}\n"
        "url=https://example.com\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="配置目录"):
        load_config(config_path)


def test_load_config_allows_external_article_directory(tmp_path):
    external_articles = tmp_path.parent / "external-articles"
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\n"
        "fid=1\n"
        f"publish_dir={external_articles}\n"
        "daily_publish_filename=./settings/daily.txt\n"
        "url=https://example.com\n"
        "jiekou=/api\n",
        encoding="utf-8",
    )

    site = load_config(config_path)[0]

    assert site.publish_dir == external_articles.resolve()


def test_save_config_round_trips_external_article_directory(tmp_path):
    config_path = tmp_path / "config.ini"
    external_articles = tmp_path.parent / "external-articles"
    site = SiteConfig(
        name="demo",
        fid=1,
        url="https://example.com",
        jiekou="/api",
        publish_dir=external_articles,
        daily_publish_filename=tmp_path / "settings" / "daily.txt",
        publish_time="00:00:00",
        interval_time=1,
        daily_publish_limit=1,
    )

    save_config(config_path, [site])

    assert load_config(config_path)[0].publish_dir == external_articles.resolve()


def test_load_config_rejects_publish_directory_equal_to_config_directory(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nfenlei=./settings/category.txt\npublish_dir=.\n"
        "daily_publish_filename=./settings/daily.txt\nurl=https://example.com\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="文章目录不能是配置目录"):
        load_config(config_path)


def test_save_config_rejects_unsafe_paths_and_negative_numbers_before_write(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text("existing", encoding="utf-8")
    site = SiteConfig(
        name="demo",
        fid=1,
        url="https://example.com",
        jiekou="/api",
        publish_dir=tmp_path,
        daily_publish_filename=tmp_path / "settings" / "daily.txt",
        publish_time="00:00:00",
        interval_time=-1,
        daily_publish_limit=1,
        image_file=tmp_path / "settings" / "images.txt",
    )

    with pytest.raises(ConfigLoadError):
        save_config(config_path, [site])

    assert config_path.read_text(encoding="utf-8") == "existing"


def test_save_config_rejects_path_outside_config_directory_before_write(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text("existing", encoding="utf-8")
    site = SiteConfig(
        name="demo",
        fid=1,
        url="https://example.com",
        jiekou="/api",
        publish_dir=tmp_path / "articles" / "demo",
        daily_publish_filename=tmp_path / "settings" / "daily.txt",
        publish_time="00:00:00",
        interval_time=1,
        daily_publish_limit=1,
        image_file=tmp_path.parent / "outside.txt",
    )

    with pytest.raises(ConfigLoadError, match="配置目录"):
        save_config(config_path, [site])

    assert config_path.read_text(encoding="utf-8") == "existing"


def test_load_config_rejects_case_insensitive_site_name_collision(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Demo]\nfenlei=./settings/demo.txt\npublish_dir=./articles/demo\n"
        "daily_publish_filename=./settings/demo_daily.txt\nurl=https://demo.example.com\n"
        "[demo]\nfenlei=./settings/other.txt\npublish_dir=./articles/other\n"
        "daily_publish_filename=./settings/other_daily.txt\nurl=https://other.example.com\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="站点标识.*(?:重复|冲突)"):
        load_config(config_path)


@pytest.mark.parametrize("shared_field", ["publish_dir", "daily_publish_filename"])
def test_load_config_rejects_cross_site_runtime_path_collision(tmp_path, shared_field):
    config_path = tmp_path / "config.ini"
    first_publish_dir = second_publish_dir = "./articles/shared"
    first_daily = second_daily = "./settings/shared_daily.txt"
    if shared_field == "publish_dir":
        second_daily = "./settings/second_daily.txt"
    else:
        second_publish_dir = "./articles/second"
    config_path.write_text(
        "[first]\nfenlei=./settings/first.txt\n"
        f"publish_dir={first_publish_dir}\ndaily_publish_filename={first_daily}\n"
        "url=https://first.example.com\n"
        "[second]\nfenlei=./settings/second.txt\n"
        f"publish_dir={second_publish_dir}\ndaily_publish_filename={second_daily}\n"
        "url=https://second.example.com\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="重复|共用|冲突"):
        load_config(config_path)


def test_save_config_rejects_case_insensitive_site_name_collision_before_write(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text("existing", encoding="utf-8")
    first = SiteConfig(
        name="Demo",
        fid=1,
        url="https://first.example.com",
        jiekou="/api",
        publish_dir=tmp_path / "articles" / "first",
        daily_publish_filename=tmp_path / "settings" / "first_daily.txt",
        publish_time="00:00:00",
        interval_time=1,
        daily_publish_limit=1,
    )
    second = SiteConfig(
        name="demo",
        fid=1,
        url="https://second.example.com",
        jiekou="/api",
        publish_dir=tmp_path / "articles" / "second",
        daily_publish_filename=tmp_path / "settings" / "second_daily.txt",
        publish_time="00:00:00",
        interval_time=1,
        daily_publish_limit=1,
    )

    with pytest.raises(ConfigLoadError, match="重复|共用|冲突"):
        save_config(config_path, [first, second])

    assert config_path.read_text(encoding="utf-8") == "existing"


@pytest.mark.parametrize("shared_field", ["publish_dir", "daily_publish_filename"])
def test_save_config_rejects_cross_site_runtime_path_collision_before_write(tmp_path, shared_field):
    config_path = tmp_path / "config.ini"
    config_path.write_text("existing", encoding="utf-8")
    first = SiteConfig(
        name="first", fid=1,
        url="https://first.example.com", jiekou="/api",
        publish_dir=tmp_path / "articles" / "first",
        daily_publish_filename=tmp_path / "settings" / "first_daily.txt",
        publish_time="00:00:00", interval_time=1, daily_publish_limit=1,
    )
    second = SiteConfig(
        name="second", fid=1,
        url="https://second.example.com", jiekou="/api",
        publish_dir=(first.publish_dir if shared_field == "publish_dir" else tmp_path / "articles" / "second"),
        daily_publish_filename=(first.daily_publish_filename if shared_field == "daily_publish_filename" else tmp_path / "settings" / "second_daily.txt"),
        publish_time="00:00:00", interval_time=1, daily_publish_limit=1,
    )

    with pytest.raises(ConfigLoadError, match="重复|共用|冲突"):
        save_config(config_path, [first, second])

    assert config_path.read_text(encoding="utf-8") == "existing"


@pytest.mark.parametrize(
    "url",
    ["https://exa mple.com", "https://example.com\\attacker", "https://example.com\x01", "https://."],
)
def test_load_and_save_reject_obviously_invalid_hostname(tmp_path, url):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nfenlei=./settings/category.txt\npublish_dir=./articles/demo\n"
        f"daily_publish_filename=./settings/daily.txt\nurl={url}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigLoadError, match="网址无效"):
        load_config(config_path)

    original = config_path.read_text(encoding="utf-8")
    site = SiteConfig(
        name="demo", fid=1,
        url=url, jiekou="/api", publish_dir=tmp_path / "articles" / "demo",
        daily_publish_filename=tmp_path / "settings" / "daily.txt",
        publish_time="00:00:00", interval_time=1, daily_publish_limit=1,
    )
    with pytest.raises(ConfigLoadError, match="网址无效"):
        save_config(config_path, [site])
    assert config_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("field", ["daily_publish_filename", "image_file"])
@pytest.mark.parametrize("owner", ["same", "other"])
def test_load_config_rejects_file_path_inside_any_publish_directory(tmp_path, field, owner):
    first_values = {
        "fenlei": "./settings/first.txt", "daily_publish_filename": "./settings/first_daily.txt",
        "image_file": "./settings/first_images.txt",
    }
    second_values = {
        "fenlei": "./settings/second.txt", "daily_publish_filename": "./settings/second_daily.txt",
        "image_file": "./settings/second_images.txt",
    }
    target = first_values if owner == "same" else second_values
    target[field] = "./articles/first/nested.txt"
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[first]\nurl=https://first.example.com\npublish_dir=./articles/first\n"
        f"fenlei={first_values['fenlei']}\ndaily_publish_filename={first_values['daily_publish_filename']}\nimage_file={first_values['image_file']}\n"
        "[second]\nurl=https://second.example.com\npublish_dir=./articles/second\n"
        f"fenlei={second_values['fenlei']}\ndaily_publish_filename={second_values['daily_publish_filename']}\nimage_file={second_values['image_file']}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="文章目录|位于"):
        load_config(config_path)


@pytest.mark.parametrize("field", ["daily_publish_filename", "image_file"])
@pytest.mark.parametrize("owner", ["same", "other"])
def test_save_config_rejects_file_path_inside_any_publish_directory_before_write(tmp_path, field, owner):
    config_path = tmp_path / "config.ini"
    config_path.write_text("existing", encoding="utf-8")
    first = SiteConfig(
        name="first", fid=1,
        url="https://first.example.com", jiekou="/api", publish_dir=tmp_path / "articles" / "first",
        daily_publish_filename=tmp_path / "settings" / "first_daily.txt", image_file=tmp_path / "settings" / "first_images.txt",
        publish_time="00:00:00", interval_time=1, daily_publish_limit=1,
    )
    second = SiteConfig(
        name="second", fid=1,
        url="https://second.example.com", jiekou="/api", publish_dir=tmp_path / "articles" / "second",
        daily_publish_filename=tmp_path / "settings" / "second_daily.txt", image_file=tmp_path / "settings" / "second_images.txt",
        publish_time="00:00:00", interval_time=1, daily_publish_limit=1,
    )
    target = first if owner == "same" else second
    setattr(target, field, first.publish_dir / "nested.txt")

    with pytest.raises(ConfigLoadError, match="文章目录|位于"):
        save_config(config_path, [first, second])
    assert config_path.read_text(encoding="utf-8") == "existing"


@pytest.mark.parametrize("endpoint", ["/api\\attacker", "/api\x00attacker"])
def test_load_and_save_reject_endpoint_with_backslash_or_c0_before_write(tmp_path, endpoint):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nfenlei=./settings/category.txt\npublish_dir=./articles/demo\n"
        f"daily_publish_filename=./settings/daily.txt\nurl=https://example.com\njiekou={endpoint}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="接口无效"):
        load_config(config_path)

    config_path.write_text("existing", encoding="utf-8")
    site = SiteConfig(
        name="demo", fid=1,
        url="https://example.com", jiekou=endpoint,
        publish_dir=tmp_path / "articles" / "demo",
        daily_publish_filename=tmp_path / "settings" / "daily.txt",
        publish_time="00:00:00", interval_time=1, daily_publish_limit=1,
    )
    with pytest.raises(ConfigLoadError, match="接口无效"):
        save_config(config_path, [site])
    assert config_path.read_text(encoding="utf-8") == "existing"


@pytest.mark.parametrize(
    ("owner", "managed_field"),
    [
        ("same", "image_file"),
        ("other", "image_file"),
        ("other", "daily_publish_filename"),
    ],
)
def test_load_config_rejects_daily_file_aliasing_any_managed_file(
    tmp_path, owner, managed_field
):
    first = {
        "fenlei": "./settings/first_category.txt",
        "image_file": "./settings/first_images.txt",
        "daily_publish_filename": "./settings/first_daily.txt",
    }
    second = {
        "fenlei": "./settings/second_category.txt",
        "image_file": "./settings/second_images.txt",
        "daily_publish_filename": "./settings/second_daily.txt",
    }
    target = first if owner == "same" else second
    first["daily_publish_filename"] = target[managed_field]
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[first]\nurl=https://first.example.com\npublish_dir=./articles/first\n"
        f"fenlei={first['fenlei']}\nimage_file={first['image_file']}\n"
        f"daily_publish_filename={first['daily_publish_filename']}\n"
        "[second]\nurl=https://second.example.com\npublish_dir=./articles/second\n"
        f"fenlei={second['fenlei']}\nimage_file={second['image_file']}\n"
        f"daily_publish_filename={second['daily_publish_filename']}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="文件|路径|共用|重复"):
        load_config(config_path)


@pytest.mark.parametrize(
    ("owner", "managed_field"),
    [
        ("same", "image_file"),
        ("other", "image_file"),
        ("other", "daily_publish_filename"),
    ],
)
def test_save_config_rejects_daily_file_aliasing_any_managed_file_before_write(
    tmp_path, owner, managed_field
):
    config_path = tmp_path / "config.ini"
    config_path.write_text("existing", encoding="utf-8")
    first = SiteConfig(
        name="first", fid=1,
        image_file=tmp_path / "settings" / "first_images.txt",
        daily_publish_filename=tmp_path / "settings" / "first_daily.txt",
        publish_dir=tmp_path / "articles" / "first", url="https://first.example.com",
        jiekou="/api", publish_time="00:00:00", interval_time=1, daily_publish_limit=1,
    )
    second = SiteConfig(
        name="second", fid=1,
        image_file=tmp_path / "settings" / "second_images.txt",
        daily_publish_filename=tmp_path / "settings" / "second_daily.txt",
        publish_dir=tmp_path / "articles" / "second", url="https://second.example.com",
        jiekou="/api", publish_time="00:00:00", interval_time=1, daily_publish_limit=1,
    )
    target = first if owner == "same" else second
    first.daily_publish_filename = getattr(target, managed_field)

    with pytest.raises(ConfigLoadError, match="文件|路径|共用|重复"):
        save_config(config_path, [first, second])
    assert config_path.read_text(encoding="utf-8") == "existing"


@pytest.mark.parametrize("publish_dir", ["./归档/pending", "."])
def test_load_config_rejects_publish_directory_overlapping_archive_tree(tmp_path, publish_dir):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nurl=https://example.com\njiekou=/api\n"
        "fenlei=./settings/category.txt\ndaily_publish_filename=./settings/daily.txt\n"
        f"publish_dir={publish_dir}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="归档|文章目录"):
        load_config(config_path)


@pytest.mark.parametrize("publish_dir", ["归档/pending", "."])
def test_save_config_rejects_publish_directory_overlapping_archive_tree_before_write(
    tmp_path, publish_dir
):
    config_path = tmp_path / "config.ini"
    config_path.write_text("existing", encoding="utf-8")
    site = SiteConfig(
        name="demo", fid=1,
        url="https://example.com", jiekou="/api",
        publish_dir=(tmp_path / publish_dir).resolve(),
        daily_publish_filename=tmp_path / "settings" / "daily.txt",
        publish_time="00:00:00", interval_time=1, daily_publish_limit=1,
    )

    with pytest.raises(ConfigLoadError, match="归档|文章目录"):
        save_config(config_path, [site])
    assert config_path.read_text(encoding="utf-8") == "existing"


def test_success_archive_directory_cannot_be_loaded_as_publish_directory(tmp_path):
    archived_dir = tmp_path / "归档" / "发布成功" / "demo"
    archived_dir.mkdir(parents=True)
    (archived_dir / "published.txt").write_text("already published", encoding="utf-8")
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nurl=https://example.com\njiekou=/api\n"
        "fenlei=./settings/category.txt\ndaily_publish_filename=./settings/daily.txt\n"
        "publish_dir=./归档/发布成功/demo\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="归档|文章目录"):
        load_config(config_path)


def _managed_path_site(tmp_path, *, name="demo"):
    return SiteConfig(
        name=name,
        fid=1,
        image_file=tmp_path / "设置" / f"{name}_images.txt",
        url="https://example.com",
        jiekou="/api",
        publish_dir=tmp_path / "文章" / name,
        daily_publish_filename=tmp_path / "设置" / f"{name}_daily.txt",
        publish_time="00:00:00",
        interval_time=1,
        daily_publish_limit=1,
    )


def _write_config_with_managed_path(config_path, field, target):
    values = {
        "fenlei": "./设置/category.txt",
        "image_file": "./设置/images.txt",
        "daily_publish_filename": "./设置/daily.txt",
    }
    values[field] = Path(target).as_posix()
    config_path.write_text(
        "[demo]\n"
        "url=https://example.com\n"
        "jiekou=/api\n"
        "publish_dir=./文章/demo\n"
        f"fenlei={values['fenlei']}\n"
        f"image_file={values['image_file']}\n"
        f"daily_publish_filename={values['daily_publish_filename']}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("field", ["image_file", "daily_publish_filename"])
@pytest.mark.parametrize("archive_owner", ["config", "runtime"])
def test_load_config_rejects_managed_file_inside_any_archive_tree(
    tmp_path, monkeypatch, field, archive_owner
):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("core.config_service.app_dir", lambda: runtime_root)
    archive_root = tmp_path / "归档" if archive_owner == "config" else runtime_root / "归档"
    config_path = tmp_path / "config.ini"
    _write_config_with_managed_path(
        config_path, field, archive_root / "发布成功" / "demo" / "managed.txt"
    )

    with pytest.raises(ConfigLoadError, match="归档"):
        load_config(config_path)


@pytest.mark.parametrize("field", ["image_file", "daily_publish_filename"])
@pytest.mark.parametrize("archive_owner", ["config", "runtime"])
def test_save_config_rejects_managed_file_inside_any_archive_tree_before_write(
    tmp_path, monkeypatch, field, archive_owner
):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("core.config_service.app_dir", lambda: runtime_root)
    archive_root = tmp_path / "归档" if archive_owner == "config" else runtime_root / "归档"
    config_path = tmp_path / "config.ini"
    original = b"original config bytes\r\n"
    config_path.write_bytes(original)
    site = _managed_path_site(tmp_path)
    setattr(site, field, archive_root / "发布成功" / "demo" / "managed.txt")

    with pytest.raises(ConfigLoadError, match="归档"):
        save_config(config_path, [site])

    assert config_path.read_bytes() == original


RESERVED_RUNTIME_FILES = [
    "config.ini",
    "fabu.log",
    "设置/schedule_state.json",
    "设置/window_state.json",
    "设置/archive_cleanup_state.txt",
]


@pytest.mark.parametrize("field", ["image_file", "daily_publish_filename"])
@pytest.mark.parametrize("reserved_path", RESERVED_RUNTIME_FILES)
def test_load_config_rejects_managed_file_aliasing_reserved_runtime_file(
    tmp_path, field, reserved_path
):
    config_path = tmp_path / "config.ini"
    _write_config_with_managed_path(config_path, field, tmp_path / reserved_path)

    with pytest.raises(ConfigLoadError, match="配置|日志|状态|保留|路径"):
        load_config(config_path)


@pytest.mark.parametrize("field", ["image_file", "daily_publish_filename"])
@pytest.mark.parametrize("reserved_path", RESERVED_RUNTIME_FILES)
def test_save_config_rejects_managed_file_aliasing_reserved_runtime_file_before_write(
    tmp_path, field, reserved_path
):
    config_path = tmp_path / "config.ini"
    original = b"original config bytes\r\n"
    config_path.write_bytes(original)
    site = _managed_path_site(tmp_path)
    setattr(site, field, tmp_path / reserved_path)

    with pytest.raises(ConfigLoadError, match="配置|日志|状态|保留|路径"):
        save_config(config_path, [site])

    assert config_path.read_bytes() == original


def test_save_config_replace_failure_preserves_old_bytes_and_removes_all_temp_files(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.ini"
    original = b"old config bytes\r\n"
    config_path.write_bytes(original)
    real_replace = Path.replace

    def fail_config_replace(path, target):
        if Path(target) == config_path:
            raise OSError("replace injected failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_config_replace)

    with pytest.raises(OSError, match="replace injected failure"):
        save_config(config_path, [_managed_path_site(tmp_path)])

    assert config_path.read_bytes() == original
    assert list(tmp_path.rglob("*.tmp")) == []


def test_non_utf8_config_is_reported_as_config_load_error(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_bytes(
        b"[demo]\nurl=https://example.com\njiekou=/api\n"
        b"fenlei=./settings/\xff.txt\npublish_dir=./articles/demo\n"
        b"daily_publish_filename=./settings/daily.txt\n"
    )

    with pytest.raises(ConfigLoadError, match="配置文件格式错误"):
        load_config(config_path)


def _write_valid_single_site_config(config_path, *, extra_lines=""):
    config_path.write_text(
        "[demo]\n"
        "fid=1\n"
        "fenlei=./settings/category.txt\n"
        "url=https://example.com\n"
        "jiekou=/api\n"
        "publish_dir=./articles/demo\n"
        "daily_publish_filename=./settings/daily.txt\n"
        "interval_time=1\n"
        "daily_publish_limit=1\n"
        f"{extra_lines}",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("raw_enabled", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("", False),
        ("disabled", False),
        ("tru", False),
    ],
)
def test_enabled_parser_accepts_only_explicit_boolean_tokens(
    tmp_path, raw_enabled, expected
):
    config_path = tmp_path / "config.ini"
    _write_valid_single_site_config(
        config_path,
        extra_lines=(
            "publish_time=09:00:00\n"
            "publish_times=09:00:00\n"
            f"enabled={raw_enabled}\n"
        ),
    )

    assert load_config(config_path)[0].enabled is expected


@pytest.mark.parametrize(
    "time_lines",
    [
        "publish_time=24:00:00\npublish_times=09:00:00\n",
        "publish_time=09:00:00\npublish_times=09:00:00,not-a-time\n",
        "publish_time=9:00\npublish_times=09:00:00\n",
    ],
)
def test_load_config_rejects_invalid_publish_time_values(tmp_path, time_lines):
    config_path = tmp_path / "config.ini"
    _write_valid_single_site_config(
        config_path,
        extra_lines=f"{time_lines}enabled=true\n",
    )

    with pytest.raises(ConfigLoadError, match="发布.*时间|时间.*无效"):
        load_config(config_path)


def test_empty_publish_time_list_round_trips_for_manual_publish_only(tmp_path):
    config_path = tmp_path / "config.ini"
    _write_valid_single_site_config(
        config_path,
        extra_lines="publish_time=\npublish_times=\nenabled=true\n",
    )

    loaded = load_config(config_path)

    assert loaded[0].publish_time == ""
    assert loaded[0].publish_times == []
    save_config(config_path, loaded)
    reloaded = load_config(config_path)
    assert reloaded[0].publish_time == ""
    assert reloaded[0].publish_times == []


def test_publish_times_are_deduplicated_when_loading_and_saving(tmp_path):
    config_path = tmp_path / "config.ini"
    _write_valid_single_site_config(
        config_path,
        extra_lines=(
            "publish_time=09:00:00\n"
            "publish_times=09:00:00,09:00:00,14:00:00,09:00:00\n"
            "enabled=true\n"
        ),
    )

    loaded = load_config(config_path)

    assert loaded[0].publish_times == ["09:00:00"]

    loaded[0].publish_times = ["14:00:00", "09:00:00", "14:00:00"]
    save_config(config_path, loaded)
    reloaded = load_config(config_path)

    assert reloaded[0].publish_times == ["14:00:00"]


def test_metadata_migration_backs_up_config_and_is_idempotent(tmp_path):
    image_file = tmp_path / "legacy-images.txt"
    image_file.write_text("[默认]\nhttps://img.example.com/a.jpg\n", encoding="utf-8")
    legacy_daily = tmp_path / "设置" / "daily.txt"
    legacy_daily.parent.mkdir(parents=True)
    legacy_daily.write_text("2026-07-15\n4", encoding="utf-8")
    site = SiteConfig(
        name="demo",
        fid=1,
        url="https://example.com",
        jiekou="/api/import/archive?token=secret",
        publish_dir=tmp_path / "文章目录" / "demo",
        daily_publish_filename=legacy_daily,
        publish_time="06:00:00",
        interval_time=60,
        daily_publish_limit=60,
        image_file=image_file,
    )
    config_path = tmp_path / "config.ini"
    save_config(config_path, [site])
    loaded = load_config(config_path)
    galleries = GalleryService(tmp_path / "设置")

    assert migrate_site_metadata(config_path, loaded, galleries) is True
    migrated = load_config(config_path)[0]
    assert migrated.site_id
    assert migrated.daily_publish_filename == (
        tmp_path / "设置" / "demo_daily.txt"
    )
    assert migrated.daily_publish_filename.read_text(encoding="utf-8") == (
        "2026-07-15\n4"
    )
    assert (tmp_path / "设置" / "daily.txt").read_text(encoding="utf-8") == (
        "2026-07-15\n4"
    )
    assert migrated.gallery_id
    assert migrated.image_file == galleries.require_gallery(migrated.gallery_id).file_path
    assert len(list(tmp_path.glob("config.ini.backup-*"))) == 1

    assert migrate_site_metadata(config_path, [migrated], galleries) is False
    assert len(list(tmp_path.glob("config.ini.backup-*"))) == 1


def test_metadata_migration_replaces_uuid_counter_with_site_key_filename(tmp_path):
    site_id = "11111111-1111-4111-8111-111111111111"
    uuid_daily = tmp_path / "设置" / "站点状态" / f"{site_id}_daily.txt"
    uuid_daily.parent.mkdir(parents=True)
    uuid_daily.write_text("2026-07-16\n5", encoding="utf-8")
    readable_daily = tmp_path / "设置" / "demo_daily.txt"
    readable_daily.write_text("2026-07-16\n1", encoding="utf-8")
    site = SiteConfig(
        name="demo",
        fid=1,
        url="https://example.com",
        jiekou="/api/import/archive?token=secret",
        publish_dir=tmp_path / "文章目录" / "demo",
        daily_publish_filename=uuid_daily,
        publish_time="06:00:00",
        interval_time=60,
        daily_publish_limit=60,
        site_id=site_id,
    )
    config_path = tmp_path / "config.ini"
    save_config(config_path, [site])

    assert migrate_site_metadata(config_path, [site]) is True

    migrated = load_config(config_path)[0]
    assert migrated.daily_publish_filename == readable_daily
    assert readable_daily.read_text(encoding="utf-8") == "2026-07-16\n5"
    assert uuid_daily.exists() is False


@pytest.mark.parametrize(
    ("publish_time", "publish_times"),
    [
        ("24:00:00", ["09:00:00"]),
        ("09:00:00", ["09:00:00", "bad"]),
        ("9:00", []),
    ],
)
def test_save_config_rejects_invalid_publish_times_without_overwriting_existing_file(
    tmp_path, publish_time, publish_times
):
    config_path = tmp_path / "config.ini"
    original = b"original config bytes\r\n"
    config_path.write_bytes(original)
    site = _managed_path_site(tmp_path)
    site.publish_time = publish_time
    site.publish_times = publish_times

    with pytest.raises(ConfigLoadError, match="发布.*时间|时间.*无效"):
        save_config(config_path, [site])

    assert config_path.read_bytes() == original
