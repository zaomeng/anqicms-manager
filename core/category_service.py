from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from core.site_factory import derive_categories_url, redact_sensitive_url
from core.state_store import atomic_write_text


@dataclass(frozen=True, slots=True)
class Category:
    id: int
    parent_id: int
    title: str


class CategoryServiceError(RuntimeError):
    pass


class CategoryCache:
    VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    @staticmethod
    def _key(publish_endpoint: str) -> str:
        parsed = urlsplit(derive_categories_url(publish_endpoint))
        hostname = (parsed.hostname or "").casefold()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme.casefold() == "https" else 80
        return f"{parsed.scheme.casefold()}://{hostname}:{port}{parsed.path}"

    def get(self, publish_endpoint: str) -> list[Category] | None:
        try:
            key = self._key(publish_endpoint)
        except ValueError:
            return None
        with self._lock:
            payload = self._load()
        raw_categories = payload["entries"].get(key)
        if not isinstance(raw_categories, list):
            return None
        try:
            return CategoryService._parse_payload(
                {"code": 0, "data": raw_categories}
            )
        except CategoryServiceError:
            return None

    def put(self, publish_endpoint: str, categories: list[Category]) -> None:
        key = self._key(publish_endpoint)
        raw_categories = [
            {
                "id": category.id,
                "parent_id": category.parent_id,
                "title": category.title,
            }
            for category in categories
        ]
        CategoryService._parse_payload({"code": 0, "data": raw_categories})
        with self._lock:
            payload = self._load()
            payload["entries"][key] = raw_categories
            atomic_write_text(
                self.path,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )

    def _load(self) -> dict:
        empty = {"version": self.VERSION, "entries": {}}
        if not self.path.exists():
            return empty
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return empty
        if (
            not isinstance(payload, dict)
            or payload.get("version") != self.VERSION
            or not isinstance(payload.get("entries"), dict)
        ):
            return empty
        return payload


class CategoryService:
    def __init__(self, session: requests.Session | None = None, timeout: int = 15) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout

    def fetch(self, publish_endpoint: str) -> list[Category]:
        categories_url = derive_categories_url(publish_endpoint)
        try:
            response = self.session.get(
                categories_url,
                params={"module_id": 0},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            message = redact_sensitive_url(str(exc))
            raise CategoryServiceError(f"栏目获取失败: {message}") from exc

        status_code = int(response.status_code)
        if 300 <= status_code < 400:
            raise CategoryServiceError(f"栏目接口禁止重定向 (HTTP {status_code})")
        if not 200 <= status_code < 300:
            raise CategoryServiceError(f"栏目接口请求失败 (HTTP {status_code})")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise CategoryServiceError("栏目接口返回的不是有效 JSON") from exc
        return self._parse_payload(payload)

    @classmethod
    def _parse_payload(cls, payload: Any) -> list[Category]:
        if not isinstance(payload, dict):
            raise CategoryServiceError("栏目接口返回格式无效")
        code = payload.get("code")
        if type(code) not in {int, str} or code not in {0, "0", 200, "200"}:
            raw_message = payload.get("msg")
            message = raw_message if isinstance(raw_message, str) and raw_message.strip() else f"code={code!r}"
            raise CategoryServiceError(f"栏目接口返回错误: {redact_sensitive_url(message)}")

        raw_categories = payload.get("data")
        if isinstance(raw_categories, dict):
            if isinstance(raw_categories.get("list"), list):
                raw_categories = raw_categories["list"]
            elif isinstance(raw_categories.get("categories"), list):
                raw_categories = raw_categories["categories"]
        if not isinstance(raw_categories, list):
            raise CategoryServiceError("栏目接口 data 必须是数组")

        categories: list[Category] = []
        ids: set[int] = set()
        for index, item in enumerate(raw_categories):
            if not isinstance(item, dict):
                raise CategoryServiceError(f"第 {index + 1} 个栏目格式无效")
            category_id = item.get("id")
            parent_id = item.get("parent_id")
            title = item.get("title")
            if type(category_id) is not int or category_id <= 0:
                raise CategoryServiceError(f"第 {index + 1} 个栏目 ID 无效")
            if type(parent_id) is not int or parent_id < 0:
                raise CategoryServiceError(f"栏目 {category_id} 的父级 ID 无效")
            if not isinstance(title, str) or not title.strip():
                raise CategoryServiceError(f"栏目 {category_id} 的名称为空")
            if category_id in ids:
                raise CategoryServiceError(f"栏目 ID {category_id} 重复")
            ids.add(category_id)
            categories.append(Category(id=category_id, parent_id=parent_id, title=title.strip()))

        parents = {category.id: category.parent_id for category in categories}
        for category in categories:
            if category.parent_id and category.parent_id not in ids:
                raise CategoryServiceError(f"栏目 {category.id} 引用了不存在的父级 {category.parent_id}")

        states: dict[int, int] = {}

        def visit(category_id: int) -> None:
            state = states.get(category_id, 0)
            if state == 1:
                raise CategoryServiceError("栏目父级关系存在循环")
            if state == 2:
                return
            states[category_id] = 1
            parent_id = parents[category_id]
            if parent_id:
                visit(parent_id)
            states[category_id] = 2

        for category_id in parents:
            visit(category_id)
        return categories
