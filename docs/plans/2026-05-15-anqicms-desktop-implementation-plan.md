# anqicms Scheduling Console Implementation Plan

> **Status: Superseded.** This document is retained only as historical context. The current product does not support batch import, and this plan must not be used to restore it.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refactor the current desktop workbench into a scheduling-first console with a separate configuration center, a fast new-site workflow, and a dedicated multi-time schedule editor.

**Architecture:** Keep the file-driven publishing engine, but extend the config model to support multi-time schedules and enabled state. Replace the article-list-centered main window with a dashboard composed of site status, today's plan, and site details. Move configuration editing into dedicated subwindows for bulk editing and onboarding.

**Tech Stack:** Python standard library, Tkinter/ttk, requests, configparser, threading/queue, pytest, PyInstaller

---

### Task 1: Extend data/config models for schedules and enable flags

**Files:**
- Modify: `models/entities.py`
- Modify: `core/config_service.py`
- Test: `tests/test_entities.py`
- Test: `tests/test_config_service.py`

**Step 1: Write the failing tests**

Cover:
- `SiteConfig` supports `enabled` and `publish_times`
- config load/save round-trips `publish_times`
- config load/save round-trips `enabled`

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_entities.py tests/test_config_service.py -v`
Expected: FAIL.

**Step 3: Write minimal implementation**

Add:
- `enabled: bool`
- `publish_times: list[str]`
- helper parsing for comma-separated time values

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_entities.py tests/test_config_service.py -v`
Expected: PASS.

### Task 2: Add schedule planning service

**Files:**
- Create: `core/schedule_service.py`
- Modify: `models/entities.py`
- Test: `tests/test_schedule_service.py`

**Step 1: Write the failing test**

Cover:
- generating today's plan entries from site schedules
- sorting plan entries by time
- disabled sites excluded from plan
- remaining runs count per site

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schedule_service.py -v`
Expected: FAIL.

**Step 3: Write minimal implementation**

Implement:
- `PlannedRun`
- `build_daily_plan(sites, summaries)`
- `next_run_for_site(site)`

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_schedule_service.py -v`
Expected: PASS.

### Task 3: Add efficient site creation/import helpers

**Files:**
- Create: `core/site_factory.py`
- Test: `tests/test_site_factory.py`

**Step 1: Write the failing test**

Cover:
- clone existing site config with renamed paths
- create site from identifier using auto-derived paths
- batch import from CSV-like rows

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_site_factory.py -v`
Expected: FAIL.

**Step 3: Write minimal implementation**

Implement:
- `derive_site_paths(site_key)`
- `clone_site_config(site, new_name, new_url, new_token)`
- `import_sites_from_rows(rows, template_site=None)`

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_site_factory.py -v`
Expected: PASS.

### Task 4: Refactor main window into scheduling dashboard

**Files:**
- Modify: `ui/main_window.py`
- Modify: `core/site_service.py`
- Test: `tests/test_main_window_smoke.py`

**Step 1: Write the failing test**

Cover:
- main window renders site status, daily plan, and site detail panels
- article list panel no longer exists as the primary center view

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_main_window_smoke.py -v`
Expected: FAIL.

**Step 3: Write minimal implementation**

Replace center panel with:
- daily plan tree
- selected site detail summary

Keep top actions for publishing and refreshing.

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_main_window_smoke.py -v`
Expected: PASS.

### Task 5: Replace config dialog with configuration center

**Files:**
- Create: `ui/config_center.py`
- Modify: `ui/main_window.py`
- Test: `tests/test_config_center_smoke.py`

**Step 1: Write the failing test**

Cover:
- configuration center instantiates
- grid includes editable columns for all key site settings

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_center_smoke.py -v`
Expected: FAIL.

**Step 3: Write minimal implementation**

Build:
- site config table
- save all
- toggle enabled
- open schedule editor
- open new site window

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config_center_smoke.py -v`
Expected: PASS.

### Task 6: Add new-site workflow window

**Files:**
- Create: `ui/new_site_dialog.py`
- Modify: `ui/config_center.py`
- Test: `tests/test_new_site_dialog_smoke.py`

**Step 1: Write the failing test**

Cover:
- window instantiates
- derived paths preview updates
- clone/import mode controls exist

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_new_site_dialog_smoke.py -v`
Expected: FAIL.

**Step 3: Write minimal implementation**

Build:
- mode selector
- site key inputs
- auto-derived path preview
- optional batch import textarea

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_new_site_dialog_smoke.py -v`
Expected: PASS.

### Task 7: Add schedule editor window

**Files:**
- Create: `ui/schedule_editor.py`
- Modify: `ui/config_center.py`
- Test: `tests/test_schedule_editor_smoke.py`

**Step 1: Write the failing test**

Cover:
- window instantiates
- selected site publish times visible
- add/remove time actions exist

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schedule_editor_smoke.py -v`
Expected: FAIL.

**Step 3: Write minimal implementation**

Build:
- per-site schedule list
- add/remove time controls
- preview of today's schedule

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_schedule_editor_smoke.py -v`
Expected: PASS.

### Task 8: Wire dashboard actions to new windows and schedule view

**Files:**
- Modify: `ui/main_window.py`
- Modify: `ui/config_center.py`
- Modify: `core/task_manager.py`
- Test: `tests/test_task_manager.py`
- Test: `tests/test_schedule_service.py`

**Step 1: Write the failing test**

Cover:
- disabled sites excluded from all-site publish command
- daily plan refresh updates after config changes

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_task_manager.py tests/test_schedule_service.py -v`
Expected: FAIL.

**Step 3: Write minimal implementation**

Wire:
- open config center from main window
- open schedule editor from config center
- publish all skips disabled sites
- dashboard refresh rebuilds the daily plan

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_task_manager.py tests/test_schedule_service.py -v`
Expected: PASS.

### Task 9: Refresh packaging inputs if required

**Files:**
- Modify: `anqicms多线程网站发布1.spec`

**Step 1: Review packaging assumptions**

Confirm any new files or resources are included or generated at runtime.

**Step 2: Apply packaging changes**

Keep the spec aligned with the new UI entrypoint and required data files.

### Task 10: Final verification

**Step 1: Run the full test suite**

Run: `python -m pytest tests -v`
Expected: PASS.

**Step 2: Run syntax verification**

Run: `python -m compileall app.py core ui models anqicms多线程网站发布1.py`
Expected: exit code 0.

**Step 3: Run manual desktop launch**

Run: `python app.py`
Expected: dashboard opens with site status, daily plan, and details.

**Step 4: Run packaging verification**

Run: `.\build-release.ps1`
Expected: PASS.
