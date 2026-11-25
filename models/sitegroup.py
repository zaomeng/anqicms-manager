"""Shared, immutable result contracts for the site-group (站群) features.

Every service (MCP client, friend links, admin jump, upgrade) and every UI
module builds against these dataclasses.  They are intentionally free of any
behaviour that touches the network, the filesystem or Tk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Friend-link operation states
# --------------------------------------------------------------------------- #
LINK_PLANNED = "planned"
LINK_CREATED = "created"
LINK_SKIPPED_EXISTS = "skipped_exists"
LINK_SKIPPED_SELF = "skipped_self"
LINK_DELETED = "deleted"
LINK_NOT_FOUND = "not_found"
LINK_FAILED = "failed"
LINK_CANCELLED = "cancelled"

# --------------------------------------------------------------------------- #
# Cross-link (互链) modes
# --------------------------------------------------------------------------- #
CROSSLINK_MESH = "mesh"
CROSSLINK_HUB = "hub"
CROSSLINK_RING = "ring"

# --------------------------------------------------------------------------- #
# Admin jump tiers
# --------------------------------------------------------------------------- #
JUMP_TIER_TICKET = "ticket"
JUMP_TIER_BRIDGE = "bridge"
JUMP_TIER_DIRECT = "direct"

# --------------------------------------------------------------------------- #
# Upgrade statuses
# --------------------------------------------------------------------------- #
UPGRADE_UPGRADED = "upgraded"
UPGRADE_ALREADY_LATEST = "already_latest"
UPGRADE_SCOPE_DISABLED = "scope_disabled"
UPGRADE_NO_TOOL = "no_tool"
UPGRADE_AUTH_FAILED = "auth_failed"
UPGRADE_UNREACHABLE = "unreachable"
UPGRADE_FAILED = "failed"
UPGRADE_TIMEOUT = "timeout"
UPGRADE_CANCELLED = "cancelled"
UPGRADE_SKIPPED = "skipped"


# --------------------------------------------------------------------------- #
# Friend links
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FriendLink:
    id: int
    title: str
    link: str
    nofollow: bool = False
    back_link: str = ""
    my_title: str = ""
    my_link: str = ""
    contact: str = ""
    remark: str = ""
    status: int | None = None
    status_text: str = ""
    raw: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True, slots=True)
class FriendLinkSpec:
    title: str
    link: str
    nofollow: bool = False
    back_link: str = ""
    my_title: str = ""
    my_link: str = ""
    contact: str = ""
    remark: str = ""


@dataclass(frozen=True, slots=True)
class LinkOpResult:
    site_name: str
    site_id: str
    action: str  # "add" | "remove" | "check"
    link: str
    title: str
    status: str
    message: str = ""  # already redacted, Chinese


@dataclass(frozen=True, slots=True)
class LinkHealth:
    site_name: str
    link: str
    reachable: bool
    has_backlink: bool | None
    http_status: int | None
    message: str


# --------------------------------------------------------------------------- #
# Cross links (互链)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CrossLinkItem:
    source_site_id: str
    source_name: str
    target_site_id: str
    target_name: str
    spec: FriendLinkSpec
    state: str = LINK_PLANNED
    message: str = ""


@dataclass(frozen=True, slots=True)
class CrossLinkPlan:
    plan_id: str
    mode: str
    created_at: str
    hub_site_id: str
    bidirectional: bool
    items: tuple[CrossLinkItem, ...]

    @property
    def counts(self) -> dict[str, int]:
        """Number of items per state."""
        result: dict[str, int] = {}
        for item in self.items:
            result[item.state] = result.get(item.state, 0) + 1
        return result

    def fingerprint(self) -> str:
        """Stable identity of the *selection* (mode, hub, direction, sites).

        Independent of plan id, timestamps and item states so that a preview
        stays valid as long as the user's selection does not change.
        """
        site_ids: set[str] = set()
        for item in self.items:
            site_ids.add(item.source_site_id)
            site_ids.add(item.target_site_id)
        return "|".join(
            (
                self.mode,
                self.hub_site_id,
                "1" if self.bidirectional else "0",
                ",".join(sorted(site_ids)),
            )
        )


@dataclass(frozen=True, slots=True)
class CrossLinkReport:
    plan: CrossLinkPlan
    results: tuple[LinkOpResult, ...]


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    plan_id: str
    source_site_id: str
    source_name: str
    target_site_id: str
    link: str
    title: str
    status: str
    created_at: str
    rolled_back_at: str = ""


@dataclass(frozen=True, slots=True)
class LedgerPlanSummary:
    plan_id: str
    mode: str
    created_at: str
    site_names: tuple[str, ...]
    created_count: int
    rolled_back_count: int


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class McpToolInfo:
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict, compare=False, hash=False)
    annotations: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True, slots=True)
class McpToolResult:
    is_error: bool
    text: str
    structured: object | None = None
    raw: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True, slots=True)
class McpProbeResult:
    site_name: str
    ok: bool
    server_name: str = ""
    server_version: str = ""
    protocol_version: str = ""
    tool_count: int = 0
    tools: tuple[McpToolInfo, ...] = ()
    friendlink_tools: tuple[str, ...] = ()
    upgrade_tools: tuple[str, ...] = ()
    jump_tools: tuple[str, ...] = ()
    message: str = ""
    hint: str = ""


# --------------------------------------------------------------------------- #
# Admin jump
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class JumpPlan:
    site_name: str
    tier: str
    target_url: str = field(repr=False)
    display_url: str
    fallback_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdminJumpResult:
    site_name: str
    tier: str
    success: bool
    display_url: str
    message: str
    fallback_reasons: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Upgrade
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SiteVersionInfo:
    site_name: str
    site_id: str
    reachable: bool
    current_version: str = ""
    latest_version: str = ""
    upgrade_available: bool | None = None
    source: str = "none"  # "mcp" | "rest" | "none"
    check_tool: str = ""
    upgrade_tool: str = ""
    message: str = ""
    hint: str = ""


@dataclass(frozen=True, slots=True)
class UpgradeProgress:
    site_name: str
    stage: str  # connecting|checking|upgrading|restarting|verifying|done
    message: str = ""


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    site_name: str
    site_id: str
    status: str
    from_version: str = ""
    to_version: str = ""
    message: str = ""
    hint: str = ""
    duration_s: float = 0.0
