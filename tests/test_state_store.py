import json

import pytest

from core.state_store import (
    StateFileError,
    ensure_daily_file,
    get_published_count,
    load_schedule_claims,
    save_schedule_claims,
    update_published_count,
)


def test_get_published_count_returns_zero_when_missing(tmp_path):
    assert get_published_count(tmp_path / "missing.txt", today="2026-05-15") == 0


def test_update_and_get_published_count_round_trip(tmp_path):
    state_file = tmp_path / "state" / "daily.txt"
    update_published_count(state_file, 5, today="2026-05-15")
    assert get_published_count(state_file, today="2026-05-15") == 5


def test_ensure_daily_file_resets_stale_date(tmp_path):
    state_file = tmp_path / "daily.txt"
    state_file.write_text("2026-05-14\n7", encoding="utf-8")
    ensure_daily_file(state_file, today="2026-05-15")
    assert get_published_count(state_file, today="2026-05-15") == 0


def test_corrupt_publish_count_fails_closed(tmp_path):
    state_file = tmp_path / "daily.txt"
    state_file.write_text("2026-05-15\nnot-a-number", encoding="utf-8")

    with pytest.raises(StateFileError):
        get_published_count(state_file, today="2026-05-15")


def test_empty_legacy_count_file_is_initialized_as_zero(tmp_path):
    state_file = tmp_path / "daily.txt"
    state_file.write_text("", encoding="utf-8")

    ensure_daily_file(state_file, today="2026-05-15")

    assert get_published_count(state_file, today="2026-05-15") == 0
    assert state_file.read_text(encoding="utf-8") == "2026-05-15\n0"


def test_publish_count_update_is_atomic(tmp_path):
    state_file = tmp_path / "daily.txt"
    update_published_count(state_file, 3, today="2026-05-15")

    assert state_file.read_text(encoding="utf-8") == "2026-05-15\n3"
    assert not state_file.with_name("daily.txt.tmp").exists()


def test_schedule_claims_round_trip_and_date_rollover(tmp_path):
    path = tmp_path / "schedule.json"
    claims = {"2026-05-15|demo|09:00:00"}
    save_schedule_claims(path, claims, today="2026-05-15")

    assert load_schedule_claims(path, today="2026-05-15") == claims
    assert load_schedule_claims(path, today="2026-05-16") == set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["date"] == "2026-05-15"


def test_corrupt_schedule_claims_fail_closed(tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(StateFileError):
        load_schedule_claims(path, today="2026-05-15")
