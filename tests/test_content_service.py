import pytest

from core.content_service import (
    add_empty_line_between_paragraphs,
    auto_insert_image,
    auto_insert_image_from_groups,
    extract_high_freq_words,
    is_valid_image_url,
    max_match_cut,
    parse_image_groups,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://img.example/a.jpg",
        "https://img.example/dynamic?id=1",
        "/uploads/a.jpg",
        "/uploads/a.webp?v=1",
        "/图片/封面",
        "/uploads/%E5%9B%BE%E7%89%87.webp",
        "/uploads/100%25.webp",
        "/uploads/a.jpg?discount=50%25",
        "https://img.example/%E5%9B%BE%E7%89%87.webp?name=%E5%B0%81%E9%9D%A2",
    ],
)
def test_image_url_validation_accepts_http_and_root_relative_links(url):
    assert is_valid_image_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "/",
        "//img.example/a.jpg",
        "uploads/a.jpg",
        "./uploads/a.jpg",
        "../uploads/a.jpg",
        "/uploads/../a.jpg",
        "/uploads/%2e%2e/a.jpg",
        "/uploads/%2E%2E/a.jpg",
        "/uploads/%5c..%5ca.jpg",
        "/uploads/%00a.jpg",
        "/uploads/%0aa.jpg",
        "/uploads/%252e%252e/a.jpg",
        "/uploads/%25x/%252e%252e/a.jpg",
        "/uploads/%25x%2500a.jpg",
        "/uploads/%25x%250aa.jpg",
        "/uploads/%25x%25E2%2580%25AEa.jpg",
        "/uploads/%GG/a.jpg",
        "/uploads/a\u202eb.jpg",
        "/uploads/%E2%80%AEb.jpg",
        "https://img.example/%00a.jpg",
        "https://img.example/%252e%252e/a.jpg",
        "ftp://img.example/a.jpg",
        "javascript:alert(1)",
        "/uploads/a b.jpg",
        "/uploads/a\\b.jpg",
        '/uploads/a" onerror="alert(1)',
        "/uploads/<script>.jpg",
        "/uploads/a.jpg\x7f",
    ],
)
def test_image_url_validation_rejects_unsafe_or_non_root_relative_links(url):
    assert is_valid_image_url(url) is False


def test_max_match_cut_prefers_longest_word():
    words = {"实验室搬家", "实验室", "搬家"}
    result = max_match_cut("实验室搬家服务", words, 5)
    assert result[0] == "实验室搬家"


def test_extract_high_freq_words_returns_repeated_multi_char_words():
    word_set = {"实验室", "搬家", "服务"}
    result = extract_high_freq_words("实验室搬家实验室搬家服务", word_set, 3)
    assert "实验室" in result
    assert "搬家" in result


def test_add_empty_line_between_paragraphs_normalizes_spacing():
    text = "a\n\nb\n\n\nc"
    assert add_empty_line_between_paragraphs(text) == "a\n\nb\n\nc"


def test_auto_insert_image_returns_selected_url(tmp_path, monkeypatch):
    image_file = tmp_path / "images.txt"
    image_file.write_text("[实验室|搬家]\nhttp://img/1.jpg\n", encoding="utf-8")
    monkeypatch.setattr("random.choice", lambda seq: seq[0])
    content, selected = auto_insert_image("<p>a</p><p>b</p>", "实验室搬家", image_file)
    assert selected == "http://img/1.jpg"
    assert "img src=" in content


def test_auto_insert_image_preserves_paragraph_markup_without_orphan_closing_tag(
    tmp_path, monkeypatch
):
    image_file = tmp_path / "images.txt"
    image_file.write_text("[实验室]\nhttps://img.example/1.jpg\n", encoding="utf-8")
    monkeypatch.setattr("random.choice", lambda seq: seq[0])

    content, selected = auto_insert_image(
        "<p>a</p><p>b</p>", "实验室搬家", image_file
    )

    assert selected == "https://img.example/1.jpg"
    assert content.startswith("<p>a</p>")
    assert content.endswith("<p>b</p>")
    assert content.count("<p>") == 2
    assert content.count("</p>") == 2
    assert "img></p>" not in content
    assert "</p></p>" not in content


def test_auto_insert_image_escapes_html_attributes_but_returns_raw_selected_url(
    tmp_path, monkeypatch
):
    image_file = tmp_path / "images.txt"
    raw_url = "https://img.example/image.jpg?size=large&format=webp"
    raw_title = '标题" onload="alert(2)&<tag>'
    image_file.write_text(f"[标题]\n{raw_url}\n", encoding="utf-8")
    monkeypatch.setattr("random.choice", lambda seq: seq[0])

    content, selected = auto_insert_image("<p>正文</p>", raw_title, image_file)

    assert selected == raw_url
    assert 'src="https://img.example/image.jpg?size=large&amp;format=webp"' in content
    assert 'title="标题&quot; onload=&quot;alert(2)&amp;&lt;tag&gt;"' in content
    assert 'alt="标题&quot; onload=&quot;alert(2)&amp;&lt;tag&gt;"' in content
    assert ' onload="alert(2)' not in content


def test_runtime_image_groups_exclude_every_invalid_url_candidate(tmp_path):
    image_file = tmp_path / "images.txt"
    image_file.write_text(
        "[default]\n"
        "not-a-url\n"
        "ftp://img.example/a.jpg\n"
        "https://user:pass@img.example/b.jpg\n"
        "https://img.example:bad/c.jpg\n"
        'https://img.example/a" onerror="alert(1).jpg\n'
        "https://img.example/valid.jpg\n",
        encoding="utf-8",
    )

    groups = parse_image_groups(image_file)

    assert groups == {"default": ["https://img.example/valid.jpg"]}


def test_auto_insert_image_does_not_use_invalid_only_gallery(tmp_path):
    image_file = tmp_path / "images.txt"
    image_file.write_text(
        '[default]\nhttps://img.example/a" onerror="alert(1).jpg\n',
        encoding="utf-8",
    )

    content, selected = auto_insert_image("<p>body</p>", "title", image_file)

    assert content == "<p>body</p>"
    assert selected == ""


def test_auto_insert_image_from_snapshot_groups_reuses_selection_logic(monkeypatch):
    groups = {
        "laboratory|moving": ("https://img.example/matched.jpg",),
        "default": ("https://img.example/default.jpg",),
    }
    monkeypatch.setattr("random.choice", lambda seq: seq[0])

    content, selected = auto_insert_image_from_groups(
        "<p>body</p>", "laboratory moving", groups
    )

    assert selected == "https://img.example/matched.jpg"
    assert 'src="https://img.example/matched.jpg"' in content
