"""Local Web Console package for AnqiCMS site-group management."""

from __future__ import annotations

from web.job_runner import JobRecord, JobRunner
from web.server import ConfigService, WebConsoleApp, WebConsoleServer, resolve_static_dir

__all__ = [
    "ConfigService",
    "JobRecord",
    "JobRunner",
    "WebConsoleApp",
    "WebConsoleServer",
    "resolve_static_dir",
]
