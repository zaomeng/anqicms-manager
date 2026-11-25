from datetime import datetime

from core.schedule_service import (
    build_daily_plan,
    is_valid_publish_time,
    next_run_for_site,
    normalize_publish_time,
    scheduled_time_for_site,
)
from models.entities import SiteConfig, SiteStatusSummary


def make_site(name="demo", enabled=True, times=None):
    return SiteConfig(
        name=name,
        fid=9,
        url="http://example.com",
        jiekou="/api",
        publish_dir=__import__("pathlib").Path("articles"),
        daily_publish_filename=__import__("pathlib").Path("daily.txt"),
        publish_time="09:00:00",
        publish_times=times or ["09:00:00", "14:00:00"],
        interval_time=10,
        daily_publish_limit=7,
        enabled=enabled,
    )


def test_next_run_for_site_picks_next_time():
    site = make_site(times=["09:00:00", "14:00:00", "20:00:00"])
    result = next_run_for_site(site, now=datetime(2026, 5, 15, 13, 0, 0))
    assert result == "14:00:00"


def test_build_daily_plan_sorts_and_skips_disabled_sites():
    sites = [make_site("a", True, ["14:00:00"]), make_site("b", False, ["09:00:00"]), make_site("c", True, ["08:00:00"])]
    sites[0].publish_time = "14:00:00"
    sites[2].publish_time = "08:00:00"
    summaries = {
        "a": SiteStatusSummary(site_name="a", pending_count=3),
        "c": SiteStatusSummary(site_name="c", pending_count=1),
    }
    plan = build_daily_plan(sites, summaries, now=datetime(2026, 5, 15, 9, 30, 0))
    assert [item.site_name for item in plan] == ["c", "a"]
    assert all(item.site_name != "b" for item in plan)


def test_build_daily_plan_emits_one_run_per_site_and_uses_canonical_time():
    site = make_site(times=["09:00:00", "09:00:00", "14:00:00", "09:00:00"])

    plan = build_daily_plan(
        [site],
        {"demo": SiteStatusSummary(site_name="demo", pending_count=4)},
        now=datetime(2026, 5, 15, 8, 0, 0),
    )

    assert [(item.site_name, item.scheduled_time) for item in plan] == [
        ("demo", "09:00:00"),
    ]


def test_build_daily_plan_limits_batch_to_pending_and_remaining_quota():
    site = make_site(times=["09:00:00"])
    summary = SiteStatusSummary(
        site_name="demo", pending_count=12, daily_limit=7, remaining_quota=4
    )

    plan = build_daily_plan([site], {"demo": summary})

    assert plan[0].total_count == 4


def test_scheduled_time_falls_back_to_earliest_legacy_time():
    site = make_site(times=["14:00:00", "08:00:00"])
    site.publish_time = "invalid"

    assert scheduled_time_for_site(site) == "08:00:00"


def test_publish_time_validation_and_normalization():
    assert is_valid_publish_time("09:00:00") is True
    assert is_valid_publish_time("9:00") is False
    assert normalize_publish_time("09:00:00") == "09:00:00"
