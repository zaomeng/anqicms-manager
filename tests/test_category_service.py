import pytest
import requests

from core.category_service import (
    Category,
    CategoryCache,
    CategoryService,
    CategoryServiceError,
)


PUBLISH_ENDPOINT = "https://www.example.com/api/import/archive?token=synthetic-secret"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.parametrize("code", [0, "0", 200, "200"])
def test_fetch_categories_accepts_supported_success_codes_and_request_contract(code):
    session = FakeSession(
        FakeResponse(
            {
                "code": code,
                "msg": "",
                "data": [
                    {"id": 1, "parent_id": 0, "title": "新闻大事"},
                    {"id": 2, "parent_id": 1, "title": "国际新闻"},
                ],
            }
        )
    )

    categories = CategoryService(session=session).fetch(PUBLISH_ENDPOINT)

    assert categories == [Category(1, 0, "新闻大事"), Category(2, 1, "国际新闻")]
    assert session.calls == [
        (
            "https://www.example.com/api/import/categories?token=synthetic-secret",
            {"params": {"module_id": 0}, "timeout": 15, "allow_redirects": False},
        )
    ]


@pytest.mark.parametrize("container_key", ["list", "categories"])
def test_fetch_categories_accepts_known_wrapped_data_shapes(container_key):
    payload = {"code": 0, "data": {container_key: [{"id": 1, "parent_id": 0, "title": "根栏目"}]}}
    categories = CategoryService(session=FakeSession(FakeResponse(payload))).fetch(PUBLISH_ENDPOINT)
    assert categories == [Category(1, 0, "根栏目")]


@pytest.mark.parametrize(
    "data, message",
    [
        ([{"id": True, "parent_id": 0, "title": "布尔"}], "ID 无效"),
        ([{"id": 0, "parent_id": 0, "title": "零"}], "ID 无效"),
        ([{"id": 1, "parent_id": False, "title": "布尔"}], "父级 ID 无效"),
        ([{"id": 1, "parent_id": 0, "title": ""}], "名称为空"),
        (
            [
                {"id": 1, "parent_id": 0, "title": "一"},
                {"id": 1, "parent_id": 0, "title": "重复"},
            ],
            "重复",
        ),
        ([{"id": 1, "parent_id": 9, "title": "孤儿"}], "不存在的父级"),
        (
            [
                {"id": 1, "parent_id": 2, "title": "一"},
                {"id": 2, "parent_id": 1, "title": "二"},
            ],
            "循环",
        ),
    ],
)
def test_category_validation_rejects_invalid_ids_titles_and_hierarchy(data, message):
    with pytest.raises(CategoryServiceError, match=message):
        CategoryService._parse_payload({"code": 0, "data": data})


def test_category_api_error_redacts_token_from_message():
    payload = {"code": -1, "msg": "Token错误 token=real-secret"}
    with pytest.raises(CategoryServiceError) as exc_info:
        CategoryService._parse_payload(payload)
    assert "real-secret" not in str(exc_info.value)
    assert "token=***" in str(exc_info.value)


def test_network_error_redacts_token_and_keeps_request_recoverable():
    session = FakeSession(requests.Timeout(f"timeout at {PUBLISH_ENDPOINT}"))
    with pytest.raises(CategoryServiceError) as exc_info:
        CategoryService(session=session).fetch(PUBLISH_ENDPOINT)
    assert "synthetic-secret" not in str(exc_info.value)
    assert "token=***" in str(exc_info.value)


@pytest.mark.parametrize("status_code", [301, 302, 307, 308])
def test_category_fetch_rejects_redirects(status_code):
    with pytest.raises(CategoryServiceError, match="禁止重定向"):
        CategoryService(session=FakeSession(FakeResponse({}, status_code))).fetch(PUBLISH_ENDPOINT)


def test_category_fetch_rejects_http_errors_and_invalid_json():
    with pytest.raises(CategoryServiceError, match="HTTP 500"):
        CategoryService(session=FakeSession(FakeResponse({}, 500))).fetch(PUBLISH_ENDPOINT)
    with pytest.raises(CategoryServiceError, match="有效 JSON"):
        CategoryService(session=FakeSession(FakeResponse(ValueError("bad json")))).fetch(PUBLISH_ENDPOINT)


def test_category_cache_persists_without_storing_endpoint_token(tmp_path):
    cache_path = tmp_path / "category_cache.json"
    categories = [Category(1, 0, "新闻"), Category(2, 1, "行业新闻")]

    CategoryCache(cache_path).put(PUBLISH_ENDPOINT, categories)

    rotated_endpoint = PUBLISH_ENDPOINT.replace("synthetic-secret", "rotated-secret")
    assert CategoryCache(cache_path).get(rotated_endpoint) == categories
    cache_text = cache_path.read_text(encoding="utf-8")
    assert "synthetic-secret" not in cache_text
    assert "rotated-secret" not in cache_text


def test_category_cache_treats_empty_list_as_hit_and_recovers_from_corruption(tmp_path):
    cache_path = tmp_path / "category_cache.json"
    cache = CategoryCache(cache_path)
    cache.put(PUBLISH_ENDPOINT, [])
    assert CategoryCache(cache_path).get(PUBLISH_ENDPOINT) == []

    cache_path.write_text("not-json", encoding="utf-8")
    assert CategoryCache(cache_path).get(PUBLISH_ENDPOINT) is None

    CategoryCache(cache_path).put(PUBLISH_ENDPOINT, [Category(1, 0, "新闻")])
    assert CategoryCache(cache_path).get(PUBLISH_ENDPOINT) == [Category(1, 0, "新闻")]
