from __future__ import annotations

import random
import re
import unicodedata
from html import escape
from ipaddress import ip_address
from pathlib import Path
from collections.abc import Mapping, Sequence
from urllib.parse import unquote, urlsplit


def load_word_set(word_file: str | Path) -> tuple[set[str], int]:
    word_set: set[str] = set()
    max_word_len = 1
    path = Path(word_file)
    if not path.exists():
        return word_set, max_word_len
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            word = line.strip()
            if word:
                word_set.add(word)
                if len(word) > max_word_len:
                    max_word_len = len(word)
    return word_set, max_word_len


def max_match_cut(text: str, word_set: set[str], max_word_len: int = 10) -> list[str]:
    result: list[str] = []
    i = 0
    text_len = len(text)
    while i < text_len:
        matched = False
        for length in range(min(max_word_len, text_len - i), 1, -1):
            word = text[i : i + length]
            if word in word_set:
                result.append(word)
                i += length
                matched = True
                break
        if not matched:
            i += 1
    return result


def extract_high_freq_words(
    text: str,
    word_set: set[str],
    max_word_len: int,
    min_length: int = 2,
    print_count: int = 3,
) -> str:
    words = max_match_cut(text, word_set, max_word_len)
    word_freq: dict[str, int] = {}
    for word in words:
        if len(word) >= min_length:
            word_freq[word] = word_freq.get(word, 0) + 1
    word_freq = {word: count for word, count in word_freq.items() if count > 1}
    sorted_words = sorted(word_freq.items(), key=lambda item: (len(item[0]), item[1]), reverse=True)
    return ",".join(word for word, _ in sorted_words[:print_count])


def add_empty_line_between_paragraphs(text: str) -> str:
    paragraphs = [item.strip() for item in text.splitlines() if item.strip()]
    return "\n\n".join(paragraphs)


def _is_valid_image_hostname(hostname: str) -> bool:
    try:
        ip_address(hostname)
        return True
    except ValueError:
        pass
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_hostname) > 253 or ascii_hostname.endswith("."):
        return False
    label_pattern = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
    return all(label_pattern.fullmatch(label) for label in ascii_hostname.split("."))


def _has_valid_percent_encoding(value: str) -> bool:
    return re.search(r"%(?![0-9A-Fa-f]{2})", value) is None


def _is_safe_image_url_layer(value: str, *, validate_percent: bool) -> bool:
    if validate_percent and not _has_valid_percent_encoding(value):
        return False
    if any(
        char.isspace()
        or unicodedata.category(char) in {"Cc", "Cf"}
        or char in {'"', "'", "<", ">", "\\"}
        for char in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        return False
    if not parsed.scheme:
        return (
            value.startswith("/")
            and not value.startswith("//")
            and not parsed.netloc
            and parsed.path != "/"
        )
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and _is_valid_image_hostname(parsed.hostname or "")
        and parsed.username is None
        and parsed.password is None
    )


def is_valid_image_url(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if not _is_safe_image_url_layer(value, validate_percent=True):
        return False
    current = value
    for _ in range(5):
        try:
            decoded = unquote(current, errors="strict")
        except UnicodeDecodeError:
            return False
        if decoded == current:
            return True
        if not _is_safe_image_url_layer(decoded, validate_percent=False):
            return False
        current = decoded
    return False


def parse_image_groups_content(content: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    current_group = "默认"
    for index, raw_line in enumerate(content.splitlines()):
        line = raw_line.strip()
        if index == 0:
            line = line.lstrip("\ufeff")
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1].strip() or "默认"
            groups.setdefault(current_group, [])
        elif is_valid_image_url(line):
            groups.setdefault(current_group, []).append(line)
    return {name: urls for name, urls in groups.items() if urls}


def parse_image_groups(image_file: str | Path) -> dict[str, list[str]]:
    path = Path(image_file)
    if not path.exists():
        return {}
    return parse_image_groups_content(path.read_text(encoding="utf-8-sig"))


def auto_insert_image(content: str, title: str, image_file: str | Path) -> tuple[str, str]:
    return auto_insert_image_from_groups(content, title, parse_image_groups(image_file))


def auto_insert_image_from_groups(
    content: str,
    title: str,
    groups: Mapping[str, Sequence[str]],
) -> tuple[str, str]:
    valid_groups = {
        group_name: [url for url in urls if is_valid_image_url(url)]
        for group_name, urls in groups.items()
        if isinstance(group_name, str)
    }
    valid_groups = {name: urls for name, urls in valid_groups.items() if urls}
    if not valid_groups:
        return content, ""
    matched_group: str | None = None
    for group_name in valid_groups:
        keywords = [keyword.strip() for keyword in group_name.split("|") if keyword.strip()]
        if any(keyword in title for keyword in keywords):
            matched_group = group_name
            break
    if matched_group is None:
        matched_group = random.choice(list(valid_groups))
    selected_image = random.choice(valid_groups[matched_group])
    safe_image = escape(selected_image, quote=True)
    safe_title = escape(title, quote=True)
    image_tag = f'<img src="{safe_image}" title="{safe_title}" alt="{safe_title}">'
    closing_tags = list(re.finditer(r"</p\s*>", content, flags=re.IGNORECASE))
    if closing_tags:
        insert_after = closing_tags[(len(closing_tags) - 1) // 2].end()
        return f"{content[:insert_after]}{image_tag}{content[insert_after:]}", selected_image
    return content + image_tag, selected_image
