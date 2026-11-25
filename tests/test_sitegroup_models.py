from __future__ import annotations

import dataclasses

import pytest

from models import sitegroup as sg
from models.sitegroup import (
    CrossLinkItem,
    CrossLinkPlan,
    FriendLink,
    FriendLinkSpec,
    JumpPlan,
    McpToolInfo,
    McpToolResult,
    UpgradeResult,
)


def item(src, dst, state=sg.LINK_PLANNED):
    return CrossLinkItem(src, src.upper(), dst, dst.upper(), FriendLinkSpec("t", f"https://{dst}.com/"), state)


def plan(items, **kw):
    values = dict(plan_id="p1", mode=sg.CROSSLINK_MESH, created_at="2026-01-01", hub_site_id="", bidirectional=True)
    values.update(kw)
    return CrossLinkPlan(items=tuple(items), **values)


def test_constants_values():
    assert (sg.LINK_PLANNED, sg.LINK_CREATED, sg.LINK_SKIPPED_EXISTS, sg.LINK_SKIPPED_SELF) == (
        "planned", "created", "skipped_exists", "skipped_self")
    assert (sg.LINK_DELETED, sg.LINK_NOT_FOUND, sg.LINK_FAILED, sg.LINK_CANCELLED) == (
        "deleted", "not_found", "failed", "cancelled")
    assert (sg.CROSSLINK_MESH, sg.CROSSLINK_HUB, sg.CROSSLINK_RING) == ("mesh", "hub", "ring")
    assert (sg.JUMP_TIER_TICKET, sg.JUMP_TIER_BRIDGE, sg.JUMP_TIER_DIRECT) == ("ticket", "bridge", "direct")
    assert sg.UPGRADE_SCOPE_DISABLED == "scope_disabled" and sg.UPGRADE_NO_TOOL == "no_tool"
    assert sg.UPGRADE_ALREADY_LATEST == "already_latest" and sg.UPGRADE_SKIPPED == "skipped"


def test_dataclasses_are_frozen_and_slotted():
    link = FriendLink(1, "t", "https://x.com/")
    with pytest.raises(dataclasses.FrozenInstanceError):
        link.title = "other"  # type: ignore[misc]
    assert not hasattr(link, "__dict__")
    assert link.status is None and link.nofollow is False


def test_raw_and_schema_dicts_excluded_from_equality_and_hash():
    a = FriendLink(1, "t", "l", raw={"a": 1})
    b = FriendLink(1, "t", "l", raw={"b": 2})
    assert a == b and hash(a) == hash(b)
    assert McpToolInfo("n", input_schema={"x": 1}) == McpToolInfo("n", annotations={"y": 2})
    assert hash(McpToolInfo("n", input_schema={"x": 1})) == hash(McpToolInfo("n"))
    assert McpToolResult(False, "t", raw={"a": 1}) == McpToolResult(False, "t")


def test_jump_plan_repr_hides_target_url():
    jp = JumpPlan("s", sg.JUMP_TIER_TICKET, "https://x.com/?ticket=SECRET", "https://x.com/system/")
    assert "SECRET" not in repr(jp)
    assert jp.target_url.endswith("SECRET") and jp.fallback_reasons == ()


def test_plan_counts_by_state():
    p = plan([item("a", "b"), item("b", "a", sg.LINK_CREATED), item("a", "c", sg.LINK_CREATED)])
    assert p.counts == {sg.LINK_PLANNED: 1, sg.LINK_CREATED: 2}
    assert plan([]).counts == {}


def test_plan_fingerprint_depends_on_selection_only():
    base = plan([item("a", "b"), item("b", "a"), item("a", "c"), item("c", "a")])
    reordered = plan([item("c", "a"), item("a", "c"), item("b", "c")], plan_id="other", created_at="later")
    assert base.fingerprint() == reordered.fingerprint()
    assert base.fingerprint() != plan(base.items, mode=sg.CROSSLINK_RING).fingerprint()
    assert base.fingerprint() != plan(base.items, bidirectional=False).fingerprint()
    assert base.fingerprint() != plan(base.items, hub_site_id="a").fingerprint()
    assert base.fingerprint() != plan([item("a", "b"), item("b", "a")]).fingerprint()
    assert plan([item("a", "b", sg.LINK_CREATED)]).fingerprint() == plan([item("a", "b")]).fingerprint()


def test_upgrade_result_defaults():
    r = UpgradeResult("s", "id", sg.UPGRADE_UPGRADED)
    assert (r.from_version, r.to_version, r.message, r.hint, r.duration_s) == ("", "", "", "", 0.0)
