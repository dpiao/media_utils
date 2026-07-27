"""macOS tray: left-click opens status window; right-click shows menu."""

from __future__ import annotations

import logging
from typing import Callable

import AppKit
import objc

_log = logging.getLogger("mediactl")

_RIGHT_TYPES = {
    getattr(AppKit, "NSEventTypeRightMouseUp", None),
    getattr(AppKit, "NSEventTypeRightMouseDown", None),
    getattr(AppKit, "NSRightMouseUp", None),
    getattr(AppKit, "NSRightMouseDown", None),
}
_RIGHT_TYPES.discard(None)


class _TrayClickTarget(AppKit.NSObject):
    """Button target that distinguishes left vs right click."""

    def initWithIcon_leftHandler_(self, icon, left_handler):  # noqa: N802
        self = objc.super(_TrayClickTarget, self).init()
        if self is None:
            return None
        self._icon = icon
        self._left_handler = left_handler
        return self

    def activate_(self, sender):  # noqa: N802
        icon = self._icon
        status_item = icon._status_item
        event = AppKit.NSApp.currentEvent()
        etype = event.type() if event is not None else None
        if etype in _RIGHT_TYPES:
            icon._update_menu()
            handle = icon._menu_handle
            if handle:
                nsmenu, _callbacks = handle
                _log.info("Tray right-click: showing menu")
                status_item.popUpStatusItemMenu_(nsmenu)
            else:
                _log.warning("Tray right-click: no menu to show")
            # Keep menu detached so next left-click does not auto-open it
            status_item.setMenu_(None)
            return
        _log.info("Tray left-click: opening status window")
        self._left_handler()


def install_left_click_status_right_click_menu(
    icon,
    on_left_click: Callable[[], None],
) -> None:
    """
    pystray on macOS attaches the menu to the status item, so any click opens
    the menu. Detach it and handle clicks ourselves.
    """
    status_item = icon._status_item
    button = status_item.button()

    original_update_menu = icon._update_menu

    def update_menu_detached() -> None:
        original_update_menu()
        # original sets the menu on the status item — detach immediately
        status_item.setMenu_(None)

    icon._update_menu = update_menu_detached  # type: ignore[method-assign]
    update_menu_detached()

    try:
        mask = AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp
        button.sendActionOn_(mask)
    except Exception as exc:
        _log.warning("sendActionOn_ failed: %s", exc)

    target = _TrayClickTarget.alloc().initWithIcon_leftHandler_(icon, on_left_click)
    icon._mediactl_click_target = target  # type: ignore[attr-defined]  # retain
    button.setTarget_(target)
    button.setAction_("activate:")
    _log.info("macOS tray: left-click=status, right-click=menu")
