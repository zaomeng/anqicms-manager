"""UI orchestration helper for batch-opening site admin backends.

Decoupled from ``core/admin_jump_service.py`` via duck-typing or an injected
``jump_fn`` callable so UI callers and tests do not import service internals.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from tkinter import messagebox

from core.integration_settings import AdminJumpSettings, IntegrationSettings
from models.entities import SiteConfig
from models.sitegroup import AdminJumpResult, JUMP_TIER_BRIDGE, JUMP_TIER_TICKET
from ui.ui_async import BackgroundRunner

_SILENT_TIERS = frozenset({JUMP_TIER_TICKET, JUMP_TIER_BRIDGE})


def _resolve_jump_settings(
    settings: IntegrationSettings | AdminJumpSettings | None,
) -> AdminJumpSettings:
    if isinstance(settings, IntegrationSettings):
        return settings.admin_jump
    if isinstance(settings, AdminJumpSettings):
        return settings
    return AdminJumpSettings()


def _resolve_jump_callable(
    service,
    jump_fn: Callable[[Sequence[SiteConfig]], Sequence[AdminJumpResult]] | None,
) -> Callable[[Sequence[SiteConfig]], Sequence[AdminJumpResult]]:
    if jump_fn is not None:
        return jump_fn
    if callable(service):
        return service
    if service is not None and hasattr(service, "open_many"):
        return service.open_many
    if service is not None and hasattr(service, "open"):
        return lambda batch: [service.open(site) for site in batch]
    raise ValueError("缺少后台跳转服务或跳转函数")


def format_jump_summary(
    results: Sequence[AdminJumpResult],
    *,
    truncated: int = 0,
) -> str:
    """Format a Chinese summary for batch admin-jump results."""
    opened = 0
    silent = 0
    direct = 0
    failed = 0
    for item in results or ():
        if item.success:
            opened += 1
            if item.tier in _SILENT_TIERS:
                silent += 1
            else:
                direct += 1
        else:
            failed += 1
    summary = f"已打开 {opened} 个后台（免密 {silent} / 直达 {direct}），失败 {failed}"
    if truncated > 0:
        summary += f"（超出上限，已跳过 {truncated} 个站点）"
    return summary


def _accepts_progress(func: Callable) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if "progress" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def open_admin_backends(
    parent,
    sites: Sequence[SiteConfig],
    service=None,
    runner: BackgroundRunner | None = None,
    *,
    jump_fn: Callable[[Sequence[SiteConfig]], Sequence[AdminJumpResult]] | None = None,
    status_callback: Callable[[str], None] | None = None,
    on_complete: Callable[[list[AdminJumpResult]], None] | None = None,
    settings: IntegrationSettings | AdminJumpSettings | None = None,
    confirm_fn: Callable[[str, str], bool] | None = None,
    warn_fn: Callable[[str, str], None] | None = None,
) -> bool:
    """Validate selection, confirm large batches, cap to ``batch_max`` and open backends.

    Returns ``True`` when the jump operation was dispatched/completed, or
    ``False`` when the selection was empty or the user cancelled confirmation.
    """
    selected = list(sites or ())
    if not selected:
        empty_msg = "请先选择至少一个站点"
        if status_callback is not None:
            status_callback(empty_msg)
        if warn_fn is not None:
            warn_fn("提示", empty_msg)
        elif status_callback is None:
            messagebox.showinfo("提示", empty_msg, parent=parent)
        return False

    callable_jump = _resolve_jump_callable(service, jump_fn)
    jump_settings = _resolve_jump_settings(settings)
    threshold = max(0, int(jump_settings.batch_confirm_threshold))
    batch_max = max(1, int(jump_settings.batch_max))

    total = len(selected)
    batch = selected[:batch_max]
    truncated = max(0, total - len(batch))

    if total > threshold:
        if truncated > 0:
            prompt = (
                f"已选择 {total} 个站点，单次最多打开 {batch_max} 个后台标签页，"
                f"将仅打开前 {len(batch)} 个。确定继续吗？"
            )
        else:
            prompt = f"即将同时打开 {total} 个站点后台标签页，确定继续吗？"
        asker = confirm_fn or (lambda title, msg: messagebox.askyesno(title, msg, parent=parent))
        if not asker("批量跳转后台", prompt):
            if status_callback is not None:
                status_callback("已取消批量跳转后台")
            return False

    if status_callback is not None:
        status_callback(f"正在打开 {len(batch)} 个站点后台…")

    def _on_success(results: Sequence[AdminJumpResult]) -> None:
        result_list = list(results or ())
        if status_callback is not None:
            status_callback(format_jump_summary(result_list, truncated=truncated))
        if on_complete is not None:
            on_complete(result_list)

    def _on_error(exc: Exception) -> None:
        if status_callback is not None:
            status_callback(f"跳转后台失败：{exc}")

    def _invoke() -> Sequence[AdminJumpResult]:
        if status_callback is not None and _accepts_progress(callable_jump):
            ui_progress = (
                lambda done, count, result: status_callback(
                    f"正在打开后台 ({done}/{count})：{getattr(result, 'site_name', '')}"
                )
            )
            progress_cb = runner.make_progress(ui_progress) if runner is not None else ui_progress
            return callable_jump(batch, progress=progress_cb)
        return callable_jump(batch)

    if runner is not None:
        runner.submit(_invoke, _on_success, _on_error)
    else:
        try:
            _on_success(_invoke())
        except Exception as exc:  # noqa: BLE001
            _on_error(exc)
    return True
