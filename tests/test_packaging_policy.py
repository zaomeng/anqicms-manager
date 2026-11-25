from pathlib import Path


def test_pyinstaller_spec_excludes_credentials_and_user_content():
    spec = Path("anqicms多线程网站发布1.spec").read_text(encoding="utf-8")

    for forbidden in ["config.ini", "sz-banjia图片.txt", "('设置'", "('文章目录'", "('归档'"]:
        assert forbidden not in spec
    assert "('jieba_data', 'jieba_data')" in spec


def test_release_build_consumes_locked_dependencies_and_generates_checksums():
    script = Path("build-release.ps1").read_text(encoding="utf-8")

    assert "requirements-build.lock" in script
    assert "requests.__version__ == '2.33.0'" in script
    assert "urllib3.__version__ == '2.7.0'" in script
    assert "SHA256SUMS.txt" in script
    assert "-Encoding utf8" in script
