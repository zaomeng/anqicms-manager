from __future__ import annotations

from datetime import datetime

from models.entities import PlannedRun, SiteConfig, SiteStatusSummary


def normalize_publish_time(value: str) -> str:
    return datetime.strptime(value.strip(), "%H:%M:%S").strftime("%H:%M:%S")


def is_valid_publish_time(value: str) -> bool:
    try:
        normalize_publish_time(value)
        return True
    except ValueError:
        return False


def next_run_for_site(site: SiteConfig, now: datetime | None = None) -> str:
    current = now or datetime.now()
    current_time = current.strftime("%H:%M:%S")
    valid_times = sorted({normalize_publish_time(item) for item in site.publish_times if is_valid_publish_time(item)})
    for publish_time in valid_times:
        if publish_time >= current_time:
            return publish_time
    return valid_times[0] if valid_times else ""


def scheduled_time_for_site(site: SiteConfig) -> str:
    """Return the single daily batch time, tolerating legacy multi-time configs."""
    if is_valid_publish_time(site.publish_time):
        return normalize_publish_time(site.publish_time)
    valid_times = sorted(
        {
            normalize_publish_time(item)
            for item in site.publish_times
            if is_valid_publish_time(item)
        }
    )
    return valid_times[0] if valid_times else ""


def build_daily_plan(
    sites: list[SiteConfig],
    summaries: dict[str, SiteStatusSummary],
    now: datetime | None = None,
) -> list[PlannedRun]:
    current = now or datetime.now()
    current_time = current.strftime("%H:%M:%S")
    plan: list[PlannedRun] = []
    for site in sites:
        if not site.enabled:
            continue
        summary = summaries.get(site.name, SiteStatusSummary(site_name=site.name))
        publish_time = scheduled_time_for_site(site)
        if not publish_time:
            continue
        status = "pending" if publish_time >= current_time else "elapsed"
        plan.append(
            PlannedRun(
                site_name=site.name,
                scheduled_time=publish_time,
                status=status,
                total_count=min(summary.pending_count, summary.remaining_quota),
                result_summary=summary.last_result,
            )
        )
    return sorted(plan, key=lambda item: (item.scheduled_time, item.site_name))
