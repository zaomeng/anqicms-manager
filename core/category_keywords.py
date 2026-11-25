from __future__ import annotations


class CategoryKeywordsError(ValueError):
    pass


def normalize_category_keywords(
    value: object,
) -> dict[int, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise CategoryKeywordsError("栏目关键词必须是对象")

    normalized: dict[int, tuple[str, ...]] = {}
    keyword_owners: dict[str, int] = {}
    for raw_category_id, raw_keywords in value.items():
        if isinstance(raw_category_id, bool):
            raise CategoryKeywordsError("栏目 ID 必须为正整数")
        if isinstance(raw_category_id, int):
            category_id = raw_category_id
        elif isinstance(raw_category_id, str):
            try:
                category_id = int(raw_category_id)
            except ValueError as exc:
                raise CategoryKeywordsError(
                    f"栏目 ID 必须为正整数: {raw_category_id}"
                ) from exc
        else:
            raise CategoryKeywordsError("栏目 ID 必须为正整数")
        if category_id <= 0:
            raise CategoryKeywordsError(f"栏目 ID 必须为正整数: {raw_category_id}")
        if category_id in normalized:
            raise CategoryKeywordsError(f"栏目 ID 重复: {category_id}")
        if not isinstance(raw_keywords, (list, tuple)):
            raise CategoryKeywordsError(f"栏目 {category_id} 的关键词必须是数组")

        keywords: list[str] = []
        seen_in_category: set[str] = set()
        for raw_keyword in raw_keywords:
            if not isinstance(raw_keyword, str):
                raise CategoryKeywordsError(f"栏目 {category_id} 的关键词必须是文本")
            keyword = raw_keyword.strip()
            if not keyword:
                raise CategoryKeywordsError(f"栏目 {category_id} 的关键词不能为空")
            if keyword in seen_in_category:
                continue
            owner = keyword_owners.get(keyword)
            if owner is not None:
                raise CategoryKeywordsError(
                    f"关键词“{keyword}”同时分配给栏目 {owner} 和 {category_id}"
                )
            seen_in_category.add(keyword)
            keyword_owners[keyword] = category_id
            keywords.append(keyword)
        normalized[category_id] = tuple(keywords)
    return normalized


def resolve_category_id(
    title: str,
    category_keywords: dict[int, tuple[str, ...]],
    default_fid: int,
) -> int:
    matched_category_id = default_fid
    longest_match = 0
    for category_id, keywords in category_keywords.items():
        for keyword in keywords:
            if keyword in title and len(keyword) > longest_match:
                matched_category_id = category_id
                longest_match = len(keyword)
    return matched_category_id
