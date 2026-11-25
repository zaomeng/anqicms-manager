from __future__ import annotations

import re
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path

import pytest

from core.friendlink_ledger import FriendLinkLedger
from core.friendlink_service import parse_batch_link_input, plan_crosslink_items
from core.integration_settings import IntegrationSettings
from models.entities import SiteConfig
from models.sitegroup import (
    CROSSLINK_HUB,
    CROSSLINK_MESH,
    CROSSLINK_RING,
    LINK_CREATED,
    LINK_DELETED,
    LINK_PLANNED,
    LINK_SKIPPED_EXISTS,
    LINK_SKIPPED_SELF,
    CrossLinkPlan,
    CrossLinkReport,
    FriendLink,
    FriendLinkSpec,
    LedgerEntry,
    LinkHealth,
    LinkOpResult,
)
from ui import friendlink_batch_panel as batch_panel_mod
from ui import friendlink_center as center_mod
from ui.friendlink_center import FriendLinkCenter


def make_root():
    last_exc: tk.TclError | None = None
    for _ in range(3):
        try:
            root = tk.Tk()
            root.withdraw()
            return root
        except tk.TclError as exc:
            last_exc = exc
            time.sleep(0.05)
    pytest.skip(f"Tk environment unavailable: {last_exc}")


def pump_until(root: tk.Tk, predicate, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            root.update()
        except tk.TclError:
            return bool(predicate())
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def make_site(name: str, *, site_group: str = "默认组") -> SiteConfig:
    return SiteConfig(
        name=name,
        fid=1,
        url=f"https://{name}.example.com",
        jiekou=f"/api/import/archive?token=tok-{name}",
        publish_dir=Path(f"articles/{name}"),
        daily_publish_filename=Path(f"logs/{name}.txt"),
        publish_time="06:00:00",
        interval_time=5,
        daily_publish_limit=10,
        site_id=f"id-{name}",
        site_title=f"{name.upper()}站",
        site_group=site_group,
    )


class FakeFriendLinkService:
    """In-memory FriendLinkService fake with zero network calls."""

    def __init__(self) -> None:
        self.links_by_site: dict[str, list[FriendLink]] = {}
        self.health_overrides: dict[str, LinkHealth] = {}
        self.next_id = 100
        self.plan_seq = 0
        self.calls: list[tuple] = []

    def seed(self, site_name: str, links: list[FriendLink]) -> None:
        self.links_by_site[site_name] = list(links)

    def list_links(self, site: SiteConfig) -> list[FriendLink]:
        self.calls.append(("list_links", site.name))
        return list(self.links_by_site.get(site.name, []))

    def add_link(
        self,
        site: SiteConfig,
        spec: FriendLinkSpec,
        *,
        existing: list[FriendLink] | None = None,
    ) -> LinkOpResult:
        self.calls.append(("add_link", site.name, spec.link))
        site_host = site.url.split("://", 1)[-1].split("/", 1)[0].casefold()
        spec_host = spec.link.split("://", 1)[-1].split("/", 1)[0].casefold()
        if site_host and spec_host == site_host:
            return LinkOpResult(
                site_name=site.name,
                site_id=site.site_id,
                action="add",
                link=spec.link,
                title=spec.title,
                status=LINK_SKIPPED_SELF,
                message="跳过本站域名",
            )
        current = self.links_by_site.setdefault(site.name, [])
        if any(item.link.rstrip("/") == spec.link.rstrip("/") for item in current):
            return LinkOpResult(
                site_name=site.name,
                site_id=site.site_id,
                action="add",
                link=spec.link,
                title=spec.title,
                status=LINK_SKIPPED_EXISTS,
                message="目标友链已存在",
            )
        self.next_id += 1
        current.append(
            FriendLink(
                id=self.next_id,
                title=spec.title,
                link=spec.link,
                nofollow=spec.nofollow,
                back_link=spec.back_link,
                remark=spec.remark,
                status=1,
                status_text="正常",
            )
        )
        return LinkOpResult(
            site_name=site.name,
            site_id=site.site_id,
            action="add",
            link=spec.link,
            title=spec.title,
            status=LINK_CREATED,
            message="已添加",
        )

    def remove_link(
        self,
        site: SiteConfig,
        link_url: str,
        *,
        existing: list[FriendLink] | None = None,
    ) -> LinkOpResult:
        self.calls.append(("remove_link", site.name, link_url))
        current = self.links_by_site.setdefault(site.name, [])
        before = len(current)
        self.links_by_site[site.name] = [
            item for item in current if item.link.rstrip("/") != link_url.rstrip("/")
        ]
        if len(self.links_by_site[site.name]) < before:
            return LinkOpResult(
                site_name=site.name,
                site_id=site.site_id,
                action="remove",
                link=link_url,
                title="",
                status=LINK_DELETED,
                message="已删除",
            )
        return LinkOpResult(
            site_name=site.name,
            site_id=site.site_id,
            action="remove",
            link=link_url,
            title="",
            status="not_found",
            message="未找到",
        )

    def check_backlinks(
        self,
        site: SiteConfig,
        links=None,
        *,
        cancel_event=None,
    ) -> list[LinkHealth]:
        self.calls.append(("check_backlinks", site.name))
        target = list(links if links is not None else self.list_links(site))
        out: list[LinkHealth] = []
        for idx, item in enumerate(target):
            url = item.link if isinstance(item, FriendLink) else str(item)
            override = self.health_overrides.get(url.rstrip("/"))
            if override is not None:
                out.append(override)
                continue
            out.append(
                LinkHealth(
                    site_name=site.name,
                    link=url,
                    reachable=True,
                    has_backlink=(idx % 2 == 0),
                    http_status=200,
                    message="OK",
                )
            )
        return out

    def batch_add(self, sites, specs, *, progress=None, cancel_event=None) -> list[LinkOpResult]:
        self.calls.append(("batch_add", [s.name for s in sites], [sp.link for sp in specs]))
        results: list[LinkOpResult] = []
        for site in sites:
            for spec in specs:
                res = self.add_link(site, spec)
                results.append(res)
                if progress is not None:
                    progress(res)
        return results

    def batch_remove(self, sites, link_urls, *, progress=None, cancel_event=None) -> list[LinkOpResult]:
        self.calls.append(("batch_remove", [s.name for s in sites], list(link_urls)))
        results: list[LinkOpResult] = []
        for site in sites:
            for url in link_urls:
                res = self.remove_link(site, url)
                results.append(res)
                if progress is not None:
                    progress(res)
        return results

    def plan_crosslinks(
        self,
        sites,
        mode: str,
        *,
        hub_site_id: str = "",
        bidirectional: bool = True,
        nofollow: bool = False,
        remark: str = "",
    ) -> CrossLinkPlan:
        self.plan_seq += 1
        plan_id = f"plan-{self.plan_seq:04d}"
        resolved_hub = ""
        if mode == CROSSLINK_HUB:
            for s in sites:
                if hub_site_id in {s.site_id, s.name}:
                    resolved_hub = s.site_id
                    break
        items = plan_crosslink_items(
            sites,
            mode,
            hub_site_id=hub_site_id,
            bidirectional=bidirectional,
            nofollow=nofollow,
            remark=remark or "站群互链",
            plan_id=plan_id,
        )
        return CrossLinkPlan(
            plan_id=plan_id,
            mode=mode,
            created_at="2026-11-21T12:00:00",
            hub_site_id=resolved_hub if mode == CROSSLINK_HUB else "",
            bidirectional=bool(bidirectional),
            items=items,
        )

    def preview_plan(self, plan: CrossLinkPlan, sites, *, progress=None, cancel_event=None) -> CrossLinkPlan:
        self.calls.append(("preview_plan", plan.plan_id, plan.mode))
        updated = []
        for item in plan.items:
            new_item = replace(item, state=LINK_PLANNED, message="待创建")
            updated.append(new_item)
            if progress is not None:
                progress(new_item)
        return replace(plan, items=tuple(updated))

    def execute_plan(
        self,
        plan: CrossLinkPlan,
        sites,
        *,
        ledger: FriendLinkLedger | None = None,
        progress=None,
        cancel_event=None,
    ) -> CrossLinkReport:
        self.calls.append(("execute_plan", plan.plan_id))
        if ledger is not None:
            ledger.record_plan(plan)
        site_map = {s.site_id: s for s in sites}
        updated_items = []
        results = []
        entries = []
        for item in plan.items:
            site = site_map[item.source_site_id]
            res = self.add_link(site, item.spec)
            results.append(res)
            updated_items.append(replace(item, state=res.status, message=res.message))
            entries.append(
                LedgerEntry(
                    plan_id=plan.plan_id,
                    source_site_id=item.source_site_id,
                    source_name=item.source_name,
                    target_site_id=item.target_site_id,
                    link=res.link,
                    title=res.title,
                    status=res.status,
                    created_at="2026-11-21T12:00:05",
                )
            )
            if progress is not None:
                progress(res)
        if ledger is not None and entries:
            ledger.record_results(plan.plan_id, entries)
        return CrossLinkReport(
            plan=replace(plan, items=tuple(updated_items)),
            results=tuple(results),
        )

    def rollback_plan(
        self,
        plan_id: str,
        sites,
        *,
        ledger: FriendLinkLedger,
        progress=None,
        cancel_event=None,
    ) -> list[LinkOpResult]:
        self.calls.append(("rollback_plan", plan_id))
        site_map = {s.site_id: s for s in sites}
        results = []
        for entry in ledger.created_links(plan_id):
            site = site_map[entry.source_site_id]
            res = self.remove_link(site, entry.link)
            ledger.mark_rolled_back(plan_id, entry.source_site_id, entry.link)
            results.append(res)
            if progress is not None:
                progress(res)
        return results

    def unlink_between(self, sites, *, progress=None, cancel_event=None) -> list[LinkOpResult]:
        self.calls.append(("unlink_between", [s.name for s in sites]))
        return []


def test_friendlink_center_tabs_and_clean_close(tmp_path):
    root = make_root()
    try:
        sites = [make_site("alpha"), make_site("beta")]
        svc = FakeFriendLinkService()
        ledger = FriendLinkLedger(tmp_path / "ledger.json")

        win = FriendLinkCenter(
            root,
            lambda: sites,
            config_path=tmp_path / "config.ini",
            settings=IntegrationSettings(),
            service=svc,
            ledger=ledger,
            confirm_fn=lambda _t, _m: True,
            warn_fn=lambda _t, _m: None,
        )
        win.withdraw()

        assert win.title() == "友链管理中心"
        tab_texts = [win.notebook.tab(tab_id, "text") for tab_id in win.notebook.tabs()]
        assert tab_texts == ["单站友链", "统一上下链", "站群互链"]

        win.select_tab("batch")
        assert win.notebook.index(win.notebook.select()) == 1
        win.select_tab("crosslink")
        assert win.notebook.index(win.notebook.select()) == 2
        win.select_tab("single")
        assert win.notebook.index(win.notebook.select()) == 0

        win.on_close()
        assert win.runner._closed is True
    finally:
        root.destroy()


def test_single_site_tab_refresh_add_check_delete(tmp_path):
    root = make_root()
    try:
        sites = [make_site("alpha"), make_site("beta")]
        svc = FakeFriendLinkService()
        svc.seed(
            "alpha",
            [
                FriendLink(id=1, title="老友链", link="https://old.example.com/", remark="初始"),
            ],
        )
        ledger = FriendLinkLedger(tmp_path / "ledger.json")

        win = FriendLinkCenter(
            root,
            lambda: sites,
            config_path=tmp_path / "config.ini",
            service=svc,
            ledger=ledger,
            initial_site_name="alpha",
            confirm_fn=lambda _t, _m: True,
            warn_fn=lambda _t, _m: None,
        )
        win.withdraw()

        # Initial auto-refresh on alpha
        assert pump_until(root, lambda: not win._single_busy and len(win.single_tree.get_children()) == 1)
        row0 = win.single_tree.item(win.single_tree.get_children()[0], "values")
        assert row0[1] == "老友链"
        assert row0[2] == "https://old.example.com/"

        # Add single link
        win.add_title_var.set("新友链")
        win.add_link_var.set("https://new.example.com")
        win.add_nofollow_var.set(True)
        win.add_remark_var.set("手动添加")
        assert win.add_single_link() is True
        assert pump_until(root, lambda: not win._single_busy and len(win.single_tree.get_children()) == 2)
        assert win.add_title_var.get() == ""
        assert "已添加友链" in win.single_status_var.get()

        # Check backlinks updates status column
        assert win.check_single_backlinks() is True
        assert pump_until(root, lambda: not win._single_busy and "反链检测完成" in win.single_status_var.get())
        statuses = [win.single_tree.item(iid, "values")[5] for iid in win.single_tree.get_children()]
        assert "正常/有反链" in statuses
        assert "可达/无反链" in statuses

        # Delete first selected link
        first_iid = win.single_tree.get_children()[0]
        win.single_tree.selection_set(first_iid)
        assert win.delete_selected_links() is True
        assert pump_until(root, lambda: not win._single_busy and len(win.single_tree.get_children()) == 1)
        assert "已删 1" in win.single_status_var.get()

        win.on_close()
    finally:
        root.destroy()


def test_batch_panel_parse_add_remove_and_export(tmp_path):
    root = make_root()
    try:
        sites = [make_site("alpha"), make_site("beta")]
        svc = FakeFriendLinkService()
        ledger = FriendLinkLedger(tmp_path / "ledger.json")

        win = FriendLinkCenter(
            root,
            lambda: sites,
            config_path=tmp_path / "config.ini",
            service=svc,
            ledger=ledger,
            confirm_fn=lambda _t, _m: True,
            warn_fn=lambda _t, _m: None,
        )
        win.withdraw()
        panel = win.batch_panel

        panel.site_picker.select_all()
        panel.set_input_text("百度|https://baidu.example.com|1|合作\nhttps://partner.example.com\n")
        specs, errors = panel.preview_input()
        assert len(specs) == 2
        assert errors == []

        # Batch add
        assert panel.start_batch_add() is True
        assert pump_until(root, lambda: not panel.is_running and len(panel.result_tree.get_children()) == 4)
        assert "已添加 4" in panel.status_var.get()
        assert panel.progress_var.get() == 100.0

        # Export CSV
        csv_file = tmp_path / "batch_results.csv"
        exported = panel.export_csv(str(csv_file))
        assert exported == str(csv_file)
        content = csv_file.read_text(encoding="utf-8-sig")
        assert "baidu.example.com" in content
        assert "已添加" in content

        # Batch remove
        assert panel.start_batch_remove() is True
        assert pump_until(root, lambda: not panel.is_running and "已删除 4" in panel.status_var.get())
        assert len(panel.result_tree.get_children()) == 4

        win.on_close()
    finally:
        root.destroy()


def test_crosslink_panel_preview_fingerprint_guard_execute_and_rollback(tmp_path):
    root = make_root()
    try:
        sites = [make_site("alpha"), make_site("beta"), make_site("gamma")]
        svc = FakeFriendLinkService()
        ledger = FriendLinkLedger(tmp_path / "ledger.json")

        win = FriendLinkCenter(
            root,
            lambda: sites,
            config_path=tmp_path / "config.ini",
            service=svc,
            ledger=ledger,
            confirm_fn=lambda _t, _m: True,
            warn_fn=lambda _t, _m: None,
        )
        win.withdraw()
        panel = win.crosslink_panel

        # Execute button must be disabled before preview
        assert str(panel.execute_button.cget("state")) == "disabled"

        # Select 3 sites and preview Mesh (3*2 = 6 items)
        panel.site_picker.set_selected(["id-alpha", "id-beta", "id-gamma"])
        assert panel.start_preview() is True
        assert pump_until(root, lambda: not panel.is_running and panel.previewed_plan is not None)
        assert len(panel.plan_tree.get_children()) == 6
        assert str(panel.execute_button.cget("state")) == "normal"

        # Changing mode to Ring invalidates fingerprint -> disables execute button
        panel.mode_var.set(CROSSLINK_RING)
        root.update()
        assert str(panel.execute_button.cget("state")) == "disabled"

        # Changing back to Mesh restores matching fingerprint -> re-enables execute button
        panel.mode_var.set(CROSSLINK_MESH)
        root.update()
        assert str(panel.execute_button.cget("state")) == "normal"

        # Changing selection invalidates fingerprint
        panel.site_picker.set_selected(["id-alpha", "id-beta"])
        root.update()
        assert str(panel.execute_button.cget("state")) == "disabled"

        # Preview Hub-Spoke on 2 sites
        panel.mode_var.set(CROSSLINK_HUB)
        panel.bidirectional_var.set(True)
        panel.hub_var.set("alpha")
        root.update()
        assert panel.start_preview() is True
        assert pump_until(root, lambda: not panel.is_running and panel.previewed_plan is not None)
        assert len(panel.plan_tree.get_children()) == 2
        assert str(panel.execute_button.cget("state")) == "normal"

        # Execute plan -> writes ledger and updates history_tree
        plan_id = panel.previewed_plan.plan_id
        assert panel.start_execute() is True
        assert pump_until(root, lambda: not panel.is_running and plan_id in panel.history_tree.get_children())
        hist_vals = panel.history_tree.item(plan_id, "values")
        assert hist_vals[0] == plan_id
        assert int(hist_vals[4]) == 2  # created_count
        assert int(hist_vals[5]) == 0  # rolled_back_count

        # Rollback selected plan
        panel.history_tree.selection_set(plan_id)
        assert panel.start_rollback_selected() is True
        assert pump_until(
            root,
            lambda: not panel.is_running and int(panel.history_tree.item(plan_id, "values")[5]) == 2,
        )
        assert "回滚完成" in panel.status_var.get()

        win.on_close()
    finally:
        root.destroy()


def _install_html_parser_shim_if_needed(monkeypatch) -> None:
    """Fallback shim for UI smoke tests before core HTML parsing merges."""
    probe_specs, _ = parse_batch_link_input('<a href="https://probe.example.com">Probe</a>')
    if probe_specs:
        return

    anchor_re = re.compile(
        r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL
    )
    href_re = re.compile(
        r"""href\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s"'=<>`]+))""",
        re.IGNORECASE,
    )
    rel_re = re.compile(
        r"""rel\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
        re.IGNORECASE,
    )

    def _shim(text: str, *, default_nofollow: bool = False):
        specs: list[FriendLinkSpec] = []
        errors: list[str] = []
        seen: set[str] = set()
        remaining_lines: list[str] = []
        for line in (text or "").splitlines():
            matches = list(anchor_re.finditer(line))
            if not matches:
                remaining_lines.append(line)
                continue
            for m in matches:
                attrs, inner = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
                hm = href_re.search(attrs)
                if not hm:
                    continue
                href = (hm.group(1) or hm.group(2) or hm.group(3) or "").strip()
                if not href.lower().startswith(("http://", "https://")):
                    continue
                rm = rel_re.search(attrs)
                rel_val = ((rm.group(1) or rm.group(2) or rm.group(3) or "") if rm else "").lower()
                nf = ("nofollow" in rel_val) or bool(default_nofollow)
                norm = href if href.endswith("/") else f"{href}/"
                key = norm.rstrip("/").lower()
                if key not in seen:
                    seen.add(key)
                    specs.append(FriendLinkSpec(title=inner or norm, link=norm, nofollow=nf))

        if remaining_lines:
            plain_specs, plain_errs = parse_batch_link_input(
                "\n".join(remaining_lines), default_nofollow=default_nofollow
            )
            for sp in plain_specs:
                key = sp.link.rstrip("/").lower()
                if key not in seen:
                    seen.add(key)
                    specs.append(sp)
            errors.extend(plain_errs)
        return specs, errors

    monkeypatch.setattr(center_mod, "parse_batch_link_input", _shim)
    monkeypatch.setattr(batch_panel_mod, "parse_batch_link_input", _shim)


def test_single_site_smart_paste_html_and_backlink_display(tmp_path, monkeypatch):
    _install_html_parser_shim_if_needed(monkeypatch)
    root = make_root()
    try:
        sites = [make_site("alpha"), make_site("beta")]
        svc = FakeFriendLinkService()
        svc.seed(
            "alpha",
            [
                FriendLink(id=1, title="老友链", link="https://old.example.com/", remark="初始"),
            ],
        )
        ledger = FriendLinkLedger(tmp_path / "ledger.json")

        win = FriendLinkCenter(
            root,
            lambda: sites,
            config_path=tmp_path / "config.ini",
            service=svc,
            ledger=ledger,
            initial_site_name="alpha",
            confirm_fn=lambda _t, _m: True,
            warn_fn=lambda _t, _m: None,
        )
        win.withdraw()

        assert str(win.check_button.cget("text")) == "自动检测反链"
        assert "无需填写反链页" in str(win.check_hint_label.cget("text"))
        assert str(win.smart_add_button.cget("text")) == "智能识别并上链"

        assert pump_until(root, lambda: not win._single_busy and len(win.single_tree.get_children()) == 1)

        # Paste mixed HTML <a> tags + 标题|网址 + duplicate + self-link
        win.set_smart_paste_text(
            '<li><a href="https://partner1.example.com" rel="nofollow">伙伴一</a></li>\n'
            '<a href="https://partner2.example.com">伙伴二</a>\n'
            '伙伴三|https://partner3.example.com\n'
            'https://old.example.com/\n'
            'https://alpha.example.com/\n'
        )
        assert win.smart_add_links() is True
        assert pump_until(root, lambda: not win._single_busy and len(win.single_tree.get_children()) == 4)

        status_text = win.single_status_var.get()
        assert "成功上链 3 条" in status_text
        assert "跳过 2 条" in status_text
        assert win.get_smart_paste_text().strip() == ""

        rows_by_title = {
            win.single_tree.item(iid, "values")[1]: win.single_tree.item(iid, "values")
            for iid in win.single_tree.get_children()
        }
        assert rows_by_title["伙伴一"][3] == "是"
        assert rows_by_title["伙伴二"][3] == "否"
        assert rows_by_title["伙伴三"][2].rstrip("/") == "https://partner3.example.com"

        # Automatic backlink check with rich LinkHealth.message & visual tags
        svc.health_overrides = {
            "https://old.example.com": LinkHealth(
                site_name="alpha",
                link="https://old.example.com/",
                reachable=True,
                has_backlink=True,
                http_status=200,
                message="有反链(dofollow · 老友链)",
            ),
            "https://partner1.example.com": LinkHealth(
                site_name="alpha",
                link="https://partner1.example.com/",
                reachable=True,
                has_backlink=True,
                http_status=200,
                message="有反链(nofollow · 伙伴一)",
            ),
            "https://partner2.example.com": LinkHealth(
                site_name="alpha",
                link="https://partner2.example.com/",
                reachable=True,
                has_backlink=False,
                http_status=200,
                message="未发现我方主域名(alpha.example.com)反链",
            ),
            "https://partner3.example.com": LinkHealth(
                site_name="alpha",
                link="https://partner3.example.com/",
                reachable=False,
                has_backlink=False,
                http_status=404,
                message="页面返回 HTTP 404",
            ),
        }
        assert win.check_single_backlinks() is True
        assert pump_until(root, lambda: not win._single_busy and "反链检测完成" in win.single_status_var.get())

        tree_items = {
            win.single_tree.item(iid, "values")[1]: (
                win.single_tree.item(iid, "values")[5],
                win.single_tree.item(iid, "tags"),
            )
            for iid in win.single_tree.get_children()
        }
        assert tree_items["老友链"][0] == "有反链(dofollow · 老友链)"
        assert "backlink_ok" in tree_items["老友链"][1]
        assert tree_items["伙伴一"][0] == "有反链(nofollow · 伙伴一)"
        assert "backlink_nofollow" in tree_items["伙伴一"][1]
        assert tree_items["伙伴二"][0] == "未发现我方主域名(alpha.example.com)反链"
        assert "backlink_missing" in tree_items["伙伴二"][1]
        assert "HTTP 404" in tree_items["伙伴三"][0]
        assert "backlink_error" in tree_items["伙伴三"][1]
        assert "含 nofollow 1" in win.single_status_var.get()

        win.on_close()
    finally:
        root.destroy()


def test_batch_panel_html_paste_preview_add_and_remove(tmp_path, monkeypatch):
    _install_html_parser_shim_if_needed(monkeypatch)
    root = make_root()
    try:
        sites = [make_site("alpha"), make_site("beta")]
        svc = FakeFriendLinkService()
        ledger = FriendLinkLedger(tmp_path / "ledger.json")

        win = FriendLinkCenter(
            root,
            lambda: sites,
            config_path=tmp_path / "config.ini",
            service=svc,
            ledger=ledger,
            confirm_fn=lambda _t, _m: True,
            warn_fn=lambda _t, _m: None,
        )
        win.withdraw()
        panel = win.batch_panel

        assert str(panel.parse_button.cget("text")) == "识别预览"
        assert "HTML" in str(panel.input_hint_label.cget("text"))

        panel.site_picker.select_all()
        panel.set_input_text(
            '<li><a href="https://html1.example.com" rel="nofollow">HTML站一</a></li>\n'
            '<a href="https://html2.example.com">HTML站二</a>\n'
        )
        specs, errors = panel.preview_input()
        assert len(specs) == 2
        assert errors == []
        assert len(panel.result_tree.get_children()) == 2
        assert "识别完成：有效 2 条" in panel.status_var.get()

        # Batch add from HTML paste
        assert panel.start_batch_add() is True
        assert pump_until(root, lambda: not panel.is_running and len(panel.result_tree.get_children()) == 4)
        assert "已添加 4" in panel.status_var.get()

        # Batch remove directly from pasted HTML <a> tags
        assert panel.start_batch_remove() is True
        assert pump_until(root, lambda: not panel.is_running and "已删除 4" in panel.status_var.get())
        assert len(panel.result_tree.get_children()) == 4

        win.on_close()
    finally:
        root.destroy()
