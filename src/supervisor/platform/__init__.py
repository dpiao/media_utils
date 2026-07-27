"""Platform-specific single-instance, autostart, and worker lists."""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from supervisor.platform import windows as _impl
elif sys.platform == "darwin":
    from supervisor.platform import macos as _impl
else:
    from supervisor.platform import macos as _impl

acquire_single_instance = _impl.acquire_single_instance
is_autostart_enabled = _impl.is_autostart_enabled
set_autostart = _impl.set_autostart
repair_autostart_if_needed = _impl.repair_autostart_if_needed
get_workers = _impl.get_workers
log_startup_diagnostics = _impl.log_startup_diagnostics

__all__ = [
    "acquire_single_instance",
    "is_autostart_enabled",
    "set_autostart",
    "repair_autostart_if_needed",
    "get_workers",
    "log_startup_diagnostics",
]
