(() => {
  "use strict";

  const VIEW_META = {
    dashboard: {
      title: "总览看板",
      subtitle: "集中监控站群健康、MCP 连通、版本与友链状态",
    },
    friendlinks: {
      title: "友链中心",
      subtitle: "单站上下链、全网批量上下链、SEO 站群互链、反链巡检与全站查链",
    },
    upgrade: {
      title: "统一升级",
      subtitle: "并发版本检测、金丝雀灰度升级与异常中文引导",
    },
    sites: {
      title: "站点管理与导入",
      subtitle: "单站编辑、老站批量赋值、纯站群新站批量导入（支持 fid=0）",
    },
    publish: {
      title: "文章发布台",
      subtitle: "定时发布队列状态、手动启停与实时脱敏日志流",
    },
    settings: {
      title: "系统设置 & MCP",
      subtitle: "热重载运行参数、一键接入 HTTP/Stdio MCP 与操作审计日志",
    },
  };

  const MODE_LABELS = {
    random_k: "SEO 随机 K 度",
    group_cross: "跨组互链",
    ring: "轮链",
    hub: "主从星型",
    mesh: "两两全互链",
  };

  const UPGRADE_STATUS_MAP = {
    upgraded: { label: "升级成功", cls: "badge-success" },
    already_latest: { label: "已是最新", cls: "badge-neutral" },
    scope_disabled: { label: "接口未开放", cls: "badge-warning" },
    no_tool: { label: "无升级工具", cls: "badge-warning" },
    auth_failed: { label: "鉴权失败", cls: "badge-danger" },
    unreachable: { label: "站点不可达", cls: "badge-danger" },
    failed: { label: "升级失败", cls: "badge-danger" },
    timeout: { label: "健康等待超时", cls: "badge-warning" },
    cancelled: { label: "已取消", cls: "badge-neutral" },
    skipped: { label: "已跳过", cls: "badge-neutral" },
  };

  const LINK_OP_STATUS_MAP = {
    created: { label: "已上链", cls: "badge-success" },
    deleted: { label: "已下链", cls: "badge-success" },
    skipped_exists: { label: "已存在跳过", cls: "badge-neutral" },
    skipped_self: { label: "本站跳过", cls: "badge-neutral" },
    planned: { label: "待创建", cls: "badge-info" },
    not_found: { label: "未找到", cls: "badge-warning" },
    failed: { label: "失败", cls: "badge-danger" },
    cancelled: { label: "已取消", cls: "badge-neutral" },
  };

  const AUDIT_STATUS_MAP = {
    ok: { label: "正常", cls: "badge-success" },
    nofollow: { label: "带 nofollow", cls: "badge-warning" },
    missing: { label: "无反链", cls: "badge-danger" },
    unreachable: { label: "不可达", cls: "badge-danger" },
    whitelisted: { label: "白名单", cls: "badge-info" },
  };

  const state = {
    activeView: "dashboard",
    friendlinkTab: "single",
    sitesTab: "list",
    health: null,
    kpis: {},
    dashboardSites: [],
    sites: [],
    groups: [],
    settings: null,
    mcpConfig: null,

    // Dashboard selection & filter
    dashSelected: new Set(),

    // Friendlink single
    singleLinks: [],
    singleSelected: new Set(),
    singleLoadedSite: "",

    // Friendlink crosslink
    crosslinkPreview: null,
    crosslinkSelectionKey: "",

    // Friendlink audit
    auditReport: null,
    auditFilter: "all",
    auditSelected: new Set(),

    // Friendlink search
    searchResults: null,
    searchSelected: new Set(),

    // Upgrade view rows keyed by site_name
    upgradeRows: new Map(),

    // Sites editor
    editingSiteName: null,

    // Publish polling
    publishPollTimer: null,

    // App self-update
    appUpdateInfo: null,

    // Jobs tracking
    jobs: new Map(),
    activeJobId: null,
    jobPollers: new Map(),
    dockMinimized: false,
  };

  const pickers = {};

  // ------------------------------------------------------------------ Utilities
  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function safeUrl(raw) {
    const text = String(raw || "").trim();
    if (/^https?:\/\//i.test(text)) {
      return text;
    }
    return "";
  }

  function renderUrlLink(raw, maxLabel = "") {
    const url = safeUrl(raw);
    const label = escapeHtml(maxLabel || raw || "-");
    if (!url) {
      return `<span class="url-cell" title="${label}">${label}</span>`;
    }
    return `<a class="url-cell" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(url)}">${label}</a>`;
  }

  function splitTokens(raw) {
    return String(raw || "")
      .split(/[\r\n,，]+/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function apiFetch(path, options = {}) {
    const opts = {
      method: options.method || "GET",
      headers: { ...(options.headers || {}) },
    };
    if (options.body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(options.body);
    }
    const resp = await fetch(path, opts);
    let payload = null;
    try {
      payload = await resp.json();
    } catch (_err) {
      throw new Error(`HTTP ${resp.status} 响应格式无效`);
    }
    if (!resp.ok || !payload || payload.ok !== true) {
      const err = new Error((payload && payload.error) || `请求失败 (HTTP ${resp.status})`);
      err.hint = (payload && payload.hint) || "";
      err.status = resp.status;
      throw err;
    }
    return payload.data;
  }

  function showToast(title, message = "", kind = "info", hint = "", durationMs = 4200) {
    const box = $("toast-container");
    const el = document.createElement("div");
    el.className = `toast toast-${kind}`;
    el.innerHTML = `
      <div class="toast-title">${escapeHtml(title)}</div>
      ${message ? `<div>${escapeHtml(message)}</div>` : ""}
      ${hint ? `<div class="toast-hint">${escapeHtml(hint)}</div>` : ""}
    `;
    box.appendChild(el);
    setTimeout(() => {
      el.remove();
    }, durationMs);
  }

  function handleError(title, err) {
    console.error(title, err);
    showToast(title, err && err.message ? err.message : String(err), "error", err && err.hint ? err.hint : "", 6000);
  }

  async function copyToClipboard(text, successMsg = "已复制到剪贴板") {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      showToast("复制成功", successMsg, "success");
    } catch (err) {
      handleError("复制失败", err);
    }
  }

  // Confirmation Modal
  let confirmResolver = null;
  function confirmAction({ title = "确认操作", message = "", detail = "", confirmText = "确认执行", danger = true }) {
    $("confirm-title").textContent = title;
    $("confirm-message").textContent = message;
    const detailEl = $("confirm-detail");
    if (detail) {
      detailEl.textContent = detail;
      detailEl.classList.remove("hidden");
    } else {
      detailEl.textContent = "";
      detailEl.classList.add("hidden");
    }
    const okBtn = $("confirm-ok");
    okBtn.textContent = confirmText;
    okBtn.className = danger ? "btn btn-danger" : "btn btn-primary";
    $("confirm-modal").classList.remove("hidden");

    return new Promise((resolve) => {
      confirmResolver = resolve;
    });
  }

  function closeConfirm(result) {
    $("confirm-modal").classList.add("hidden");
    if (confirmResolver) {
      const fn = confirmResolver;
      confirmResolver = null;
      fn(result);
    }
  }

  // ------------------------------------------------------------------ Theme & Navigation
  function initTheme() {
    const saved = localStorage.getItem("anqicms_theme") || "light";
    applyTheme(saved);
  }

  function applyTheme(theme) {
    const norm = theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = norm;
    $("theme-icon").textContent = norm === "dark" ? "☀️" : "🌙";
    localStorage.setItem("anqicms_theme", norm);
  }

  function switchView(viewName) {
    if (!VIEW_META[viewName]) return;
    state.activeView = viewName;

    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === viewName);
    });
    document.querySelectorAll(".view-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === `view-${viewName}`);
    });

    const meta = VIEW_META[viewName];
    $("page-title").textContent = meta.title;
    $("page-subtitle").textContent = meta.subtitle;
    $("sidebar").classList.remove("open");

    if (viewName === "dashboard") {
      loadDashboard();
    } else if (viewName === "friendlinks") {
      if (state.friendlinkTab === "crosslink") {
        loadCrosslinkHistory();
      }
    } else if (viewName === "upgrade") {
      syncUpgradeTableFromCache();
    } else if (viewName === "sites") {
      renderSitesTable();
    } else if (viewName === "publish") {
      loadPublishStatus();
      loadPublishLogs();
    } else if (viewName === "settings") {
      loadSettingsAndMcp();
      loadAuditLogs();
    }
  }

  function switchFriendlinkTab(tab) {
    state.friendlinkTab = tab;
    document.querySelectorAll("[data-fl-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.flTab === tab);
    });
    document.querySelectorAll("#view-friendlinks .subtab-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === `fl-panel-${tab}`);
    });
    if (tab === "crosslink") {
      loadCrosslinkHistory();
    }
  }

  function switchSitesTab(tab) {
    state.sitesTab = tab;
    document.querySelectorAll("[data-sites-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.sitesTab === tab);
    });
    document.querySelectorAll("#view-sites .subtab-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === `sites-panel-${tab}`);
    });
  }

  // ------------------------------------------------------------------ Reusable Site Picker
  class SitePicker {
    constructor(containerId, options = {}) {
      this.container = $(containerId);
      this.onChange = options.onChange || null;
      this.selected = new Set();
      this.group = "";
      this.query = "";
      this._mounted = false;
    }

    mount() {
      if (!this.container || this._mounted) return;
      this._mounted = true;
      this.container.innerHTML = `
        <div class="site-picker">
          <div class="picker-toolbar">
            <select class="input input-sm picker-group" style="max-width:140px;">
              <option value="">全部分组</option>
            </select>
            <input type="search" class="input input-sm picker-search" placeholder="筛选站点名/网址..." style="flex:1;min-width:130px;" />
            <button type="button" class="btn btn-ghost btn-xs picker-all">全选</button>
            <button type="button" class="btn btn-ghost btn-xs picker-none">全不选</button>
            <button type="button" class="btn btn-ghost btn-xs picker-invert">反选</button>
            <span class="badge badge-neutral picker-count">已选 0</span>
          </div>
          <div class="picker-list"></div>
        </div>
      `;
      this.groupEl = this.container.querySelector(".picker-group");
      this.searchEl = this.container.querySelector(".picker-search");
      this.listEl = this.container.querySelector(".picker-list");
      this.countEl = this.container.querySelector(".picker-count");

      this.groupEl.addEventListener("change", () => {
        this.group = this.groupEl.value;
        this.renderList();
      });
      this.searchEl.addEventListener("input", () => {
        this.query = this.searchEl.value.trim().toLowerCase();
        this.renderList();
      });
      this.container.querySelector(".picker-all").addEventListener("click", () => {
        this.getFilteredSites().forEach((s) => this.selected.add(s.name));
        this.renderList();
        this._emit();
      });
      this.container.querySelector(".picker-none").addEventListener("click", () => {
        this.selected.clear();
        this.renderList();
        this._emit();
      });
      this.container.querySelector(".picker-invert").addEventListener("click", () => {
        this.getFilteredSites().forEach((s) => {
          if (this.selected.has(s.name)) this.selected.delete(s.name);
          else this.selected.add(s.name);
        });
        this.renderList();
        this._emit();
      });
      this.listEl.addEventListener("change", (ev) => {
        const cb = ev.target;
        if (!cb || !cb.dataset || !cb.dataset.site) return;
        const name = cb.dataset.site;
        if (cb.checked) this.selected.add(name);
        else this.selected.delete(name);
        const itemEl = cb.closest(".picker-item");
        if (itemEl) itemEl.classList.toggle("selected", cb.checked);
        this.updateCount();
        this._emit();
      });
    }

    _emit() {
      if (typeof this.onChange === "function") {
        this.onChange(this.getSelected());
      }
    }

    getFilteredSites() {
      return state.sites.filter((s) => {
        if (this.group && (s.site_group || "").trim() !== this.group) return false;
        if (this.query) {
          const hay = `${s.name} ${s.site_title || ""} ${s.url || ""} ${s.site_group || ""}`.toLowerCase();
          if (!hay.includes(this.query)) return false;
        }
        return true;
      });
    }

    refreshOptions() {
      this.mount();
      // Remove stale selections
      const validNames = new Set(state.sites.map((s) => s.name));
      for (const name of Array.from(this.selected)) {
        if (!validNames.has(name)) this.selected.delete(name);
      }
      const prevGroup = this.group;
      this.groupEl.innerHTML = `<option value="">全部分组</option>${state.groups
        .map((g) => `<option value="${escapeHtml(g)}">${escapeHtml(g)}</option>`)
        .join("")}`;
      if (state.groups.includes(prevGroup)) {
        this.groupEl.value = prevGroup;
      } else {
        this.group = "";
      }
      this.renderList();
    }

    renderList() {
      this.mount();
      const filtered = this.getFilteredSites();
      if (!filtered.length) {
        this.listEl.innerHTML = `<div class="muted-text" style="padding:8px;">无匹配站点</div>`;
      } else {
        this.listEl.innerHTML = filtered
          .map((s) => {
            const checked = this.selected.has(s.name);
            const titleText = s.site_title ? `${s.name} · ${s.site_title}` : s.name;
            return `
              <label class="picker-item ${checked ? "selected" : ""}" title="${escapeHtml(titleText + (s.url ? " (" + s.url + ")" : ""))}">
                <input type="checkbox" data-site="${escapeHtml(s.name)}" ${checked ? "checked" : ""} />
                <span class="picker-item-text">${escapeHtml(s.name)}</span>
                ${s.site_group ? `<span class="picker-item-group">${escapeHtml(s.site_group)}</span>` : ""}
              </label>
            `;
          })
          .join("");
      }
      this.updateCount();
    }

    updateCount() {
      if (this.countEl) {
        this.countEl.textContent = `已选 ${this.selected.size} / ${state.sites.length}`;
      }
    }

    getSelected() {
      return Array.from(this.selected);
    }

    setSelected(names) {
      this.selected = new Set(names || []);
      this.renderList();
      this._emit();
    }
  }

  function initPickers() {
    pickers.flBatch = new SitePicker("fl-batch-picker");
    pickers.flXl = new SitePicker("fl-xl-picker", {
      onChange: () => {
        updateCrosslinkHubOptions();
        invalidateCrosslinkPreview();
      },
    });
    pickers.flAudit = new SitePicker("fl-audit-picker");
    pickers.flSearch = new SitePicker("fl-search-picker");
    pickers.upgrade = new SitePicker("upg-picker");
    pickers.sitesAssign = new SitePicker("sites-assign-picker");
    pickers.publish = new SitePicker("pub-picker");
    Object.values(pickers).forEach((p) => p.mount());
  }

  function refreshAllPickersAndSelects() {
    Object.values(pickers).forEach((p) => p.refreshOptions());
    populateGroupSelects();
    populateSingleSiteSelect();
    updateCrosslinkHubOptions();
  }

  // ------------------------------------------------------------------ Job Runner & Live Dock
  function renderJobDock() {
    const allJobs = Array.from(state.jobs.values()).sort((a, b) =>
      String(b.created_at || "").localeCompare(String(a.created_at || ""))
    );
    const activeCount = allJobs.filter((j) => j.status === "pending" || j.status === "running").length;

    // Update topbar & sidebar indicators
    const topDot = $("topbar-job-dot");
    const sideDot = $("sidebar-job-dot");
    if (activeCount > 0) {
      topDot.className = "status-dot running";
      sideDot.className = "status-dot running";
      $("topbar-job-label").textContent = `${activeCount} 个任务运行中`;
      $("sidebar-job-text").textContent = `后台任务：${activeCount} 个进行中`;
    } else {
      topDot.className = "status-dot";
      sideDot.className = "status-dot";
      $("topbar-job-label").textContent = allJobs.length ? `最近 ${allJobs.length} 个任务` : "无运行中任务";
      $("sidebar-job-text").textContent = "后台任务：空闲";
    }

    const dock = $("job-dock");
    const job = state.activeJobId ? state.jobs.get(state.activeJobId) : allJobs[0];
    if (!job) {
      dock.classList.add("hidden");
      return;
    }

    dock.classList.toggle("minimized", state.dockMinimized);
    $("btn-job-toggle").textContent = state.dockMinimized ? "展开" : "收起";

    const statusMeta = {
      pending: { label: "排队中", cls: "badge-neutral", dot: "status-dot running" },
      running: { label: "运行中", cls: "badge-info", dot: "status-dot running" },
      completed: { label: "已完成", cls: "badge-success", dot: "status-dot success" },
      failed: { label: "失败", cls: "badge-danger", dot: "status-dot danger" },
      cancelled: { label: "已取消", cls: "badge-warning", dot: "status-dot" },
    }[job.status] || { label: job.status, cls: "badge-neutral", dot: "status-dot" };

    $("job-dock-title").textContent = job.title || "后台任务";
    $("job-dock-status").textContent = statusMeta.label;
    $("job-dock-status").className = `badge ${statusMeta.cls}`;
    $("job-dock-dot").className = statusMeta.dot;

    const pct = typeof job.percent === "number" ? job.percent : 0;
    $("job-dock-fill").style.width = `${pct}%`;
    $("job-dock-pct").textContent = job.total > 0 ? `${job.done}/${job.total} (${pct}%)` : `${pct}%`;
    $("job-dock-msg").textContent = job.error ? `错误：${job.error}` : job.message || "处理中...";

    const canCancel = job.status === "pending" || job.status === "running";
    $("btn-job-cancel").classList.toggle("hidden", !canCancel);
    $("btn-job-cancel").disabled = Boolean(job.cancel_requested);

    const logs = Array.isArray(job.logs) ? job.logs.slice(-30) : [];
    const logsEl = $("job-dock-logs");
    if (!logs.length) {
      logsEl.innerHTML = `<div class="muted-text">等待进度明细...</div>`;
    } else {
      logsEl.innerHTML = logs
        .map(
          (entry) =>
            `<div class="job-log-line">[${escapeHtml((entry.ts || "").slice(11))}] ${escapeHtml(entry.message || "")}</div>`
        )
        .join("");
      logsEl.scrollTop = logsEl.scrollHeight;
    }

    const historyEl = $("job-dock-history");
    historyEl.innerHTML = allJobs
      .slice(0, 5)
      .map(
        (j) => `
          <button type="button" class="filter-pill ${j.job_id === job.job_id ? "active" : ""}" data-switch-job="${escapeHtml(j.job_id)}">
            ${escapeHtml(j.title)} (${escapeHtml(j.status)})
          </button>
        `
      )
      .join("");
  }

  function trackJob(jobId, initialJob = null, onFinish = null) {
    if (initialJob) {
      state.jobs.set(jobId, initialJob);
    }
    state.activeJobId = jobId;
    state.dockMinimized = false;
    $("job-dock").classList.remove("hidden");
    renderJobDock();

    if (state.jobPollers.has(jobId)) return;

    let polling = false;
    const timer = setInterval(async () => {
      if (polling) return;
      polling = true;
      try {
        const job = await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`);
        state.jobs.set(jobId, job);
        renderJobDock();

        if (job.status === "completed" || job.status === "failed" || job.status === "cancelled") {
          clearInterval(timer);
          state.jobPollers.delete(jobId);

          if (job.status === "completed") {
            showToast(`${job.title} 完成`, job.message || "任务已全部执行完毕", "success");
          } else if (job.status === "cancelled") {
            showToast(`${job.title} 已取消`, job.message || "", "warning");
          } else {
            showToast(`${job.title} 失败`, job.error || job.message || "", "error");
          }

          if (typeof onFinish === "function") {
            try {
              await onFinish(job);
            } catch (cbErr) {
              console.error("Job callback error:", cbErr);
            }
          }
          loadDashboard({ silent: true });
        }
      } catch (err) {
        clearInterval(timer);
        state.jobPollers.delete(jobId);
        handleError("轮询任务状态失败", err);
      } finally {
        polling = false;
      }
    }, 500);

    state.jobPollers.set(jobId, timer);
  }

  async function syncExistingJobs() {
    try {
      const data = await apiFetch("/api/jobs?limit=12");
      const list = data.jobs || [];
      for (const job of list) {
        state.jobs.set(job.job_id, job);
        if (job.status === "pending" || job.status === "running") {
          trackJob(job.job_id, job);
        }
      }
      renderJobDock();
    } catch (_err) {
      // Ignore startup job fetch error
    }
  }

  // ------------------------------------------------------------------ Core Data Loading
  function updateVersionPills(version, updateAvailable = false, latestVersion = "") {
    const cleanVer = String(version || "2.0.0").replace(/^v/i, "");
    const verLabel = `v${cleanVer}`;
    const currentEl = $("app-update-current-ver");
    if (currentEl) currentEl.textContent = verLabel;

    const topText = $("topbar-version-text");
    const sidePill = $("sidebar-version-pill");
    if (updateAvailable && latestVersion) {
      const cleanLatest = String(latestVersion).replace(/^v/i, "");
      if (topText) topText.textContent = `${verLabel} → v${cleanLatest}`;
      if (sidePill) {
        sidePill.textContent = `${verLabel} · 有新版`;
        sidePill.className = "badge badge-info";
      }
    } else {
      if (topText) topText.textContent = verLabel;
      if (sidePill) {
        sidePill.textContent = verLabel;
        sidePill.className = "badge badge-neutral";
      }
    }
  }

  async function loadHealth() {
    try {
      const data = await apiFetch("/api/health");
      state.health = data;
      const ver = data.version || "2.0.0";
      $("app-version-label").textContent = `v${ver} · 端口 ${data.port}`;
      updateVersionPills(
        ver,
        Boolean(state.appUpdateInfo && state.appUpdateInfo.update_available),
        state.appUpdateInfo ? state.appUpdateInfo.latest_version : ""
      );
      if (data.github_repo) {
        const repoLink = $("app-update-repo-link");
        if (repoLink) {
          repoLink.textContent = data.github_repo;
          repoLink.href = `https://github.com/${data.github_repo}/releases`;
        }
      }
    } catch (_err) {
      // Non-fatal
    }
  }

  async function loadDashboard({ silent = false } = {}) {
    try {
      const data = await apiFetch("/api/dashboard");
      state.kpis = data.kpis || {};
      state.dashboardSites = data.sites || [];
      state.sites = data.sites || [];
      state.groups = data.groups || [];

      renderKpis();
      renderDashboardTable();
      refreshAllPickersAndSelects();
      if (state.activeView === "sites") {
        renderSitesTable();
      }
      if (state.activeView === "upgrade") {
        syncUpgradeTableFromCache();
      }
      $("sidebar-site-meta").textContent = `共管理 ${state.sites.length} 个站点 · ${state.groups.length} 个分组`;
    } catch (err) {
      if (!silent) handleError("加载总览数据失败", err);
    }
  }

  async function refreshAllData() {
    await Promise.all([loadHealth(), loadDashboard(), loadSettingsQuiet()]);
    if (state.activeView === "publish") {
      loadPublishStatus();
      loadPublishLogs();
    } else if (state.activeView === "settings") {
      loadSettingsAndMcp();
      loadAuditLogs();
    } else if (state.activeView === "friendlinks" && state.friendlinkTab === "crosslink") {
      loadCrosslinkHistory();
    }
    showToast("数据已刷新", "", "success", "", 2200);
  }

  async function loadSettingsQuiet() {
    try {
      state.settings = await apiFetch("/api/settings");
      if (state.settings && state.settings.upgrade && !$("upg-concurrency").dataset.touched) {
        $("upg-concurrency").value = Math.min(5, Math.max(1, state.settings.upgrade.max_concurrency || 3));
      }
      if (state.settings && state.settings.friendlink && !$("fl-xl-remark").value) {
        $("fl-xl-remark").value = state.settings.friendlink.default_remark || "站群互链";
      }
    } catch (_err) {
      // Ignore
    }
  }

  function populateGroupSelects() {
    const curDash = $("dash-group-filter").value;
    const curSites = $("sites-group-filter").value;
    const opts = `<option value="">全部分组</option>${state.groups
      .map((g) => `<option value="${escapeHtml(g)}">${escapeHtml(g)}</option>`)
      .join("")}`;
    $("dash-group-filter").innerHTML = opts;
    $("sites-group-filter").innerHTML = opts;
    if (state.groups.includes(curDash)) $("dash-group-filter").value = curDash;
    if (state.groups.includes(curSites)) $("sites-group-filter").value = curSites;

    const ga = $("fl-xl-group-a").value;
    const gb = $("fl-xl-group-b").value;
    const gOptsA = `<option value="">自动跨分组</option>${state.groups
      .map((g) => `<option value="${escapeHtml(g)}">${escapeHtml(g)}</option>`)
      .join("")}`;
    const gOptsB = `<option value="">自动匹配其他分组</option>${state.groups
      .map((g) => `<option value="${escapeHtml(g)}">${escapeHtml(g)}</option>`)
      .join("")}`;
    $("fl-xl-group-a").innerHTML = gOptsA;
    $("fl-xl-group-b").innerHTML = gOptsB;
    if (state.groups.includes(ga)) $("fl-xl-group-a").value = ga;
    if (state.groups.includes(gb)) $("fl-xl-group-b").value = gb;
  }

  // ------------------------------------------------------------------ 1. Dashboard
  function renderKpis() {
    const k = state.kpis || {};
    $("kpi-total-sites").textContent = k.total_sites ?? 0;
    $("kpi-enabled-sites").textContent = `已启用发布 ${k.enabled_sites ?? 0} 站`;
    $("kpi-mcp-ok").textContent = k.mcp_ok_sites ?? 0;
    $("kpi-mcp-fail").textContent = `异常 ${k.mcp_failed_sites ?? 0} 站`;
    $("kpi-updates").textContent = k.updates_available ?? 0;
    $("kpi-broken-links").textContent = k.broken_links ?? 0;
    $("kpi-total-links").textContent = `已缓存友链 ${k.total_links ?? 0} 条`;
    $("kpi-publish-pair").textContent = `${k.published_today ?? 0} / ${k.pending_articles ?? 0}`;
    $("kpi-publish-state").textContent = k.publish_running ? "自动发布队列运行中" : "发布队列空闲";

    const updBadge = $("badge-updates");
    updBadge.textContent = String(k.updates_available || 0);
    updBadge.classList.toggle("hidden", !(k.updates_available > 0));

    const brokenBadge = $("badge-broken");
    brokenBadge.textContent = String(k.broken_links || 0);
    brokenBadge.classList.toggle("hidden", !(k.broken_links > 0));

    $("badge-publish").classList.toggle("hidden", !k.publish_running);
  }

  function getFilteredDashboardSites() {
    const grp = $("dash-group-filter").value;
    const st = $("dash-status-filter").value;
    const q = $("dash-search").value.trim().toLowerCase();

    return state.dashboardSites.filter((s) => {
      if (grp && (s.site_group || "").trim() !== grp) return false;
      if (st === "updatable" && s.update_available !== true) return false;
      if (st === "mcp_ok" && s.mcp_ok !== true) return false;
      if (st === "mcp_fail" && s.mcp_ok !== false) return false;
      if (st === "broken" && !((s.broken_link_count || 0) > 0)) return false;
      if (st === "enabled" && !s.enabled) return false;
      if (q) {
        const hay = `${s.name} ${s.site_title || ""} ${s.url || ""} ${s.site_group || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  function renderDashboardTable() {
    const tbody = $("dash-table-body");
    const rows = getFilteredDashboardSites();
    const validNames = new Set(state.dashboardSites.map((s) => s.name));
    for (const name of Array.from(state.dashSelected)) {
      if (!validNames.has(name)) state.dashSelected.delete(name);
    }

    $("dash-selected-count").textContent = `已选 ${state.dashSelected.size} 站`;
    $("dash-master-check").checked = rows.length > 0 && rows.every((r) => state.dashSelected.has(r.name));

    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty-cell">没有符合条件的站点</td></tr>`;
      return;
    }

    tbody.innerHTML = rows
      .map((s) => {
        const checked = state.dashSelected.has(s.name);
        const verHtml = s.current_version
          ? s.update_available
            ? `<span class="mono">${escapeHtml(s.current_version)}</span> <span class="badge badge-info">可升 ${escapeHtml(s.latest_version || "新版")}</span>`
            : `<span class="mono">${escapeHtml(s.current_version)}</span> <span class="badge badge-neutral">最新</span>`
          : `<span class="muted-text">未检测</span>`;

        const mcpHtml =
          s.mcp_ok === true
            ? `<span class="badge badge-success" title="工具数：${(s.mcp_tools || []).length}">已连通 (${(s.mcp_tools || []).length})</span>`
            : s.mcp_ok === false
            ? `<span class="badge badge-danger">连接失败</span>`
            : `<span class="badge badge-neutral">未探测</span>`;

        const linkCount = s.link_count === null || s.link_count === undefined ? "-" : s.link_count;
        const brokenCount = s.broken_link_count || 0;
        const linkHtml =
          brokenCount > 0
            ? `<span class="mono">${linkCount}</span> / <span class="badge badge-danger">掉 ${brokenCount}</span>`
            : `<span class="mono">${linkCount}</span> / <span class="muted-text">0</span>`;

        const pubHtml = s.enabled
          ? `<span class="mono">${s.published_today ?? 0}/${s.daily_publish_limit ?? 0}</span> <span class="muted-text">(待 ${s.pending_count ?? 0})</span>`
          : `<span class="muted-text">未启用 (待 ${s.pending_count ?? 0})</span>`;

        return `
          <tr class="${checked ? "row-selected" : ""}">
            <td class="col-check">
              <input type="checkbox" class="dash-row-check" data-site="${escapeHtml(s.name)}" ${checked ? "checked" : ""} />
            </td>
            <td>
              <div class="cell-main">${escapeHtml(s.name)}</div>
              <div class="cell-sub" title="${escapeHtml(s.site_title || s.display_name || "")}">${escapeHtml(s.site_title || "-")}</div>
            </td>
            <td>${s.site_group ? `<span class="badge badge-neutral">${escapeHtml(s.site_group)}</span>` : `<span class="muted-text">-</span>`}</td>
            <td>${renderUrlLink(s.url)}</td>
            <td>${verHtml}</td>
            <td>${mcpHtml}</td>
            <td>${linkHtml}</td>
            <td>${pubHtml}</td>
            <td class="col-actions">
              <div class="inline-controls">
                <button type="button" class="btn btn-secondary btn-xs" data-dash-jump="${escapeHtml(s.name)}">跳后台</button>
                <button type="button" class="btn btn-ghost btn-xs" data-dash-fl="${escapeHtml(s.name)}">友链</button>
                <button type="button" class="btn btn-ghost btn-xs" data-dash-probe="${escapeHtml(s.name)}">测MCP</button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  async function jumpAdminSingle(siteName, { openWindow = true } = {}) {
    try {
      const data = await apiFetch("/api/admin/jump", {
        method: "POST",
        body: { site_name: siteName, open_browser: false },
      });
      if (openWindow && data.url) {
        window.open(data.url, "_blank", "noopener");
      }
      const tierText = { ticket: "Ticket免密", bridge: "本地反代静默登录", direct: "直达后台" }[data.tier] || data.tier;
      showToast(
        `${data.site_name} 跳后台就绪`,
        `模式：${tierText}`,
        "success",
        data.reason ? `说明：${data.reason}` : ""
      );
      return data;
    } catch (err) {
      handleError(`${siteName} 跳后台失败`, err);
      return null;
    }
  }

  async function handleDashDiscover() {
    const selected = Array.from(state.dashSelected);
    try {
      const data = await apiFetch("/api/sites/discover", {
        method: "POST",
        body: {
          site_names: selected.length ? selected : undefined,
          probe_mcp: true,
          backfill_titles: true,
        },
      });
      trackJob(data.job_id, data.job);
    } catch (err) {
      handleError("启动站点探测失败", err);
    }
  }

  async function handleDashProbeMcp(explicitNames = null) {
    const selected = explicitNames || Array.from(state.dashSelected);
    try {
      const data = await apiFetch("/api/sites/probe-mcp", {
        method: "POST",
        body: { site_names: selected.length ? selected : undefined },
      });
      trackJob(data.job_id, data.job);
    } catch (err) {
      handleError("启动 MCP 探测失败", err);
    }
  }

  async function handleDashBatchJump() {
    const selected = Array.from(state.dashSelected);
    if (!selected.length) {
      showToast("请先勾选站点", "请在表格中勾选需要免密跳后台的站点", "warning");
      return;
    }
    const aj = (state.settings && state.settings.admin_jump) || {};
    const threshold = aj.batch_confirm_threshold || 5;
    const batchMax = aj.batch_max || 30;
    const staggerMs = aj.batch_stagger_ms ?? 400;

    if (selected.length > batchMax) {
      showToast("超出单批上限", `当前设置了单批最多打开 ${batchMax} 个后台，请缩减选中站点数量`, "warning");
      return;
    }
    if (selected.length > threshold) {
      const ok = await confirmAction({
        title: "批量免密跳后台确认",
        message: `即将批量打开 ${selected.length} 个站点后台标签页（浏览器可能会拦截弹窗，请允许本站点弹出窗口）。是否继续？`,
        detail: selected.join("、"),
        confirmText: `打开 ${selected.length} 个后台`,
        danger: false,
      });
      if (!ok) return;
    }

    let okCount = 0;
    for (let i = 0; i < selected.length; i++) {
      const res = await jumpAdminSingle(selected[i], { openWindow: true });
      if (res) okCount++;
      if (i + 1 < selected.length && staggerMs > 0) {
        await sleep(staggerMs);
      }
    }
    showToast("批量跳后台完成", `成功打开 ${okCount} / ${selected.length} 个站点后台`, okCount > 0 ? "success" : "warning");
  }

  // ------------------------------------------------------------------ 2. FriendLinks
  function populateSingleSiteSelect() {
    const sel = $("fl-single-site-select");
    const q = $("fl-single-site-search").value.trim().toLowerCase();
    const prev = sel.value || state.singleLoadedSite;
    const list = state.sites.filter((s) => {
      if (!q) return true;
      return `${s.name} ${s.site_title || ""} ${s.url || ""}`.toLowerCase().includes(q);
    });
    sel.innerHTML = list.length
      ? list
          .map(
            (s) =>
              `<option value="${escapeHtml(s.name)}">${escapeHtml(s.name)}${s.site_title ? ` (${escapeHtml(s.site_title)})` : ""}</option>`
          )
          .join("")
      : `<option value="">无匹配站点</option>`;
    if (prev && list.some((s) => s.name === prev)) {
      sel.value = prev;
    }
  }

  async function loadSingleSiteFriendlinks(siteName = null) {
    const target = siteName || $("fl-single-site-select").value;
    if (!target) {
      showToast("请选择站点", "", "warning");
      return;
    }
    $("fl-single-site-select").value = target;
    state.singleLoadedSite = target;
    $("fl-single-table-body").innerHTML = `<tr><td colspan="7" class="empty-cell">正在读取 ${escapeHtml(target)} 的实时友链...</td></tr>`;
    try {
      const data = await apiFetch(`/api/friendlinks/site/${encodeURIComponent(target)}`);
      state.singleLinks = data.links || [];
      state.singleSelected.clear();
      $("fl-single-count").textContent = `共 ${data.link_count ?? state.singleLinks.length} 条`;
      renderSingleLinksTable();
    } catch (err) {
      $("fl-single-table-body").innerHTML = `<tr><td colspan="7" class="empty-cell">${escapeHtml(err.message)}</td></tr>`;
      handleError(`读取 ${target} 友链失败`, err);
    }
  }

  function renderSingleLinksTable() {
    const tbody = $("fl-single-table-body");
    const links = state.singleLinks || [];
    $("fl-single-master-check").checked =
      links.length > 0 && links.every((l) => state.singleSelected.has(l.link));

    if (!links.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-cell">该站点暂无友情链接</td></tr>`;
      return;
    }
    tbody.innerHTML = links
      .map((item) => {
        const checked = state.singleSelected.has(item.link);
        return `
          <tr class="${checked ? "row-selected" : ""}">
            <td class="col-check">
              <input type="checkbox" class="fl-single-check" data-link="${escapeHtml(item.link)}" ${checked ? "checked" : ""} />
            </td>
            <td class="mono">${escapeHtml(item.id)}</td>
            <td>${escapeHtml(item.title || "-")}</td>
            <td>${renderUrlLink(item.link)}</td>
            <td>${item.nofollow ? `<span class="badge badge-warning">nofollow</span>` : `<span class="badge badge-success">dofollow</span>`}</td>
            <td>${escapeHtml(item.remark || "-")}</td>
            <td>${escapeHtml(item.status_text || "-")}</td>
          </tr>
        `;
      })
      .join("");
  }

  async function parseFriendlinksPreview(text, defaultNofollow) {
    return apiFetch("/api/friendlinks/parse", {
      method: "POST",
      body: { text, default_nofollow: Boolean(defaultNofollow) },
    });
  }

  async function handleSingleParse() {
    const text = $("fl-single-paste").value;
    if (!text.trim()) {
      showToast("请先粘贴 HTML 或友链文本", "", "warning");
      return;
    }
    try {
      const data = await parseFriendlinksPreview(text, $("fl-single-nofollow").checked);
      $("fl-single-parse-status").textContent = `识别 ${data.count} 条${data.errors.length ? `，${data.errors.length} 行异常` : ""}`;
      const strip = $("fl-single-parse-preview");
      strip.classList.remove("hidden");
      strip.innerHTML =
        data.specs
          .map(
            (sp) =>
              `<span class="badge ${sp.nofollow ? "badge-warning" : "badge-info"}" title="${escapeHtml(sp.link)}">${escapeHtml(sp.title)} → ${escapeHtml(sp.link)}${sp.nofollow ? " [nf]" : ""}</span>`
          )
          .join("") +
        data.errors.map((e) => `<span class="badge badge-danger">${escapeHtml(e)}</span>`).join("");
    } catch (err) {
      handleError("解析友链失败", err);
    }
  }

  async function handleSingleAdd() {
    const site = $("fl-single-site-select").value;
    const text = $("fl-single-paste").value;
    if (!site) return showToast("请先选择站点", "", "warning");
    if (!text.trim()) return showToast("请输入待添加的友链", "", "warning");
    try {
      const data = await apiFetch("/api/friendlinks/batch-add", {
        method: "POST",
        body: {
          site_names: [site],
          text_or_specs: text,
          skip_existing: true,
          nofollow: $("fl-single-nofollow").checked,
          remark: $("fl-single-remark").value.trim(),
        },
      });
      trackJob(data.job_id, data.job, async () => {
        await loadSingleSiteFriendlinks(site);
      });
    } catch (err) {
      handleError("单站上链失败", err);
    }
  }

  async function handleSingleRemoveSelected() {
    const site = $("fl-single-site-select").value;
    const urls = Array.from(state.singleSelected);
    if (!site || !urls.length) {
      showToast("请先勾选要删除的友链", "", "warning");
      return;
    }
    const ok = await confirmAction({
      title: "确认删除友链",
      message: `确定要从站点「${site}」删除选中的 ${urls.length} 条友链吗？`,
      detail: urls.join("\n"),
      confirmText: "确认删除",
    });
    if (!ok) return;
    try {
      const data = await apiFetch("/api/friendlinks/batch-remove", {
        method: "POST",
        body: { site_names: [site], text_or_urls: urls },
      });
      trackJob(data.job_id, data.job, async () => {
        await loadSingleSiteFriendlinks(site);
      });
    } catch (err) {
      handleError("删除友链失败", err);
    }
  }

  // 2.2 Batch Add / Remove
  async function handleBatchParse() {
    const text = $("fl-batch-input").value;
    if (!text.trim()) return showToast("请输入 HTML 或友链文本", "", "warning");
    try {
      const data = await parseFriendlinksPreview(text, $("fl-batch-nofollow").checked);
      const box = $("fl-batch-preview-box");
      box.classList.remove("hidden");
      const errHtml = data.errors.length
        ? `<div class="alert-box" style="margin-bottom:8px;">解析异常（${data.errors.length} 行）：${data.errors.map(escapeHtml).join("；")}</div>`
        : "";
      const rowsHtml = data.specs.length
        ? `<div class="table-wrap table-short"><table class="data-table"><thead><tr><th>标题</th><th>链接</th><th>nofollow</th><th>备注</th></tr></thead><tbody>${data.specs
            .map(
              (sp) =>
                `<tr><td>${escapeHtml(sp.title)}</td><td>${renderUrlLink(sp.link)}</td><td>${sp.nofollow ? "是" : "否"}</td><td>${escapeHtml(sp.remark || "-")}</td></tr>`
            )
            .join("")}</tbody></table></div>`
        : `<div class="muted-text">未识别到有效友链</div>`;
      box.innerHTML = `<div class="subcard-head"><strong>识别预览：共 ${data.count} 条有效友链</strong></div>${errHtml}${rowsHtml}`;
    } catch (err) {
      handleError("识别友链失败", err);
    }
  }

  function renderBatchResultTable(results, counts = {}) {
    $("fl-batch-result-counts").innerHTML = Object.entries(counts)
      .map(([k, v]) => {
        const meta = LINK_OP_STATUS_MAP[k] || { label: k, cls: "badge-neutral" };
        return `<span class="badge ${meta.cls}">${escapeHtml(meta.label)}: ${v}</span>`;
      })
      .join("");

    const tbody = $("fl-batch-result-body");
    if (!results || !results.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">无返回结果</td></tr>`;
      return;
    }
    tbody.innerHTML = results
      .map((r) => {
        const meta = LINK_OP_STATUS_MAP[r.status] || { label: r.status, cls: "badge-neutral" };
        return `
          <tr>
            <td><strong>${escapeHtml(r.site_name)}</strong></td>
            <td>${r.action === "add" ? "上链" : r.action === "remove" ? "下链" : escapeHtml(r.action)}</td>
            <td>${escapeHtml(r.title || "-")}</td>
            <td>${renderUrlLink(r.link)}</td>
            <td><span class="badge ${meta.cls}">${escapeHtml(meta.label)}</span></td>
            <td>${escapeHtml(r.message || "-")}</td>
          </tr>
        `;
      })
      .join("");
  }

  async function handleBatchAdd() {
    const sites = pickers.flBatch.getSelected();
    const text = $("fl-batch-input").value;
    if (!sites.length) return showToast("请先选择目标站点", "", "warning");
    if (!text.trim()) return showToast("请输入待添加友链", "", "warning");
    try {
      const data = await apiFetch("/api/friendlinks/batch-add", {
        method: "POST",
        body: {
          site_names: sites,
          text_or_specs: text,
          skip_existing: $("fl-batch-skip-existing").checked,
          nofollow: $("fl-batch-nofollow").checked,
          remark: $("fl-batch-remark").value.trim(),
        },
      });
      trackJob(data.job_id, data.job, (job) => {
        const res = job.result || {};
        renderBatchResultTable(res.results || [], res.counts || {});
      });
    } catch (err) {
      handleError("批量上链失败", err);
    }
  }

  async function handleBatchRemove() {
    const sites = pickers.flBatch.getSelected();
    const text = $("fl-batch-input").value;
    if (!sites.length) return showToast("请先选择目标站点", "", "warning");
    if (!text.trim()) return showToast("请输入待删除友链网址或 HTML", "", "warning");
    const ok = await confirmAction({
      title: "批量下链确认",
      message: `确定从已选中的 ${sites.length} 个站点批量删除输入内容中的所有友链吗？`,
      confirmText: "确认批量下链",
    });
    if (!ok) return;
    try {
      const data = await apiFetch("/api/friendlinks/batch-remove", {
        method: "POST",
        body: { site_names: sites, text_or_urls: text },
      });
      trackJob(data.job_id, data.job, (job) => {
        const res = job.result || {};
        renderBatchResultTable(res.results || [], res.counts || {});
      });
    } catch (err) {
      handleError("批量下链失败", err);
    }
  }

  // 2.3 Crosslink
  function updateCrosslinkDynamicFields() {
    const mode = $("fl-xl-mode").value;
    $("fl-xl-k-wrap").classList.toggle("hidden", !(mode === "random_k" || mode === "group_cross"));
    $("fl-xl-hub-wrap").classList.toggle("hidden", mode !== "hub");
    $("fl-xl-group-a-wrap").classList.toggle("hidden", mode !== "group_cross");
    $("fl-xl-group-b-wrap").classList.toggle("hidden", mode !== "group_cross");
    $("fl-xl-bidirectional-wrap").classList.toggle("hidden", mode === "random_k" || mode === "mesh");
    $("fl-xl-avoid-reciprocal-wrap").classList.toggle("hidden", mode !== "random_k");
    $("fl-xl-avoid-root-wrap").classList.toggle("hidden", !(mode === "random_k" || mode === "group_cross"));
  }

  function updateCrosslinkHubOptions() {
    const sel = $("fl-xl-hub");
    const prev = sel.value;
    const picked = pickers.flXl ? pickers.flXl.getSelected() : [];
    const pool = picked.length
      ? state.sites.filter((s) => picked.includes(s.name))
      : state.sites;
    sel.innerHTML = pool
      .map((s) => `<option value="${escapeHtml(s.site_id || s.name)}">${escapeHtml(s.name)}${s.site_title ? ` (${escapeHtml(s.site_title)})` : ""}</option>`)
      .join("");
    if (prev && Array.from(sel.options).some((o) => o.value === prev)) {
      sel.value = prev;
    }
  }

  function buildCrosslinkSelectionKey() {
    const sites = pickers.flXl ? [...pickers.flXl.getSelected()].sort() : [];
    return JSON.stringify({
      sites,
      mode: $("fl-xl-mode").value,
      k: $("fl-xl-k").value,
      hub: $("fl-xl-hub").value,
      ga: $("fl-xl-group-a").value,
      gb: $("fl-xl-group-b").value,
      bi: $("fl-xl-bidirectional").checked,
      ar: $("fl-xl-avoid-reciprocal").checked,
      ard: $("fl-xl-avoid-root").checked,
      nf: $("fl-xl-nofollow").checked,
      remark: $("fl-xl-remark").value,
      seed: $("fl-xl-seed").value,
    });
  }

  function invalidateCrosslinkPreview() {
    state.crosslinkPreview = null;
    state.crosslinkSelectionKey = "";
    $("btn-fl-xl-execute").disabled = true;
    $("fl-xl-fp-hint").textContent = "参数或站点已变更，请先点击「生成预览计划」重新校验指纹。";
  }

  function renderCrosslinkTable(items, counts = {}, isExecuted = false) {
    $("fl-xl-preview-summary").innerHTML = Object.entries(counts)
      .map(([k, v]) => {
        const meta = LINK_OP_STATUS_MAP[k] || { label: k, cls: "badge-neutral" };
        return `<span class="badge ${meta.cls}">${escapeHtml(meta.label)}: ${v}</span>`;
      })
      .join("");

    const tbody = $("fl-xl-preview-body");
    if (!items || !items.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty-cell">当前策略下未生成互链条目</td></tr>`;
      return;
    }
    tbody.innerHTML = items
      .map((it) => {
        const stateVal = it.state || it.status || "planned";
        const meta = LINK_OP_STATUS_MAP[stateVal] || { label: stateVal, cls: "badge-neutral" };
        return `
          <tr>
            <td><strong>${escapeHtml(it.source_name || it.site_name)}</strong></td>
            <td>${escapeHtml(it.target_name || "-")}</td>
            <td>${escapeHtml(it.title)}</td>
            <td>${renderUrlLink(it.link)}</td>
            <td>${it.nofollow ? "是" : "否"}</td>
            <td>${escapeHtml(it.remark || "-")}</td>
            <td><span class="badge ${meta.cls}">${escapeHtml(meta.label)}</span></td>
            <td>${escapeHtml(it.message || (isExecuted ? "" : "-"))}</td>
          </tr>
        `;
      })
      .join("");
  }

  async function handleCrosslinkPreview() {
    const sites = pickers.flXl.getSelected();
    if (sites.length < 2) {
      showToast("至少选择 2 个站点", "站群互链需要选择 2 个及以上站点", "warning");
      return;
    }
    const mode = $("fl-xl-mode").value;
    const payload = {
      mode,
      site_names: sites,
      hub_site: mode === "hub" ? $("fl-xl-hub").value : "",
      bidirectional: $("fl-xl-bidirectional").checked,
      nofollow: $("fl-xl-nofollow").checked,
      remark: $("fl-xl-remark").value.trim(),
      avoid_reciprocal: $("fl-xl-avoid-reciprocal").checked,
      avoid_same_root_domain: $("fl-xl-avoid-root").checked,
      group_a: mode === "group_cross" && $("fl-xl-group-a").value ? $("fl-xl-group-a").value : null,
      group_b: mode === "group_cross" && $("fl-xl-group-b").value ? $("fl-xl-group-b").value : null,
    };
    const kVal = $("fl-xl-k").value.trim();
    if (kVal && (mode === "random_k" || mode === "group_cross")) {
      payload.k = Number(kVal);
    }
    const seedVal = $("fl-xl-seed").value.trim();
    if (seedVal) {
      payload.seed = Number(seedVal);
    }

    try {
      const data = await apiFetch("/api/friendlinks/crosslink/preview", {
        method: "POST",
        body: payload,
      });
      state.crosslinkPreview = data;
      state.crosslinkSelectionKey = buildCrosslinkSelectionKey();
      renderCrosslinkTable(data.items || [], data.counts || {}, false);
      const plannedCount = (data.counts && data.counts.planned) || 0;
      const canExec = Boolean(data.fingerprint) && (data.items || []).length > 0;
      $("btn-fl-xl-execute").disabled = !canExec;
      $("fl-xl-fp-hint").textContent = canExec
        ? `预览指纹已锁定 (${data.fingerprint.slice(0, 36)}${data.fingerprint.length > 36 ? "..." : ""})，待创建 ${plannedCount} 条。`
        : "未生成有效待创建互链。";
      showToast("互链预览已生成", `共 ${data.items.length} 条记录，待创建 ${plannedCount} 条`, "success");
    } catch (err) {
      invalidateCrosslinkPreview();
      handleError("生成互链预览失败", err);
    }
  }

  async function handleCrosslinkExecute() {
    if (!state.crosslinkPreview || state.crosslinkSelectionKey !== buildCrosslinkSelectionKey()) {
      invalidateCrosslinkPreview();
      showToast("预览已失效", "请重新点击「生成预览计划」", "warning");
      return;
    }
    const { fingerprint, plan_id, counts } = state.crosslinkPreview;
    const plannedCount = (counts && counts.planned) || 0;
    const ok = await confirmAction({
      title: "执行站群互链确认",
      message: `即将执行互链计划 ${plan_id}（待创建 ${plannedCount} 条友链），执行过程会写入回滚台账。确认继续？`,
      confirmText: "确认执行互链",
      danger: false,
    });
    if (!ok) return;

    try {
      const data = await apiFetch("/api/friendlinks/crosslink/execute", {
        method: "POST",
        body: { fingerprint, plan_id },
      });
      invalidateCrosslinkPreview();
      $("fl-xl-fp-hint").textContent = "互链计划已提交执行，如需再次执行请重新生成预览。";
      trackJob(data.job_id, data.job, async (job) => {
        const res = job.result || {};
        renderCrosslinkTable(res.items || [], res.result_counts || res.counts || {}, true);
        await loadCrosslinkHistory();
      });
    } catch (err) {
      handleError("执行互链失败", err);
    }
  }

  async function handleExportHtml() {
    const sites = pickers.flXl.getSelected();
    try {
      const data = await apiFetch("/api/friendlinks/export-html", {
        method: "POST",
        body: {
          site_names: sites.length ? sites : undefined,
          include_nofollow: $("fl-xl-export-nofollow").checked,
        },
      });
      $("export-count-text").textContent = `已生成 ${data.count} 条站点 <a> 换链 HTML（已自动轮换或取主标题）`;
      $("export-html-text").value = data.html || "";
      $("export-modal").classList.remove("hidden");
    } catch (err) {
      handleError("导出换链 HTML 失败", err);
    }
  }

  async function loadCrosslinkHistory() {
    try {
      const data = await apiFetch("/api/friendlinks/crosslink/history");
      const plans = data.plans || [];
      const tbody = $("fl-xl-history-body");
      if (!plans.length) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty-cell">暂无历史互链台账</td></tr>`;
        return;
      }
      tbody.innerHTML = plans
        .map((p) => {
          const modeLabel = MODE_LABELS[p.mode] || p.mode;
          const remaining = Math.max(0, (p.created_count || 0) - (p.rolled_back_count || 0));
          const sitesText = (p.site_names || []).join(", ");
          return `
            <tr>
              <td class="mono">${escapeHtml(p.plan_id)}</td>
              <td><span class="badge badge-info">${escapeHtml(modeLabel)}</span></td>
              <td class="mono">${escapeHtml(p.created_at || "-")}</td>
              <td><span class="url-cell" title="${escapeHtml(sitesText)}">${escapeHtml((p.site_names || []).length)} 站 (${escapeHtml(sitesText)})</span></td>
              <td class="mono">${p.created_count ?? 0}</td>
              <td class="mono">${p.rolled_back_count ?? 0}</td>
              <td class="col-actions">
                <button type="button" class="btn btn-danger btn-xs" data-xl-rollback="${escapeHtml(p.plan_id)}" ${remaining <= 0 ? "disabled" : ""}>
                  ${remaining <= 0 ? "已全回滚" : "一键回滚"}
                </button>
              </td>
            </tr>
          `;
        })
        .join("");
    } catch (err) {
      handleError("读取互链台账失败", err);
    }
  }

  async function handleCrosslinkRollback(planId) {
    const ok = await confirmAction({
      title: "回滚互链计划确认",
      message: `确定要一键回滚计划「${planId}」吗？只会精准删除该计划当时实际新建且尚未回滚的友链。`,
      confirmText: "确认回滚",
    });
    if (!ok) return;
    try {
      const data = await apiFetch("/api/friendlinks/crosslink/rollback", {
        method: "POST",
        body: { plan_id: planId },
      });
      trackJob(data.job_id, data.job, async () => {
        await loadCrosslinkHistory();
      });
    } catch (err) {
      handleError("互链回滚失败", err);
    }
  }

  // 2.4 Audit
  function auditRowKey(item) {
    return `${item.site_name}\x00${item.link}`;
  }

  async function handleAuditRun() {
    const sites = pickers.flAudit.getSelected();
    const whitelist = splitTokens($("fl-audit-whitelist").value);
    try {
      const data = await apiFetch("/api/friendlinks/audit", {
        method: "POST",
        body: {
          site_names: sites.length ? sites : undefined,
          whitelist_domains: whitelist,
        },
      });
      trackJob(data.job_id, data.job, (job) => {
        state.auditReport = job.result || { items: [], site_errors: {}, summary: {} };
        state.auditSelected.clear();
        state.auditFilter = "all";
        renderAuditView();
      });
    } catch (err) {
      handleError("启动反链巡检失败", err);
    }
  }

  function renderAuditView() {
    const report = state.auditReport || { items: [], site_errors: {}, summary: {} };
    const s = report.summary || {};
    const counts = {
      all: s.total ?? (report.items || []).length,
      missing: s.missing ?? 0,
      unreachable: s.unreachable ?? 0,
      nofollow: s.nofollow ?? 0,
      ok: s.ok ?? 0,
      whitelisted: s.whitelisted ?? 0,
    };
    const labels = {
      all: "全部",
      missing: "无反链",
      unreachable: "不可达",
      nofollow: "nofollow",
      ok: "正常",
      whitelisted: "白名单",
    };

    $("fl-audit-summary").innerHTML = Object.keys(labels)
      .map(
        (key) => `
          <button type="button" class="filter-pill ${state.auditFilter === key ? "active" : ""}" data-audit-filter="${key}">
            ${labels[key]} ${counts[key]}
          </button>
        `
      )
      .join("");

    const errs = Object.entries(report.site_errors || {});
    const errEl = $("fl-audit-errors");
    if (errs.length) {
      errEl.classList.remove("hidden");
      errEl.innerHTML = `<strong>以下站点拉取友链失败：</strong>${errs.map(([k, v]) => `${escapeHtml(k)}: ${escapeHtml(v)}`).join("；")}`;
    } else {
      errEl.classList.add("hidden");
    }

    const items = (report.items || []).filter(
      (it) => state.auditFilter === "all" || it.status === state.auditFilter
    );
    const tbody = $("fl-audit-table-body");
    $("fl-audit-master-check").checked =
      items.length > 0 && items.every((it) => state.auditSelected.has(auditRowKey(it)));

    if (!items.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-cell">当前筛选条件下无反链记录</td></tr>`;
      return;
    }

    tbody.innerHTML = items
      .map((it) => {
        const key = auditRowKey(it);
        const checked = state.auditSelected.has(key);
        const meta = AUDIT_STATUS_MAP[it.status] || { label: it.status, cls: "badge-neutral" };
        return `
          <tr class="${checked ? "row-selected" : ""}">
            <td class="col-check">
              <input type="checkbox" class="fl-audit-check" data-key="${escapeHtml(key)}" ${checked ? "checked" : ""} />
            </td>
            <td><strong>${escapeHtml(it.site_name)}</strong></td>
            <td>${escapeHtml(it.title || "-")}</td>
            <td>${renderUrlLink(it.link)}</td>
            <td class="mono">${it.http_status ?? "-"}</td>
            <td><span class="badge ${meta.cls}">${escapeHtml(meta.label)}</span></td>
            <td>${escapeHtml(it.message || "-")}</td>
          </tr>
        `;
      })
      .join("");
  }

  async function batchRemoveGroupedBySite(items, keyFn, title, onAllDone) {
    if (!items.length) return;
    const grouped = new Map();
    for (const it of items) {
      const sname = it.site_name;
      if (!grouped.has(sname)) grouped.set(sname, []);
      grouped.get(sname).push(it.link);
    }
    const ok = await confirmAction({
      title,
      message: `即将从 ${grouped.size} 个站点共计删除 ${items.length} 条友链记录（按站点精确分组下链）。确认执行？`,
      detail: Array.from(grouped.entries())
        .map(([s, urls]) => `${s}: ${urls.length} 条 (${urls.slice(0, 3).join(", ")}${urls.length > 3 ? "..." : ""})`)
        .join("\n"),
      confirmText: `确认下链 (${items.length} 条)`,
    });
    if (!ok) return;

    const entries = Array.from(grouped.entries());
    let completed = 0;
    const removedKeys = new Set();
    for (const [siteName, urls] of entries) {
      try {
        const data = await apiFetch("/api/friendlinks/batch-remove", {
          method: "POST",
          body: { site_names: [siteName], text_or_urls: urls },
        });
        trackJob(data.job_id, data.job, async (job) => {
          const results = (job.result && job.result.results) || [];
          for (const r of results) {
            if (r.status === "deleted" || r.status === "not_found") {
              removedKeys.add(`${r.site_name}\x00${r.link}`);
            }
          }
          completed++;
          if (completed === entries.length && typeof onAllDone === "function") {
            onAllDone(removedKeys);
          }
        });
      } catch (err) {
        completed++;
        handleError(`站点 ${siteName} 下链提交失败`, err);
      }
    }
  }

  function handleAuditRemoveSelected() {
    const items = ((state.auditReport && state.auditReport.items) || []).filter((it) =>
      state.auditSelected.has(auditRowKey(it))
    );
    if (!items.length) {
      showToast("请先勾选掉链项", "可点击「全选掉链项」快速勾选异常链接", "warning");
      return;
    }
    batchRemoveGroupedBySite(items, auditRowKey, "批量下掉选中的掉链", (removedKeys) => {
      if (!state.auditReport) return;
      state.auditReport.items = (state.auditReport.items || []).filter(
        (it) => !removedKeys.has(auditRowKey(it))
      );
      for (const k of removedKeys) state.auditSelected.delete(k);
      // Recompute summary
      const summary = { total: 0, ok: 0, nofollow: 0, missing: 0, unreachable: 0, whitelisted: 0 };
      for (const it of state.auditReport.items) {
        summary.total++;
        if (it.status in summary) summary[it.status]++;
      }
      state.auditReport.summary = summary;
      renderAuditView();
    });
  }

  // 2.5 Search
  function searchRowKey(item) {
    return `${item.site_name}\x00${item.link}`;
  }

  async function handleSearchRun() {
    const sites = pickers.flSearch.getSelected();
    const query = $("fl-search-query").value.trim();
    try {
      const data = await apiFetch("/api/friendlinks/search", {
        method: "POST",
        body: {
          site_names: sites.length ? sites : undefined,
          query,
          match_root_domain: $("fl-search-root-domain").checked,
        },
      });
      trackJob(data.job_id, data.job, (job) => {
        state.searchResults = job.result || { matches: [], site_errors: {}, total_links: 0 };
        state.searchSelected.clear();
        renderSearchView();
      });
    } catch (err) {
      handleError("启动全站查链失败", err);
    }
  }

  function renderSearchView() {
    const res = state.searchResults || { matches: [], site_errors: {}, total_links: 0 };
    const matches = res.matches || [];
    $("fl-search-summary").textContent = `命中 ${matches.length} 条（扫描总链 ${res.total_links ?? 0} 条）`;

    const errs = Object.entries(res.site_errors || {});
    const errEl = $("fl-search-errors");
    if (errs.length) {
      errEl.classList.remove("hidden");
      errEl.innerHTML = `<strong>部分站点拉取失败：</strong>${errs.map(([k, v]) => `${escapeHtml(k)}: ${escapeHtml(v)}`).join("；")}`;
    } else {
      errEl.classList.add("hidden");
    }

    $("fl-search-master-check").checked =
      matches.length > 0 && matches.every((m) => state.searchSelected.has(searchRowKey(m)));

    const tbody = $("fl-search-table-body");
    if (!matches.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-cell">未找到匹配的友链记录</td></tr>`;
      return;
    }

    tbody.innerHTML = matches
      .map((m) => {
        const key = searchRowKey(m);
        const checked = state.searchSelected.has(key);
        return `
          <tr class="${checked ? "row-selected" : ""}">
            <td class="col-check">
              <input type="checkbox" class="fl-search-check" data-key="${escapeHtml(key)}" ${checked ? "checked" : ""} />
            </td>
            <td><strong>${escapeHtml(m.site_name)}</strong></td>
            <td>${escapeHtml(m.title || "-")}</td>
            <td>${renderUrlLink(m.link)}</td>
            <td class="mono">${escapeHtml(m.root_domain || "-")}</td>
            <td>${m.nofollow ? `<span class="badge badge-warning">nofollow</span>` : `<span class="badge badge-success">dofollow</span>`}</td>
            <td>${escapeHtml(m.remark || "-")}</td>
          </tr>
        `;
      })
      .join("");
  }

  function handleSearchRemoveSelected() {
    const matches = (state.searchResults && state.searchResults.matches) || [];
    const selected = matches.filter((m) => state.searchSelected.has(searchRowKey(m)));
    if (!selected.length) {
      showToast("请先勾选需要删除的链接", "", "warning");
      return;
    }
    batchRemoveGroupedBySite(selected, searchRowKey, "一键删除查链结果", (removedKeys) => {
      if (!state.searchResults) return;
      state.searchResults.matches = (state.searchResults.matches || []).filter(
        (m) => !removedKeys.has(searchRowKey(m))
      );
      for (const k of removedKeys) state.searchSelected.delete(k);
      renderSearchView();
    });
  }

  // ------------------------------------------------------------------ 3. Upgrade
  function syncUpgradeTableFromCache() {
    for (const site of state.sites) {
      if (!state.upgradeRows.has(site.name)) {
        state.upgradeRows.set(site.name, {
          site_name: site.name,
          current_version: site.current_version || "",
          latest_version: site.latest_version || "",
          status: site.update_available === true ? "updatable" : site.update_available === false ? "already_latest" : "unknown",
          source: "",
          check_tool: "",
          upgrade_tool: "",
          duration_s: null,
          message: site.last_checked_at ? `缓存于 ${site.last_checked_at}` : "尚未检测版本",
          hint: "",
        });
      }
    }
    renderUpgradeTable();
  }

  function renderUpgradeTable() {
    const rows = state.sites.map(
      (s) =>
        state.upgradeRows.get(s.name) || {
          site_name: s.name,
          current_version: s.current_version || "",
          latest_version: s.latest_version || "",
          status: "unknown",
          message: "尚未检测版本",
          hint: "",
        }
    );

    const tbody = $("upg-table-body");
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-cell">暂无站点配置</td></tr>`;
      return;
    }

    let updatable = 0;
    let upgraded = 0;
    let issues = 0;
    for (const r of rows) {
      if (r.status === "updatable") updatable++;
      if (r.status === "upgraded") upgraded++;
      if (["scope_disabled", "no_tool", "auth_failed", "unreachable", "failed", "timeout"].includes(r.status)) {
        issues++;
      }
    }
    $("upg-summary").innerHTML = `
      <span class="badge badge-info">可升级 ${updatable}</span>
      <span class="badge badge-success">已升级 ${upgraded}</span>
      <span class="badge ${issues ? "badge-warning" : "badge-neutral"}">告警/异常 ${issues}</span>
    `;

    tbody.innerHTML = rows
      .map((r) => {
        let badgeHtml = `<span class="badge badge-neutral">未检测</span>`;
        if (r.status === "updatable") {
          badgeHtml = `<span class="badge badge-info">发现新版可升级</span>`;
        } else if (r.status in UPGRADE_STATUS_MAP) {
          const meta = UPGRADE_STATUS_MAP[r.status];
          badgeHtml = `<span class="badge ${meta.cls}">${escapeHtml(meta.label)}</span>`;
        }
        const toolsLabel = [r.source, r.upgrade_tool || r.check_tool].filter(Boolean).join(" / ") || "-";
        const durLabel = r.duration_s !== null && r.duration_s !== undefined ? `${r.duration_s}s` : "-";
        return `
          <tr>
            <td><strong>${escapeHtml(r.site_name)}</strong></td>
            <td class="mono">${escapeHtml(r.current_version || "-")}</td>
            <td class="mono">${escapeHtml(r.latest_version || "-")}</td>
            <td>${badgeHtml}</td>
            <td class="mono">${escapeHtml(toolsLabel)}</td>
            <td class="mono">${escapeHtml(durLabel)}</td>
            <td>
              <div>${escapeHtml(r.message || "-")}</div>
              ${r.hint ? `<div class="hint-box">${escapeHtml(r.hint)}</div>` : ""}
            </td>
          </tr>
        `;
      })
      .join("");
  }

  async function handleUpgradeCheck() {
    const sites = pickers.upgrade.getSelected();
    try {
      const data = await apiFetch("/api/upgrade/check", {
        method: "POST",
        body: { site_names: sites.length ? sites : undefined },
      });
      trackJob(data.job_id, data.job, (job) => {
        const items = (job.result && job.result.items) || [];
        for (const info of items) {
          let status = "unknown";
          if (!info.reachable) status = "unreachable";
          else if (info.upgrade_available === true) status = "updatable";
          else if (info.upgrade_available === false) status = "already_latest";
          if (!info.upgrade_tool && info.hint && info.hint.includes("未通过 MCP")) {
            status = info.upgrade_available ? "no_tool" : status;
          }
          state.upgradeRows.set(info.site_name, {
            site_name: info.site_name,
            current_version: info.current_version || "",
            latest_version: info.latest_version || "",
            status,
            source: info.source || "",
            check_tool: info.check_tool || "",
            upgrade_tool: info.upgrade_tool || "",
            duration_s: null,
            message: info.message || "",
            hint: info.hint || "",
          });
        }
        renderUpgradeTable();
      });
    } catch (err) {
      handleError("批量检测版本失败", err);
    }
  }

  async function handleUpgradeProbeMcp() {
    const sites = pickers.upgrade.getSelected();
    try {
      const data = await apiFetch("/api/sites/probe-mcp", {
        method: "POST",
        body: { site_names: sites.length ? sites : undefined },
      });
      trackJob(data.job_id, data.job, (job) => {
        const items = (job.result && job.result.items) || [];
        for (const p of items) {
          const prev = state.upgradeRows.get(p.site_name) || {};
          state.upgradeRows.set(p.site_name, {
            ...prev,
            site_name: p.site_name,
            status: p.ok ? prev.status || "unknown" : "unreachable",
            upgrade_tool: (p.upgrade_tools && p.upgrade_tools[0]) || prev.upgrade_tool || "",
            message: p.message || "",
            hint: p.hint || prev.hint || "",
          });
        }
        renderUpgradeTable();
      });
    } catch (err) {
      handleError("探测 MCP 失败", err);
    }
  }

  async function handleUpgradeRun() {
    const sites = pickers.upgrade.getSelected();
    if (!sites.length) {
      showToast("请先勾选要升级的站点", "出于安全保护，批量升级必须显式选择目标站点", "warning");
      return;
    }
    const concurrency = Math.min(5, Math.max(1, Number($("upg-concurrency").value) || 3));
    const canary = $("upg-canary").checked;
    const stopOnFailure = $("upg-stop-fail").checked;

    const ok = await confirmAction({
      title: "统一升级高危操作确认",
      message: `即将对选中的 ${sites.length} 个站点执行系统升级（并发：${concurrency}，${canary ? "金丝雀先行" : "直接并发"}，${stopOnFailure ? "失败即停" : "失败继续"}）。确认执行？`,
      detail: sites.join("、"),
      confirmText: `确认升级 (${sites.length} 站)`,
    });
    if (!ok) return;

    try {
      const data = await apiFetch("/api/upgrade/run", {
        method: "POST",
        body: {
          site_names: sites,
          confirmed: true,
          concurrency,
          canary,
          stop_on_failure: stopOnFailure,
        },
      });
      trackJob(data.job_id, data.job, (job) => {
        const items = (job.result && job.result.items) || [];
        for (const res of items) {
          const prev = state.upgradeRows.get(res.site_name) || {};
          state.upgradeRows.set(res.site_name, {
            ...prev,
            site_name: res.site_name,
            current_version: res.to_version || res.from_version || prev.current_version || "",
            latest_version: res.to_version || prev.latest_version || "",
            status: res.status,
            duration_s: res.duration_s,
            message: res.message || "",
            hint: res.hint || "",
          });
        }
        renderUpgradeTable();
      });
    } catch (err) {
      handleError("执行统一升级失败", err);
    }
  }

  // ------------------------------------------------------------------ 4. Sites Management
  function renderSitesTable() {
    const grp = $("sites-group-filter").value;
    const enabledVal = $("sites-enabled-filter").value;
    const q = $("sites-search").value.trim().toLowerCase();

    const filtered = state.sites.filter((s) => {
      if (grp && (s.site_group || "").trim() !== grp) return false;
      if (enabledVal === "true" && !s.enabled) return false;
      if (enabledVal === "false" && s.enabled) return false;
      if (q) {
        const hay = `${s.name} ${s.site_title || ""} ${s.url || ""} ${s.site_group || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    const tbody = $("sites-table-body");
    if (!filtered.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">无匹配站点</td></tr>`;
      return;
    }

    tbody.innerHTML = filtered
      .map((s) => {
        const isEditing = state.editingSiteName === s.name;
        const creds = [
          s.has_mcp_token ? "MCP" : "",
          s.admin_username && s.has_admin_password ? "后台" : "",
          s.has_import_token ? "导入" : "",
        ].filter(Boolean);
        return `
          <tr class="clickable-row ${isEditing ? "row-selected" : ""}" data-edit-site="${escapeHtml(s.name)}">
            <td><strong>${escapeHtml(s.name)}</strong></td>
            <td><span class="url-cell" title="${escapeHtml(s.site_title || "")}">${escapeHtml(s.site_title || "-")}</span></td>
            <td>${s.site_group ? `<span class="badge badge-neutral">${escapeHtml(s.site_group)}</span>` : "-"}</td>
            <td>${renderUrlLink(s.url)}</td>
            <td>${creds.length ? creds.map((c) => `<span class="badge badge-info">${c}</span>`).join(" ") : `<span class="muted-text">未配</span>`}</td>
            <td>${s.enabled ? `<span class="badge badge-success">启用发布</span>` : `<span class="badge badge-neutral">纯管站</span>`}</td>
          </tr>
        `;
      })
      .join("");
  }

  function loadSiteIntoForm(site = null) {
    if (!site) {
      state.editingSiteName = null;
      $("site-editor-title").textContent = "新增站点";
      $("btn-site-delete").classList.add("hidden");
      $("sf-name").readOnly = false;
      $("sf-name").value = "";
      $("sf-group").value = "";
      $("sf-title").value = "";
      $("sf-url").value = "";
      $("sf-jiekou").value = "";
      $("sf-jiekou-masked").textContent = "";
      $("sf-mcp-token").value = "";
      $("sf-mcp-token").placeholder = "MCP Token（可选）";
      $("sf-clear-mcp").checked = false;
      $("sf-admin-path").value = "/system/";
      $("sf-admin-user").value = "";
      $("sf-admin-pass").value = "";
      $("sf-admin-pass").placeholder = "后台密码（可选）";
      $("sf-clear-pass").checked = false;
      $("sf-fid").value = "0";
      $("sf-limit").value = "60";
      $("sf-interval").value = "60";
      $("sf-pubtime").value = "06:00:00";
      $("sf-enabled").checked = false;
    } else {
      state.editingSiteName = site.name;
      $("site-editor-title").textContent = `编辑站点：${site.name}`;
      $("btn-site-delete").classList.remove("hidden");
      $("sf-name").readOnly = true;
      $("sf-name").value = site.name;
      $("sf-group").value = site.site_group || "";
      $("sf-title").value = site.site_title || "";
      $("sf-url").value = site.url || "";
      $("sf-jiekou").value = "";
      $("sf-jiekou-masked").textContent = site.jiekou_masked
        ? `当前已配置接口：${site.jiekou_masked}（上方留空则保持不变）`
        : "当前未配置导入接口";
      $("sf-mcp-token").value = "";
      $("sf-mcp-token").placeholder = site.has_explicit_mcp_token
        ? `已配置 (${site.mcp_token_masked})，留空保留`
        : "留空自动复用导入 Token 或填写新 Token";
      $("sf-clear-mcp").checked = false;
      $("sf-admin-path").value = site.admin_path || "/system/";
      $("sf-admin-user").value = site.admin_username || "";
      $("sf-admin-pass").value = "";
      $("sf-admin-pass").placeholder = site.has_admin_password
        ? `已配置 (${site.admin_password_masked})，留空保留`
        : "输入后台密码";
      $("sf-clear-pass").checked = false;
      $("sf-fid").value = String(site.fid ?? 0);
      $("sf-limit").value = String(site.daily_publish_limit ?? 60);
      $("sf-interval").value = String(site.interval_time ?? 60);
      $("sf-pubtime").value = site.publish_time || "06:00:00";
      $("sf-enabled").checked = Boolean(site.enabled);
    }
    renderSitesTable();
  }

  async function handleSiteSave(ev) {
    ev.preventDefault();
    const isEdit = Boolean(state.editingSiteName);
    const name = $("sf-name").value.trim();
    if (!name) return showToast("站点标识不能为空", "", "warning");

    const payload = {
      name,
      site_group: $("sf-group").value.trim(),
      site_title: $("sf-title").value.trim(),
      url: $("sf-url").value.trim(),
      admin_path: $("sf-admin-path").value.trim(),
      admin_username: $("sf-admin-user").value.trim(),
      fid: Number($("sf-fid").value || 0),
      daily_publish_limit: Number($("sf-limit").value || 60),
      interval_time: Number($("sf-interval").value || 60),
      publish_time: $("sf-pubtime").value.trim(),
      enabled: $("sf-enabled").checked,
    };

    const jiekouInput = $("sf-jiekou").value.trim();
    if (jiekouInput) {
      if (!payload.url && /^https?:\/\//i.test(jiekouInput)) {
        payload.full_endpoint = jiekouInput;
      } else {
        payload.jiekou = jiekouInput;
      }
    }

    const mcpTok = $("sf-mcp-token").value.trim();
    if (mcpTok) {
      payload.mcp_token = mcpTok;
    } else if (isEdit && $("sf-clear-mcp").checked) {
      payload.clear_mcp_token = true;
    }

    const adminPass = $("sf-admin-pass").value.trim();
    if (adminPass) {
      payload.admin_password = adminPass;
    } else if (isEdit && $("sf-clear-pass").checked) {
      payload.clear_admin_password = true;
    }

    try {
      const data = await apiFetch("/api/sites", { method: "POST", body: payload });
      showToast(data.created ? "站点已新增" : "站点已更新", `站点：${data.site.name}`, "success");
      await loadDashboard({ silent: true });
      loadSiteIntoForm(data.site);
    } catch (err) {
      handleError("保存站点失败", err);
    }
  }

  async function handleSiteDelete() {
    const target = state.editingSiteName;
    if (!target) return;
    const ok = await confirmAction({
      title: "删除站点确认",
      message: `确定从配置中永久移除站点「${target}」吗？（不会删除站群本地文章归档文件夹）`,
      confirmText: "确认删除",
    });
    if (!ok) return;
    try {
      await apiFetch(`/api/sites/${encodeURIComponent(target)}`, { method: "DELETE" });
      showToast("站点已删除", target, "success");
      loadSiteIntoForm(null);
      await loadDashboard({ silent: true });
    } catch (err) {
      handleError("删除站点失败", err);
    }
  }

  // 4.2 Bulk Assign / Text update
  async function handleBulkAssign() {
    const siteNames = pickers.sitesAssign.getSelected();
    if (!siteNames.length) {
      showToast("请先选择站点", "", "warning");
      return;
    }
    const fields = {};
    const strMap = [
      ["ba-group", "site_group"],
      ["ba-title", "site_title"],
      ["ba-mcp-token", "mcp_token"],
      ["ba-admin-path", "admin_path"],
      ["ba-admin-user", "admin_username"],
      ["ba-admin-pass", "admin_password"],
    ];
    for (const [elId, key] of strMap) {
      const v = $(elId).value.trim();
      if (v) fields[key] = v;
    }
    if ($("ba-fid").value.trim() !== "") fields.fid = Number($("ba-fid").value);
    if ($("ba-limit").value.trim() !== "") fields.daily_publish_limit = Number($("ba-limit").value);
    const enabledVal = $("ba-enabled").value;
    if (enabledVal === "true") fields.enabled = true;
    else if (enabledVal === "false") fields.enabled = false;

    if (!Object.keys(fields).length) {
      showToast("请至少填写一个需要赋值的字段", "", "warning");
      return;
    }

    const ok = await confirmAction({
      title: "批量赋值老站确认",
      message: `即将对选中的 ${siteNames.length} 个站点统一更新字段：${Object.keys(fields).join("、")}。确认应用？`,
      confirmText: "确认批量更新",
      danger: false,
    });
    if (!ok) return;

    try {
      const data = await apiFetch("/api/sites/bulk-update", {
        method: "POST",
        body: { mode: "assign", site_names: siteNames, fields, dry_run: false },
      });
      showToast("批量赋值完成", `已更新 ${data.count} 个站点`, "success");
      await loadDashboard({ silent: true });
    } catch (err) {
      handleError("批量赋值失败", err);
    }
  }

  async function handleBulkTextUpdate(dryRun) {
    const text = $("ba-text-input").value;
    if (!text.trim()) return showToast("请输入文本匹配行", "", "warning");
    if (!dryRun) {
      const ok = await confirmAction({
        title: "执行文本批量更新",
        message: "确定将匹配到的字段写入站点配置吗？",
        confirmText: "确认写入",
        danger: false,
      });
      if (!ok) return;
    }
    try {
      const data = await apiFetch("/api/sites/bulk-update", {
        method: "POST",
        body: { mode: "text", text, dry_run: dryRun },
      });
      const box = $("sites-assign-result");
      box.classList.remove("hidden");
      box.innerHTML = `
        <div class="subcard-head">
          <strong>${dryRun ? "匹配预览" : "更新结果"}：匹配 ${data.matched_count} 条 / 未匹配 ${data.unmatched_count} 条 / 异常 ${data.error_count} 条</strong>
        </div>
        <div class="table-wrap table-short">
          <table class="data-table">
            <thead><tr><th>行</th><th>输入标识</th><th>匹配站点</th><th>状态</th><th>说明</th></tr></thead>
            <tbody>
              ${(data.items || [])
                .map(
                  (it) =>
                    `<tr><td class="mono">${it.line}</td><td>${escapeHtml(it.identifier)}</td><td>${escapeHtml(it.matched_site || "-")}</td><td><span class="badge ${it.status === "matched" ? "badge-success" : it.status === "error" ? "badge-danger" : "badge-neutral"}">${escapeHtml(it.status)}</span></td><td>${escapeHtml(it.message)}</td></tr>`
                )
                .join("")}
            </tbody>
          </table>
        </div>
      `;
      if (!dryRun) {
        showToast("文本批量更新完成", `已更新 ${(data.updated || []).length} 个站点`, "success");
        await loadDashboard({ silent: true });
      }
    } catch (err) {
      handleError("文本批量更新失败", err);
    }
  }

  // 4.3 Bulk Import (Smart Paste Import)
  function renderImportSiteRow(site, badgeCls, badgeText) {
    const mcpCell = site.has_mcp_token
      ? `<span class="mono">${escapeHtml(site.mcp_token_masked || "已配置")}</span>`
      : `<span class="muted-text">未配</span>`;
    const jiekouCell = site.has_import_token
      ? `<span class="mono" title="${escapeHtml(site.jiekou_masked || "")}">${escapeHtml(site.jiekou_masked || "已配置")}</span>`
      : `<span class="muted-text">未配</span>`;
    const groupCell = site.site_group
      ? `<span class="badge badge-neutral">${escapeHtml(site.site_group)}</span>`
      : `<span class="muted-text">-</span>`;
    return `
      <tr>
        <td><span class="badge ${badgeCls}">${badgeText}</span></td>
        <td><strong>${escapeHtml(site.name)}</strong></td>
        <td>${escapeHtml(site.site_title || "(待探测)")}</td>
        <td>${renderUrlLink(site.url)}</td>
        <td>${mcpCell}</td>
        <td>${jiekouCell}</td>
        <td>${groupCell}</td>
      </tr>
    `;
  }

  async function handleBulkImport(dryRun) {
    const text = $("sites-import-text").value;
    if (!text.trim()) return showToast("请先粘贴 MCP JSON、导入接口 URL 或站点列表", "", "warning");
    const defaultGroupEl = $("importDefaultGroup");
    const defaultGroup = defaultGroupEl ? defaultGroupEl.value.trim() : "";

    if (!dryRun) {
      const ok = await confirmAction({
        title: "确认智能导入并入库",
        message: "即将写入新增站点配置（自动创建默认站群目录），并为已有站点补全缺失的 Token / 接口 / 标题。确认执行？",
        confirmText: "确认入库",
        danger: false,
      });
      if (!ok) return;
    }

    try {
      const data = await apiFetch("/api/sites/bulk-import", {
        method: "POST",
        body: { text, dry_run: dryRun, default_group: defaultGroup },
      });
      const box = $("sites-import-preview-box");
      box.classList.remove("hidden");

      const newSites = data.new_sites || data.sites || [];
      const updatedSites = data.updated_sites || [];
      const warnings = data.warnings || [];
      const importedCount = data.imported_count ?? newSites.length;
      const updatedCount = data.updated_count ?? updatedSites.length;
      const mergedCount = data.merged_count || 0;

      const summaryEl = $("sites-import-preview-summary");
      summaryEl.innerHTML = `<strong>${dryRun ? "导入预览" : "入库完成"}：新增 ${importedCount} 站 / 补全已有 ${updatedCount} 站${
        mergedCount ? ` / 按域名合并 ${mergedCount} 组` : ""
      }${warnings.length ? ` / 提示 ${warnings.length} 条` : ""}</strong>`;

      const rowsHtml = [
        ...newSites.map((s) => renderImportSiteRow(s, "badge-success", "新增站点")),
        ...updatedSites.map((s) => renderImportSiteRow(s, "badge-info", "补全已有站点")),
        ...warnings.map((w) => {
          const linePrefix = w.line ? `第${escapeHtml(w.line)}行` : "-";
          return `
            <tr>
              <td><span class="badge badge-warning">提示/跳过</span></td>
              <td>${escapeHtml(w.name || linePrefix)}</td>
              <td><span class="muted-text">${escapeHtml(w.reason || "已跳过")}</span></td>
              <td>${w.url ? renderUrlLink(w.url) : `<span class="muted-text">-</span>`}</td>
              <td><span class="muted-text">-</span></td>
              <td><span class="muted-text">-</span></td>
              <td><span class="muted-text">-</span></td>
            </tr>
          `;
        }),
      ].join("");

      $("importPreviewBody").innerHTML =
        rowsHtml || `<tr><td colspan="7" class="empty-cell">未解析到可处理的站点数据</td></tr>`;

      if (!dryRun) {
        showToast("智能导入完成", `新增 ${importedCount} 站，补全 ${updatedCount} 站`, "success");
        await loadDashboard({ silent: true });

        const affectedNames = Array.from(
          new Set([...newSites, ...updatedSites].map((s) => s.name).filter(Boolean))
        );
        if ($("sites-import-auto-discover").checked && affectedNames.length) {
          const disc = await apiFetch("/api/sites/discover", {
            method: "POST",
            body: { site_names: affectedNames, probe_mcp: true, backfill_titles: true },
          });
          trackJob(disc.job_id, disc.job);
        }
      }
    } catch (err) {
      handleError("批量导入失败", err);
    }
  }

  // ------------------------------------------------------------------ 5. Publish
  async function loadPublishStatus() {
    try {
      const data = await apiFetch("/api/publish/status");
      const badge = $("pub-status-badge");
      if (data.running) {
        badge.textContent = data.stopping ? "正在停止" : data.paused ? "已暂停" : "运行中";
        badge.className = `badge ${data.stopping ? "badge-warning" : "badge-success"}`;
      } else {
        badge.textContent = "已停止";
        badge.className = "badge badge-neutral";
      }

      const st = data.stats || {};
      $("pub-stats-bar").textContent = st.started_at
        ? `最近批次：总计 ${st.total || 0}，成功 ${st.success || 0}，失败 ${st.failed || 0}（开始 ${st.started_at}${st.finished_at ? ` ~ 结束 ${st.finished_at}` : ""}）`
        : "暂无本次运行统计";

      $("btn-pub-start").disabled = Boolean(data.running);
      $("btn-pub-stop").disabled = !data.running;

      const diag = data.diagnostics || {};
      const tbody = $("pub-table-body");
      const sites = data.sites || [];
      if (!sites.length) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty-cell">暂无站点</td></tr>`;
      } else {
        tbody.innerHTML = sites
          .map((s) => {
            const d = diag[s.site_name];
            const msg = (d && d.message) || s.last_error || "-";
            const stCls =
              s.status === "error" ? "badge-danger" : s.pending_count > 0 ? "badge-info" : "badge-neutral";
            return `
              <tr>
                <td><strong>${escapeHtml(s.site_name)}</strong></td>
                <td><span class="badge ${stCls}">${escapeHtml(s.status || "-")}</span></td>
                <td class="mono">${s.pending_count ?? 0}</td>
                <td class="mono">${s.article_count ?? 0}</td>
                <td class="mono">${s.published_today ?? 0} / ${s.daily_limit ?? 0}</td>
                <td class="mono">${s.remaining_quota ?? 0}</td>
                <td>${escapeHtml(msg)}</td>
              </tr>
            `;
          })
          .join("");
      }

      managePublishAutoRefresh(Boolean(data.running));
    } catch (err) {
      handleError("获取发布状态失败", err);
    }
  }

  async function loadPublishLogs() {
    try {
      const data = await apiFetch("/api/publish/logs?lines=200");
      $("pub-log-file").textContent = data.log_file ? `日志文件：${data.log_file}` : "";
      const box = $("pub-log-lines");
      const lines = data.lines || [];
      box.textContent = lines.length ? lines.join("\n") : "暂无发布日志";
      box.scrollTop = box.scrollHeight;
    } catch (err) {
      handleError("读取发布日志失败", err);
    }
  }

  function managePublishAutoRefresh(running) {
    if (state.publishPollTimer) {
      clearInterval(state.publishPollTimer);
      state.publishPollTimer = null;
    }
    if (running && $("pub-auto-refresh").checked && state.activeView === "publish") {
      state.publishPollTimer = setInterval(() => {
        if (state.activeView !== "publish" || !$("pub-auto-refresh").checked) {
          managePublishAutoRefresh(false);
          return;
        }
        loadPublishStatus();
        loadPublishLogs();
      }, 2500);
    }
  }

  async function handlePublishStart() {
    const selected = pickers.publish.getSelected();
    const allowDisabled = $("pub-allow-disabled").checked;
    try {
      const data = await apiFetch("/api/publish/start", {
        method: "POST",
        body: {
          site_names: selected.length ? selected : undefined,
          allow_disabled: selected.length ? allowDisabled : false,
        },
      });
      if (!data.started) {
        showToast("未启动发布", data.message || "没有满足条件的待发任务", "warning");
      } else {
        showToast("发布任务已启动", `涉及 ${data.sites.length} 个站点，共 ${data.task_count} 篇文章`, "success");
      }
      await loadPublishStatus();
      await loadPublishLogs();
    } catch (err) {
      handleError("启动发布失败", err);
    }
  }

  async function handlePublishStop() {
    const ok = await confirmAction({
      title: "停止发布任务",
      message: "确定请求停止当前运行中的批量文章发布队列吗？",
      confirmText: "确认停止",
    });
    if (!ok) return;
    try {
      await apiFetch("/api/publish/stop", { method: "POST", body: {} });
      showToast("已发送停止信号", "", "info");
      await loadPublishStatus();
      await loadPublishLogs();
    } catch (err) {
      handleError("停止发布失败", err);
    }
  }

  // ------------------------------------------------------------------ 6. Settings, MCP & Audit
  function renderAppUpdateCard(info) {
    if (!info) return;
    const currentVer = String(info.current_version || (state.health && state.health.version) || "2.0.0").replace(/^v/i, "");
    const latestVer = String(info.latest_version || "").replace(/^v/i, "");
    const available = Boolean(info.update_available);

    updateVersionPills(currentVer, available, latestVer);

    const badge = $("app-update-status-badge");
    const applyBtn = $("btn-app-update-apply");
    const detailEl = $("app-update-detail");
    const titleEl = $("app-update-latest-title");
    const linkEl = $("app-update-release-link");
    const notesEl = $("app-update-notes");

    if (info.error) {
      badge.textContent = "检查失败";
      badge.className = "badge badge-danger";
      applyBtn.disabled = true;
    } else if (available) {
      badge.textContent = latestVer ? `发现新版 v${latestVer}` : "发现新版本";
      badge.className = "badge badge-info";
      applyBtn.disabled = false;
    } else {
      badge.textContent = latestVer ? `已是最新 (v${latestVer})` : "已是最新版本";
      badge.className = "badge badge-success";
      applyBtn.disabled = true;
    }

    const releaseUrl = safeUrl(info.release_url);
    if (releaseUrl) {
      linkEl.href = releaseUrl;
      linkEl.classList.remove("hidden");
    } else {
      linkEl.classList.add("hidden");
    }

    const notesText = String(info.release_notes || info.body || info.message || "").trim();
    if (available || notesText || releaseUrl) {
      detailEl.classList.remove("hidden");
      titleEl.textContent = available && latestVer
        ? `最新版本：v${latestVer}${info.asset_name ? ` (${info.asset_name})` : ""}`
        : info.message || "版本详情";
      notesEl.textContent = notesText || "暂无 Release Notes";
    } else {
      detailEl.classList.add("hidden");
    }
  }

  async function handleAppUpdateCheck({ silent = false } = {}) {
    const btn = $("btn-app-update-check");
    const prevText = btn ? btn.textContent : "检查程序更新";
    if (btn) {
      btn.disabled = true;
      btn.textContent = "检查中...";
    }
    try {
      const data = await apiFetch("/api/app/update/check");
      state.appUpdateInfo = data;
      renderAppUpdateCard(data);
      if (!silent) {
        if (data.update_available) {
          showToast("发现程序新版本", `最新版本 v${String(data.latest_version || "").replace(/^v/i, "")}`, "info");
        } else {
          showToast("当前已是最新版本", data.message || `v${String(data.current_version || "2.0.0").replace(/^v/i, "")}`, "success");
        }
      }
    } catch (err) {
      if (!silent) handleError("检查程序更新失败", err);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = prevText;
      }
    }
  }

  async function handleAppUpdateApply() {
    const info = state.appUpdateInfo;
    if (!info || !info.update_available) {
      showToast("当前无可用更新", "请先点击「检查程序更新」确认是否有新版发布", "warning");
      return;
    }
    const latestVer = String(info.latest_version || "").replace(/^v/i, "");
    const ok = await confirmAction({
      title: "确认下载并自动更新程序",
      message: `即将从 GitHub Releases 下载并校验最新版本${latestVer ? ` v${latestVer}` : ""}。若当前为 EXE 运行，校验通过后服务将自动重启完成替换；若为源码模式，将返回更新指引。确认继续？`,
      detail: [info.download_url, info.release_url].filter(Boolean).join("\n"),
      confirmText: "一键下载并更新",
      danger: false,
    });
    if (!ok) return;

    const btn = $("btn-app-update-apply");
    if (btn) btn.disabled = true;
    try {
      const data = await apiFetch("/api/app/update/apply", { method: "POST", body: {} });
      trackJob(data.job_id, data.job, (job) => {
        const res = job.result || {};
        if (res.source_mode) {
          showToast("源码运行模式", res.message || "请运行 git pull 或前往 GitHub Releases 下载新版 EXE", "info", res.release_url || "", 7000);
          renderAppUpdateCard({
            ...info,
            message: res.message,
            release_notes: `${res.message}\nRelease: ${res.release_url || "-"}\nEXE 下载: ${res.download_url || "-"}`,
          });
          if (btn) btn.disabled = !info.update_available;
        } else if (res.restarting) {
          showToast("程序更新包校验通过", res.message || "即将自动退出并替换重启...", "success", "", 8000);
        } else {
          if (btn) btn.disabled = !info.update_available;
        }
      });
    } catch (err) {
      if (btn) btn.disabled = !info.update_available;
      handleError("执行程序自动更新失败", err);
    }
  }

  function fillSettingsForm(s) {
    if (!s) return;
    $("settings-path-label").textContent = s.settings_path ? `配置文件：${s.settings_path}` : "";
    const mcp = s.mcp || {};
    $("set-mcp-endpoint").value = mcp.endpoint_path ?? "/api/mcp";
    $("set-mcp-proto").value = mcp.protocol_version ?? "2025-06-18";
    $("set-mcp-fallback").value = (mcp.fallback_protocol_versions || []).join(", ");
    $("set-mcp-connect").value = mcp.connect_timeout ?? 5.0;
    $("set-mcp-read").value = mcp.read_timeout ?? 20.0;
    $("set-mcp-ssl").checked = Boolean(mcp.verify_ssl);

    const fl = s.friendlink || {};
    $("set-fl-conc").value = fl.max_concurrency ?? 6;
    $("set-fl-req-timeout").value = fl.request_timeout ?? 15.0;
    $("set-fl-chk-timeout").value = fl.check_timeout ?? 8.0;
    $("set-fl-delay").value = fl.per_request_delay_s ?? 0.2;
    $("set-fl-remark").value = fl.default_remark ?? "站群互链";
    $("set-fl-nofollow").checked = Boolean(fl.default_nofollow);

    const aj = s.admin_jump || {};
    $("set-aj-thresh").value = aj.batch_confirm_threshold ?? 5;
    $("set-aj-max").value = aj.batch_max ?? 30;
    $("set-aj-stagger").value = aj.batch_stagger_ms ?? 400;
    $("set-aj-ttl").value = aj.bridge_ttl_s ?? 60.0;
    $("set-aj-login-paths").value = (aj.login_api_paths || []).join(", ");
    $("set-aj-ticket-tpl").value = aj.ticket_url_template ?? "";
    $("set-aj-ticket").checked = Boolean(aj.enable_ticket);
    $("set-aj-silent").checked = Boolean(aj.enable_silent_login);

    const upg = s.upgrade || {};
    $("set-upg-conc").value = upg.max_concurrency ?? 3;
    $("set-upg-call-timeout").value = upg.upgrade_call_timeout_s ?? 180.0;
    $("set-upg-restart-wait").value = upg.restart_wait_s ?? 8.0;
    $("set-upg-health-timeout").value = upg.health_timeout_s ?? 240.0;
    $("set-upg-health-interval").value = upg.health_interval_s ?? 5.0;
  }

  async function loadSettingsAndMcp() {
    try {
      const [settingsData, mcpCfg] = await Promise.all([
        apiFetch("/api/settings"),
        apiFetch("/api/mcp-config"),
      ]);
      state.settings = settingsData;
      state.mcpConfig = mcpCfg;
      fillSettingsForm(settingsData);

      $("mcp-http-url").textContent = `接口地址：${mcpCfg.http_url || ""}`;
      $("mcp-http-json").textContent = mcpCfg.streamable_http_json || JSON.stringify(mcpCfg.streamable_http, null, 2);
      $("mcp-stdio-meta").textContent = `脚本：${mcpCfg.mcp_script || ""}`;
      $("mcp-stdio-json").textContent = mcpCfg.stdio_json || JSON.stringify(mcpCfg.stdio, null, 2);
    } catch (err) {
      handleError("加载设置与 MCP 配置失败", err);
    }
  }

  async function handleSettingsSave(ev) {
    ev.preventDefault();
    const payload = {
      mcp: {
        endpoint_path: $("set-mcp-endpoint").value.trim(),
        protocol_version: $("set-mcp-proto").value.trim(),
        fallback_protocol_versions: splitTokens($("set-mcp-fallback").value),
        connect_timeout: Number($("set-mcp-connect").value),
        read_timeout: Number($("set-mcp-read").value),
        verify_ssl: $("set-mcp-ssl").checked,
      },
      friendlink: {
        max_concurrency: Number($("set-fl-conc").value),
        request_timeout: Number($("set-fl-req-timeout").value),
        check_timeout: Number($("set-fl-chk-timeout").value),
        per_request_delay_s: Number($("set-fl-delay").value),
        default_remark: $("set-fl-remark").value,
        default_nofollow: $("set-fl-nofollow").checked,
      },
      admin_jump: {
        batch_confirm_threshold: Number($("set-aj-thresh").value),
        batch_max: Number($("set-aj-max").value),
        batch_stagger_ms: Number($("set-aj-stagger").value),
        bridge_ttl_s: Number($("set-aj-ttl").value),
        login_api_paths: splitTokens($("set-aj-login-paths").value),
        ticket_url_template: $("set-aj-ticket-tpl").value.trim(),
        enable_ticket: $("set-aj-ticket").checked,
        enable_silent_login: $("set-aj-silent").checked,
      },
      upgrade: {
        max_concurrency: Math.min(5, Math.max(1, Number($("set-upg-conc").value))),
        upgrade_call_timeout_s: Number($("set-upg-call-timeout").value),
        restart_wait_s: Number($("set-upg-restart-wait").value),
        health_timeout_s: Number($("set-upg-health-timeout").value),
        health_interval_s: Number($("set-upg-health-interval").value),
      },
    };

    try {
      const updated = await apiFetch("/api/settings", { method: "PUT", body: payload });
      state.settings = updated;
      fillSettingsForm(updated);
      showToast("设置已保存并热重载", "后端服务已立即应用新配置", "success");
    } catch (err) {
      handleError("保存设置失败", err);
    }
  }

  async function loadAuditLogs() {
    const cat = $("audit-log-category").value;
    try {
      const data = await apiFetch(`/api/audit-logs?limit=100&category=${encodeURIComponent(cat)}`);
      const ops = data.operations || [];
      const tbody = $("audit-logs-body");
      if (!ops.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-cell">暂无操作审计日志</td></tr>`;
        return;
      }
      tbody.innerHTML = ops
        .map((op) => {
          const okBadge =
            op.status === "ok" || op.status === "upgraded"
              ? "badge-success"
              : op.status === "failed" || op.status === "error"
              ? "badge-danger"
              : "badge-neutral";
          return `
            <tr>
              <td class="mono">${escapeHtml((op.ts || op.created_at || "").replace("T", " "))}</td>
              <td><span class="badge badge-neutral">${escapeHtml(op.category || "")}/${escapeHtml(op.action || "")}</span></td>
              <td>${escapeHtml(op.site_name || "-")}</td>
              <td><span class="badge ${okBadge}">${escapeHtml(op.status || "-")}</span></td>
              <td>${escapeHtml(op.summary || "-")}</td>
            </tr>
          `;
        })
        .join("");
    } catch (err) {
      handleError("加载审计日志失败", err);
    }
  }

  // ------------------------------------------------------------------ Event Wiring
  function bindEvents() {
    // Sidebar nav & mobile toggle
    $("nav-list").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".nav-item");
      if (btn && btn.dataset.view) {
        switchView(btn.dataset.view);
      }
    });
    $("btn-mobile-menu").addEventListener("click", () => $("sidebar").classList.add("open"));
    $("btn-sidebar-close").addEventListener("click", () => $("sidebar").classList.remove("open"));

    // Theme & Refresh
    $("btn-theme-toggle").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
    });
    $("btn-refresh-all").addEventListener("click", refreshAllData);

    // Job dock controls
    $("topbar-job-pill").addEventListener("click", () => {
      $("job-dock").classList.remove("hidden");
      state.dockMinimized = false;
      renderJobDock();
    });
    $("sidebar-job-trigger").addEventListener("click", () => {
      $("job-dock").classList.remove("hidden");
      state.dockMinimized = false;
      renderJobDock();
    });
    $("btn-job-toggle").addEventListener("click", () => {
      state.dockMinimized = !state.dockMinimized;
      renderJobDock();
    });
    $("btn-job-close").addEventListener("click", () => $("job-dock").classList.add("hidden"));
    $("btn-job-cancel").addEventListener("click", async () => {
      if (!state.activeJobId) return;
      try {
        await apiFetch(`/api/jobs/${encodeURIComponent(state.activeJobId)}/cancel`, {
          method: "POST",
          body: {},
        });
        showToast("已请求取消任务", "", "info");
      } catch (err) {
        handleError("取消任务失败", err);
      }
    });
    $("job-dock-history").addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-switch-job]");
      if (!btn) return;
      state.activeJobId = btn.dataset.switchJob;
      renderJobDock();
    });

    // Confirmation & Export modals
    $("confirm-close").addEventListener("click", () => closeConfirm(false));
    $("confirm-cancel").addEventListener("click", () => closeConfirm(false));
    $("confirm-ok").addEventListener("click", () => closeConfirm(true));
    $("confirm-modal").addEventListener("click", (ev) => {
      if (ev.target === $("confirm-modal")) closeConfirm(false);
    });
    $("export-close").addEventListener("click", () => $("export-modal").classList.add("hidden"));
    $("export-close-btn").addEventListener("click", () => $("export-modal").classList.add("hidden"));
    $("btn-export-copy").addEventListener("click", () =>
      copyToClipboard($("export-html-text").value, "换链 HTML 已复制")
    );

    // Dashboard events
    $("dash-group-filter").addEventListener("change", renderDashboardTable);
    $("dash-status-filter").addEventListener("change", renderDashboardTable);
    $("dash-search").addEventListener("input", renderDashboardTable);
    $("dash-select-all").addEventListener("click", () => {
      getFilteredDashboardSites().forEach((s) => state.dashSelected.add(s.name));
      renderDashboardTable();
    });
    $("dash-select-none").addEventListener("click", () => {
      state.dashSelected.clear();
      renderDashboardTable();
    });
    $("dash-master-check").addEventListener("change", (ev) => {
      const checked = ev.target.checked;
      getFilteredDashboardSites().forEach((s) => {
        if (checked) state.dashSelected.add(s.name);
        else state.dashSelected.delete(s.name);
      });
      renderDashboardTable();
    });
    $("dash-table-body").addEventListener("change", (ev) => {
      const cb = ev.target.closest(".dash-row-check");
      if (!cb) return;
      const name = cb.dataset.site;
      if (cb.checked) state.dashSelected.add(name);
      else state.dashSelected.delete(name);
      renderDashboardTable();
    });
    $("dash-table-body").addEventListener("click", (ev) => {
      const jumpBtn = ev.target.closest("[data-dash-jump]");
      if (jumpBtn) {
        jumpAdminSingle(jumpBtn.dataset.dashJump);
        return;
      }
      const flBtn = ev.target.closest("[data-dash-fl]");
      if (flBtn) {
        const sname = flBtn.dataset.dashFl;
        switchView("friendlinks");
        switchFriendlinkTab("single");
        loadSingleSiteFriendlinks(sname);
        return;
      }
      const probeBtn = ev.target.closest("[data-dash-probe]");
      if (probeBtn) {
        handleDashProbeMcp([probeBtn.dataset.dashProbe]);
      }
    });
    $("btn-dash-discover").addEventListener("click", handleDashDiscover);
    $("btn-dash-probe").addEventListener("click", () => handleDashProbeMcp());
    $("btn-dash-batch-jump").addEventListener("click", handleDashBatchJump);

    // Friendlinks subtabs
    document.querySelectorAll("[data-fl-tab]").forEach((btn) => {
      btn.addEventListener("click", () => switchFriendlinkTab(btn.dataset.flTab));
    });

    // 2.1 Single friendlinks
    $("fl-single-site-search").addEventListener("input", populateSingleSiteSelect);
    $("btn-fl-single-load").addEventListener("click", () => loadSingleSiteFriendlinks());
    $("btn-fl-single-jump").addEventListener("click", () => {
      const sname = $("fl-single-site-select").value;
      if (sname) jumpAdminSingle(sname);
    });
    $("btn-fl-single-parse").addEventListener("click", handleSingleParse);
    $("btn-fl-single-add").addEventListener("click", handleSingleAdd);
    $("btn-fl-single-select-all").addEventListener("click", () => {
      state.singleLinks.forEach((l) => state.singleSelected.add(l.link));
      renderSingleLinksTable();
    });
    $("btn-fl-single-select-none").addEventListener("click", () => {
      state.singleSelected.clear();
      renderSingleLinksTable();
    });
    $("fl-single-master-check").addEventListener("change", (ev) => {
      const checked = ev.target.checked;
      state.singleLinks.forEach((l) => {
        if (checked) state.singleSelected.add(l.link);
        else state.singleSelected.delete(l.link);
      });
      renderSingleLinksTable();
    });
    $("fl-single-table-body").addEventListener("change", (ev) => {
      const cb = ev.target.closest(".fl-single-check");
      if (!cb) return;
      if (cb.checked) state.singleSelected.add(cb.dataset.link);
      else state.singleSelected.delete(cb.dataset.link);
      renderSingleLinksTable();
    });
    $("btn-fl-single-remove-selected").addEventListener("click", handleSingleRemoveSelected);

    // 2.2 Batch friendlinks
    $("btn-fl-batch-parse").addEventListener("click", handleBatchParse);
    $("btn-fl-batch-add").addEventListener("click", handleBatchAdd);
    $("btn-fl-batch-remove").addEventListener("click", handleBatchRemove);

    // 2.3 Crosslink
    $("fl-xl-mode").addEventListener("change", () => {
      updateCrosslinkDynamicFields();
      invalidateCrosslinkPreview();
    });
    [
      "fl-xl-k",
      "fl-xl-hub",
      "fl-xl-group-a",
      "fl-xl-group-b",
      "fl-xl-bidirectional",
      "fl-xl-avoid-reciprocal",
      "fl-xl-avoid-root",
      "fl-xl-nofollow",
      "fl-xl-remark",
      "fl-xl-seed",
    ].forEach((id) => {
      $(id).addEventListener("change", invalidateCrosslinkPreview);
      $(id).addEventListener("input", invalidateCrosslinkPreview);
    });
    $("btn-fl-xl-preview").addEventListener("click", handleCrosslinkPreview);
    $("btn-fl-xl-execute").addEventListener("click", handleCrosslinkExecute);
    $("btn-fl-xl-export").addEventListener("click", handleExportHtml);
    $("btn-fl-xl-history-refresh").addEventListener("click", loadCrosslinkHistory);
    $("fl-xl-history-body").addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-xl-rollback]");
      if (btn) handleCrosslinkRollback(btn.dataset.xlRollback);
    });

    // 2.4 Audit
    $("btn-fl-audit-run").addEventListener("click", handleAuditRun);
    $("fl-audit-summary").addEventListener("click", (ev) => {
      const pill = ev.target.closest("[data-audit-filter]");
      if (!pill) return;
      state.auditFilter = pill.dataset.auditFilter;
      renderAuditView();
    });
    $("btn-fl-audit-select-broken").addEventListener("click", () => {
      const items = (state.auditReport && state.auditReport.items) || [];
      items.forEach((it) => {
        if (it.status === "missing" || it.status === "unreachable") {
          state.auditSelected.add(auditRowKey(it));
        }
      });
      renderAuditView();
    });
    $("btn-fl-audit-select-none").addEventListener("click", () => {
      state.auditSelected.clear();
      renderAuditView();
    });
    $("fl-audit-master-check").addEventListener("change", (ev) => {
      const checked = ev.target.checked;
      const items = ((state.auditReport && state.auditReport.items) || []).filter(
        (it) => state.auditFilter === "all" || it.status === state.auditFilter
      );
      items.forEach((it) => {
        if (checked) state.auditSelected.add(auditRowKey(it));
        else state.auditSelected.delete(auditRowKey(it));
      });
      renderAuditView();
    });
    $("fl-audit-table-body").addEventListener("change", (ev) => {
      const cb = ev.target.closest(".fl-audit-check");
      if (!cb) return;
      if (cb.checked) state.auditSelected.add(cb.dataset.key);
      else state.auditSelected.delete(cb.dataset.key);
      renderAuditView();
    });
    $("btn-fl-audit-remove-selected").addEventListener("click", handleAuditRemoveSelected);

    // 2.5 Search
    $("btn-fl-search-run").addEventListener("click", handleSearchRun);
    $("fl-search-query").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") handleSearchRun();
    });
    $("btn-fl-search-select-all").addEventListener("click", () => {
      ((state.searchResults && state.searchResults.matches) || []).forEach((m) =>
        state.searchSelected.add(searchRowKey(m))
      );
      renderSearchView();
    });
    $("btn-fl-search-select-none").addEventListener("click", () => {
      state.searchSelected.clear();
      renderSearchView();
    });
    $("fl-search-master-check").addEventListener("change", (ev) => {
      const checked = ev.target.checked;
      ((state.searchResults && state.searchResults.matches) || []).forEach((m) => {
        if (checked) state.searchSelected.add(searchRowKey(m));
        else state.searchSelected.delete(searchRowKey(m));
      });
      renderSearchView();
    });
    $("fl-search-table-body").addEventListener("change", (ev) => {
      const cb = ev.target.closest(".fl-search-check");
      if (!cb) return;
      if (cb.checked) state.searchSelected.add(cb.dataset.key);
      else state.searchSelected.delete(cb.dataset.key);
      renderSearchView();
    });
    $("btn-fl-search-remove-selected").addEventListener("click", handleSearchRemoveSelected);

    // 3. Upgrade
    $("upg-concurrency").addEventListener("input", () => {
      $("upg-concurrency").dataset.touched = "1";
    });
    $("btn-upg-check").addEventListener("click", handleUpgradeCheck);
    $("btn-upg-probe").addEventListener("click", handleUpgradeProbeMcp);
    $("btn-upg-run").addEventListener("click", handleUpgradeRun);

    // 4. Sites management
    document.querySelectorAll("[data-sites-tab]").forEach((btn) => {
      btn.addEventListener("click", () => switchSitesTab(btn.dataset.sitesTab));
    });
    $("sites-group-filter").addEventListener("change", renderSitesTable);
    $("sites-enabled-filter").addEventListener("change", renderSitesTable);
    $("sites-search").addEventListener("input", renderSitesTable);
    $("btn-site-new").addEventListener("click", () => loadSiteIntoForm(null));
    $("btn-site-reset").addEventListener("click", () => loadSiteIntoForm(null));
    $("sites-table-body").addEventListener("click", (ev) => {
      const row = ev.target.closest("[data-edit-site]");
      if (!row) return;
      const site = state.sites.find((s) => s.name === row.dataset.editSite);
      if (site) loadSiteIntoForm(site);
    });
    $("site-form").addEventListener("submit", handleSiteSave);
    $("btn-site-delete").addEventListener("click", handleSiteDelete);

    $("btn-sites-assign-apply").addEventListener("click", handleBulkAssign);
    $("btn-sites-text-preview").addEventListener("click", () => handleBulkTextUpdate(true));
    $("btn-sites-text-apply").addEventListener("click", () => handleBulkTextUpdate(false));
    $("btn-sites-import-preview").addEventListener("click", () => handleBulkImport(true));
    $("btn-sites-import-confirm").addEventListener("click", () => handleBulkImport(false));

    // 5. Publish
    $("btn-pub-start").addEventListener("click", handlePublishStart);
    $("btn-pub-stop").addEventListener("click", handlePublishStop);
    $("btn-pub-refresh").addEventListener("click", () => {
      loadPublishStatus();
      loadPublishLogs();
    });
    $("btn-pub-logs-refresh").addEventListener("click", loadPublishLogs);
    $("pub-auto-refresh").addEventListener("change", () => loadPublishStatus());

    // 6. Settings & MCP
    $("settings-form").addEventListener("submit", handleSettingsSave);
    $("btn-settings-reload").addEventListener("click", loadSettingsAndMcp);
    $("btn-copy-mcp-http").addEventListener("click", () =>
      copyToClipboard($("mcp-http-json").textContent, "HTTP MCP JSON 已复制")
    );
    $("btn-copy-mcp-stdio").addEventListener("click", () =>
      copyToClipboard($("mcp-stdio-json").textContent, "Stdio MCP JSON 已复制")
    );
    $("audit-log-category").addEventListener("change", loadAuditLogs);
    $("btn-audit-logs-refresh").addEventListener("click", loadAuditLogs);

    // App version pills & self-updater
    const jumpToUpdateAndCheck = () => {
      switchView("settings");
      handleAppUpdateCheck();
    };
    $("topbar-version-pill").addEventListener("click", jumpToUpdateAndCheck);
    $("sidebar-version-pill").addEventListener("click", jumpToUpdateAndCheck);
    $("btn-app-update-check").addEventListener("click", () => handleAppUpdateCheck());
    $("btn-app-update-apply").addEventListener("click", handleAppUpdateApply);
  }

  // ------------------------------------------------------------------ Init
  async function init() {
    initTheme();
    initPickers();
    updateCrosslinkDynamicFields();
    bindEvents();
    loadSiteIntoForm(null);

    await Promise.all([loadHealth(), loadDashboard(), loadSettingsQuiet(), syncExistingJobs()]);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
