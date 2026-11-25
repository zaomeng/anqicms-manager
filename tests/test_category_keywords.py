import pytest

from core.category_keywords import (
    CategoryKeywordsError,
    normalize_category_keywords,
    resolve_category_id,
)


def test_normalize_category_keywords_preserves_order_and_deduplicates_within_category():
    result = normalize_category_keywords(
        {"9": [" 普通搬家 ", "普通搬家"], "3": ["实验室"]}
    )

    assert result == {9: ("普通搬家",), 3: ("实验室",)}
    assert list(result) == [9, 3]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "必须是对象"),
        ({"bad": ["词"]}, "正整数"),
        ({"0": ["词"]}, "正整数"),
        ({"3": "词"}, "必须是数组"),
        ({"3": [1]}, "必须是文本"),
        ({"3": ["  "]}, "不能为空"),
        ({"3": ["词"], "4": ["词"]}, "同时分配"),
    ],
)
def test_normalize_category_keywords_rejects_invalid_values(value, message):
    with pytest.raises(CategoryKeywordsError, match=message):
        normalize_category_keywords(value)


def test_resolve_category_id_prefers_longest_match():
    keywords = {3: ("实验室",), 8: ("实验室搬家",)}

    assert resolve_category_id("北京实验室搬家公司", keywords, 9) == 8


def test_resolve_category_id_uses_configuration_order_for_equal_length():
    keywords = {4: ("室搬",), 3: ("实验",)}

    assert resolve_category_id("实验室搬家", keywords, 9) == 4


def test_resolve_category_id_is_case_sensitive_and_falls_back():
    assert resolve_category_id("example", {3: ("Example",)}, 9) == 9
