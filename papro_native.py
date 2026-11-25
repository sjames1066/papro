"""PAPro's own window: the same page, shown by Windows' WebView2 inside PAPro's process (pywebview), not in an Edge window.
Anything missing (pywebview, WebView2, a non-Windows system) and PAPro uses the browser window as before."""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("papro")

WEBVIEW2_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_installed() -> bool:
    try:
        import winreg
    except ImportError:
        return False
    for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_ID}"),
                      (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_ID}"),
                      (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_ID}")):
        try:
            with winreg.OpenKey(hive, sub) as key:
                version = winreg.QueryValueEx(key, "pv")[0]
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def available() -> bool:
    if not sys.platform.startswith("win") or os.environ.get("PAPRO_BROWSER_WINDOW"):
        return False
    try:
        import webview  # noqa: F401
    except Exception as exc:
        log.info("PAPro's own window is not available (%s) - using the browser window", exc)
        return False
    if not webview2_installed():
        log.info("Microsoft WebView2 is not installed - using the browser window")
        return False
    return True


class NativeWindow:
    """One window that is hidden and shown, never really closed until PAPro quits."""

    def __init__(self, url: str, title: str, icon: Path | None, storage: Path, size: tuple[int, int], backend) -> None:
        self.url, self.title, self.icon, self.storage, self.size = url, title, icon, storage, size
        self.backend = backend        # closing_action() -> "hide" | "quit"; wants_minimize_to_tray(); saved_window_rect(); remember_window_rect(); quit()
        self.win = None
        self.want_show = False
        self.placed = False
        self.quitting = False
        self._lock = threading.Lock()
        self._tools = None

    # -- running ----------------------------------------------------------------------------------------------------
    def run(self, show: bool) -> bool:
        """Create the window and run the window loop on this (the main) thread until the window is destroyed.
        False if it could not be started at all (the caller then falls back to the browser window)."""
        self.want_show = show or self.want_show
        try:
            import webview

            self.webview = webview
            self.win = webview.create_window(self.title, self.url, width=self.size[0], height=self.size[1], hidden=True,
                                             min_size=(820, 560), text_select=True)
            events = self.win.events
            events.loaded += self._loaded
            events.closing += self._closing
            events.minimized += self._minimized
            self.storage.mkdir(parents=True, exist_ok=True)
            kwargs = {"gui": "edgechromium", "private_mode": False, "storage_path": str(self.storage)}
            if self.icon and self.icon.is_file():
                kwargs["icon"] = str(self.icon)
            threading.Timer(8.0, self._appear).start()   # in case the page's "loaded" never comes
            try:
                webview.start(**kwargs)
            except TypeError:
                kwargs.pop("icon", None)
                webview.start(**kwargs)
            return True
        except Exception:
            log.exception("PAPro's own window could not start")
            return self.placed   # it showed a page before failing: the window did start, so PAPro just ends

    # -- window events (run on the window thread) ---------------------------------------------------------------------
    def _loaded(self) -> None:
        self._appear()

    def _appear(self) -> None:
        """First time only: put the window where it was last time, then show it if it is meant to be shown."""
        with self._lock:
            if self.placed or self.win is None:
                return
            self.placed = True
        try:
            tools = self._window_tools()
            if tools:
                aware, find, get_rect, set_rect, on_screen = tools
                aware()
                saved = self.backend.saved_window_rect()
                hwnd = find(self.title, os.getpid())
                if hwnd and saved and on_screen(saved):
                    set_rect(hwnd, saved)
        except Exception:
            log.exception("Could not restore the window position")
        if self.want_show:
            self._show_now()
        threading.Thread(target=self._watch, daemon=True).start()

    def _closing(self):
        if self.quitting:
            return True
        if self.backend.closing_action() == "hide":
            try:
                self.win.hide()
            except Exception:
                log.exception("Could not hide the window")
            return False
        self.quitting = True
        log.info("The window was closed: quitting (Settings: keep running in the tray is off)")
        threading.Thread(target=self.backend.quit, daemon=True).start()
        return True

    def _minimized(self) -> None:
        if self.backend.wants_minimize_to_tray():
            try:
                self.win.hide()
            except Exception:
                log.exception("Could not hide the window")

    # -- what PAPro asks of it -------------------------------------------------------------------------------------
    def show(self) -> None:
        self.want_show = True
        if self.win is not None and self.placed:
            self._show_now()

    def _show_now(self) -> None:
        try:
            self.win.show()
            self.win.restore()
        except Exception:
            log.exception("Could not show the window")

    def destroy(self) -> None:
        self.quitting = True
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:
            pass

    # -- remembering where it is -----------------------------------------------------------------------------------
    def _window_tools(self):
        if self._tools is None:
            import papro_ui

            self._tools = papro_ui._window_tools() or False
        return self._tools or None

    def _watch(self) -> None:
        """Save the window's position and size (real pixels) once it has stopped moving."""
        try:
            tools = self._window_tools()
            if not tools:
                return
            aware, find, get_rect, set_rect, on_screen = tools
            aware()
            last, written = None, self.backend.saved_window_rect()
            while not self.quitting:
                time.sleep(1.0)
                hwnd = find(self.title, os.getpid())
                rect = get_rect(hwnd) if hwnd else None
                if rect and rect == last and rect != written:
                    self.backend.remember_window_rect(rect)
                    written = rect
                if rect:
                    last = rect
        except Exception:
            log.exception("Window position tracking stopped")
