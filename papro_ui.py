"""
PAPro window: a small local web page shown in its own app-style window (Edge or Chrome in --app mode).

papro.py runs the server inside the tray process. The page only ever talks to 127.0.0.1, every request must carry a
random token, and nothing here needs an extra package. The backend object (see papro.py, UIMixin) supplies:

    ui_snapshot() -> dict        everything the page shows
    ui_action(name, data) -> dict   install / uninstall / update / ...
    ui_logo(icao) -> (bytes, mime)
    ui_icon() -> bytes           the app icon as PNG
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from papro_fonts import INTER

log = logging.getLogger("papro")


def work_area() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the usable desktop on Windows, in the units Chromium's window flags use."""
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    return 0, 0, 1920, 1040


def centered_geometry(width: int = 1040, height: int = 800) -> tuple[int, int, int, int]:
    """Window size (shrunk to fit small screens) and the position that puts it in the middle of the screen."""
    left, top, area_w, area_h = work_area()
    width, height = min(width, area_w - 40), min(height, area_h - 40)
    return left + (area_w - width) // 2, top + (area_h - height) // 2, width, height


def _browser_candidates() -> list[Path]:
    roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    parts = [("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe")]
    return [Path(root, *part) for part in parts for root in roots if root]


def raise_window(title: str = "PAPro", wait: float = 8.0) -> None:
    """Windows: bring the PAPro window to the front of everything. It is found by its title once it has appeared, and
    Windows' rules against stealing focus are got around by a brief 'always on top'. Runs in the background."""
    if not sys.platform.startswith("win"):
        return

    def work() -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, wintypes.UINT]
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            found: list = []
            enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def visit(hwnd, _lparam):
                if user32.IsWindowVisible(hwnd):
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, buf, 256)
                    if buf.value == title:
                        found.append(hwnd)
                return True

            deadline = time.time() + wait
            while time.time() < deadline and not found:
                user32.EnumWindows(enum_type(visit), 0)
                if not found:
                    time.sleep(0.25)
            if not found:
                return
            hwnd = found[0]
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE: also un-minimizes
            user32.keybd_event(0x12, 0, 0, 0)  # a tap of Alt lets this process take the foreground
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)
            flags = 0x0001 | 0x0002 | 0x0040  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
            user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, flags)  # HWND_TOPMOST...
            user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, flags)  # ...then back to normal
            user32.SetForegroundWindow(hwnd)
        except Exception:
            log.exception("Could not bring the window to the front")

    threading.Thread(target=work, daemon=True).start()


CGNAT = ipaddress.ip_network("100.64.0.0/10")   # carrier-grade NAT range: Tailscale and some VPNs use it


class UIServer:
    def __init__(self, backend, profile_dir: Path) -> None:
        self.backend = backend
        self.profile_dir = profile_dir
        self.token = "papro"   # not a secret: a custom header the page sends, so other websites cannot make the browser call the API
        self.httpd: ThreadingHTTPServer | None = None
        self.lan_on = False             # Settings: let other devices on the network / a VPN open PAPro
        self.window: subprocess.Popen | None = None
        self.last_poll = 0.0
        self._lock = threading.Lock()

    # server -------------------------------------------------------------
    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # keep the log file quiet
                pass

            def _host(self) -> str:
                return self.headers.get("Host", "")

            def _host_ok(self) -> bool:
                """The address in the browser must be an address of this PC (localhost or a number like 10.0.0.82 - never a
                made-up name that merely points here), and a request from another device is only answered when it comes from
                a private / VPN network."""
                host, _, port = self._host().rpartition(":")
                if port != str(server.httpd.server_address[1]):
                    return False
                host = host.strip("[]")
                if host != "localhost":
                    try:
                        ipaddress.ip_address(host)
                    except ValueError:
                        return False
                    if host not in ("127.0.0.1", "::1") and not server.lan_on:
                        return False
                try:
                    peer = ipaddress.ip_address(self.client_address[0].split("%")[0])
                except ValueError:
                    return False
                return peer.is_loopback or (server.lan_on and (peer.is_private or peer in CGNAT))

            def _token(self) -> str:
                return self.headers.get("X-Token") or parse_qs(urlparse(self.path).query).get("t", [""])[0]

            def _send(self, code: int, body: bytes, mime: str = "application/json") -> None:
                etag = None
                if mime.startswith("image/"):  # revalidate every time, so a new logo.png shows up straight away
                    etag = '"' + hashlib.md5(body).hexdigest() + '"'
                    if self.headers.get("If-None-Match") == etag:
                        code, body = 304, b""
                self.send_response(code)
                if code != 304:
                    self.send_header("Content-Type", mime)
                if etag:
                    self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache" if etag else "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, data) -> None:
                self._send(code, json.dumps(data).encode("utf-8"))

            def _allowed(self, write: bool = False) -> bool:
                """Reading pages and data: any request to this PC's own address. Changing anything (POST): must come from
                PAPro's own page - a custom header (which a web page on another site cannot send without our consent)
                and, if the browser says where it came from, the same address."""
                ok = self._host_ok()
                if ok and write:
                    origin = self.headers.get("Origin")
                    port = server.httpd.server_address[1]
                    ok = self.headers.get("X-Token") == server.token and (origin is None or origin == "http://" + self._host())
                if ok:
                    return True
                self._json(403, {"error": "forbidden"})
                return False

            def do_GET(self) -> None:  # noqa: N802
                if not self._allowed():
                    return
                parsed = urlparse(self.path)
                path = parsed.path
                try:
                    if path == "/":
                        self._send(200, server.page().encode("utf-8"), "text/html; charset=utf-8")
                    elif path == "/api/state":
                        server.last_poll = time.monotonic()
                        if self.headers.get("X-View") == "studio":   # Pack Studio is on screen: PAPro keeps quiet while someone records
                            server.backend.studio_seen()
                        self._json(200, server.backend.ui_snapshot())
                    elif path.startswith("/logo/"):
                        data, mime = server.backend.ui_logo(path[6:].split(".")[0].upper())
                        self._send(200, data, mime)
                    elif path == "/icon.png":
                        self._send(200, server.backend.ui_icon(), "image/png")
                    elif path == "/studio/logo":  # the pack's own logo, for the preview
                        q = parse_qs(parsed.query)
                        data = server.backend.studio_logo(q.get("icao", [""])[0], q.get("voice", [""])[0])
                        if data is None:
                            self._json(404, {"error": "not found"})
                        else:
                            self._send(200, data, "image/png")
                    elif path == "/test/audio":  # a PA built by the Test tab
                        q = parse_qs(parsed.query)
                        data = server.backend.test_audio(q.get("key", [""])[0], q.get("file", [""])[0])
                        if data is None:
                            self._json(404, {"error": "not found"})
                        else:
                            self._send(200, data, "audio/ogg")
                    elif path == "/studio/audio":  # a processed clip, for the play button
                        q = parse_qs(parsed.query)
                        data = server.backend.studio_audio(q.get("icao", [""])[0], q.get("voice", [""])[0], q.get("slot", [""])[0])
                        if data is None:
                            self._json(404, {"error": "not found"})
                        else:
                            self._send(200, data, "audio/ogg")
                    else:
                        self._json(404, {"error": "not found"})
                except Exception:
                    log.exception("UI request failed: %s", path)
                    self._json(500, {"error": "internal error"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._allowed(write=True):
                    return
                parsed = urlparse(self.path)
                path = parsed.path
                try:
                    if path == "/import/upload":  # a pack zip from this computer, streamed to disk (they can be hundreds of MB)
                        q = parse_qs(parsed.query)
                        size = int(self.headers.get("Content-Length") or 0)
                        if size <= 0 or size > 1024 * 1024 * 1024:
                            self.close_connection = True
                            self._json(413, {"error": "That file is empty or larger than 1 GB."})
                            return
                        try:
                            dest = server.backend.import_stage_path()
                        except Exception as exc:
                            self.close_connection = True
                            self._json(400, {"error": str(exc)})
                            return
                        left = size
                        with open(dest, "wb") as out:
                            while left > 0:
                                chunk = self.rfile.read(min(1 << 20, left))
                                if not chunk:
                                    break
                                out.write(chunk)
                                left -= len(chunk)
                        self._json(200, server.backend.import_local(dest, q.get("file", [""])[0]))
                        return
                    if path == "/logo/upload":  # a logo picked on the dashboard for an installed pack
                        q = parse_qs(parsed.query)
                        size = int(self.headers.get("Content-Length") or 0)
                        if size <= 0 or size > 6 * 1024 * 1024:
                            self.close_connection = True
                            self._json(413, {"error": "That image is empty or too large (5 MB limit)."})
                            return
                        self._json(200, server.backend.library_logo_set(q.get("icao", [""])[0], self.rfile.read(size)))
                        return
                    if path == "/studio/upload":  # raw audio bytes; details in the query string
                        q = parse_qs(parsed.query)
                        size = int(self.headers.get("Content-Length") or 0)
                        if size <= 0 or size > 60 * 1024 * 1024:
                            self.close_connection = True
                            self._json(413, {"error": "That file is empty or too large (60 MB limit)."})
                            return
                        body = self.rfile.read(size)
                        self._json(200, server.backend.studio_upload(
                            q.get("icao", [""])[0], q.get("voice", [""])[0], q.get("slot", [""])[0],
                            q.get("filename", ["take.wav"])[0], body))
                        return
                    length = min(int(self.headers.get("Content-Length") or 0), 65536)
                    data = json.loads(self.rfile.read(length) or b"{}")
                    if not path.startswith("/api/") or not isinstance(data, dict):
                        self._json(404, {"error": "not found"})
                        return
                    self._json(200, server.backend.ui_action(path[5:], data))
                except Exception:
                    log.exception("UI action failed: %s", path)
                    self._json(500, {"error": "internal error"})

        try:   # the same address every time, so the browser remembers "allow microphone" between launches
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 47654), Handler)
        except OSError:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        # "localhost" can mean ::1 (IPv6) instead of 127.0.0.1 for some programs (Electron / Node apps such as EFBx): answer there too
        class Server6(ThreadingHTTPServer):
            address_family = socket.AF_INET6

            def server_bind(self) -> None:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                super().server_bind()

        try:
            self.httpd6 = Server6(("::1", self.httpd.server_address[1]), Handler)
            self.httpd6.daemon_threads = True
            threading.Thread(target=self.httpd6.serve_forever, daemon=True).start()
        except (OSError, AttributeError):
            self.httpd6 = None   # no IPv6 loopback on this PC: IPv4 only, as before
        try:
            self.set_lan(bool(self.backend.lan_enabled()))
        except Exception:
            log.exception("Could not set up network access")

    _ips_cache: tuple = (0.0, [])

    def private_ips(self) -> list[str]:
        """This PC's own network addresses that a phone / tablet / VPN peer could use (192.168.x.x, 10.x.x.x, 172.16-31.x.x,
        100.64-127.x.x ...), including VPN adapters. Re-read at most every 20 seconds."""
        stamp, cached = self._ips_cache
        if time.monotonic() - stamp < 20:
            return cached
        found: set[str] = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                found.add(info[4][0])
        except OSError:
            pass
        try:   # the address used to reach the internet / VPN (nothing is sent)
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("10.255.255.255", 1))
            found.add(probe.getsockname()[0])
            probe.close()
        except OSError:
            pass
        if sys.platform == "win32":   # every adapter, VPN ones included
            try:
                text = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=6,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                found.update(re.findall(r"IPv4[^:\n]*:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})", text))
            except (OSError, subprocess.SubprocessError):
                pass
        ips = []
        for ip in sorted(found):
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if (addr.is_private or addr in CGNAT) and not addr.is_loopback and not addr.is_link_local:
                ips.append(ip)
        self._ips_cache = (time.monotonic(), ips)
        return ips

    def set_lan(self, enabled: bool) -> None:
        """Let other devices open PAPro (EFBx on another PC, or over a VPN). Off: PAPro listens on this PC only. On: it listens on
        every network adapter - including a VPN that connects later - but only answers private / VPN addresses."""
        if self.httpd is None or enabled == self.lan_on:
            return
        port = self.httpd.server_address[1]
        handler = self.httpd.RequestHandlerClass
        old = self.httpd
        old.shutdown()           # the port is free again only once the old listener is closed
        old.server_close()
        try:
            new = ThreadingHTTPServer(("0.0.0.0" if enabled else "127.0.0.1", port), handler)
        except OSError:
            log.warning("Could not switch PAPro's %s listener - keeping the old one", "network" if enabled else "local")
            new = ThreadingHTTPServer(("127.0.0.1" if enabled else "0.0.0.0", port), handler)
            enabled = not enabled
        self.lan_on = enabled
        new.daemon_threads = True
        self.httpd = new
        threading.Thread(target=new.serve_forever, daemon=True).start()
        log.info("Network access %s%s", "on: " if enabled else "off", ", ".join(f"{ip}:{port}" for ip in self.private_ips()) if enabled else "")

    def url(self) -> str:
        return f"http://localhost:{self.httpd.server_address[1]}/"

    def ensure_server(self) -> None:
        """Start the web server if it is not running yet. PAPro does this at startup, so the page and the network address (EFBx,
        an iPad) work when PAPro was started by Windows or MSFS, without anyone opening the window first."""
        with self._lock:
            if self.httpd is None:
                self.start()

    # window -------------------------------------------------------------
    def open_window(self) -> None:
        """Open the PAPro window (or leave the already open one alone)."""
        with self._lock:
            if self.httpd is None:
                self.start()
            if self.window is not None and self.window.poll() is None:
                raise_window()  # already open (maybe behind other windows): bring it forward
                return
            url = self.url()
            for exe in _browser_candidates():
                if exe.is_file():
                    try:
                        self.profile_dir.mkdir(parents=True, exist_ok=True)
                        x, y, w, h = centered_geometry()
                        self.window = subprocess.Popen(
                            [str(exe), f"--app={url}", f"--user-data-dir={self.profile_dir}", f"--window-size={w},{h}",
                             f"--window-position={x},{y}",
                             "--no-first-run", "--no-default-browser-check", "--hide-crash-restore-bubble"],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        raise_window()
                        return
                    except OSError:
                        log.exception("Could not start %s", exe)
            webbrowser.open(url)  # no Edge/Chrome found: the default browser still works

    def close_window(self) -> None:
        """Close the PAPro window (the browser process it runs in, with its helper processes)."""
        proc = self.window
        if proc is None or proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=8,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                proc.terminate()
        except (OSError, subprocess.SubprocessError):
            log.exception("Could not close the window")

    def page(self) -> str:
        faces = "\n".join(
            f"@font-face{{font-family:'Inter';font-weight:{w};font-style:normal;font-display:swap;"
            f"src:url(data:font/woff;base64,{INTER[name]}) format('woff');}}"
            for name, w in (("Regular", 400), ("Medium", 500), ("SemiBold", 600), ("Bold", 700))
        )
        return PAGE.replace("/*FONTS*/", faces).replace("__TOKEN__", self.token)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PAPro</title>
<link rel="icon" href="/icon.png?t=__TOKEN__&v=2">
<style>
/*FONTS*/
:root{
  --bg:#0d0a1a; --surface:#16122a; --surface2:#1e1938; --line:rgba(255,255,255,.08); --line2:rgba(255,255,255,.14);
  --text:#f5f3fc; --muted:#a09bbd; --faint:#6f6a8c;
  --purple:#6b2fd6; --magenta:#ce3a80; --orange:#ff9218;
  --grad:linear-gradient(120deg,#6b2fd6 0%,#ce3a80 55%,#ff9218 100%);
  --ok:#3ddc97; --warn:#ffb454; --err:#ff6b81;
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
html{background:var(--bg)}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;background:radial-gradient(900px 500px at 8% -10%,rgba(107,47,214,.28),transparent 60%),
     radial-gradient(800px 480px at 100% 0%,rgba(255,146,24,.14),transparent 60%)}   /* a fixed layer, not a fixed background: embedded browsers re-stretch those when the page height changes */
body{background:transparent;
     color:var(--text);font:400 14px/1.5 'Inter',system-ui,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased;
     font-feature-settings:'cv11','ss01'}
.wrap{max-width:860px;margin:0 auto;padding:26px 28px 18px;min-height:100vh;display:flex;flex-direction:column}
.verbar{margin-top:auto;padding-top:26px;text-align:center;font-size:12px;font-weight:500;color:var(--faint);letter-spacing:.04em}
header{display:flex;align-items:center;gap:14px;margin-bottom:22px}
header img{width:46px;height:46px;border-radius:12px;box-shadow:0 6px 20px rgba(107,47,214,.45)}
header h1{font-size:21px;font-weight:700;margin:0;letter-spacing:-.02em}
header p{margin:0;color:var(--muted);font-size:12.5px}
.spacer{flex:1}
.updbar{display:flex;align-items:center;gap:14px;padding:12px 16px;margin-bottom:14px;border-color:rgba(255,146,24,.5);background:rgba(255,146,24,.08)}
.updbar>div{flex:1;min-width:0}.updbar b{display:block;font-size:13.5px}.updbar span{color:var(--muted);font-size:12.5px}
.cog{width:38px;height:38px;padding:0;display:inline-flex;align-items:center;justify-content:center;border-radius:12px;background:transparent;border:1px solid var(--line);color:var(--muted);cursor:pointer;transition:.15s}
.cog:hover{color:var(--text);background:rgba(255,255,255,.07);border-color:var(--line2)}
.cog:hover svg{transform:rotate(45deg)}
.cog svg{transition:transform .25s}
.switch{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px;cursor:pointer;user-select:none}
.track{width:62px;height:26px;border-radius:99px;background:rgba(255,107,129,.12);border:1px solid rgba(255,107,129,.4);position:relative;transition:.2s}
.track::before{content:"OFF";position:absolute;top:0;bottom:0;right:11px;display:flex;align-items:center;font-size:10.5px;font-weight:800;letter-spacing:.08em;color:#ff8c9e;transition:.2s}
.track::after{content:"";position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#8a849f;transition:.2s}
.switch.on .track{background:var(--grad);border-color:transparent}
.switch.on .track::before{content:"ON";right:auto;left:12px;color:#fff}
.switch.on .track::after{left:41px;background:#fff}
.card{background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,.015));border:1px solid var(--line);
      border-radius:18px;padding:20px 22px;backdrop-filter:blur(6px)}
.card.tight{margin-top:8px!important;margin-bottom:8px!important;padding:10px 18px}
.card.tight h2.fold{margin:0!important;font-size:16px}.card.tight h2.fold[data-open="1"]{margin-bottom:12px!important}
.label{font-size:11px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--faint);margin:0 0 12px}
.herorow{display:flex;gap:16px;align-items:stretch;margin-bottom:26px}.herorow .hero{flex:1;min-width:0;margin-bottom:0}
.knobbox{flex:none;width:182px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;padding:14px 10px;background:linear-gradient(#1b1c21,#111216);border-color:#2b2d35}
.knobbox .kt{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#9a9ca8;font-weight:600}
.knob{position:relative;width:132px;height:132px;border-radius:50%;cursor:pointer;user-select:none;outline:none;-webkit-tap-highlight-color:transparent}
.knob:focus-visible{box-shadow:0 0 0 3px rgba(255,255,255,.35)}
.kscale{position:absolute;inset:0;width:100%;height:100%}.kscale line{stroke:#6f717c;stroke-width:2;stroke-linecap:round}.kscale line.lit{stroke:#f2f2f4}
.kscale text{fill:#c9cad2;font-size:9px;font-weight:700;text-anchor:middle}
/* a black matte knob with a knurled skirt and a white pointer, like an audio-control-panel volume knob */
.kcap{position:absolute;inset:25px;border-radius:50%;display:flex;align-items:center;justify-content:space-between;padding:0 6px;
  background:radial-gradient(circle at 50% 38%,#2a2b31 0 52%,#16171b 53% 100%);border:2px solid #0a0a0c;
  box-shadow:0 6px 0 #050506,0 10px 14px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.14),inset 0 0 0 5px #1c1d22,inset 0 0 0 6px #34353c;transition:transform .12s,box-shadow .12s}
.kcap::before{content:"";position:absolute;inset:-5px;border-radius:50%;background:repeating-conic-gradient(#25262c 0 4deg,#0e0e11 4deg 8deg);z-index:-1;box-shadow:0 0 0 1px #3a3b42}
.kptr{position:absolute;left:50%;top:6px;width:4px;height:calc(50% - 8px);margin-left:-2px;border-radius:2px;background:#f4f4f6;transform-origin:50% calc(100% + 0px);
  box-shadow:0 0 0 1px rgba(0,0,0,.4);transition:transform .12s,background .2s,opacity .2s}
.kcap i{position:relative;font-style:normal;font-size:18px;color:#5d5f69;line-height:1;opacity:0;transition:opacity .15s}.knob:hover .kcap i{opacity:.9}
.knob.on .kcap{box-shadow:0 6px 0 #050506,0 10px 14px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.14),inset 0 0 0 5px #1c1d22,inset 0 0 0 6px #34353c,0 0 0 2px rgba(255,255,255,.18)}
.knob:not(.on) .kcap{transform:translateY(5px);box-shadow:0 1px 0 #050506,0 3px 6px rgba(0,0,0,.55),inset 0 3px 9px rgba(0,0,0,.6),inset 0 0 0 5px #17181c,inset 0 0 0 6px #2a2b31}
.knob:not(.on) .kptr{background:#8b8d98;opacity:.7}
.knobbox .kpa{font-size:21px;font-weight:700;letter-spacing:.16em;color:#f2f2f4;line-height:1;margin-top:4px;transition:color .2s,opacity .2s}
.knobbox:has(.knob:not(.on)) .kpa{color:#7b7d88}
.knobbox .kv{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums;color:#e8e8ec}.knobbox .kst{font-size:12px;color:#8e909c}
@media(max-width:760px){.herorow{flex-direction:column}.knobbox{width:auto;flex-direction:row;gap:16px}}
.hero.ok{border-color:rgba(80,200,140,.65);background:linear-gradient(rgba(80,200,140,.10),rgba(80,200,140,.04));box-shadow:0 0 0 1px rgba(80,200,140,.25),0 8px 30px rgba(80,200,140,.10)}
.hero{display:flex;align-items:center;gap:20px;margin-bottom:26px}
.hero .logo{width:68px;height:68px;border-radius:16px;flex:none;object-fit:cover;background:var(--surface2)}
.route{font-size:34px;font-weight:700;letter-spacing:-.03em;line-height:1.1}
.route .arrow{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;margin:0 6px}
.dest-name{color:var(--muted);margin-top:2px}
.h-ids{font-size:13px;font-weight:600;color:var(--muted);letter-spacing:.02em;margin-bottom:3px}
.h-name{font-size:26px;font-weight:700;letter-spacing:-.02em;line-height:1.15}
.h-ac{display:inline-flex;align-items:center;gap:8px;margin-top:10px;padding:4px 12px 4px 10px;border-radius:999px;font-size:12.5px;font-weight:600;max-width:100%}
.h-ac i{width:8px;height:8px;border-radius:50%;background:currentColor;flex:none}.h-ac span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.h-phase{margin-top:12px;max-width:340px}.h-ptop{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px;font-size:12.5px}.h-ptop b{font-size:15px;white-space:nowrap}.h-plab{white-space:nowrap;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-size:10.5px;font-weight:700}.h-psrc{color:var(--muted);font-size:11px;margin-left:auto}
.h-pbar{display:flex;gap:3px;margin-top:6px}.h-pbar i{flex:1;height:4px;border-radius:2px;background:rgba(160,166,178,.22)}.h-pbar i.done{background:rgba(61,220,151,.45)}.h-pbar i.now{background:var(--ok);box-shadow:0 0 6px rgba(61,220,151,.6)}
.h-ac.ok{background:rgba(61,220,151,.14);color:var(--ok)}.h-ac.warn{background:rgba(255,180,84,.15);color:var(--warn)}
.h-dest{font-size:15px;color:var(--muted);margin-top:4px}.h-dest b{color:var(--text);font-weight:600}.h-dest b.h-orig{color:var(--muted)}.h-arrow{color:var(--magenta);font-weight:700;white-space:pre}
.h-nopack{font-size:15px;color:var(--muted);line-height:1.4}.h-nopack a{color:var(--text);font-weight:600;text-decoration:underline}
.msg{margin-top:10px;font-size:13px;color:var(--muted)}
.msg b{color:var(--text);font-weight:600}
.hero .company{font-size:15px;font-weight:600;color:var(--text);margin-bottom:2px}
.hero .company .ids{font-weight:500;color:var(--muted);letter-spacing:.02em}
.hero .company .nm + .ids{margin-left:12px;padding-left:12px;border-left:1px solid var(--line2)}
.hero .side .credit a{color:var(--muted);text-decoration:none}
.hero .side .credit a.who{color:inherit;text-decoration:underline;text-decoration-color:rgba(160,166,178,.5);text-underline-offset:3px;transition:color .15s,text-decoration-color .15s}
.hero .side .credit a.who:hover{color:#d4b66a;text-decoration-color:rgba(212,182,106,.8)}
.hero .side .desc{font-style:italic;max-width:300px}
.voicerow{display:flex;align-items:center;gap:10px}
select.voice{font:600 13px 'Inter',sans-serif;color:var(--text);background:var(--surface2);border:1px solid var(--line2);border-radius:11px;padding:9px 12px;cursor:pointer;color-scheme:dark}
select.voice:disabled{opacity:.45}
.hero .side{margin-left:auto;display:flex;flex-direction:column;align-items:flex-end;gap:8px;text-align:right}
.small{font-size:12px;color:var(--faint)}
button{font:600 13px 'Inter',sans-serif;color:var(--text);background:var(--surface2);border:1px solid var(--line2);
       border-radius:11px;padding:9px 16px;cursor:pointer;transition:.15s}
button:hover{border-color:rgba(255,255,255,.3);background:#262047}
button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--grad);border-color:transparent;color:#fff;box-shadow:0 6px 18px rgba(206,58,128,.3)}
button.primary:hover{filter:brightness(1.1)}
button.ghost{background:transparent}
button.save{background:transparent;border-color:var(--ok);color:var(--ok)}
button.save:hover{background:rgba(61,220,151,.14);border-color:var(--ok)}
button.save.done{background:var(--ok);border-color:var(--ok);color:#06231a}
button.save.done:hover{filter:brightness(1.08)}
button.danger:hover{border-color:var(--err);color:var(--err)}
input[type=text]{font:500 14px 'Inter',sans-serif;color:var(--text);background:var(--bg);border:1px solid var(--line2);
       border-radius:11px;padding:10px 13px;width:100%;outline:none}
input[type=text]:focus{border-color:var(--magenta)}
.tlab select,.tlab input{height:44px;box-sizing:border-box}
.tlab{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--faint);position:relative}
#t-spoken{position:absolute;left:2px;top:100%;margin-top:4px;white-space:nowrap;color:var(--magenta);font-size:11.5px}
#t-build.go{background:linear-gradient(#35c47a,#22a763);color:#fff;border:1px solid #1d8f55;font-weight:700;font-size:15px;padding:0 30px;height:44px;border-radius:12px;cursor:pointer;box-shadow:0 4px 14px rgba(53,196,122,.28)}
#t-build.go:hover{filter:brightness(1.08)}#t-build.go:disabled{opacity:.5;cursor:default}
.tvol{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--faint);font-size:12px}.tvol input{width:130px;accent-color:var(--magenta);cursor:pointer}.tvol span{min-width:34px;text-align:right;font-variant-numeric:tabular-nums}
#t-all{font-size:18px;font-weight:700;padding:16px 44px;border-radius:16px;min-width:230px}
#t-all.running{background:var(--surface2);border-color:var(--line2);box-shadow:none}
#t-all:disabled{opacity:.5}
#t-now{color:var(--text);font-size:14px}
.tt{font:600 12.5px 'Inter',sans-serif;color:var(--faint);min-width:38px;font-variant-numeric:tabular-nums}
#t-seek{accent-color:var(--magenta);cursor:pointer;height:6px}
.tmsg{padding:18px 4px;color:var(--faint);font-size:14px}
.trow.now{background:rgba(206,58,128,.14)}.trow.now .tn{font-weight:600}
.trow{display:flex;align-items:center;gap:12px;padding:9px 12px;border-bottom:1px solid var(--line)}
.trow:last-child{border-bottom:0}
.trow button{width:34px;height:34px;border-radius:50%;padding:0}
.trow .tn{font-size:13.5px}.trow .tf{margin-left:auto;color:var(--faint);font-size:11.5px}
.section-head{display:flex;align-items:baseline;gap:10px;margin:0 2px 12px}
.section-head h2{margin:0;font-size:15px;font-weight:600}
.section-head span{color:var(--faint);font-size:12.5px}
.tsticky{position:sticky;top:0;z-index:20;margin:16px -14px 0;padding:12px 14px;background:rgba(13,10,26,.92);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
#t-bar{margin-top:12px}
#t-list .section-head{margin:30px 2px 12px}
#t-list>.section-head:first-child,#t-list>.small+.section-head{margin-top:6px}
.packs{display:grid;grid-template-columns:1fr;gap:12px}
.pack{padding:16px 20px}
.pack .top{display:flex;align-items:center;gap:16px}
.lw{position:relative;display:inline-block;flex:none;line-height:0}
.lw .ed{position:absolute;inset:0;width:100%;height:100%;padding:0;border:0;border-radius:inherit;background:rgba(10,8,20,.62);color:#fff;display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .15s;cursor:pointer}
.lw:hover .ed,.lw .ed:focus-visible{opacity:1}
.pack .lw{border-radius:14px}.hero .lw{border-radius:16px}
.pack .logo{width:56px;height:56px;border-radius:14px;flex:none;object-fit:cover;background:var(--surface2)}
.pack .name{font-weight:600;font-size:15px;letter-spacing:-.01em}
.pack .meta{color:var(--muted);font-size:12.5px}
.chip{display:inline-block;font-size:11px;font-weight:600;padding:2px 9px;border-radius:99px;margin-left:8px;vertical-align:1px}
.chip.ok{background:rgba(61,220,151,.14);color:var(--ok)}
.chip.up{background:rgba(255,180,84,.15);color:var(--warn)}
.pack .act{margin-left:auto;display:flex;gap:8px;align-items:center}
.pack .live{width:340px}
.bar{height:8px;border-radius:99px;background:rgba(255,255,255,.08);overflow:hidden}
.bar i{display:block;height:100%;width:0;border-radius:99px;background:var(--grad);transition:width .35s ease}
.bar.indet i{width:38%!important;animation:slide 1.1s ease-in-out infinite}
@keyframes slide{0%{margin-left:-40%}100%{margin-left:102%}}
.prog{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:7px}
.prog b{color:var(--text);font-weight:600}
.err{color:var(--err);font-size:12.5px}
.empty{color:var(--muted);padding:26px;text-align:center;border:1px dashed var(--line2);border-radius:16px}
footer{margin-top:28px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;color:var(--faint);font-size:12.5px}
button.kofi{margin-left:6px;background:rgba(255,146,24,.12);border-color:rgba(255,146,24,.45);color:#ffb454!important;font-weight:600!important}
button.kofi span{color:#ff6b81;margin-right:4px}
button.kofi:hover{background:rgba(255,146,24,.22)}
  .support{margin-top:30px;text-align:center;color:var(--faint);font-size:12.5px}
footer button{padding:7px 12px;font-weight:500;font-size:12.5px;color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;min-height:34px}
.chip2{display:inline-flex;align-items:center;gap:8px;padding:6px 8px 6px 14px;border-radius:99px;background:var(--surface2);border:1px solid var(--line2);font-weight:600}
.chip2 .to{color:var(--faint);font-weight:500}
.chip2 button{padding:0;width:22px;height:22px;border-radius:50%;line-height:1;font-size:15px;background:transparent;border-color:transparent;color:var(--muted)}
.chip2 button:hover{background:rgba(255,107,129,.18);color:var(--err)}
.addrow{display:flex;align-items:center;gap:10px}
.addrow input{width:96px;text-transform:uppercase;text-align:center;letter-spacing:.06em;font-weight:600}
.facts{display:grid;gap:10px}
.facts div{background:var(--surface2);border:1px solid var(--line);border-radius:12px;padding:11px 14px;display:flex;flex-direction:column;gap:2px}
.facts b{font-weight:600}
.facts span{font:500 12px ui-monospace,Consolas,monospace;color:#d9b8ff;word-break:break-all}
.facts i{font-style:normal;color:var(--muted);font-size:12.5px}
.setting{margin-bottom:18px}
.setting .label{margin-bottom:8px}
.radios{display:grid;gap:8px}
.radio{padding:11px 14px;border-radius:12px;border:1px solid var(--line2);background:var(--surface2);cursor:pointer;display:flex;flex-direction:column;gap:1px}
.radio b{font-weight:600}
.radio span{color:var(--muted);font-size:12.5px}
.radio.sel{border-color:transparent;background:linear-gradient(var(--surface2),var(--surface2)) padding-box,var(--grad) border-box;border:1px solid transparent;box-shadow:0 0 0 1px rgba(206,58,128,.35)}
.modal{position:fixed;inset:0;background:rgba(5,3,14,.72);backdrop-filter:blur(5px);display:none;align-items:center;justify-content:center;z-index:5}
.modal.show{display:flex}
#m-settings .row{position:sticky;bottom:-26px;background:var(--surface);padding:12px 0 2px}
.dialog{max-height:92vh;overflow-y:auto;background:var(--surface);border:1px solid var(--line2);border-radius:20px;padding:26px;width:440px;max-width:92vw;
        box-shadow:0 30px 80px rgba(0,0,0,.6)}
.dialog h3{margin:0 0 4px;font-size:18px;letter-spacing:-.02em}
.dialog p{margin:0 0 16px;color:var(--muted)}
.opts{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.opt{padding:14px;border-radius:13px;border:1px solid var(--line2);background:var(--surface2);cursor:pointer;font-weight:600;text-align:center}
.opt.sel{border-color:transparent;background:var(--grad);color:#fff}
.check{display:flex;gap:9px;align-items:center;color:var(--muted);margin-bottom:18px;cursor:pointer;user-select:none}
.row{display:flex;gap:10px;justify-content:flex-end}
.tabs{display:flex;gap:6px;margin:0 0 18px}
.tabs button{background:transparent;border-color:transparent;color:var(--muted);padding:8px 16px;border-radius:999px}
.tabs button.on{background:var(--surface2);border-color:var(--line2);color:var(--text)}
details.master{border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.02);margin:14px 0}
details.master>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:12px;padding:14px 16px;font-weight:700;font-size:15px}
details.master>summary::-webkit-details-marker{display:none}
details.master>summary:before{content:"▸";color:var(--muted);transition:transform .15s}
details.master[open]>summary:before{transform:rotate(90deg)}
details.master .pill{font-size:11px;font-weight:600;padding:2px 9px;border-radius:99px}
details.master .pill.p{background:rgba(206,58,128,.18);color:#ff8fc0}details.master .pill.s{background:rgba(107,47,214,.25);color:#b9a2ff}
details.master .count{margin-left:auto;font-weight:600;font-size:12.5px;color:var(--muted)}
details.master .inner{padding:0 14px 14px}
.langbox{border:1px solid var(--line);border-radius:14px;padding:12px;background:rgba(255,255,255,.02)}.langbox.req{border-color:rgba(206,58,128,.55)}
.langbox b{display:block;margin-bottom:6px;font-size:13px;color:var(--text)}.langbox select{max-width:none!important;width:100%}
.sd-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.sd-grid label{display:flex;flex-direction:column;gap:4px;color:var(--faint);font-size:12px}
.sd-grid label.wide{grid-column:1/-1}
.sd-grid input{width:100%;box-sizing:border-box}
.sd-meta{color:var(--muted);font-size:12.5px;margin-top:12px;line-height:1.5}
.ptrack{height:8px;border-radius:99px;background:var(--surface2);overflow:hidden;margin:6px 0 4px}
.ptrack i{display:block;height:100%;background:var(--grad)}
.keepopen{margin:14px 0 0;padding:12px 14px;border:1px solid rgba(240,200,60,.55);background:rgba(240,200,60,.09);border-radius:12px}
.keepopen.slim{margin:12px 0 22px;padding:9px 38px 9px 14px;position:relative}
.keepx{position:absolute;top:6px;right:8px;width:24px;height:24px;padding:0;border:none;background:transparent;color:var(--faint);font-size:20px;line-height:1;cursor:pointer;border-radius:6px}
.keepx:hover{color:var(--text);background:rgba(255,255,255,.08)}
.keepno{display:block;margin-top:6px;font-size:12px;color:var(--faint);cursor:pointer}
.keepno input{margin-right:6px;vertical-align:-2px}
.keepopen.slim b{display:inline;margin:0 8px 0 0}
.keepopen b{display:block;color:#f0c83c;margin-bottom:4px}
.keepopen span{color:var(--muted);font-size:13px;line-height:1.45}
.logorow{display:flex;align-items:center;gap:14px;margin-top:16px;flex-wrap:wrap}
.logorow > div{flex:1;min-width:200px}
.logoimg{width:56px;height:56px;border-radius:14px;object-fit:cover;background:var(--surface2)}
details.grp{margin:14px 0}
details.grp summary{cursor:pointer;font-weight:600;font-size:14px;padding:6px 2px;color:var(--text)}
details.grp.sub{margin:6px 0 6px 16px;padding-left:10px;border-left:2px solid var(--line,#ddd)}
details.grp.sub summary{font-weight:500;font-size:13.5px}
details.grp summary span{color:var(--faint);font-weight:500;font-size:12.5px;margin-left:8px}
.slot{display:grid;grid-template-columns:1fr auto;gap:2px 12px;align-items:center;padding:9px 12px;margin:6px 0;border:1px solid var(--line);border-radius:12px;background:var(--surface2)}
.seekrow{grid-column:1/-1;display:flex;align-items:center;gap:10px;margin-top:6px}.seekrow input[type=range]{flex:1;accent-color:var(--magenta);cursor:pointer;height:6px}.seekrow .tt{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;min-width:30px}.seekrow .tt:last-child{text-align:right}
.slot.done{border-color:rgba(80,200,140,.35)}
.slot.skipped{border-color:rgba(240,200,60,.6);background:rgba(240,200,60,.09)}
.slot .nm2 em.sk{color:#f0c83c}
.slot .nm2{font-weight:600}
.slot .nm2 em{font-style:normal;color:var(--err);margin-left:6px;font-size:11px}
.slot .sc{grid-column:1;color:var(--faint);font-size:12.5px}
.slot .st{grid-column:1;color:var(--ok,#7ee0a8);font-size:12px}
.slot .act{grid-column:2;grid-row:1/4;display:flex;gap:6px;align-items:center}
.slot .act button{padding:6px 11px}
.slot .act button.danger.arm,button.danger.arm{background:#e5304a;border-color:#e5304a;color:#fff;font-weight:600}
.slot .act button.sq{width:32px;height:32px;padding:0;display:inline-flex;align-items:center;justify-content:center;border-radius:8px}
.slot .sc.custom{white-space:pre-wrap}
.slot .sc.editable{cursor:text;border-radius:6px}
.slot .sc.editable:hover{background:rgba(255,255,255,.05)}
.slot.reading .sc.editable{cursor:default}
textarea.sc-edit{width:100%;box-sizing:border-box;resize:none;overflow:hidden;font:500 13px 'Inter',sans-serif;color:var(--text);background:var(--bg);border:1px solid var(--magenta);border-radius:8px;padding:7px 9px;outline:none}
.slot .act button.rec{background:rgba(255,107,129,.16);border-color:rgba(255,107,129,.5)}
.slot .act button.live{background:var(--err);color:#fff;border-color:transparent;min-width:112px;font-variant-numeric:tabular-nums}
#view-studio{overflow-anchor:none}
.addline{display:flex;gap:8px;margin:8px 0}
.addline input,.addline textarea{flex:1;font:inherit}
#t-ac.bad{border-color:var(--err);box-shadow:0 0 0 2px rgba(255,107,129,.25)}
.card h2.fold{cursor:pointer;user-select:none;display:flex;align-items:center;gap:8px}.card h2.fold::before{content:"▾";font-size:15px;color:var(--muted);transition:transform .15s}.card h2.fold[data-open="0"]::before{transform:rotate(-90deg)}
.volwrap{position:relative;margin:30px 4px 4px;padding-top:6px}.volwrap input[type=range]{width:100%;margin:0}
.volmark{position:absolute;top:-4px;width:0;height:30px;border-left:2px solid var(--warn);pointer-events:none}.volmark span{position:absolute;top:-17px;left:0;transform:translateX(-50%);font-size:11px;font-weight:600;color:var(--warn);white-space:nowrap}
.typbtn{margin-left:10px;padding:3px 10px;font-size:12px;border-radius:999px;background:rgba(107,47,214,.16);color:var(--text);border:1px solid var(--line)}
.typpop{position:fixed;z-index:60;background:var(--surface,#1c1c26);border:1px solid var(--line);border-radius:12px;padding:10px;box-shadow:0 12px 36px rgba(0,0,0,.45);width:250px;max-height:340px;overflow:auto}
.typpop label{display:flex;align-items:center;gap:8px;padding:3px 4px;font-size:13px;cursor:pointer}.typpop .tg{display:grid;grid-template-columns:1fr 1fr;gap:0 6px}
.typpop input[type=text]{width:100%;margin-top:8px}.typpop hr{border:0;border-top:1px solid var(--line);margin:6px 0}
.chiprow{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.sdbar{position:sticky;bottom:0;background:linear-gradient(transparent,var(--bg,#0d0a1f) 30%);padding:18px 0 4px;display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap}
.meter{height:14px;border-radius:99px;background:var(--surface2);overflow:hidden;position:relative;margin:8px 0 4px}
.meter i{position:absolute;left:0;top:0;bottom:0;width:0;background:linear-gradient(90deg,#4cd08a 0 70%,#f0c040 70% 90%,#ff6b81 90%);transition:width .06s}
.meter b{position:absolute;top:0;bottom:0;width:2px;background:#fff;left:0}
.slot.reading{border-color:var(--magenta);background:linear-gradient(var(--surface2),var(--surface2)) padding-box}
.slot.reading .sc{font-size:20px;line-height:1.35;color:var(--text);padding:6px 0}
.slot .sc.custom::before{content:"script · ";color:var(--magenta);font-size:12px}
#m-mic select{width:100%}
#view-studio>.section-head:not(:first-child){margin-top:30px}
.ptrack.wait i{width:40%!important;animation:slide 1.1s ease-in-out infinite alternate}
@keyframes slide{from{margin-left:0}to{margin-left:60%}}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--surface2);border:1px solid var(--line2);
       padding:10px 16px;border-radius:12px;opacity:0;transition:.25s;pointer-events:none}
.toast.show{opacity:1}
body.remote .pc-only{display:none!important}body.remote .remote-note{display:block!important}#m-settings .dialog{max-width:calc(100vw - 24px);max-height:calc(100vh - 24px);overflow-y:auto}html{-webkit-touch-callout:none;-webkit-tap-highlight-color:transparent}body{-webkit-user-select:none;user-select:none}input,textarea,select,[contenteditable]{-webkit-user-select:text;user-select:text}img,a{-webkit-user-drag:none;user-drag:none}.skipmenu{position:fixed;z-index:60;background:var(--surface2,#1c1738);border:1px solid var(--line,#2b2550);border-radius:12px;padding:6px;box-shadow:0 12px 30px rgba(0,0,0,.45);min-width:250px}.skipmenu button{display:block;width:100%;text-align:left;background:none;border:0;color:var(--text);padding:9px 12px;border-radius:8px;font:inherit;font-weight:600;cursor:pointer}.skipmenu button:hover{background:rgba(255,255,255,.07)}.skipmenu small{display:block;font-weight:400;color:var(--muted);font-size:12px;margin-top:2px}</style>
</head>
<body>
<script>try{var h=location.hostname;if(h&&h!=="localhost"&&h!=="127.0.0.1"&&h!=="[::1]"&&h!=="::1")document.body.classList.add("remote")}catch(e){}</script>
<div class="wrap">
  <header>
    <img src="/icon.png?t=__TOKEN__&v=2" alt="">
    <div><h1>PAPro</h1><p>Cabin announcements for MSFS</p></div>
    <div class="spacer"></div>
    <div class="switch" id="auto"><span>Active</span><div class="track"></div></div>
    <button class="cog" id="settings" title="Settings" aria-label="Settings"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.65 8.9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.01A1.7 1.7 0 0 0 10 3.09V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.01A1.7 1.7 0 0 0 20.91 10H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1z"/></svg></button>
  </header>

  <nav class="tabs"><button id="tab-main" class="on">Dashboard</button><button id="tab-studio">Pack Studio</button><button id="tab-dl">Download Packs</button><button id="tab-test">Test</button></nav>
  <div id="view-main">
  <div class="card updbar" id="updbar" style="display:none"><div><b id="upd-t"></b><span id="upd-s"></span></div><button class="primary" id="upd-go">Download</button></div>
  <div class="herorow"><section class="card hero" id="hero"></section>
    <aside class="card knobbox" id="knobbox" style="display:none"><div class="kt">Captain's PA</div>
      <div class="knob on" id="knob" tabindex="0" role="slider" aria-label="PA volume" aria-valuemin="0" aria-valuemax="100" title="Click the middle to mute / unmute. Click the left or right side, or scroll, to change the volume.">
        <svg class="kscale" viewBox="0 0 140 140" id="kscale"></svg>
        <div class="kcap"><div class="kptr" id="kptr"></div><i class="kl">&#8249;</i><i class="kr">&#8250;</i></div></div>
      <div class="kpa" id="kpa">PA</div><div class="kv" id="kvol">70%</div><div class="kst" id="kst">PAs audible</div></aside></div>
  <div class="keepopen slim" id="keep-note"><button class="keepx" id="keep-x" title="Close this reminder" aria-label="Close this reminder">&times;</button><b>Keep PAPro running during your flight</b><span>It builds the landing announcement with the real local time once you have landed. Leave it in the tray until you have parked.</span>
    <label class="keepno"><input type="checkbox" id="keep-never"> Don't remind me again</label></div>

  <div class="section-head"><h2>Installed airline packs</h2><span id="packsub"></span></div>
  <div class="packs" id="packs"></div>

  <footer>
    <button class="ghost" id="aliases-btn">Airline aliases</button>
  </footer>
  </div>
  <div id="view-dl" style="display:none">
    <div class="section-head"><h2>Download packs</h2><span id="dlsub"></span><span style="margin-left:auto;display:flex;gap:10px"><button class="ghost" id="refresh">Refresh list</button><button class="ghost" id="import-btn">Import a pack</button></span></div>
    <div class="packs" id="dlpacks"></div>
  </div>
  <div id="view-test" style="display:none">
    <div class="section-head"><h2>Test your packs</h2><span id="t-sub">Builds the full set of PAs for a flight in a scratch folder, so nothing in the sim changes.</span></div>
    <div class="card" style="padding:16px 16px 30px;display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end">
      <label class="tlab">Pack<select class="voice" id="t-air"></select></label>
      <label class="tlab">Voice<select class="voice" id="t-voice"></select></label>
      <label class="tlab">Destination<select class="voice" id="t-dest"></select></label>
      <label class="tlab">Aircraft type<input type="text" id="t-ac" list="t-aclist" maxlength="4" placeholder="Any" autocomplete="off" style="width:130px;text-transform:uppercase" title="ICAO aircraft type to build for, e.g. A320. Empty = the normal PAs. Packs can have PAs that only play on some types."><datalist id="t-aclist"></datalist></label>
      <label class="tlab">Local arrival time<input type="text" id="t-time" value="14:35" style="width:90px"><span id="t-spoken"></span></label>
      <button class="ghost" id="t-flight" title="Fill in everything from your current SimBrief flight (airline, destination, aircraft, arrival time, flight number, origin) and build it" style="padding:14px 18px">Test Current Flight</button>
      <button class="go" id="t-build" title="Build these announcements again, re-rolling the variations (Enter does the same)">Go</button>
    </div>
    <div class="tsticky">
    <div style="display:flex;align-items:center;gap:16px">
      <button class="primary" id="t-all">&#9654;  Test All</button><button class="ghost" id="t-skip" style="display:none;padding:14px 22px">⏭  Skip</button><button class="ghost" id="t-stop" style="display:none;padding:14px 22px">■  Stop</button><span id="t-now"></span>
      <label class="tvol" title="How loud the test playback is (this page only; your cabin PA settings are not changed)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a10 10 0 0 1 0 14"/></svg><input type="range" id="t-vol" min="0" max="100" value="100" step="1"><span id="t-volv">100%</span></label>
    </div>
    <div id="t-bar" style="display:none;align-items:center;gap:12px">
      <span id="t-cur" class="tt">0:00</span><input type="range" id="t-seek" min="0" max="1000" value="0" step="1" style="flex:1"><span id="t-dur" class="tt">0:00</span>
    </div>
    </div>
    <div id="t-list" style="margin-top:18px"></div>
  </div>
  <div id="view-studio" style="display:none"></div>
  <div class="support">Enjoying PAPro? <button class="kofi" data-open="kofi"><span>&#9829;</span> Buy me a Ko-fi</button></div>
  <div class="verbar" id="ver"></div>
</div>

<div class="modal" id="m-choice"><div class="dialog">
  <h3 id="c-title"></h3><p id="c-text"></p>
  <div class="opts" id="c-opts"></div>
  <div class="row"><button class="primary" id="c-ok">Use this one</button></div>
</div></div>

<div class="modal" id="m-name"><div class="dialog">
  <h3>SimBrief username</h3>
  <p>The name you log in to SimBrief with (not the numeric ID). PAPro reads your latest flight plan from it.</p>
  <input type="text" id="n-input" placeholder="e.g. captainsmith123" autocomplete="off" spellcheck="false">
  <div class="row" style="margin-top:18px"><button class="ghost" id="n-cancel">Cancel</button><button class="primary" id="n-ok">Save</button></div>
</div>
</div>

<div class="modal" id="m-welcome"><div class="dialog" style="width:580px;max-height:92vh;overflow:auto">
  <h3>Welcome to PAPro</h3>
  <p>PAPro swaps the cabin announcements in your Fenix A320 for ones that match your flight: the right airline, voice and destination, with the local time read live from the sim.</p>
  <div class="facts">
    <div><b>1 &middot; Tell PAPro who you are on SimBrief</b><i>Enter your SimBrief username below. PAPro reads your latest flight plan (airline and destination) from SimBrief, and that is what powers everything: it decides which recording to use. It only reads your plan. It never changes anything on SimBrief and sends nothing else anywhere.</i></div>
    <div><b>2 &middot; Get some announcements</b><i>Install an airline pack from the Packs list on the main screen, or record your own in the Pack Studio tab.</i></div>
    <div><b>3 &middot; Plan and fly</b><i>Generate a flight in SimBrief as usual. PAPro updates your announcements by itself when you start MSFS, or any time you press Refresh.</i></div>
  </div>
  <div class="keepopen"><b>Keep PAPro running for the whole flight</b><span>PAPro prepares your announcements as the flight goes on, including building the landing announcement with the real local time once you have landed. If you quit it (or close the window while "quit when I close the window" is on in Settings), those announcements will not be updated. Leave it running in the system tray from before boarding until you have parked.</span></div>
  <label class="small" for="w-name" style="display:block;margin:16px 0 6px;font-weight:600;color:var(--text)">Your SimBrief username (the name you log in with, not the numeric ID)</label>
  <input type="text" id="w-name" placeholder="e.g. captainsmith123" autocomplete="off" spellcheck="false">
  <div class="small" style="margin:18px 0 6px"><b>What PAPro changes on this PC</b></div>
  <div class="facts">
    <div><b>Your Fenix airline packs</b><span id="w-fenix"></span><i>The welcome-aboard, arrival and refuelling announcements in these folders are replaced with the recording for your destination. Nothing else is touched.</i></div>
    <div><b>Your originals are kept</b><span id="w-orig"></span><i>The first time PAPro changes a pack it saves a copy of the original files. Restore them any time from the tray icon menu &rarr; Restore original announcements.</i></div>
    <div><b>Your recordings</b><span id="w-rec"></span><i>Airline packs you install, and your own recordings, are stored here.</i></div>
  </div>
  <p class="small" style="margin:14px 0 18px">Closing this window is fine: PAPro keeps running in the system tray (near the clock). Open it again by double-clicking the tray icon.</p>
  <div class="row"><button class="primary" id="w-ok">Get started</button></div>
</div></div>

<div class="modal" id="m-manage"><div class="dialog" style="width:540px">
  <h3 id="mg-title">Manage pack</h3>
  <p id="mg-sub" class="small"></p>
  <div id="mg-voices"></div>
  <div class="small" id="mg-msg" style="margin-top:10px;color:var(--err);min-height:16px"></div>
  <div class="row" style="margin-top:8px"><button class="primary" id="mg-done">Done</button></div>
</div></div>
<div class="modal" id="m-import"><div class="dialog" style="width:540px">
  <h3>Import an airline pack</h3>
  <p>Add a pack from a GitHub link or a zip file on this computer. Only install packs from people you trust: a pack is a set of audio files that PAPro copies into your Fenix folder.</p>
  <label class="small" for="i-url" style="display:block;margin:10px 0 6px;font-weight:600;color:var(--text)">From GitHub or a link</label>
  <input type="text" id="i-url" placeholder="https://github.com/name/repo/releases/tag/baw-1.0.0 or a link to a .zip" autocomplete="off" spellcheck="false">
  <div class="small" style="margin:12px 0 6px;font-weight:600;color:var(--text)">Or from this computer</div>
  <div style="display:flex;gap:10px;align-items:center"><button id="i-pick">Choose a .zip file...</button><span class="small" id="i-file">No file chosen</span><input type="file" id="i-input" accept=".zip,application/zip" style="display:none"></div>
  <div class="small" id="i-msg" style="margin-top:12px;color:var(--err);min-height:16px"></div>
  <div class="row" style="margin-top:10px"><button class="ghost" id="i-cancel">Cancel</button><button class="primary" id="i-ok">Import</button></div>
</div></div>
<div class="modal" id="m-aliases"><div class="dialog" style="width:520px">
  <h3>Airline aliases</h3>
  <p>Use another airline's recordings, e.g. BA Shuttle (SHT) with the BAW pack.</p>
  <div class="chips" id="alias-list"></div>
  <div class="addrow">
    <input type="text" id="a-alias" maxlength="3" placeholder="Airline" autocomplete="off" spellcheck="false">
    <span class="small">uses</span>
    <input type="text" id="a-source" maxlength="3" placeholder="Pack" list="a-sources" autocomplete="off" spellcheck="false">
    <datalist id="a-sources"></datalist>
    <button class="primary" id="a-add">Add alias</button>
  </div>
  <div class="small" style="margin:12px 0 18px">Fenix only uses an airline pack when its folder matches the livery's airline code, so PAPro builds a pack folder for the alias from the other airline's files.</div>
  <div class="row"><button class="primary" id="a-done">Done</button></div>
</div></div>

<div class="modal" id="m-settings"><div class="dialog" style="width:500px">
  <h3>Settings</h3><p>How PAPro behaves. Changes apply when you press Save.</p>
  <div class="small remote-note" style="display:none;margin-bottom:14px">You are viewing PAPro from another device, so settings that belong to the PC (window, startup, audio output, updates) are hidden here. Change those on the PC itself.</div>
  <div class="setting pc-only"><div class="label">Updates</div>
    <div style="display:flex;align-items:center;gap:12px"><div style="flex:1;min-width:0"><b id="s-ver" style="display:block;font-size:13.5px"></b><span id="s-chk" style="color:var(--muted);font-size:12.5px"></span></div>
      <button id="s-check">Check for updates</button><button class="primary" id="s-upd" style="display:none">Update now</button></div></div>
  <div class="setting"><div class="label">SimBrief username</div>
    <input type="text" id="s-name" placeholder="e.g. captainsmith123" autocomplete="off" spellcheck="false"></div>
  <div class="setting pc-only"><div class="label">When I close this window</div>
    <div class="radios" id="s-close">
      <div class="radio" data-v="no"><b>Close PAPro</b><span>PAPro quits. It only builds the landing announcement while it is running, so keep it open during a flight</span></div>
      <div class="radio" data-v="yes"><b>Keep it running in the tray</b><span>The window closes but PAPro carries on; double-click the tray icon to open it again</span></div>
    </div></div>
  <div class="setting pc-only"><div class="label">When I minimize this window</div>
    <div class="radios" id="s-min">
      <div class="radio" data-v="no"><b>Minimize normally</b><span>It stays in the taskbar</span></div>
      <div class="radio" data-v="yes"><b>Hide it in the tray</b><span>It leaves the taskbar; double-click the tray icon to bring it back</span></div>
    </div></div>
  <div class="setting"><div class="label">Extra announcements (nut allergy, Wi-Fi down...)</div>
    <div class="radios" id="s-extra">
      <div class="radio" data-v="0"><b>Never</b><span>The welcome announcement is always the same</span></div>
      <div class="radio" data-v="25"><b>Now and then</b><span>About one flight in four gets a random bolt-on from your extra folder</span></div>
      <div class="radio" data-v="50"><b>Often</b><span>About one flight in two</span></div>
    </div></div>
  <div class="setting"><div class="label">How the announcement pieces join</div>
    <div class="radios" id="s-flow">
      <div class="radio" data-v="flow"><b>Natural flow</b><span>The greeting, airport name and the rest are cropped tight and run into each other like one sentence</span></div>
      <div class="radio" data-v="pauses"><b>Clear pauses</b><span>A short gap between each piece</span></div>
    </div></div>
  <div class="setting"><div class="label">PA volume</div>
    <div class="radios" id="s-boost">
      <div class="radio" data-v="0"><b>As recorded</b><span>No change to the announcements</span></div>
      <div class="radio" data-v="15"><b>Louder (+15%)</b><span>A little louder than the recordings</span></div>
      <div class="radio" data-v="30"><b>Louder (+30%)</b><span>Recommended if the cabin PAs sound quiet in the sim</span></div>
      <div class="radio" data-v="50"><b>Much louder (+50%)</b><span>Peaks are softly limited so it never distorts</span></div>
    </div></div>
  <div class="setting pc-only"><div class="label">Audio output</div>
    <div class="small" style="margin-bottom:10px">For aircraft that PAPro plays announcements for itself (not the Fenix, which plays them in the sim). Choose where the sound comes out.</div>
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <select class="voice" id="s-adev" style="flex:1;min-width:200px;max-width:100%"></select>
      <button class="ghost" id="s-atest">Test sound</button>
      <button class="ghost" id="s-arefresh" title="Look for outputs again (e.g. after plugging in a headset)">Refresh</button>
    </div>
    <div class="small" id="s-anote" style="margin-top:8px"></div>
    <label class="check" style="margin:12px 0 0"><input type="checkbox" id="s-aplay"> Play the announcements through PAPro when the aircraft isn't a Fenix</label>
    <div class="small" style="margin-top:6px">Volume and mute are on the PA knob on the Dashboard.</div>
  </div>
  <div class="setting"><div class="label">GSX integration</div>
    <div class="small" style="margin-bottom:10px">Reads GSX's boarding and deboarding status so the Dashboard's flight phase follows the ground services. Does nothing if GSX isn't installed.</div>
    <label class="check" style="margin:0"><input type="checkbox" id="s-gsx"> Use GSX for boarding and deboarding</label>
  </div>
  <div class="setting pc-only"><div class="label">Start PAPro automatically</div>
    <div class="radios" id="s-start">
      <div class="radio" data-v="off"><b>No</b><span>I'll start it myself</span></div>
      <div class="radio" data-v="msfs"><b>With MSFS</b><span>The sim launches PAPro every time it starts (through its EXE.xml)</span></div>
      <div class="radio" data-v="windows"><b>With Windows</b><span>Starts when you sign in to your PC</span></div>
    </div>
    <label class="check" id="s-quit-row" style="margin:12px 0 0"><input type="checkbox" id="s-quit"> Close PAPro when MSFS closes</label>
  </div>
  <div class="setting pc-only"><div class="label">PAPro's web address</div>
    <div class="small" style="margin-bottom:10px">PAPro also runs as a local web page. This address is always the same, so another app (such as EFBx.io) can open it as an internal app. It only works on this PC while PAPro is running.</div>
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><input type="text" id="s-addr" readonly style="flex:1;min-width:220px;font:600 12px 'Inter',sans-serif" onclick="this.select()"><button class="ghost" id="s-addr-copy">Copy</button><button class="ghost" data-open="browser">Open in browser</button></div>
    <div class="small" style="margin:14px 0 4px"><b>Open PAPro from another device</b> (EFBx on an iPad, a tablet or another PC) with one of these addresses. It works on your home network or over a VPN, while PAPro is running. Anyone on that network can reach it, so only use trusted networks. Windows may ask once to allow PAPro through its firewall - allow it for private networks.</div>
    <div id="s-lan-addrs" style="margin-top:8px"></div>
  </div>
  <div class="setting pc-only"><div class="label">Fenix packs folder and log</div>
    <div style="display:flex;align-items:center;gap:12px"><button class="ghost" data-open="fenix">Open folder</button><span class="small">Where PAPro puts your announcements for the Fenix.</span></div>
    <div style="display:flex;align-items:center;gap:12px;margin-top:10px"><button class="ghost" data-open="log">View log</button><span class="small">Useful if something is not working.</span></div></div>
  <div class="row"><button class="ghost" id="s-cancel">Cancel</button><button class="primary" id="s-save">Save</button></div>
</div></div>

<div class="modal" id="m-mic"><div class="dialog">
  <h3>Audio settings</h3><p>Pick your microphone, speak normally and keep the bar in the green.</p>
  <div class="label small">Input device</div><select id="mic-dev" class="voice" style="width:100%;margin:6px 0 14px"></select>
  <div class="label small">Input boost <span id="mic-gv"></span></div><input type="range" id="mic-gain" min="-12" max="18" step="1" style="width:100%">
  <div class="meter"><i id="mic-bar"></i><b id="mic-peak"></b></div><div class="small" id="mic-msg">Starting the microphone...</div>
  <div class="label small" style="margin-top:14px">Playback device (for the play and test buttons)</div><select id="out-dev" class="voice" style="width:100%;margin:6px 0 14px"></select>
  <div class="label small">Playback volume <span id="out-gv"></span></div><input type="range" id="out-vol" min="0" max="200" step="5" style="width:100%">
  <div class="small" style="margin-top:6px">Windows lists PAPro in its volume mixer under the browser that shows this window (Microsoft Edge or Chrome), and only while a sound is playing.</div>
  <div class="row" style="margin-top:16px;justify-content:space-between"><button id="mic-test">Test: record 3 s and play back</button><button class="primary" id="mic-done">Done</button></div>
</div></div>
<div class="modal" id="m-up"><div class="dialog" style="width:560px">
  <h3>Upgrade to the new names and folders</h3><p>These clips use old names or folders. Renaming them lets them use the new announcement types, tags and modes. Only your Studio copy changes now. When you press "Save changes to my library", the old-named files there are replaced by these.</p>
  <div id="up-list" style="max-height:48vh;overflow:auto;margin-bottom:14px"></div>
  <div class="small" style="margin-bottom:14px">The takeoff and landing versions share one new name and are told apart by flight-phase tags (PreFlight = before takeoff, Descent/Approach = before landing). That is our reading of Fenix's guide - test it in the sim.</div>
  <div class="row"><button id="up-cancel">Not now</button><button class="primary" id="up-ok">Rename them</button></div>
</div></div>
<div class="modal" id="m-abandon"><div class="dialog">
  <h3 id="ab-title">Abandon this pack?</h3><p id="ab-text"></p>
  <div class="row"><button id="ab-cancel">Keep it</button><button class="primary" id="ab-ok" style="background:var(--err);border-color:transparent">Delete the pack</button></div>
</div></div>
<div class="modal" id="m-quiet"><div class="dialog">
  <h3 id="q-t">No sound picked up</h3><p id="q-s"></p>
  <div class="row"><button id="q-close">Close</button><button class="primary" id="q-open">Open Audio settings</button></div>
</div></div>
<div class="modal" id="m-job"><div class="dialog"><h3 id="job-t">Working...</h3><p id="job-s">Please wait - this can take a moment.</p><div class="ptrack"><i id="job-bar"></i></div></div></div>
<div class="toast" id="toast"></div>

<script>
document.addEventListener("contextmenu",e=>{if(!e.target.closest||!e.target.closest("input,textarea,[contenteditable]"))e.preventDefault()});document.addEventListener("dragstart",e=>{if(!e.target.closest||!e.target.closest("[draggable=true]"))e.preventDefault()});
const TOKEN="__TOKEN__";
const $=s=>document.querySelector(s);
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e};
let snap=null, structSig="", promptId=null, selected=null, setupShown=false;

async function api(name,data){
  try{const r=await fetch("/api/"+name,{method:"POST",headers:{"X-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify(data||{})});
      const j=await r.json(); if(j.error) toast(j.error); return j;}catch(e){toast("Lost contact with PAPro");return {}}
}
function confirmDiscard(p){
  $("#ab-title").textContent=p.in_library?"Remove from packs in progress?":"Abandon this pack?";
  $("#ab-ok").textContent=p.in_library?"Remove":"Delete the pack";$("#ab-cancel").textContent=p.in_library?"Cancel":"Keep it";
  $("#ab-text").textContent=p.in_library?"This only removes "+p.icao+" - "+p.voice+" from your packs in progress. The pack in your Airlines library is kept exactly as it is.":"This deletes all the recordings and details of "+p.icao+" - "+p.voice+" in Pack Studio. It cannot be undone. Your Airlines library is not touched.";
  $("#m-abandon").classList.add("show");
  $("#ab-cancel").onclick=()=>$("#m-abandon").classList.remove("show");
  $("#ab-ok").onclick=async()=>{$("#m-abandon").classList.remove("show");const r=await studio("discard",{icao:p.icao,voice:p.voice});if(r.ok)toast(p.in_library?"Removed from packs in progress - your library pack is untouched":"Pack discarded")};
}
function toast(t,ms){const x=$("#toast");x.textContent=t;x.classList.add("show");clearTimeout(toast.t);toast.t=setTimeout(()=>x.classList.remove("show"),ms||3200)}
function editableLogo(icao,cls){ // the logo with a pencil that appears on hover: pick a new image for this airline
  const w=el("span","lw"); w.append(logo(icao,cls));
  const b=el("button","ed"); b.title="Change this logo"; b.type="button";
  b.innerHTML='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
  const f=el("input"); f.type="file"; f.accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"; f.style.display="none";
  f.onchange=async()=>{const file=f.files[0]; if(!file)return; if(file.size>5*1024*1024){toast("That image is too large (5 MB limit)");return}
    try{const r=await fetch("/logo/upload?"+new URLSearchParams({icao:icao,t:TOKEN}),{method:"POST",headers:{"X-Token":TOKEN},body:file});const j=await r.json();
      toast(j.error||"Logo changed",j.error?6000:3000); if(j.ok)await refreshNow()}catch(e){toast("Upload failed",6000)}};
  b.onclick=e=>{e.stopPropagation();f.click()}; w.append(b,f); return w}
function logo(icao,cls){const i=el("img",cls);i.alt=icao;i.src="/logo/"+icao+".png?t="+TOKEN+"&v="+(snap&&snap.logo_v||0);return i}

let heroSig="", keepClosed=false;
function renderUpd(){const a=snap.app_update,b=$("#updbar");if(!a||!a.url){b.style.display="none";return}
  b.style.display="";$("#upd-t").textContent="PAPro "+a.version+" is available";$("#upd-s").textContent="";$("#upd-go").textContent=a.auto?"Update now":"Download"}
$("#upd-go").onclick=()=>{if(snap.app_update&&snap.app_update.auto){$("#upd-go").disabled=true;toast("Downloading the update - PAPro will restart by itself",6000)}api("open",{what:"update"})};
function renderKeep(){$("#keep-note").style.display=(keepClosed||snap.hide_reminder)?"none":""}
$("#keep-x").onclick=async()=>{keepClosed=true;renderKeep();if($("#keep-never").checked)await api("hide_reminder",{hide:true})};
// The on-screen PA knob, for aircraft whose PAs PAPro plays itself (not the Fenix): push = audible / muted, left-right or wheel = volume.
let knobSave=null;
function drawKnobScale(){const sv=$("#kscale");if(sv.firstChild)return;let h="";
  for(let i=0;i<=10;i++){const a=(-135+27*i)*Math.PI/180,x1=70+Math.sin(a)*54,y1=70-Math.cos(a)*54,x2=70+Math.sin(a)*(i%5===0?64:60),y2=70-Math.cos(a)*(i%5===0?64:60);h+='<line data-i="'+i+'" x1="'+x1.toFixed(1)+'" y1="'+y1.toFixed(1)+'" x2="'+x2.toFixed(1)+'" y2="'+y2.toFixed(1)+'"/>'}
  sv.innerHTML=h}
function paintKnob(){const k=snap&&snap.settings&&snap.settings.pa_knob||{on:true,volume:70},st=window.knobLocal||k,on=!!st.on,v=st.volume;drawKnobScale();
  $("#knob").classList.toggle("on",on);$("#kptr").style.transform="rotate("+(-135+270*v/100)+"deg)";$("#knob").setAttribute("aria-valuenow",v);
  $("#kscale").querySelectorAll("line").forEach(l=>l.classList.toggle("lit",on&&(+l.dataset.i)*10<=v));
  $("#kvol").textContent=v+"%";$("#kst").textContent=on?(v===0?"PAs audible (volume 0)":"PAs audible"):"Muted - playback continues"}
function knobSet(p){window.knobLocal={...(window.knobLocal||(snap.settings&&snap.settings.pa_knob)||{on:true,volume:70}),...p};paintKnob();
  clearTimeout(knobSave);knobSave=setTimeout(async()=>{const d=window.knobLocal;await api("pa_knob",{on:d.on,volume:d.volume});try{snap=await (await fetch("/api/state",{headers:{"X-Token":TOKEN}})).json()}catch(e){}window.knobLocal=null;paintKnob()},250)}
(function initKnob(){const kn=$("#knob"),cur=()=>window.knobLocal||(snap&&snap.settings&&snap.settings.pa_knob)||{on:true,volume:70};
  kn.onclick=e=>{const r=$(".kcap").getBoundingClientRect(),x=(e.clientX-r.left)/r.width,c=cur();
    if(x<0.34)knobSet({volume:Math.max(0,c.volume-5)});else if(x>0.66)knobSet({volume:Math.min(100,c.volume+5)});else knobSet({on:!c.on})};
  kn.addEventListener("wheel",e=>{e.preventDefault();const c=cur();knobSet({volume:Math.max(0,Math.min(100,c.volume+(e.deltaY<0?5:-5)))})},{passive:false});
  kn.onkeydown=e=>{const c=cur();if(e.key==="ArrowLeft"||e.key==="ArrowDown"){e.preventDefault();knobSet({volume:Math.max(0,c.volume-5)})}
    else if(e.key==="ArrowRight"||e.key==="ArrowUp"){e.preventDefault();knobSet({volume:Math.min(100,c.volume+5)})}else if(e.key===" "||e.key==="Enter"){e.preventDefault();knobSet({on:!c.on})}}})();
function renderKnob(){const box=$("#knobbox"),show=!!(snap&&snap.aircraft&&!snap.aircraft.supported);box.style.display=show?"":"none";if(show&&!knobSave)paintKnob()}
function renderHero(){
  const sig=JSON.stringify([snap.setup_needed,snap.company,snap.voices,snap.flight,snap.message,snap.applied,snap.updated,snap.busy,snap.logo_v,(snap.packs||[]).filter(p=>p.installed).length,snap.aircraft,snap.phase]);
  if(sig===heroSig) return; heroSig=sig;
  const h=$("#hero"); h.replaceChildren(); h.classList.toggle("ok",!!snap.applied);
  const f=snap.flight;
  const anyPack=(snap.packs||[]).some(p=>p.installed);
  const noPack=!anyPack||!!(f&&f.airline&&!snap.voices);   // no pack at all, or none for this flight's airline
  if(f&&f.airline&&!noPack){h.append(editableLogo(f.airline,"logo"))}
  const mid=el("div"); mid.style.minWidth="0";
  if(noPack){
    const t=el("div","h-nopack"); t.append(document.createTextNode(anyPack&&f&&f.airline?"No "+f.airline+" airline pack installed. Please download or import one ":"No airline pack installed. Please download or import one "));
    const a=el("a",null,"here"); a.href="#"; a.onclick=e=>{e.preventDefault();setView("dl")}; t.append(a,document.createTextNode("."));
    mid.append(t);
  }else if(f&&f.dest){
    const ids=[f.number,f.callsign].filter(Boolean).join(" / ");
    if(ids) mid.append(el("div","h-ids",ids));
    mid.append(el("div","h-name",snap.company||f.airline||"???"));
    const d=el("div","h-dest"); if(f.origin){const o=el("b","h-orig",f.origin),ar=el("span","h-arrow"," \u2192 ");d.append(o,ar)} d.append(el("b",null,f.dest)); if(f.name) d.append(document.createTextNode(" | "+f.name)); mid.append(d);
  }else mid.append(el("div","h-name","No flight yet"));
  if(snap.message&&!noPack){const m=el("div","msg"); m.textContent=snap.message; mid.append(m)}
  if(snap.aircraft){const ac=snap.aircraft,chip=el("div","h-ac "+(ac.supported?"ok":"warn"));chip.title=ac.supported?"PAs can be played in the sim for this aircraft":"PAPro can't play PAs inside this aircraft yet";
    chip.append(el("i"),el("span",null,ac.title+(ac.supported?" · PAs supported":" · not supported yet")));mid.append(chip)}
  if(snap.phase){const ph=snap.phase,box=el("div","h-phase");box.title="Worked out from the sim: speed, direction, altitude, lights and doors (and GSX when it is on). It only moves forward until a new flight plan is loaded.";
    const top=el("div","h-ptop");top.append(el("span","h-plab","Flight phase"),el("b",null,ph.name),el("span","h-psrc",""));
    const bar=el("div","h-pbar");ph.steps.forEach((n,i)=>{const sg=el("i",i<ph.index?"done":i===ph.index?"now":"");sg.title=n;bar.append(sg)});box.append(top,bar);
    if(ph.can_board){const bb=el("button","ghost","Start boarding");bb.style.marginTop="10px";bb.title="Begin the boarding phase (this aircraft has no GSX status to do it for you)";bb.onclick=()=>api("phase_boarding");box.append(bb)}
    if(ph.can_disarm){const bd=el("button","ghost","Disarm Doors PA");bd.style.marginTop="10px";bd.title="Play the Disarm Doors announcement now";bd.onclick=()=>api("phase_disarm");box.append(bd)}
    mid.append(box)}
  h.append(mid);
  const side=el("div","side");
  const b=el("button","primary"); b.innerHTML='<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:7px"><path d="M21 12a9 9 0 0 1-15.4 6.4L3 16"/><path d="M3 12a9 9 0 0 1 15.4-6.4L21 8"/><path d="M21 3v5h-5"/><path d="M3 21v-5h5"/></svg>Refresh'; b.title="Rebuild this flight's announcements now"; b.disabled=snap.busy||!snap.voices; b.onclick=()=>api("update");
  if(!snap.voices&&f&&f.airline) b.title="No voices for "+f.airline+" yet - install or import its airline pack first";
  const v=snap.voices;
  if(v){
    {
      const row=el("div","voicerow"); const sel=el("select","voice"); sel.title="Voice artist";
      v.options.forEach(o=>{const op=el("option",null,o.name); op.value=o.id; sel.append(op)});
      if(v.options.length>1){const rnd=el("option",null,"Random voice"); rnd.value="random"; sel.append(rnd)}
      sel.value=v.options.some(o=>o.id===v.selected)||v.selected==="random"&&v.options.length>1?v.selected:v.options[0].id; sel.disabled=snap.busy;
      sel.onchange=()=>api("voice",{voice:sel.value});
      row.append(el("span","small","Voice")); row.append(sel); row.append(b); side.append(row);
    }
    const cur=v.options.find(o=>o.id===(v.selected==="random"||v.options.length===1?(v.selected==="random"?v.used:v.options[0].id):v.selected))||(v.options.length===1?v.options[0]:null);
    if(v.selected==="random"&&cur) side.append(el("span","small","This flight: "+cur.name));
    if(cur&&(cur.artist||cur.accent)){
      const line=el("span","small credit");
      const link=cur.donate_url;   // the name goes to the artist's support page (Ko-fi...)
      if(cur.artist){
        line.append(document.createTextNode("Voiced by "));
        if(link){const a=el("a","who",cur.artist); a.href=link; a.target="_blank"; a.rel="noopener"; a.title="Support "+cur.artist; line.append(a)}
        else line.append(document.createTextNode(cur.artist));
      }
      if(cur.accent) line.append(document.createTextNode((cur.artist?" · ":"")+cur.accent));
      side.append(line);
    }
  } else side.append(b);
  if(snap.updated&&!noPack) side.append(el("span","small","Last applied "+snap.updated));
  h.append(side);
}

function packCard(p,editable){
  const c=el("div","card pack"); c.id="pack-"+(p.key||p.icao);
  const top=el("div","top"); top.append(editable&&p.installed?editableLogo(p.icao,"logo"):logo(p.icao,"logo"));
  const info=el("div"); const n=el("div","name",p.name);
  if(p.installed&&p.update){n.append(el("span","chip up","Update available"))}
  else if(p.catalog&&p.installed){n.append(el("span","chip ok","Installed"))}
  info.append(n);
  const ver=p.version&&p.version!=="imported"&&p.version!=="Your recordings"?(/^[0-9]/.test(p.version)?"v"+p.version:p.version):"";
  info.append(el("div","meta",[p.icao,ver,p.size_mb?p.size_mb+" MB":""].filter(Boolean).join("  ·  ")));
  if(p.voices&&p.voices.length) info.append(el("div","meta",(p.voices.length>1?"Voices: ":"Voice: ")+p.voices.join(", ")));
  top.append(info);
  const act=el("div","act");
  if(!p.task){
    if(!p.installed||p.update){const b=el("button","primary",p.update?"Update":"Install");b.disabled=snap.busy;b.onclick=()=>api("install",{id:p.id||p.update_id||p.icao});act.append(b)}
    if(p.installed&&!p.catalog){const m=el("button","ghost","Manage");m.disabled=snap.busy;m.onclick=()=>openManage(p.icao);act.append(m)}
    if(p.installed&&!p.catalog){const own=p.own||p.version==="Your recordings";const b=el("button","danger","Uninstall");b.disabled=snap.busy;b.onclick=()=>{if(confirm(own?"Remove the "+p.name+" pack?\nThis takes your "+p.icao+" recordings out of PAPro (a copy is kept in the PAPro backups folder).":"Uninstall the "+p.name+" pack?\nYour own recordings stay."))api("uninstall",{icao:p.icao})};act.append(b)}
  }
  if(p.task){
    const live=el("div","live");
    const pr=el("div","prog"); const a=el("span"); a.id="pt-"+(p.key||p.icao); const b=el("b"); b.id="pp-"+(p.key||p.icao); pr.append(a,b);
    const bar=el("div","bar"); bar.id="pb-"+(p.key||p.icao); bar.append(el("i")); live.append(pr,bar); act.append(live);
  }
  top.append(act); c.append(top);
  return c;
}
const packSigs={};
function renderPackList(boxId,list,emptyText){
  const box=$("#"+boxId);
  const sig=JSON.stringify([list.map(p=>[p.key||p.icao,p.installed,p.update,!!p.task,p.name,p.version,p.voices]),snap.busy,snap.catalog_error,snap.logo_v,emptyText]);
  if(sig!==packSigs[boxId]){
    packSigs[boxId]=sig; box.replaceChildren();
    if(!list.length){const e=el("div","empty",emptyText); e.style.gridColumn="1/-1"; box.append(e)}
    list.forEach(p=>box.append(packCard(p,boxId==='packs')));   // only the Dashboard list lets you change a logo
  }
  list.forEach(p=>{
    if(!p.task) return;
    const t=$("#pt-"+(p.key||p.icao)),pp=$("#pp-"+(p.key||p.icao)),pb=$("#pb-"+(p.key||p.icao)); if(!t) return;
    t.textContent=p.task.text||"Working";
    const f=p.task.frac;
    pb.classList.toggle("indet",f===null||f===undefined);
    if(f!==null&&f!==undefined){pp.textContent=Math.round(f*100)+"%";pb.firstChild.style.width=(f*100)+"%"}else pp.textContent="";
  });
}
function renderPacks(){
  const mine=snap.packs.filter(p=>p.installed&&!p.catalog), more=snap.packs.filter(p=>p.catalog||!p.installed);
  renderPackList("packs",mine,"No airline packs installed yet. Open the Download Packs tab to get one.");
  renderPackList("dlpacks",more,snap.catalog_error||(snap.packs.length?"Every available pack is installed.":"No airline packs are available to download yet. You can still use Import a pack."));
  $("#packsub").textContent=mine.length?mine.length+" installed":"";
  $("#dlsub").textContent=more.length?more.length+" available":"";
}
let manageIcao=null;
function openManage(icao){manageIcao=icao;$("#mg-msg").textContent="";renderManage();$("#m-manage").classList.add("show")}
function renderManage(){
  const p=(snap.packs||[]).find(x=>x.icao===manageIcao&&x.installed); if(!p){$("#m-manage").classList.remove("show");return}
  $("#mg-title").textContent="Manage "+p.name; $("#mg-sub").textContent=p.icao+" - open a voice in Pack Studio to re-record or fix it, or take a voice out of the airline.";
  const box=$("#mg-voices"); const items=p.voice_items||[]; const sig=JSON.stringify(items); if(box.dataset.s===sig) return; box.dataset.s=sig; box.replaceChildren();
  if(!items.length) box.append(el("div","empty","This airline has no voices yet. Add one in Pack Studio or Download packs."));
  items.forEach(v=>{
    const row=el("div","card"); row.style.cssText="display:flex;align-items:center;gap:10px;padding:10px 14px;margin-top:8px";
    const nm=el("div","name",v.key==="Default"?(v.name==="Default"?"Main recordings":v.name):v.name); nm.style.flex="1"; row.append(nm);
    const ed=el("button","ghost","Edit in Pack Studio"); ed.onclick=async()=>{const r=await studio("import",{icao:p.icao,voice:v.key});if(r.ok){$("#m-manage").classList.remove("show");setView("studio")}}; row.append(ed);
    if(v.key!=="Default"){const rm=el("button","danger","Remove");rm.onclick=async()=>{const last=items.length<=1;if(!confirm(last?"This is the last voice in "+p.name+", so removing it uninstalls the whole pack.\nA copy is kept in the PAPro backups folder.":"Remove the "+v.name+" voice from "+p.name+"?\nA copy is kept in the PAPro backups folder."))return;const r=await api("remove_voice",{icao:p.icao,voice:v.key});if(r.ok){box.dataset.s="";if(last)$("#m-manage").classList.remove("show");toast(last?"Pack uninstalled":"Removed "+v.name)}};row.append(rm)}
    box.append(row);
  });
}
$("#mg-done").onclick=()=>$("#m-manage").classList.remove("show");
let importFile=null,importWatch=null;   // importWatch: after an import starts, go back to the Dashboard when it has installed
$("#import-btn").onclick=()=>{importFile=null;$("#i-url").value="";$("#i-input").value="";$("#i-file").textContent="No file chosen";$("#i-msg").textContent="";$("#m-import").classList.add("show");setTimeout(()=>$("#i-url").focus(),60)};
$("#i-cancel").onclick=()=>$("#m-import").classList.remove("show");
$("#i-pick").onclick=()=>$("#i-input").click();
$("#i-input").onchange=()=>{const f=$("#i-input").files[0];importFile=f||null;$("#i-file").textContent=f?f.name+" ("+Math.round(f.size/1048576)+" MB)":"No file chosen";if(f)$("#i-url").value=""};
$("#i-url").oninput=()=>{if($("#i-url").value){importFile=null;$("#i-input").value="";$("#i-file").textContent="No file chosen"}};
$("#i-ok").onclick=async()=>{
  const msg=$("#i-msg"),url=$("#i-url").value.trim();msg.textContent="";
  if(!importFile&&!url){msg.textContent="Paste a link or choose a zip file.";return}
  $("#i-ok").disabled=true;$("#i-ok").textContent="Importing...";
  try{
    let r;
    if(importFile){const q=new URLSearchParams({file:importFile.name,t:TOKEN});const x=await fetch("/import/upload?"+q,{method:"POST",headers:{"X-Token":TOKEN},body:importFile});r=await x.json()}
    else r=await api("import_url",{url});
    if(r.error){msg.textContent=r.error}else{$("#m-import").classList.remove("show");toast("Importing - watch the Download Packs tab for progress",5000);importWatch={t0:Date.now(),seen:false,n:snap.installs_done||0};setView("dl")}
  }catch(e){msg.textContent="Import failed - could not send the file."}
  $("#i-ok").disabled=false;$("#i-ok").textContent="Import";
};
let aliasSig="";
function renderAliases(){
  const sig=JSON.stringify([snap.aliases,snap.alias_sources]); if(sig===aliasSig) return; aliasSig=sig;
  const box=$("#alias-list"); box.replaceChildren();
  const entries=Object.entries(snap.aliases||{});
  if(!entries.length) box.append(el("span","small","No aliases yet."));
  entries.forEach(([a,s])=>{
    const c=el("span","chip2"); c.append(document.createTextNode(a)); c.append(el("span","to","\u2192")); c.append(document.createTextNode(s));
    const x=el("button",null,"\u00d7"); x.title="Remove"; x.onclick=()=>api("alias_remove",{alias:a}); c.append(x); box.append(c);
  });
  const dl=$("#a-sources"); dl.replaceChildren(); (snap.alias_sources||[]).forEach(v=>{const o=document.createElement("option");o.value=v;dl.append(o)});
}
let welcomeShown=false;
function renderWelcome(){
  const w=snap.welcome, m=$("#m-welcome");
  if(!w&&!snap.setup_needed){m.classList.remove("show");return}
  if(welcomeShown) return; welcomeShown=true;
  const f=w||snap.welcome_info||{};
  $("#w-fenix").textContent=f.fenix||"(found automatically once you've installed an airline pack)";
  $("#w-orig").textContent=f.originals||""; $("#w-rec").textContent=f.recordings||"";
  $("#w-name").value=snap.username||"";
  m.classList.add("show"); setTimeout(()=>$("#w-name").focus(),80);
}
$("#w-ok").onclick=async()=>{
  const v=$("#w-name").value.trim();
  if(!v){toast("Enter your SimBrief username to continue",5000);$("#w-name").focus();return}
  $("#m-welcome").classList.remove("show");
  if(v!==(snap.username||""))await api("username",{name:v});
  await api("welcomed");snap.welcome=null;snap.setup_needed=false;
};
$("#w-name").addEventListener("keydown",e=>{if(e.key==="Enter")$("#w-ok").click()});
$("#aliases-btn").onclick=()=>{$("#m-aliases").classList.add("show");setTimeout(()=>$("#a-alias").focus(),50)};
$("#a-done").onclick=()=>$("#m-aliases").classList.remove("show");
$("#a-add").onclick=async()=>{
  const a=$("#a-alias").value.trim(), s=$("#a-source").value.trim(); if(!a||!s){toast("Enter both airline codes");return}
  const r=await api("alias_add",{alias:a,source:s}); if(!r.error){$("#a-alias").value="";$("#a-source").value="";toast(a.toUpperCase()+" now uses "+s.toUpperCase()+" recordings")}
};
["a-alias","a-source"].forEach(id=>$("#"+id).addEventListener("keydown",e=>{if(e.key==="Enter")$("#a-add").click()}));
function renderPrompt(){
  const p=snap.prompt, m=$("#m-choice");
  if(!p){m.classList.remove("show");promptId=null;return}
  if(promptId===p.id) return;
  promptId=p.id; selected=p.options[0];
  $("#c-title").textContent=p.title; $("#c-text").textContent=p.text;
  const o=$("#c-opts"); o.replaceChildren();
  p.options.forEach(name=>{const d=el("div","opt"+(name===selected?" sel":""),name);
    d.onclick=()=>{selected=name;[...o.children].forEach(x=>x.classList.toggle("sel",x===d))}; o.append(d)});
  m.classList.add("show");
}
$("#c-ok").onclick=async()=>{await api("answer",{id:promptId,choice:selected});$("#m-choice").classList.remove("show");promptId="done:"+promptId};

function openName(){ $("#n-input").value=snap.username||""; $("#m-name").classList.add("show"); setTimeout(()=>$("#n-input").focus(),50) }
let cfg={close:"no",autostart:"off",min:"no",time:"yes",extra:"25"};
function pickRadio(id,val){document.querySelectorAll("#"+id+" .radio").forEach(r=>r.classList.toggle("sel",r.dataset.v===val))}
document.querySelectorAll(".radios").forEach(g=>g.querySelectorAll(".radio").forEach(r=>r.onclick=()=>{
  cfg[g.id==="s-close"?"close":g.id==="s-min"?"min":g.id==="s-time"?"time":g.id==="s-extra"?"extra":g.id==="s-boost"?"boost":g.id==="s-flow"?"flow":"autostart"]=r.dataset.v; pickRadio(g.id,r.dataset.v); $("#s-quit-row").style.display=cfg.autostart==="msfs"?"flex":"none"}));
function openSettings(){
  const s=snap.settings||{}; cfg={close:s.close_to_tray?"yes":"no",autostart:s.autostart||"off",min:s.minimize_to_tray?"yes":"no",time:s.local_time===false?"no":"yes",extra:String(s.extra_chance===undefined?25:s.extra_chance),boost:String(s.pa_boost===undefined?30:s.pa_boost),flow:s.join_style==="pauses"?"pauses":"flow"};
  $("#s-name").value=snap.username||""; $("#s-quit").checked=s.quit_with_msfs!==false;
  pickRadio("s-start",cfg.autostart); pickRadio("s-close",cfg.close); pickRadio("s-min",cfg.min); pickRadio("s-extra",cfg.extra);pickRadio("s-boost",cfg.boost);pickRadio("s-flow",cfg.flow);
  $("#s-quit-row").style.display=cfg.autostart==="msfs"?"flex":"none";
  $("#s-addr").value=snap.address||"";
  const lan=s.lan||{};renderLanAddrs(lan);
  const au=s.audio||{};audioChosen=au.device||"";$("#s-aplay").checked=au.playback!==false;$("#s-gsx").checked=s.gsx!==false;loadAudioDevices();
  renderSetUpd();$("#m-settings").classList.add("show");
}
$("#settings").onclick=openSettings;
function renderLanAddrs(lan){const box=$("#s-lan-addrs");box.replaceChildren();
  if(!(lan.addresses||[]).length){box.append(el("div","small","No network address found for this PC."));return}
  (lan.addresses||[]).forEach(a=>{const row=el("div");row.style.cssText="display:flex;gap:8px;align-items:center;margin-top:4px";const i=document.createElement("input");i.type="text";i.readOnly=true;i.value=a;i.style.cssText="flex:1;min-width:200px;font:600 12px 'Inter',sans-serif";i.onclick=()=>i.select();row.append(i);box.append(row)})}
$("#s-addr-copy").onclick=async()=>{try{await navigator.clipboard.writeText($("#s-addr").value);toast("Address copied")}catch(e){$("#s-addr").select();toast("Press Ctrl+C to copy")}};
let audioChosen="";
async function loadAudioDevices(){
  const sel=$("#s-adev"),note=$("#s-anote");sel.replaceChildren();
  const r=await api("audio_devices",{});const list=(r&&r.devices)||[];
  const add=(v,t)=>{const o=document.createElement("option");o.value=v;o.textContent=t;sel.append(o)};
  add("","Windows default output");
  list.forEach(d=>add(d.name,d.name+(d.default?" (default)":"")));
  if(audioChosen&&!list.some(d=>d.name===audioChosen)){add(audioChosen,audioChosen+" (not connected)")}
  sel.value=audioChosen;
  note.textContent=list.length?"":r&&r.problem==="missing"?"This copy of PAPro doesn't include the audio library yet. Install the latest build (or run Setup PAPro.bat if you run it from the source folder), then press Refresh.":r&&r.problem?"Couldn't read the sound outputs: "+r.problem:"No sound outputs were found. PAPro will use the Windows default when one is available.";
}
$("#s-adev").onchange=()=>{audioChosen=$("#s-adev").value};
$("#s-arefresh").onclick=loadAudioDevices;
$("#s-atest").onclick=async()=>{const r=await api("audio_test",{device:$("#s-adev").value});if(r&&r.ok)toast("Playing a test chime")};
function renderSetUpd(){const a=snap&&snap.app_update;$("#s-ver").textContent="PAPro v"+((snap&&snap.version)||"");
  const t=snap&&snap.update_checked?new Date(snap.update_checked):null;const when=t?"Last checked "+t.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}):"Not checked yet";
  $("#s-chk").textContent=a?"PAPro "+a.version+" is available. "+when:(t?"You're up to date. ":"")+when;
  const u=$("#s-upd");u.style.display=a?"":"none";u.textContent=a&&a.auto?"Update now":"Download"}
$("#s-check").onclick=async()=>{const b=$("#s-check");b.disabled=true;b.textContent="Checking...";const r=await api("check_update",{});b.disabled=false;b.textContent="Check for updates";await refreshNow();renderSetUpd();if(r.ok)toast(r.latest?"PAPro "+r.latest+" is available":"You're up to date (PAPro v"+r.version+")")};
$("#s-upd").onclick=()=>$("#upd-go").click();
$("#s-cancel").onclick=()=>$("#m-settings").classList.remove("show");
$("#s-save").onclick=async()=>{
  const name=$("#s-name").value.trim();
  if(name&&name!==(snap.username||"")){const r=await api("username",{name}); if(r.error)return}
  const r=await api("settings",{close_to_tray:cfg.close==="yes",autostart:cfg.autostart,minimize_to_tray:cfg.min==="yes",extra_chance:parseInt(cfg.extra,10),pa_boost:parseInt(cfg.boost,10),join_style:cfg.flow,quit_with_msfs:$("#s-quit").checked,audio_device:audioChosen,audio_playback:$("#s-aplay").checked,gsx:$("#s-gsx").checked,});
  if(!r.error){$("#m-settings").classList.remove("show");toast("Settings saved")}
};
let minSent=false;
setInterval(()=>{ // a minimized Chromium window reports its position as far off screen
  const away=window.screenX<=-30000||window.screenY<=-30000;
  if(away&&!minSent&&snap&&snap.settings&&snap.settings.minimize_to_tray){minSent=true;api("minimized")}
  if(!away)minSent=false;
},500);
window.addEventListener("pagehide",()=>{try{fetch("/api/closing",{method:"POST",keepalive:true,headers:{"X-Token":TOKEN,"Content-Type":"application/json"},body:"{}"})}catch(e){}});
$("#n-cancel").onclick=()=>$("#m-name").classList.remove("show");
$("#n-ok").onclick=async()=>{const v=$("#n-input").value.trim(); if(!v)return; $("#m-name").classList.remove("show"); await api("username",{name:v}); toast("Saved - fetching your flight")};
$("#n-input").addEventListener("keydown",e=>{if(e.key==="Enter")$("#n-ok").click()});
$("#auto").onclick=()=>api("auto",{on:!snap.auto});
$("#refresh").onclick=async()=>{await api("refresh");toast("Refreshing pack list")};
document.querySelectorAll("[data-open]").forEach(b=>b.onclick=()=>api("open",{what:b.dataset.open}));

// ---------------------------------------------------------------- Pack Studio
let airportFilter="", view="main", studioSig="", recState=null, openGroups=new Set(["Boarding"]), playing=null;
function setView(v){view=v;for(const n of ["main","studio","dl","test"]){$("#view-"+n).style.display=v===n?"":"none";$("#tab-"+n).classList.toggle("on",v===n)}
  if(v==="studio"){studioSig="";renderStudio()} if(v==="test")testInit()}
let tOpts=null,tKey="",tAudio=null,tRows=[],tIdx=-1,tPlaylist=false,tBusy=false,tPaused=false;
function tMsg(t){const L=$("#t-list");L.innerHTML="";L.appendChild(el("div","tmsg",t))}
let tSig="",tAllTypes=[];
async function testInit(){
  const r=await api("test_options"); if(!r.ok){tMsg(r.error||"Could not read your library.");return}
  // only voices that have at least one recorded airport can be built; re-read the library every time the tab opens,
  // so a pack imported or recorded since last time shows up straight away
  const list=r.airlines.map(a=>({...a,voices:a.voices.filter(v=>v.airports.length)})).filter(a=>a.voices.length);
  const sig=JSON.stringify(list); if(sig===tSig&&tOpts)return; tSig=sig; tOpts=list; tAllTypes=r.all_types||[];
  const keep={air:$("#t-air").value,voice:$("#t-voice").value,dest:$("#t-dest").value,ac:$("#t-ac").value};
  const a=$("#t-air"); a.innerHTML=""; for(const x of tOpts){const o=el("option",null,x.icao+" - "+x.name);o.value=x.icao;a.appendChild(o)}
  if(!tOpts.length){$("#t-voice").innerHTML="";$("#t-dest").innerHTML="";tMsg("No pack in your library has a recorded airport yet. Import a pack (Download Packs tab) or record one in Pack Studio and press Save.");return}
  a.onchange=()=>testFillVoices(); $("#t-voice").onchange=()=>testFillDest(); $("#t-ac").oninput=()=>$("#t-ac").classList.remove("bad");$("#t-ac").onchange=()=>{$("#t-ac").value=$("#t-ac").value.trim().toUpperCase();if(testAcOk())testStale()}; $("#t-dest").onchange=testStale;
  $("#t-time").onchange=()=>{const m=$("#t-time").value.trim().match(/^(\d{1,2})[:.]?(\d{2})$/); if(m&&+m[1]<24&&+m[2]<60)$("#t-time").value=m[1].padStart(2,"0")+":"+m[2]; tSpoken(); testStale()};
  $("#t-time").oninput=tSpoken;
  $("#t-build").onclick=()=>testBuild(true);
  $("#t-flight").onclick=testFlight;
  $("#view-test").onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey&&!e.ctrlKey&&!e.altKey&&["INPUT","SELECT"].includes(e.target.tagName)&&e.target.id!=="t-seek"){e.preventDefault();$("#t-build").click()}};
  $("#t-all").onclick=()=>{ if(tPlaylist&&tAudio){ if(tPaused){tPaused=false;tAudio.play().catch(()=>{})}else{tPaused=true;tAudio.pause()} testMark(); return} testAll()};
  $("#t-skip").onclick=()=>{ if(tIdx<0||!tRows.length)return; tPlaylist=true; tPaused=false; testPlay(tIdx+1)};
  $("#t-stop").onclick=testStop;
  { let v=100; try{const s=localStorage.getItem("papro_test_vol"); if(s!==null&&!isNaN(+s))v=Math.max(0,Math.min(100,+s))}catch(e){}
    const sl=$("#t-vol"); sl.value=v; $("#t-volv").textContent=v+"%";
    sl.oninput=()=>{const x=+sl.value; $("#t-volv").textContent=x+"%"; try{localStorage.setItem("papro_test_vol",String(x))}catch(e){} if(tAudio)tAudio.volume=Math.min(1,(tAudio._base??1)*x/100)} }
  const sk=$("#t-seek"); sk.oninput=()=>{tDrag=true;const a=tAudio;if(a&&isFinite(a.duration))$("#t-cur").textContent=tFmt(sk.value/1000*a.duration)};
  sk.onchange=()=>{const a=tAudio;if(a&&isFinite(a.duration))a.currentTime=sk.value/1000*a.duration;tDrag=false;tBar()};
  if([...a.options].some(o=>o.value===keep.air))a.value=keep.air;
  testFillVoices(keep);
}
function testFillAc(keep){const x=tOpts.find(a=>a.icao===$("#t-air").value),mine=(x&&x.aircraft_types)||[],dl=$("#t-aclist");dl.innerHTML="";
  [...mine,...tAllTypes.filter(t=>!mine.includes(t))].forEach(t=>{const o=el("option");o.value=t;dl.appendChild(o)});
  $("#t-ac").value=keep&&keep.ac!=null?keep.ac:(mine[0]||"");$("#t-ac").classList.remove("bad")}
function testAcOk(){const v=$("#t-ac").value.trim().toUpperCase(),ok=!v||tAllTypes.includes(v);$("#t-ac").classList.toggle("bad",!ok);
  $("#t-ac").title=ok?"":"“"+v+"” is not an ICAO aircraft type";if(!ok)tMsg("“"+v+"” is not an ICAO aircraft type. Use codes like A320, B738 or B38M, or leave it empty for the normal PAs.");return ok}
function testFillVoices(keep){testFillAc(keep);const x=tOpts.find(a=>a.icao===$("#t-air").value),v=$("#t-voice"); v.innerHTML="";
  for(const y of x.voices){const o=el("option",null,y.name);o.value=y.key;v.appendChild(o)}
  if(keep&&keep.voice&&[...v.options].some(o=>o.value===keep.voice))v.value=keep.voice; testFillDest(keep)}
function testFillDest(keep){const x=tOpts.find(a=>a.icao===$("#t-air").value),y=x.voices.find(v=>v.key===$("#t-voice").value),d=$("#t-dest"); d.innerHTML="";
  testStop(); tKey=""; tRows=[];
  for(const p of y.airports){const o=el("option",null,p);o.value=p;d.appendChild(o)}
  if(keep&&keep.dest&&[...d.options].some(o=>o.value===keep.dest))d.value=keep.dest;
  if(!y.airports.length){const o=el("option",null,"(no airports recorded)");o.value="";d.appendChild(o);tMsg("This voice has no airport recordings, so there is nothing to build yet.");return}
  testStale()}
function testStale(){tCtx={origin:"",number:"",callsign:""};testStop();tKey="";tRows=[];$("#t-list").innerHTML="";tMsg("Choose your options, then press Go to build the announcements.")}
function testMark(){tBar();tRows.forEach((r,i)=>{r.row.classList.toggle("now",i===tIdx);r.btn.textContent=(i===tIdx&&tAudio)?"■":"▶"});
  $("#t-all").textContent=tPlaylist?(tPaused?"▶  Resume":"❚❚  Pause"):"▶  Test All";
  $("#t-all").classList.toggle("running",tPlaylist);
  for(const id of ["#t-skip","#t-stop"])$(id).style.display=tIdx>=0?"":"none";
  $("#t-now").textContent=tIdx>=0?(tPaused?"Paused ":"Playing ")+(tIdx+1)+" of "+tRows.length+": "+tRows[tIdx].label:""}
function testStop(){tPlaylist=false;tPaused=false; if(tAudio){tAudio.onended=null;tAudio.pause();tAudio=null} if(tUrl){URL.revokeObjectURL(tUrl);tUrl=null} tIdx=-1; testMark()}
function tSpoken(){ // the PAs speak minutes in steps of five: show what will actually be said
  const m=$("#t-time").value.trim().match(/^(\d{1,2})[:.]?(\d{2})$/),e=$("#t-spoken"); if(!m||+m[1]>23||+m[2]>59){e.textContent="";return}
  let h=+m[1],n=Math.round(+m[2]/5+1e-9)*5; if(n>=60){n=0;h=(h+1)%24}
  const t=String(h).padStart(2,"0")+":"+String(n).padStart(2,"0"); e.textContent=(+m[2]%5)?"spoken as "+t:""}
function tFmt(s){s=Math.max(0,Math.floor(s||0));return Math.floor(s/60)+":"+String(s%60).padStart(2,"0")}
let tDrag=false,tUrl=null;
function tBar(){const a=tAudio,on=!!a&&tIdx>=0;$("#t-bar").style.display=on?"flex":"none"; if(!on)return;
  const d=isFinite(a.duration)?a.duration:0; $("#t-cur").textContent=tFmt(a.currentTime); $("#t-dur").textContent=tFmt(d);
  if(!tDrag)$("#t-seek").value=d?Math.round(a.currentTime/d*1000):0}
function testPlay(i){ // play one clip; in playlist mode move on to the next when it ends
  if(tAudio){tAudio.onended=null;tAudio.pause();tAudio=null} if(tUrl){URL.revokeObjectURL(tUrl);tUrl=null}
  if(i<0||i>=tRows.length){const was=tPlaylist;testStop(); if(was)toast("Test finished"); return}
  tIdx=i; tPaused=false; const t=tRows[i], a=newAudio(); a._base=a.volume; a.volume=Math.min(1,a.volume*(+$("#t-vol").value)/100); tAudio=a; testMark(); t.row.scrollIntoView({block:"nearest",behavior:"smooth"});
  a.onended=()=>{ if(tAudio!==a)return; if(tPlaylist) testPlay(i+1); else testStop()};
  a.onerror=()=>{ if(tAudio!==a)return; toast("Could not play "+t.label); if(tPlaylist) testPlay(i+1); else testStop()};
  a.ontimeupdate=a.ondurationchange=a.onloadedmetadata=()=>{if(tAudio===a)tBar()};
  fetch("/test/audio?t="+TOKEN+"&key="+tKey+"&file="+encodeURIComponent(t.file)+"&n="+Date.now(),{cache:"no-store"}).then(r=>{if(!r.ok)throw 0;return r.blob()}).then(blob=>{
    if(tAudio!==a)return; tUrl=URL.createObjectURL(blob); a.src=tUrl; return a.play()}).catch(()=>{ if(tAudio===a&&!a.src){toast("Could not play "+t.label);testStop()}});
}
async function testAll(){ if(tBusy)return; if(!tKey||!tRows.length){tMsg("Press Go first to build the announcements, then Test All.");return} tPlaylist=true; testPlay(0)}
let tCtx={origin:"",number:"",callsign:""};
async function testFlight(){
  if(tBusy)return; tMsg("Reading your SimBrief flight...");
  const f=await api("test_flight"); if(!f.ok){tMsg(f.error||"Could not read your SimBrief flight.");return}
  const a=$("#t-air"); if(![...a.options].some(o=>o.value===f.airline)){tMsg("You have no voice with a recorded airport for "+f.airline+" ("+f.plan_airline+" on the plan), so there is nothing to test.");return}
  a.value=f.airline; testFillVoices({voice:f.voice,dest:f.dest,ac:f.aircraft});
  const dsel=$("#t-dest"); if(![...dsel.options].some(o=>o.value===f.dest)){const o=el("option",null,f.dest+" (not recorded)");o.value=f.dest;dsel.appendChild(o)} dsel.value=f.dest;
  $("#t-ac").value=f.aircraft||""; if(f.arrival){const m=f.arrival.match(/^(\d{1,2}):(\d{2})/); $("#t-time").value=m?m[1].padStart(2,"0")+":"+m[2]:f.arrival; tSpoken()}
  tCtx={origin:f.origin||"",number:f.number||"",callsign:f.callsign||""};
  await testBuild(true);
}
async function testBuild(rebuild){
  if(tBusy||!testAcOk())return; tBusy=true; testStop(); tMsg("Building your announcements..."); $("#t-all").disabled=true;
  const r=await api("test_build",{icao:$("#t-air").value,voice:$("#t-voice").value,dest:$("#t-dest").value,arrival:$("#t-time").value,aircraft:$("#t-ac").value.trim().toUpperCase(),rebuild:rebuild,origin:tCtx.origin,number:tCtx.number,callsign:tCtx.callsign});
  tBusy=false; $("#t-all").disabled=false;
  if(!r.ok){tKey="";tRows=[];tMsg(r.error||"Could not build - PAPro did not answer.");return}
  tKey=r.key; tRows=[]; const L=$("#t-list"); L.innerHTML=""; const groups={};
  for(const t of r.triggers)(groups[t.group]=groups[t.group]||[]).push(t);
  for(const g of Object.keys(groups)){   // already in the order the announcements are heard on a flight
    L.appendChild(el("div","section-head")).appendChild(el("h2",null,g));
    const c=el("div","card"); for(const t of groups[g]){const row=el("div","trow"),b=el("button","ghost","▶");
      const item={file:t.file,label:t.label,row:row,btn:b}; const idx=tRows.length; tRows.push(item);
      b.onclick=()=>{ if(tIdx===idx&&tAudio){testStop();return} tPlaylist=false; testPlay(idx)};
      row.append(b,el("span","tn",t.label),el("span","tf",t.file)); c.appendChild(row)}
    L.appendChild(c)}
  if(r.message){const m=el("div","small",r.message);m.style.cssText="margin:0 0 12px";L.prepend(m)}
  testMark();
}
$("#tab-main").onclick=()=>setView("main"); $("#tab-studio").onclick=()=>setView("studio"); $("#tab-dl").onclick=()=>setView("dl"); $("#tab-test").onclick=()=>setView("test");

function field(label,key,cur,ph,wide){const l=el("label",wide?"wide":"",label);const i=el("input");i.type="text";i.value=cur[key]||"";i.placeholder=ph||"";i.dataset.k=key;l.append(i);return l}
function skipMenu(btn,c,r){
  document.querySelectorAll(".skipmenu").forEach(m=>m.remove());
  const m=el("div","skipmenu");const rc=btn.getBoundingClientRect();
  const mk=(t,sub,mode)=>{const b=el("button",null,t);b.append(el("small",null,sub));b.onclick=()=>{m.remove();studio("skip",{...pid(c),slot:r.id,skip:true,mode})};return b};
  m.append(mk("Use the default sound","PAPro's default clip plays for this PA","default"),mk("No sound","A 1 second silent clip is added, so nothing plays","silent"));
  document.body.append(m);
  m.style.top=Math.min(rc.bottom+6,innerHeight-m.offsetHeight-8)+"px";m.style.left=Math.max(8,Math.min(rc.right-m.offsetWidth,innerWidth-m.offsetWidth-8))+"px";
  setTimeout(()=>document.addEventListener("click",()=>m.remove(),{once:true}),0);
}
function pid(c){return {icao:c.icao,voice:c.voice}}
async function studio(name,data){const r=await api("studio_"+name,data);await refreshNow();return r}
async function refreshNow(){try{const r=await fetch("/api/state",{headers:{"X-Token":TOKEN}});snap=await r.json();studioSig="";renderStudio()}catch(e){}}

function wavBlob(chunks,rate){
  let n=0;chunks.forEach(c=>n+=c.length);const buf=new ArrayBuffer(44+n*2),v=new DataView(buf);
  const w=(o,s)=>{for(let i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i))};
  w(0,"RIFF");v.setUint32(4,36+n*2,true);w(8,"WAVEfmt ");v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
  v.setUint32(24,rate,true);v.setUint32(28,rate*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);w(36,"data");v.setUint32(40,n*2,true);
  let o=44;chunks.forEach(c=>{for(let i=0;i<c.length;i++){const x=Math.max(-1,Math.min(1,c[i]));v.setInt16(o,x<0?x*0x8000:x*0x7fff,true);o+=2}});
  return new Blob([buf],{type:"audio/wav"});
}
// ---- microphone settings (kept in this window's browser storage) ----
function micPrefs(){try{return Object.assign({device:"",gain:0,out:"",vol:100},JSON.parse(localStorage.getItem("paproMic")||"{}"))}catch(e){return{device:"",gain:0}}}
function micSave(p){try{localStorage.setItem("paproMic",JSON.stringify(p))}catch(e){}}
async function micStream(){
  const p=micPrefs(), a={echoCancellation:false,noiseSuppression:false,autoGainControl:false,channelCount:1};
  if(p.device)a.deviceId={exact:p.device};
  try{return await navigator.mediaDevices.getUserMedia({audio:a})}
  catch(e){if(p.device){micSave({...p,device:""});return await navigator.mediaDevices.getUserMedia({audio:{...a,deviceId:undefined}})}throw e}
}
let micCtx=null, micTimer=null, micStreamNow=null;
// Playback volume (0-200%): up to 100% is the element volume, above that a WebAudio gain adds the boost the sim applies.
let volCtx=null;const volNodes=new Map();
function applyVol(){const v=Math.max(0,micPrefs().vol)/100;volNodes.forEach((g,a)=>{if(!a.isConnected&&a.paused&&a.ended)volNodes.delete(a);g.gain.value=v});document.querySelectorAll("audio").forEach(a=>{a.volume=Math.min(1,v)});
  for(const a of [typeof player!=="undefined"&&player&&player.audio,typeof tAudio!=="undefined"&&tAudio])if(a&&!volNodes.has(a))a.volume=Math.min(1,v)}
function newAudio(url){const a=url?new Audio(url):new Audio(),p=micPrefs(),v=Math.max(0,p.vol)/100;a.volume=Math.min(1,v);
  if(v>1){try{volCtx=volCtx||new (window.AudioContext||window.webkitAudioContext)();const g=volCtx.createGain();g.gain.value=v;volCtx.createMediaElementSource(a).connect(g).connect(volCtx.destination);volNodes.set(a,g);
    if(p.out&&volCtx.setSinkId)volCtx.setSinkId(p.out).catch(()=>{});a.addEventListener("play",()=>{if(volCtx.state==="suspended")volCtx.resume()});return a}catch(e){}}
  if(p.out&&a.setSinkId)a.setSinkId(p.out).catch(()=>{});return a}
async function micStop(){clearInterval(micTimer);micTimer=null;if(micStreamNow){micStreamNow.getTracks().forEach(t=>t.stop());micStreamNow=null}if(micCtx){try{await micCtx.close()}catch(e){}micCtx=null}}
async function micStart(){
  await micStop();const msg=$("#mic-msg"),p=micPrefs();
  let err=null;
  try{micStreamNow=await micStream()}catch(e){err=e}
  let all=[];try{all=await navigator.mediaDevices.enumerateDevices()}catch(e){}
  const devs=all.filter(d=>d.kind==="audioinput"),sel=$("#mic-dev");sel.textContent="";
  const cur=micStreamNow?micStreamNow.getAudioTracks()[0].getSettings().deviceId:"";
  devs.forEach((d,i)=>{const o=el("option",null,d.label||"Microphone "+(i+1));o.value=d.deviceId;sel.append(o)});
  if(!devs.length){const o=el("option",null,"No microphone found");o.value="";sel.append(o)}
  sel.value=p.device&&devs.some(d=>d.deviceId===p.device)?p.device:cur;
  const outs=all.filter(d=>d.kind==="audiooutput"),os=$("#out-dev");os.textContent="";
  outs.forEach((d,i)=>{const o=el("option",null,d.label||"Speakers "+(i+1));o.value=d.deviceId;os.append(o)});
  os.value=p.out&&outs.some(d=>d.deviceId===p.out)?p.out:(outs[0]?outs[0].deviceId:"");
  os.disabled=!("setSinkId" in HTMLMediaElement.prototype);$("#out-vol").value=p.vol;$("#out-gv").textContent=p.vol+"%";
  $("#mic-gain").value=p.gain;$("#mic-gv").textContent=(p.gain>0?"+":"")+p.gain+" dB";
  if(err){const n=err.name||"";
    msg.textContent=n==="NotAllowedError"||n==="SecurityError"?"The microphone is blocked. In Windows open Settings > Privacy & security > Microphone and turn on Microphone access and 'Let desktop apps access your microphone', then reopen this.":
      n==="NotFoundError"||n==="OverconstrainedError"?"No microphone was found. Plug one in (or enable it in Windows Sound settings > Input) and reopen this.":
      "Could not open the microphone ("+(n||"error")+(err.message?": "+err.message:"")+").";
    return}
  micCtx=new (window.AudioContext||window.webkitAudioContext)();const an=micCtx.createAnalyser();an.fftSize=1024;micCtx.createMediaStreamSource(micStreamNow).connect(an);
  const buf=new Float32Array(an.fftSize);let peak=0,hold=0,clip=0;
  micTimer=setInterval(()=>{an.getFloatTimeDomainData(buf);const g=Math.pow(10,micPrefs().gain/20);let m=0;for(const v of buf)m=Math.max(m,Math.abs(v)*g);
    peak=Math.max(m,peak*.9);hold=Math.max(m,hold*.995);if(m>=.99)clip=20;else if(clip)clip--;
    const db=m>0?20*Math.log10(m):-90,w=Math.max(0,Math.min(1,(db+60)/60));$("#mic-bar").style.width=(w*100)+"%";
    const hd=hold>0?20*Math.log10(hold):-90;$("#mic-peak").style.left=Math.max(0,Math.min(1,(hd+60)/60))*100+"%";
    msg.textContent=clip?"Too loud - it is clipping. Turn the boost down or move back.":hd<-30?"Very quiet - move closer or raise the boost.":hd<-18?"A bit quiet - fine, PAPro evens it out, but a little more is better.":hd>-3?"Close to clipping - back off a little.":"Good level.";
  },60);
}
function openMic(){$("#m-mic").classList.add("show");micStart()}
$("#mic-dev").onchange=()=>{micSave({...micPrefs(),device:$("#mic-dev").value});micStart()};
$("#out-dev").onchange=()=>micSave({...micPrefs(),out:$("#out-dev").value});
$("#out-vol").oninput=()=>{const v=+$("#out-vol").value;micSave({...micPrefs(),vol:v});$("#out-gv").textContent=v+"%";applyVol()};
$("#mic-gain").oninput=()=>{const g=+$("#mic-gain").value;micSave({...micPrefs(),gain:g});$("#mic-gv").textContent=(g>0?"+":"")+g+" dB"};
$("#mic-done").onclick=async()=>{await micStop();$("#m-mic").classList.remove("show")};
$("#mic-test").onclick=async()=>{
  const b=$("#mic-test");if(b.disabled)return;b.disabled=true;
  try{const rec=await recordClip(3000,t=>b.textContent="Recording... "+Math.ceil(t)+" s");b.textContent="Playing back...";const url=URL.createObjectURL(rec);const a=newAudio(url);a.onended=()=>{b.disabled=false;b.textContent="Test: record 3 s and play back"};a.play()}
  catch(e){toast("Could not record - check the microphone");b.disabled=false;b.textContent="Test: record 3 s and play back"}
};
async function recordClip(ms,tick){ // records for ms from the chosen microphone with the chosen boost; returns a WAV blob
  const stream=await micStream(),ctx=new (window.AudioContext||window.webkitAudioContext)(),src=ctx.createMediaStreamSource(stream),node=ctx.createScriptProcessor(4096,1,1),chunks=[],g=Math.pow(10,micPrefs().gain/20);
  node.onaudioprocess=e=>chunks.push(Float32Array.from(e.inputBuffer.getChannelData(0),v=>v*g));src.connect(node);node.connect(ctx.destination);
  const t0=Date.now();await new Promise(r=>{const i=setInterval(()=>{const left=(ms-(Date.now()-t0))/1000;if(tick)tick(left);if(left<=0){clearInterval(i);r()}},100)});
  node.disconnect();src.disconnect();stream.getTracks().forEach(t=>t.stop());const rate=ctx.sampleRate;await ctx.close();return wavBlob(chunks,rate);
}
function micAlert(title,text){$("#q-t").textContent=title;$("#q-s").textContent=text;$("#m-quiet").classList.add("show")}
$("#q-close").onclick=()=>$("#m-quiet").classList.remove("show");
$("#q-open").onclick=()=>{$("#m-quiet").classList.remove("show");openMic()};
async function startRec(slot,btn){
  if(recState){return}
  let stream;
  try{stream=await micStream()}
  catch(e){micAlert("No microphone access","PAPro could not open a microphone. Allow microphone access when the window asks, check that one is plugged in and not blocked in Windows privacy settings, or upload a recording instead.");return}
  const ctx=new (window.AudioContext||window.webkitAudioContext)(), src=ctx.createMediaStreamSource(stream), node=ctx.createScriptProcessor(4096,1,1), chunks=[];
  const gain=Math.pow(10,micPrefs().gain/20);
  node.onaudioprocess=e=>chunks.push(Float32Array.from(e.inputBuffer.getChannelData(0),v=>v*gain));
  src.connect(node);node.connect(ctx.destination);
  recState={slot,btn,t0:Date.now(),stop:async()=>{
    node.disconnect();src.disconnect();stream.getTracks().forEach(t=>t.stop());const rate=ctx.sampleRate;await ctx.close();
    const st=recState;recState=null;clearInterval(st.timer);const rw=btn.closest(".slot");if(rw)rw.classList.remove("reading");
    let peak=0;for(const ch of chunks)for(let i=0;i<ch.length;i++){const v=Math.abs(ch[i]);if(v>peak)peak=v}
    if(!chunks.length||peak<0.003){ // nothing but silence: do not keep the take
      studioSig="";renderStudio();micAlert("No sound was picked up","The recording is silent. Check that the right microphone is selected, that it is not muted, and that the level meter moves when you speak - then record it again.");return}
    await upload(slot,wavBlob(chunks,rate),"take.wav");
    if(peak<0.03)micAlert("That recording is very quiet","It was saved, but the microphone level is low. Open Audio settings, raise the input boost or move closer, and re-record for the best result.");}};
  btn.textContent="■ Stop 0:00";btn.classList.add("live");const row=btn.closest(".slot");if(row){row.classList.add("reading");requestAnimationFrame(()=>row.scrollIntoView({block:"center"}))}
  recState.timer=setInterval(()=>{const s=Math.floor((Date.now()-recState.t0)/1000);btn.textContent="■ Stop "+Math.floor(s/60)+":"+String(s%60).padStart(2,"0")},250);
}
async function upload(slot,blob,name){
  const c=snap.studio.current; toast("Processing...");
  const q=new URLSearchParams({icao:c.icao,voice:c.voice,slot,filename:name,t:TOKEN});
  try{const r=await fetch("/studio/upload?"+q,{method:"POST",headers:{"X-Token":TOKEN},body:blob});const j=await r.json();
    if(j.error)toast(j.error,9000);else toast("Done - "+j.seconds+"s");}
  catch(e){toast("Upload failed - the recording was not saved. Try again or use Upload.",9000)}
  await refreshNow();
}
let player=null; // {id, audio}: the clip being played, paused or resumed
function syncPlayer(){
  document.querySelectorAll("#view-studio .pp").forEach(b=>{const on=player&&player.id===b.dataset.id,playingNow=on&&!player.audio.paused;
    b.textContent=playingNow?"⏸":"▶";b.title=playingNow?"Pause":"Play"});
  document.querySelectorAll("#view-studio .stp").forEach(b=>{b.style.display=player&&player.id===b.dataset.id?"":"none"});
  syncSeek();
}
// a seek bar under the clip that is playing (or paused): drag or click to jump, arrow keys step
function fmtT(t){t=Math.max(0,Math.floor(t||0));return Math.floor(t/60)+":"+String(t%60).padStart(2,"0")}
function syncSeek(){
  document.querySelectorAll("#view-studio .slot").forEach(slot=>{
    const b=slot.querySelector(".pp"),on=!!(b&&player&&player.id===b.dataset.id);let row=slot.querySelector(".seekrow");
    if(!on){if(row)row.remove();return}
    if(!row){
      row=el("div","seekrow");const cur=el("span","tt","0:00"),rg=document.createElement("input"),dur=el("span","tt","0:00");
      rg.type="range";rg.min=0;rg.max=1000;rg.value=0;rg.step=1;rg.setAttribute("aria-label","Playback position");
      rg.oninput=()=>{const a=player&&player.audio;row.dataset.drag="1";if(a&&isFinite(a.duration)){a.currentTime=rg.value/1000*a.duration;cur.textContent=fmtT(a.currentTime)}};
      rg.onchange=()=>{delete row.dataset.drag};rg.onpointerup=()=>{delete row.dataset.drag};
      row.append(cur,rg,dur);slot.append(row);
    }
    updateSeek(row)})
}
function updateSeek(row){
  const a=player&&player.audio;if(!a)return;const d=isFinite(a.duration)?a.duration:0,sp=row.children;
  sp[0].textContent=fmtT(a.currentTime);sp[2].textContent=fmtT(d);
  if(!row.dataset.drag)sp[1].value=d?Math.round(a.currentTime/d*1000):0}
function tickSeek(){document.querySelectorAll("#view-studio .seekrow").forEach(updateSeek)}
function togglePlay(c,id){
  if(player&&player.id===id){const a=player.audio;if(a.paused)a.play().catch(()=>{});else a.pause();syncPlayer();return}
  stopPlay();
  const a=newAudio();player={id,audio:a};
  ["play","pause"].forEach(e=>a.addEventListener(e,syncPlayer));["timeupdate","loadedmetadata","durationchange"].forEach(e=>a.addEventListener(e,tickSeek));
  a.addEventListener("ended",()=>{if(player&&player.audio===a)player=null;syncPlayer()});syncPlayer();
  // fetched whole first, so the seek bar can jump anywhere in the clip
  fetch("/studio/audio?"+new URLSearchParams({icao:c.icao,voice:c.voice,slot:id,t:TOKEN,v:Date.now()})).then(r=>{if(!r.ok)throw 0;return r.blob()}).then(blob=>{
    if(!player||player.audio!==a)return;a.src=URL.createObjectURL(blob);return a.play()}).then(syncPlayer).catch(()=>{if(player&&player.audio===a)player=null;syncPlayer();toast("Could not play that")});
}
function stopPlay(){if(player){const a=player.audio;player=null;a.pause();a.currentTime=0}syncPlayer()}
function typesPicker(c,r,anchor){
  document.querySelectorAll(".typpop").forEach(x=>x.remove());
  const valid=new Set(c.aircraft_types||[]);let types=r.types.filter(t=>t!=="ALL");
  const pop=el("div","typpop"),all=el("label"),ac=el("input");ac.type="checkbox";ac.checked=!types.length;all.append(ac,document.createTextNode("All aircraft"));pop.append(all,el("hr"));
  const chips=el("div","chiprow");chips.style.minHeight="28px";const inp=el("input");inp.type="text";inp.maxLength=4;inp.placeholder="Type an ICAO code, e.g. A320";inp.style.textTransform="uppercase";inp.setAttribute("list","typlist");inp.autocomplete="off";
  const err=el("div","small");err.style.cssText="color:var(--err);min-height:16px;margin-top:4px";
  const dl=el("datalist");dl.id="typlist";(c.aircraft_types||[]).forEach(t=>{const o=el("option");o.value=t;dl.append(o)});
  const draw=()=>{chips.textContent="";types.forEach(t=>{const b=el("button","ghost",t+" ✕");b.style.padding="2px 9px";b.onclick=()=>{types=types.filter(x=>x!==t);ac.checked=!types.length;draw()};chips.append(b)});ac.checked=!types.length};
  const add=()=>{const v=inp.value.trim().toUpperCase();if(!v)return true;
    if(!valid.has(v)){err.textContent="“"+v+"” is not an ICAO aircraft type (codes like A320, B738, B38M)";return false}
    err.textContent="";if(!types.includes(v))types.push(v);inp.value="";draw();return true};
  ac.onchange=()=>{if(ac.checked){types=[];draw()}else if(!types.length)ac.checked=true};
  inp.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();add()}});inp.addEventListener("input",()=>{err.textContent="";if(inp.value.trim().length>=3&&valid.has(inp.value.trim().toUpperCase()))add()});
  pop.append(chips,inp,dl,err);draw();
  document.body.append(pop);const rc=anchor.getBoundingClientRect();pop.style.left=Math.max(8,Math.min(innerWidth-266,rc.left))+"px";pop.style.top=Math.min(innerHeight-pop.offsetHeight-8,rc.bottom+6)+"px";inp.focus();
  let closed=false;const close=async()=>{if(closed)return;if(inp.value.trim()&&!add()){inp.focus();return}
    closed=true;document.removeEventListener("mousedown",away,true);pop.remove();dl.remove();
    const next=types.length?types:["ALL"];if(JSON.stringify([...next].sort())===JSON.stringify([...r.types].sort()))return;
    const res=await studio("typed_types",{...pid(c),slot:r.id,types:next});if(res&&res.ok)toast("Now used on: "+(next.includes("ALL")?"all aircraft":next.join(", ")));studioSig="";renderStudio()};
  const away=e=>{if(!pop.contains(e.target)&&e.target!==anchor)close()};setTimeout(()=>document.addEventListener("mousedown",away,true),0);
  pop.addEventListener("keydown",e=>{if(e.key==="Escape"){inp.value="";close()}});
}
function slotRow(c,r){
  const d=el("div","slot"+(r.done?" done":"")+(r.skipped?" skipped":""));
  const nm=el("div","nm2",r.label); if(r.skipped)nm.append(el("em","sk","skipped - default sound plays")); else if(r.silent)nm.append(el("em","sk","no sound")); else if(r.required&&!r.done)nm.append(el("em",null,"needed"));
  if(r.typed){const tb=el("button","typbtn","✈ "+(r.types.includes("ALL")?"All aircraft":r.types.join(", "))+" ▾");tb.title="Aircraft types this announcement plays on";tb.onclick=e=>{e.stopPropagation();typesPicker(c,r,tb)};nm.append(tb)}
  if(r.modes){const ms=el("select","voice");ms.style.cssText="margin-left:10px;padding:3px 8px;font-size:12px";ms.title="How Fenix plays it: Prerecorded = chosen in the cabin crew panel; Manual = the purser announces it (adds [Manual] to the file name)";
    r.modes.forEach(m=>{const o=el("option",null,m);o.value=m;ms.append(o)});ms.value=r.mode;ms.onchange=()=>studio("mode",{...pid(c),slot:r.id,mode:ms.value});nm.append(ms)}
  d.append(nm);
  const scl=el("div","sc editable"+(r.custom?" custom":""),r.script?(r.custom||r.music?r.script:"Say: "+r.script):"Click to add a script");
  scl.title="Click to edit this script. Enter saves it, Shift+Enter adds a line, Esc undoes.";
  scl.onclick=()=>{
    if(scl.querySelector("textarea")||recState)return;
    const ta=el("textarea","sc-edit");ta.value=r.script||"";ta.rows=1;scl.textContent="";scl.classList.remove("custom");scl.append(ta);
    const fit=()=>{ta.style.height="auto";ta.style.height=ta.scrollHeight+"px"};fit();ta.focus();ta.setSelectionRange(ta.value.length,ta.value.length);
    let timer=null,finished=false;const save=text=>api("studio_script",{...pid(c),slot:r.id,text});
    ta.oninput=()=>{fit();clearTimeout(timer);timer=setTimeout(()=>save(ta.value),250)};   // saved as you type
    const finish=async(undo)=>{if(finished)return;finished=true;clearTimeout(timer);
      const text=undo?(r.custom?r.script:""):ta.value;ta.blur();await save(text);await refreshNow()};
    ta.onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();finish(false)}else if(e.key==="Escape"){e.preventDefault();finish(true)}};
    ta.onblur=()=>finish(false);
  };
  d.append(scl);
  if(r.variants&&r.variants.length)d.append(el("div","st","Also has: "+r.variants.join(", ")));
  if(r.silent)d.append(el("div","st","No sound: a 1 second silent clip overrides the default pack's clip"));
  else if(r.done)d.append(el("div","st",r.imported?"From your library - unchanged"+(r.lufs!=null?" · "+r.lufs+" LUFS":""):r.seconds+" s"+(r.music?" · unchanged":"")));
  const act=el("div","act");
  const rec=el("button","rec",r.done?"● Re-record":"● Record");rec.onclick=()=>{if(recState&&recState.slot===r.id){recState.stop()}else startRec(r.id,rec)};
  if(r.music)rec.style.display="none";
  const up=el("button",null,"Upload");const f=el("input");f.type="file";f.accept="audio/*,.wav,.mp3,.ogg,.m4a,.flac";f.style.display="none";
  f.onchange=()=>{if(f.files[0])upload(r.id,f.files[0],f.files[0].name)};up.onclick=()=>f.click();act.append(rec,up,f);
  if(r.done){const xp=el("button","sq");xp.title="Export this recording as a single .ogg file, to send for testing or tweaking";
    xp.innerHTML='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/></svg>';
    xp.onclick=()=>{const q=new URLSearchParams({icao:c.icao,voice:c.voice,slot:r.id,t:TOKEN});const a=document.createElement("a");a.href="/studio/audio?"+q;
      a.download=(c.icao+"_"+c.voice+"_"+r.id.replace(/[\\/:*?"<>|]+/g,"_").replace(/\.ogg$/i,"")).replace(/\s+/g,"_")+".ogg";document.body.appendChild(a);a.click();a.remove();toast("Exported "+r.label)};act.append(xp)}
  if(r.silent){const us=el("button","ghost","Unskip");us.title="Remove the silent clip and put this back on the checklist";us.onclick=()=>studio("skip",{...pid(c),slot:r.id,skip:false});act.prepend(us)}
  if(r.skipped){const un=el("button","ghost","Unskip");un.title="Put this back on the checklist";un.onclick=()=>studio("skip",{...pid(c),slot:r.id,skip:false});const ns=el("button","ghost","No sound");ns.title="Skipped: the default pack's clip plays. Press to play nothing instead (a 1 second silent clip is added)";ns.onclick=()=>studio("skip",{...pid(c),slot:r.id,skip:true,mode:"silent"});act.append(un,ns)}
  else if(!r.done&&!r.music){const sk=el("button","ghost","Skip ▾");sk.title="Not recording this one";sk.onclick=e=>{e.stopPropagation();skipMenu(sk,c,r)};act.append(sk)}
  const isAc=r.folder==="aircraft",isCrew=r.crew==="base"||r.crew==="name"||!!r.service,isAp=r.folder==="airports"||isAc||isCrew,apCode=r.folder==="airports"?r.id.slice(("airports/"+c.icao+"_").length):r.label;
  if(r.done||isAp){const p=el("button","pp","▶");p.dataset.id=r.id;p.title="Play";p.onclick=()=>togglePlay(c,r.id);const sp2=el("button","stp","⏹");sp2.dataset.id=r.id;sp2.title="Stop";sp2.style.display="none";sp2.onclick=stopPlay;const x=el("button","danger","✕");x.title=isAp?(isCrew?(r.service?"Remove this version":"Remove this from the crew introduction"):isAc?"Remove this aircraft":"Remove this airport")+(r.done?" and its recording":""):"Remove";let armT=null;x.onclick=()=>{if(!x.classList.contains("arm")){x.classList.add("arm");x.textContent="Delete?";x.title="Click again to delete";armT=setTimeout(()=>{x.classList.remove("arm");x.textContent="✕";x.title="Remove"},3500);return}clearTimeout(armT);isCrew?(r.service?studio("service",{...pid(c),name:r.service,remove:true}):r.crew==="base"?studio("crew_base",{...pid(c),code:r.stem.slice(5),remove:true}):studio("crew_name",{...pid(c),name:r.label.replace("Crew name: ",""),remove:true})):isAp?studio(isAc?"aircraft":"airport",{...pid(c),code:apCode,remove:true}):studio("delete",{...pid(c),slot:r.id})};if(r.done)act.append(p,sp2);if(!(r.typed||r.variant_of))act.append(x)}
  if(!r.typed&&!r.variant_of&&Object.keys(c.typed_bases||{}).includes(r.stem)){const at=el("button","sq ghost");at.title="+ aircraft version";at.setAttribute("aria-label","+ aircraft version");
    at.innerHTML='<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z"/></svg>';
    at.onclick=async()=>{const x=await studio("typed_add",{...pid(c),base:r.stem,types:["ALL"]});if(x.ok){openGroups.add(r.group);toast("Added below - choose which aircraft types it is for");studioSig="";renderStudio()}};act.append(at)}
  if(r.typed){const xt=el("button","danger","✕");xt.title="Remove this aircraft PA";let armT=null;xt.onclick=()=>{if(!xt.classList.contains("arm")){xt.classList.add("arm");xt.textContent="Delete?";armT=setTimeout(()=>{xt.classList.remove("arm");xt.textContent="✕"},3000);return}clearTimeout(armT);studio("typed_remove",{...pid(c),slot:r.id})};act.append(xt)}
  if(r.variant_of){const xv=el("button","danger","✕");xv.title="Remove this variation";let armV=null;xv.onclick=()=>{if(!xv.classList.contains("arm")){xv.classList.add("arm");xv.textContent="Delete?";armV=setTimeout(()=>{xv.classList.remove("arm");xv.textContent="✕"},3500);return}clearTimeout(armV);studio("variant",{...pid(c),base:r.variant_of,n:r.variant_n,remove:true})};act.append(xv)}
  if(r.custom){const cs=el("button","sq ghost","✕");cs.title="Remove this item's script";cs.style.fontSize="11px";cs.onclick=()=>studio("script",{...pid(c),slot:r.id,text:""});act.insertBefore(cs,up.nextSibling)}
  if(r.variantable){const av=el("button","sq ghost","+");av.title="Add a variation: record another take of this announcement. PAPro picks one at random each flight.";av.style.fontSize="18px";av.style.lineHeight="1";av.onclick=async()=>{openGroups.add(r.group);await studio("variant",{...pid(c),base:r.stem});studioSig="";renderStudio()};act.append(av)}
  d.append(act);return d;
}
function faEditor(c,d){
  const cr=c.crew||{fa:{}};
  d.append(el("div","small","Flight attendants per aircraft type (the number is said in the introduction; the crew introduction is used only on aircraft types listed here):"));
  const fa={...cr.fa};const list=el("div");list.style.cssText="display:flex;flex-direction:column;gap:6px;margin:8px 0;max-width:360px";
  const saveFa=async()=>{const r=await api("studio_crew",{...pid(c),fa});if(r.ok){cr.fa={...fa}}else if(r.error)toast(r.error,5000)};
  const draw=()=>{list.textContent="";Object.keys(fa).sort().forEach(t=>{const row=el("div");row.style.cssText="display:flex;align-items:center;gap:8px";
    const nm=el("b",null,t);nm.style.cssText="width:70px";const n=el("input");n.type="number";n.min=1;n.max=12;n.value=fa[t];n.style.width="70px";n.onchange=()=>{fa[t]=Math.max(1,Math.min(12,+n.value||1));n.value=fa[t];saveFa()};
    const x=el("button","ghost","✕");x.onclick=()=>{delete fa[t];draw();saveFa()};row.append(nm,n,el("span","small","flight attendants"),x);list.append(row)})};
  draw();d.append(list);
  const add=el("div","addline"),ti=el("input");ti.type="text";ti.placeholder="Aircraft type, e.g. A320";ti.setAttribute("list","crewtypes");ti.style.textTransform="uppercase";
  const dl=el("datalist");dl.id="crewtypes";(c.aircraft_types||[]).forEach(t=>{const o=el("option");o.value=t;dl.append(o)});
  const ni=el("input");ni.type="number";ni.min=1;ni.max=12;ni.value=4;ni.style.width="70px";const ab=el("button",null,"Add");
  ab.onclick=()=>{const t=ti.value.trim().toUpperCase();if(!t)return;fa[t]=Math.max(1,Math.min(12,+ni.value||4));ti.value="";draw();saveFa()};
  add.append(ti,dl,ni,ab);d.append(add);
}
function crewBaseAdd(c,d){

        const bl=el("div","addline"),bi=el("input");bi.type="text";bi.placeholder="Base airport, e.g. KATL (several: KATL, KJFK)";bi.style.textTransform="uppercase";const bb=el("button",null,"Add base");
        bb.onclick=async()=>{if(!bi.value.trim())return;const r=await studio("crew_base",{...pid(c),code:bi.value});if(r.error)toast(r.error,5000);else openCrew("Bases")};
        bi.addEventListener("keydown",e=>{if(e.key==="Enter")bb.click()});bl.append(bi,bb);d.append(bl)
}
function crewNameAdd(c,d){

        const nl=el("div","addline");nl.style.marginTop="8px";const ni=el("input");ni.type="text";ni.placeholder="Crew first name, e.g. Sarah";const nb=el("button",null,"Add name"),nr=el("button","ghost","Random name");
        nr.title="Suggest a name to record. It keeps the crew about half men and half women.";
        nr.onclick=async()=>{const r=await api("studio_crew_random",pid(c));if(r.name){ni.value=r.name;toast(r.name+" ("+r.gender+") - press Add name, then record it")}};
        nb.onclick=async()=>{if(!ni.value.trim())return;const r=await studio("crew_name",{...pid(c),name:ni.value});if(r.error)toast(r.error,5000);else openCrew("Crew names")};
        ni.addEventListener("keydown",e=>{if(e.key==="Enter")nb.click()});nl.append(ni,nr,nb);d.append(nl)
}
function openCrew(sub){openGroups.add("Safety briefing");openGroups.add("Safety briefing/Crew introduction");if(sub)openGroups.add("Safety briefing/Crew introduction/"+sub)}
function crewControls(c,d){
      const cr=c.crew||{order:[],fa:{}};let order=[...cr.order];
      d.append(el("div","small","The pre-safety briefing is built for each flight from these pieces, in the order below (drag to reorder, ✕ to delete a piece, the buttons add more). The number of flight attendants comes from the aircraft type (set above) and is said with the hour clips (1-12) from After Landing. A flight from a recorded base uses that base, otherwise one is picked at random, and each flight attendant gets a different random name."));
      const intros=c.rows.filter(r=>r.crew==="intro"),bases=c.rows.filter(r=>r.crew==="base"),names=c.rows.filter(r=>r.crew==="name");
      const miss=[];if(!intros.length)miss.push("an intro part");intros.filter(r=>!r.done).forEach(r=>miss.push(r.label));
      if(order.includes("base")&&!bases.some(r=>r.done))miss.push("at least one base");
      const most=Math.max(1,...Object.values(cr.fa||{}).map(Number));const nDone=names.filter(r=>r.done).length;
      if(order.includes("names")&&nDone<most)miss.push((most-nDone)+" more crew name"+(most-nDone>1?"s":"")+" (at least "+most+" recorded, the largest crew in Crew introduction settings)");
      const warn=el("div","small",miss.length?"Not complete yet - until it is, the normal Pre-safety briefing is used. Still to do: "+miss.join(", ")+".":"Complete - this introduction replaces the normal Pre-safety briefing on aircraft types that have a number set.");
      warn.style.cssText="margin:8px 0;color:"+(miss.length?"var(--warn,#d9822b)":"var(--ok,#3aa76d)");d.append(warn);
      const lab=k=>k.startsWith("intro:")?"Intro part "+k.slice(6):k==="number"?"Number of flight attendants":k==="base"?"Base":"Crew names";
      const ol=el("div");ol.style.cssText="display:flex;flex-direction:column;gap:6px;max-width:460px;margin:10px 0";
      const saveOrder=async()=>{await api("studio_crew",{...pid(c),order});cr.order=[...order]};
      let dragFrom=-1;
      const drawOrder=()=>{ol.textContent="";order.forEach((k,i)=>{const row=el("div");row.draggable=true;row.style.cssText="display:flex;align-items:center;gap:8px;padding:6px 10px;border:1px solid var(--line);border-radius:10px;cursor:grab";
        const gp=el("span",null,"⠿");gp.style.cssText="color:var(--muted);font-size:16px";row.append(gp);
        const t=el("b",null,(i+1)+". "+lab(k));t.style.flex="1";row.append(t);
        const up=el("button","ghost","↑"),dn=el("button","ghost","↓"),x=el("button","danger","✕");up.style.padding=dn.style.padding="2px 9px";up.disabled=i===0;dn.disabled=i===order.length-1;
        x.title="Delete this piece from the introduction";
        up.onclick=()=>{[order[i-1],order[i]]=[order[i],order[i-1]];drawOrder();saveOrder()};dn.onclick=()=>{[order[i+1],order[i]]=[order[i],order[i+1]];drawOrder();saveOrder()};
        let arm=null;x.onclick=async()=>{if(!x.classList.contains("arm")){x.classList.add("arm");x.textContent="Delete?";arm=setTimeout(()=>{x.classList.remove("arm");x.textContent="✕"},3000);return}clearTimeout(arm);openCrew();await studio("crew_section",{...pid(c),item:k,remove:true})};
        row.ondragstart=e=>{dragFrom=i;e.dataTransfer.effectAllowed="move";try{e.dataTransfer.setData("text/plain",String(i))}catch(_){}row.style.opacity=".5"};
        row.ondragend=()=>{row.style.opacity="";[...ol.children].forEach(n=>n.style.outline="")};
        row.ondragover=e=>{e.preventDefault();e.dataTransfer.dropEffect="move";row.style.outline="2px solid var(--accent,#8a5cf6)"};
        row.ondragleave=()=>{row.style.outline=""};
        row.ondrop=e=>{e.preventDefault();row.style.outline="";if(dragFrom<0||dragFrom===i)return;const [m]=order.splice(dragFrom,1);order.splice(i,0,m);dragFrom=-1;drawOrder();saveOrder()};
        row.append(up,dn,x);ol.append(row)});
        if(!order.length)ol.append(el("div","small","Nothing here yet - add a piece below."))};
      drawOrder();const ot=el("div","small","Order the pieces are said in");ot.style.margin="10px 0 4px";d.append(ot,ol);
      const ctl=el("div","row");ctl.style.cssText="justify-content:flex-start;gap:8px;margin-bottom:10px;flex-wrap:wrap";
      const addBtn=(text,item,title)=>{const b=el("button","ghost",text);b.title=title;b.onclick=async()=>{openCrew();const r=await studio("crew_section",{...pid(c),item});if(r.error)toast(r.error,5000)};ctl.append(b)};
      addBtn("+ Intro part","intro","Add another recorded part to the introduction");
      if(!order.includes("number"))addBtn("+ Number","number","Say the number of flight attendants");
      if(!order.includes("base"))addBtn("+ Base","base","Say a base");
      if(!order.includes("names"))addBtn("+ Crew names","names","Say the crew's names");
      d.append(ctl);

}
function openDescent(){openGroups.add("Descent & approach");openGroups.add("Descent & approach/Descent Started City-based PA")}
let savedSig="";
const LANDING_SUB="After Landing PA",BOARDING_SUB="Boarding Welcome PA";
function openBoarding(){openGroups.add("Boarding");openGroups.add("Boarding/Boarding Welcome PA")}
function openLanding(){openGroups.add("After Landing");openGroups.add("After Landing/After Landing PA")}
// a PA built from recorded parts plus one special piece (the city / the crew base), in an order the pack creator sets
function pieceControls(c,d,o){
  let order=[...o.order];
  d.append(el("div","small",o.intro));
  const lab=k=>o.specials[k]?o.specials[k].label:"Part "+k.slice(5);
  const ol=el("div");ol.style.cssText="display:flex;flex-direction:column;gap:6px;max-width:460px;margin:10px 0";
  const saveOrder=async()=>{await api(o.orderAction,{...pid(c),order});o.save([...order])};
  let dragFrom=-1;
  const draw=()=>{ol.textContent="";order.forEach((k,i)=>{const row=el("div");row.draggable=true;row.style.cssText="display:flex;align-items:center;gap:8px;padding:6px 10px;border:1px solid var(--line);border-radius:10px;cursor:grab";
    const gp=el("span",null,"⠿");gp.style.cssText="color:var(--muted);font-size:16px";row.append(gp);
    const t=el("b",null,(i+1)+". "+lab(k));t.style.flex="1";row.append(t);
    const up=el("button","ghost","↑"),dn=el("button","ghost","↓"),x=el("button","danger","✕");up.style.padding=dn.style.padding="2px 9px";up.disabled=i===0;dn.disabled=i===order.length-1;
    x.title="Delete this piece";
    up.onclick=()=>{[order[i-1],order[i]]=[order[i],order[i-1]];draw();saveOrder()};dn.onclick=()=>{[order[i+1],order[i]]=[order[i],order[i+1]];draw();saveOrder()};
    let arm=null;x.onclick=async()=>{if(!x.classList.contains("arm")){x.classList.add("arm");x.textContent="Delete?";arm=setTimeout(()=>{x.classList.remove("arm");x.textContent="✕"},3000);return}clearTimeout(arm);o.open();await studio(o.secAction,{...pid(c),item:k,remove:true})};
    row.ondragstart=e=>{dragFrom=i;e.dataTransfer.effectAllowed="move";try{e.dataTransfer.setData("text/plain",String(i))}catch(_){}row.style.opacity=".5"};
    row.ondragend=()=>{row.style.opacity="";[...ol.children].forEach(n=>n.style.outline="")};
    row.ondragover=e=>{e.preventDefault();e.dataTransfer.dropEffect="move";row.style.outline="2px solid var(--accent,#8a5cf6)"};
    row.ondragleave=()=>{row.style.outline=""};
    row.ondrop=e=>{e.preventDefault();row.style.outline="";if(dragFrom<0||dragFrom===i)return;const [m]=order.splice(dragFrom,1);order.splice(i,0,m);dragFrom=-1;draw();saveOrder()};
    row.append(up,dn,x);ol.append(row)});
    if(!order.length)ol.append(el("div","small","Nothing here yet - add a piece below."))};
  draw();const ot=el("div","small","Order the pieces are said in");ot.style.margin="10px 0 4px";d.append(ot,ol);
  const ctl=el("div","row");ctl.style.cssText="justify-content:flex-start;gap:8px;margin-bottom:10px;flex-wrap:wrap";
  const addBtn=(text,item,title)=>{const b=el("button","ghost",text);b.title=title;b.onclick=async()=>{o.open();const r=await studio(o.secAction,{...pid(c),item});if(r.error)toast(r.error,5000)};ctl.append(b)};
  addBtn("+ Part","part","Add another recorded part to the announcement");
  Object.keys(o.specials).forEach(k=>{if(!order.includes(k)&&o.specials[k].add)addBtn(o.specials[k].add,k,o.specials[k].title)});
  d.append(ctl);
}
function descentControls(c,d){
  pieceControls(c,d,{order:c.descent_order||["part:1","city","part:2"],specials:{city:{label:"City name (from the Airports section)",add:"+ City name",title:"Say the destination's airport name"}},
    intro:"PAPro builds the descent announcement for each flight from these pieces, in this order (drag to reorder, ✕ to delete a piece, the buttons add more). The city is the destination's airport name from the Airports section. Until every piece is recorded, the Generic clip is used.",
    secAction:"descent_section",orderAction:"studio_descent_order",open:openDescent,save:v=>{c.descent_order=v}});
}
function boardingControls(c,d){
  pieceControls(c,d,{order:c.boarding_order||["greeting","airport","part:1"],specials:{
      greeting:{label:"Greeting (good morning / afternoon / evening / night, recorded below)",add:"+ Greeting",title:"Say the time-of-day greeting"},
      airport:{label:"Airport name (from the Airports section)",add:"+ Airport name",title:"Say the destination's name"}},
    intro:"PAPro builds the welcome aboard for each flight from these pieces, in this order (drag to reorder, ✕ to delete a piece, the buttons add more). For example: Greeting (“Ladies and gentlemen, good morning, welcome on board this flight to”), Airport name, Part 1 (“We'll be flying for...”). If a recorded part is missing, the generic welcome is used.",
    secAction:"boarding_section",orderAction:"studio_boarding_order",open:openBoarding,save:v=>{c.boarding_order=v}});
}
function landingControls(c,d){
  pieceControls(c,d,{order:c.landing_order||["part:1","airport","time","part:2"],specials:{
      welcome:{label:"Welcome to... (older recording, before the airport name)"},
      airport:{label:"Airport name (from the Airports section)",add:"+ Airport name",title:"Say the airport's name"},
      time:{label:"Local time (the hour and minute clips from Time and Numbers)",add:"+ Local time",title:"Say the local time"},
      base:{label:"Crew base (the base chosen for this flight in the Crew introduction)",add:"+ Crew base",title:"Say the crew base (record bases under Safety briefing > Crew introduction)"}},
    intro:"PAPro builds the landing announcement for each flight from these pieces, in this order (drag to reorder, ✕ to delete a piece, the buttons add more). For example: Part 1 (“Ladies and gentlemen, welcome to”), Airport name, Part 2 (“...where the local time is”), Local time, Part 3 (“On behalf of this”), Crew base, Part 4 (“-based crew, thank you for choosing...”). If a recorded part or the crew base is missing, the generic After Landing PA is used.",
    secAction:"landing_section",orderAction:"studio_landing_order",open:openLanding,save:v=>{c.landing_order=v}});
}
function buildChecklist(c,host){
  const pr=el("div",null);pr.style.margin="16px 2px";pr.append(el("div","small",c.done+" of "+c.total+" recorded or skipped"+(c.missing?" · "+c.missing+" required still to do":" · all required clips done")));
  const nSk=c.rows.filter(r=>r.skipped).length;
  if(nSk){const ub=el("button","ghost","Unskip all ("+nSk+")");ub.style.cssText="margin-top:8px;padding:6px 12px";ub.title="Put every skipped announcement back on the checklist";
    ub.onclick=async()=>{const r=await studio("unskip_all",pid(c));if(r&&r.ok)toast(r.count+" announcement"+(r.count==1?"":"s")+" put back on the checklist")};pr.append(ub)}
  const bar=el("div","ptrack");const fill=el("i");fill.style.width=Math.round(100*c.done/c.total)+"%";bar.append(fill);pr.append(bar);host.append(pr);
  const groups=[];c.rows.forEach(r=>{let g=groups.find(x=>x.name===r.group);if(!g){g={name:r.group,rows:[]};groups.push(g)}g.rows.push(r)});
  ["Airports","Boarding music","Bolt-ons"].forEach(n=>{if(!groups.find(x=>x.name===n))groups.push({name:n,rows:[]})});
  const order=["Airports","Boarding","Boarding music","Bolt-ons","Boarding complete & doors","Safety briefing","Before takeoff","Climb & cruise","Descent & approach","After Landing","Deboarding","Emergency","Time and Numbers"];groups.sort((a,b)=>order.indexOf(a.name)-order.indexOf(b.name));
  groups.forEach(gr=>{
    const d=el("details","grp");d.open=openGroups.has(gr.name);d.ontoggle=()=>{d.open?openGroups.add(gr.name):openGroups.delete(gr.name)};
    const sm=el("summary",null,gr.name);sm.append(el("span",null,gr.rows.filter(r=>r.done||r.skipped).length+"/"+gr.rows.length));d.append(sm);
    if(gr.name==="Boarding"){
      const bl=el("label","check");bl.style.cssText="display:flex;align-items:center;gap:8px;margin:4px 0 10px";const bc=el("input");bc.type="checkbox";bc.checked=!c.boarding_off;
      bl.append(bc,document.createTextNode(" Add customization (greeting, airport name and your own parts)"));
      bc.onchange=async()=>{openGroups.add("Boarding");if(bc.checked)openBoarding();const r=await studio("boarding_custom",{...pid(c),on:bc.checked});if(r.ok)toast(bc.checked?"Boarding customization added below":"Boarding customization removed")};
      d.append(bl);
    }
    if(gr.name==="Safety briefing"){
      const cl=el("label","check");cl.style.cssText="display:flex;align-items:center;gap:8px;margin:4px 0 10px";const cc=el("input");cc.type="checkbox";cc.checked=!!(c.crew&&c.crew.on);
      cl.append(cc,document.createTextNode(" Add crew introduction (number of flight attendants, base and crew names)"));
      cc.onchange=async()=>{openCrew();const r=await studio("crew",{...pid(c),on:cc.checked});if(r.ok)toast(cc.checked?"Crew introduction added below":"Crew introduction removed")};
      d.append(cl);
    }
    if(gr.name==="Climb & cruise"){
      d.append(el("div","small","Cabin service can have named versions, for example Domestic and International. Name each version (the name is what the pilot sees) and record it below the normal Cabin service. With more than one version, PAPro asks which one to use when a flight is loaded."));
      const sl=el("div","addline"),si=el("input");si.type="text";si.placeholder="Version name, e.g. Domestic";const sb=el("button",null,"Add cabin service version");
      sb.onclick=async()=>{if(!si.value.trim())return;const r=await studio("service",{...pid(c),name:si.value});if(r.error)toast(r.error,5000);else openGroups.add("Climb & cruise")};
      si.addEventListener("keydown",e=>{if(e.key==="Enter")sb.click()});sl.append(si,sb);d.append(sl);
    }
    if(gr.name==="Descent & approach"){
      const dl=el("label","check");dl.style.cssText="display:flex;align-items:center;gap:8px;margin:4px 0 10px";const dc=el("input");dc.type="checkbox";dc.checked=!!c.descent_city;
      dl.append(dc,document.createTextNode(" Add destination name in Descent PA"));
      dc.onchange=async()=>{openGroups.add("Descent & approach");if(dc.checked)openDescent();const r=await studio("descent_city",{...pid(c),on:dc.checked});if(r.ok)toast(dc.checked?"Record the two descent parts below (the airport name clip is the one used in the other announcements)":"Descent announcement back to the single clip")};
      d.append(dl);
      d.append(el("div","small","Adds a “Descent Started City-based PA” sub-section, for a PA like “We've begun our initial descent into Atlanta. Please ensure all of your carry-on items are properly stowed and fasten your seat belt.” Until both parts are recorded, the normal Descent Started clip is used."));
    }
    if(gr.name==="After Landing"){
      const tl=el("label","check");tl.style.cssText="display:flex;align-items:center;gap:8px;margin:4px 0 10px";const tc=el("input");tc.type="checkbox";tc.checked=!c.landing_off;
      tl.append(tc,document.createTextNode(" Add customization (airport name, local time, crew base and your own parts)"));
      tc.onchange=async()=>{openGroups.add("After Landing");if(tc.checked)openLanding();const r=await studio("landing_custom",{...pid(c),on:tc.checked});if(r.ok)toast(tc.checked?"After Landing customization added below":"After Landing customization removed")};
      d.append(tl);
}
    if(gr.name==="Time and Numbers"){
      const pl=el("label","check");pl.style.cssText="display:flex;align-items:center;gap:8px;margin:4px 0 10px";const pc=el("input");pc.type="checkbox";pc.checked=!!c.time_period;
      pl.append(pc,document.createTextNode(" Use “in the morning / afternoon / evening” instead of AM / PM"));
      pc.onchange=async()=>{openGroups.add("Time and Numbers");const r=await studio("time_period",{...pid(c),on:pc.checked});if(r.ok)toast(pc.checked?"Record the three “in the...” clips below":"Back to AM / PM")};
      d.append(pl);
      d.append(el("div","small",c.time_period?"The time is said as hour, minutes, then “in the morning” (before noon), “in the afternoon” (noon to 6 pm) or “in the evening” (6 pm on). On the hour it is just the hour then the phrase.":"The time is said as hour, minutes, then AM or PM. On the hour it is just the hour then AM or PM."))}
    if(gr.name==="Time and Numbers"){d.append(el("div","small","The clock used by the landing announcement (“...where the local time is four thirty-five P.M.”). The hour clips (1-12) are also used to say the number of flight attendants in the crew introduction."))}
    if(gr.name==="Airports"){
      d.append(el("div","small","Add your airports (paste a whole list if you like) and record just each name (for example “London Heathrow”). PAPro mixes that one clip into both the BoardingWelcome and the landing PA. Add a terminal variant like EGLL_T5 if you record one."));
      const line=el("div","addline"),i=el("textarea");i.rows=2;i.placeholder="EGLL, EGKK, EGCC  (paste a list: commas, semicolons, spaces or new lines)";i.style.textTransform="uppercase";i.style.resize="vertical";const add=el("button",null,"Add airports");
      add.onclick=async()=>{if(!i.value.trim())return;const text=i.value;i.value="";i.blur();const r=await studio("airport",{...pid(c),code:text});if(!r.ok)i.value=text;if(r.ok){openGroups.add("Airports");toast(r.added+(r.added===1?" airport added":" airports added")+(r.skipped&&r.skipped.length?" - skipped: "+r.skipped.slice(0,8).join(", "):""),r.skipped&&r.skipped.length?6000:3200)}studioSig="";renderStudio()};
      i.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();add.click()}});line.append(i,add);d.append(line);
      if(gr.rows.length>4){
        const fl=el("input");fl.type="search";fl.placeholder="Search airports (code or name)";fl.value=airportFilter;fl.style.cssText="margin:12px 0 4px;width:100%";fl.setAttribute("aria-label","Search airports");
        const none=el("div","small","No airport matches that search.");none.style.cssText="display:none;margin:8px 2px";
        const apply=()=>{airportFilter=fl.value;const q=fl.value.trim().toLowerCase();let n=0;
          d.querySelectorAll(":scope > .slot").forEach(row=>{const hit=!q||row.textContent.toLowerCase().includes(q);row.style.display=hit?"":"none";if(hit)n++});
          none.style.display=q&&!n?"":"none"};
        fl.addEventListener("input",apply);d.append(fl,none);setTimeout(apply,0);
      }
    }
    if(gr.name==="Boarding music"){
      d.append(el("div","small","Background music played while passengers board. Add a track name, then upload the audio - use only music you have the right to share. It is not recorded or processed, just converted to .ogg."));
      const line=el("div","addline"),i=el("input");i.type="text";i.placeholder="Departure lounge";const add=el("button",null,"Add track");
      add.onclick=async()=>{if(!i.value.trim())return;const r=await studio("music",{...pid(c),name:i.value});if(r.ok)openGroups.add("Boarding music");studioSig="";renderStudio()};
      i.addEventListener("keydown",e=>{if(e.key==="Enter")add.click()});line.append(i,add);d.append(line);
      const ml=el("div","small");ml.style.cssText="display:flex;align-items:center;gap:12px;margin:12px 2px";
      const sl=el("input");sl.type="range";sl.min=5;sl.max=100;sl.step=5;sl.value=c.music_level;sl.style.cssText="flex:1;max-width:260px";
      const lv=el("b",null,c.music_level+"%");
      sl.oninput=()=>{lv.textContent=sl.value+"%"};
      sl.onchange=async()=>{stopPlay();toast("Setting the music volume...");const r=await api("studio_music_level",{...pid(c),level:+sl.value});if(r.ok){c.music_level=+sl.value;studioSig=JSON.stringify(snap.studio)}if(r.ok)toast(r.tracks?"Music volume set on "+r.tracks+(r.tracks===1?" track":" tracks"):"Music volume saved - applies to tracks you add")};
      ml.append(el("span",null,"Music volume (% of the announcements):"),sl,lv);ml.title="50% is half as loud as the announcements (6 dB quieter). Changing it re-levels the tracks already in this pack.";d.append(ml);
      if(c.music.length){const cr=el("div","chiprow");c.music.forEach(a=>{const b=el("button","ghost",a+" ✕");b.onclick=()=>studio("music",{...pid(c),name:a,remove:true});cr.append(b)});d.append(cr)}
    }
    if(gr.name==="Bolt-ons"){
      d.append(el("div","small","Short announcements PAPro sometimes adds to the welcome - e.g. “Nut allergy”, “Wi-Fi unavailable”, “Delayed by air traffic control”."));
      const line=el("div","addline"),i=el("input");i.type="text";i.placeholder="Nut allergy";const add=el("button",null,"Add bolt-on");
      add.onclick=async()=>{if(!i.value.trim())return;const r=await studio("extra",{...pid(c),name:i.value});if(r.ok)openGroups.add("Bolt-ons");studioSig="";renderStudio()};
      i.addEventListener("keydown",e=>{if(e.key==="Enter")add.click()});line.append(i,add);d.append(line);
      if(c.extras.length){const cr=el("div","chiprow");c.extras.forEach(a=>{const b=el("button","ghost",a.replace(/_/g," ")+" ✕");b.onclick=()=>studio("extra",{...pid(c),name:a,remove:true});cr.append(b)});d.append(cr)}
    }
    const crewHere=gr.name==="Safety briefing"&&c.crew&&c.crew.on;
    const landHere=gr.name==="After Landing"&&!c.landing_off;
    const boardHere=gr.name==="Boarding"&&!c.boarding_off;
    if(gr.rows.some(r=>r.part)||crewHere||landHere||boardHere){
      const parts=[];if(crewHere)parts.push({name:"Crew introduction",rows:[]});
      gr.rows.forEach(r=>{let p=parts.find(x=>x.name===r.part);if(!p){p={name:r.part,rows:[]};parts.push(p)}p.rows.push(r)});
      if(landHere&&!parts.find(x=>x.name===LANDING_SUB))parts.unshift({name:LANDING_SUB,rows:[]});
      if(boardHere&&!parts.find(x=>x.name===BOARDING_SUB))parts.unshift({name:BOARDING_SUB,rows:[]});
      parts.forEach(pt=>{const key=gr.name+"/"+pt.name,sd=el("details","grp sub");sd.open=openGroups.has(key);sd.ontoggle=()=>{sd.open?openGroups.add(key):openGroups.delete(key)};
        const ss=el("summary",null,pt.name);ss.append(el("span",null,pt.rows.filter(r=>r.done||r.skipped).length+"/"+pt.rows.length));sd.append(ss);if(crewHere&&pt.name==="Crew introduction")crewControls(c,sd);if(pt.name==="Descent Started City-based PA")descentControls(c,sd);if(pt.name==="After Landing PA")landingControls(c,sd);if(pt.name==="Boarding Welcome PA")boardingControls(c,sd);
        if(crewHere&&pt.name==="Crew introduction"){
          pt.rows.filter(r=>r.crew==="intro").forEach(r=>sd.append(slotRow(c,r)));
          const sub=(title,rows,adder,count)=>{const k="Safety briefing/Crew introduction/"+title,dd=el("details","grp sub");dd.open=openGroups.has(k);dd.ontoggle=()=>{dd.open?openGroups.add(k):openGroups.delete(k)};
            const sm=el("summary",null,title);sm.append(el("span",null,count!=null?String(count):rows.filter(r=>r.done||r.skipped).length+"/"+rows.length));dd.append(sm);adder(dd);rows.forEach(r=>dd.append(slotRow(c,r)));sd.append(dd)};
          const ord=(c.crew&&c.crew.order)||[];
          sub("Aircraft types",[],dd=>faEditor(c,dd),Object.keys((c.crew&&c.crew.fa)||{}).length);
          if(ord.includes("base"))sub("Bases",pt.rows.filter(r=>r.crew==="base"),dd=>crewBaseAdd(c,dd));
          if(ord.includes("names"))sub("Crew names",pt.rows.filter(r=>r.crew==="name"),dd=>crewNameAdd(c,dd));
        }else pt.rows.forEach(r=>sd.append(slotRow(c,r)));d.append(sd)});
    }else gr.rows.forEach(r=>d.append(slotRow(c,r)));
    host.append(d);
  });
}

let newPackOpen=false;
function renderStudio(){
  if(view!=="studio"||!snap)return;
  const root=$("#view-studio"), s=snap.studio||{projects:[],current:null}, sig=JSON.stringify(s);
  if(sig===studioSig||recState||root.contains(document.activeElement)&&["INPUT","TEXTAREA"].includes(document.activeElement.tagName)&&!["checkbox","radio"].includes(document.activeElement.type))return;
  studioSig=sig; root.textContent="";
  const c=s.current;
  if(!c){
    const card=el("section","card");const hs=el("h2",null,"Pack Studio");hs.style.marginTop="0";card.append(hs);
    const intro=el("p","small","Pack Studio offers the ability to record or upload announcements alongside automatic volume balancing and cabin EQ effects. Certain sections offer aircraft type customizations and most sections allow for additional recordings to improve and enhance variety.");
    intro.style.fontSize="13.5px";intro.style.color="var(--muted)";card.append(intro);
    if(!newPackOpen){const row=el("div","row");row.style.cssText="margin-top:14px;justify-content:flex-start";const go=el("button","primary","Start New Pack");go.onclick=()=>{newPackOpen=true;studioSig="";renderStudio()};row.append(go);card.append(row);root.append(card)}
    else{
    const g=el("div","sd-grid");const ic=el("label",null,"Airline code (3 letters)"),i1=el("input");i1.type="text";i1.maxLength=3;i1.placeholder="BAW";i1.style.textTransform="uppercase";ic.append(i1);
    const an=el("label",null,"Airline name"),i2=el("input");i2.type="text";i2.placeholder="British Airways";an.append(i2);
    const vn=el("label","wide","Name of this voice"),i3=el("input");i3.type="text";i3.placeholder="Emma";vn.append(i3);g.append(ic,an,vn);card.append(g);
    const row=el("div","row");row.style.cssText="margin-top:14px;justify-content:flex-start";const go=el("button","primary","Start New Pack"),cn=el("button","ghost","Cancel");
    cn.onclick=()=>{newPackOpen=false;studioSig="";renderStudio()};
    go.onclick=async()=>{const r=await studio("create",{icao:i1.value,airline:i2.value,voice:i3.value});if(r.ok){newPackOpen=false;toast("Pack started")}};row.append(go,cn);card.append(row);root.append(card);setTimeout(()=>i1.focus(),0)}
    if(s.library&&s.library.length){const h=el("div","section-head");h.append(el("h2",null,"Edit a pack from your library"),el("span",null,"change clips, add airports, fix a recording"));root.append(h);
      s.library.forEach(p=>{const b=el("button","ghost",p.icao+" - "+p.name);b.style.margin="0 8px 8px 0";b.onclick=async()=>{const r=await studio("import",p);if(r.ok)toast("Opened for editing - your clips are unchanged until you replace them")};root.append(b)})}
    if(s.projects.length){const h=el("div","section-head");h.append(el("h2",null,"Your packs in progress"));root.append(h);
      s.projects.forEach(p=>{const w=el("span","chip2");w.style.margin="0 8px 8px 0";const b=el("button","ghost",p.icao+" · "+p.voice+(p.airline?" · "+p.airline:""));b.style.cssText="width:auto;height:auto;border-radius:10px;padding:6px 10px;color:var(--text)";b.onclick=()=>studio("open",p);
        const x=el("button",null,"✕");x.title=p.in_library?"Remove from packs in progress (the pack in your library stays)":"Abandon this pack";x.onclick=()=>confirmDiscard(p);
        w.append(b,x);root.append(w)})}
    return;
  }
  const top=el("div","section-head");const back=el("button","ghost","← All packs");back.onclick=()=>studio("close");
  top.append(back,el("h2",null,c.airline+" · "+c.voice+" ("+c.icao+")"));
  const sp=el("span");sp.style.cssText="margin-left:auto;display:flex;gap:8px";
  const mic=el("button",null,"🎧 Audio settings");mic.onclick=openMic;
  sp.append(mic);
  top.append(sp);root.append(top);
  if(c.secondary){ // languages: the order they are said in (primary first); per-announcement order once both are in the library
    const L=c.languages||{available:[],order:[],per_pa:{},enabled:false,known:{},pas:[]},lc=el("section","card");lc.style.marginBottom="16px";
    const hd=el("h2",null,"Languages");lc.append(hd);
    lc.append(el("div","small","Every announcement is built in the primary language, then (after a short pause) in the secondary language, and the two are joined into one PA. Once both languages are saved to your library you can also say a particular announcement in the other order here."));
    const names=Object.fromEntries(L.available.map(a=>[a.code,a.name]));
    if(L.available.length<2){const t=el("div","small",c.language?"Save the pack to your library (Save) to set a different order for particular announcements.":"No language is set for this voice yet. Choose one in Voice details below.");t.style.marginTop="10px";lc.append(t)}
    else{
      const en=el("label","check");en.style.margin="12px 0 8px";const cb=el("input");cb.type="checkbox";cb.checked=!!L.enabled;en.append(cb,document.createTextNode(" Announce in several languages"));lc.append(en);
      let order=[...L.order],per={...L.per_pa};
      const save=async()=>{const r=await studio("lang_save",{...pid(c),enabled:cb.checked,order,per_pa:per});if(r.ok)toast("Language order saved")};
      cb.onchange=save;
      const list=el("div");list.style.cssText="display:flex;flex-direction:column;gap:6px;max-width:420px";
      const draw=()=>{list.textContent="";order.forEach((code,i)=>{const row=el("div","facts");row.style.cssText="display:flex;align-items:center;gap:8px;padding:6px 10px;border:1px solid var(--line);border-radius:10px";
        const v=(L.available.find(a=>a.code===code)||{voices:[]}).voices.join(", ");row.append(el("b",null,(i+1)+". "+(names[code]||code)));const sm=el("span","small","  "+v);sm.style.flex="1";row.append(sm);
        const up=el("button","ghost","↑"),dn=el("button","ghost","↓");up.style.padding=dn.style.padding="2px 9px";up.disabled=i===0;dn.disabled=i===order.length-1;
        up.onclick=()=>{[order[i-1],order[i]]=[order[i],order[i-1]];draw();save()};dn.onclick=()=>{[order[i+1],order[i]]=[order[i],order[i+1]];draw();save()};row.append(up,dn);list.append(row)})};
      draw();const lab=el("div","small","Language order (the first one is said first)");lab.style.margin="4px 0 6px";lc.append(lab,list);
      const dt=el("details","grp");dt.style.marginTop="14px";dt.append(el("summary",null,"Different order for particular announcements"));
      dt.append(el("div","small","Leave on “Default order” unless an announcement should be said in another order, for example the safety demonstration in English first."));
      const tab=el("div");tab.style.cssText="display:grid;grid-template-columns:1fr auto;gap:6px 14px;margin-top:8px;max-width:560px;align-items:center";
      L.pas.forEach(pa=>{const sel=el("select","voice");const d0=el("option",null,"Default order");d0.value="";sel.append(d0);
        order.forEach(code=>{const o=el("option",null,(names[code]||code)+" first");o.value=code;sel.append(o)});
        sel.value=(per[pa.base]||[])[0]||"";sel.onchange=()=>{if(sel.value){per[pa.base]=[sel.value,...order.filter(x=>x!==sel.value)]}else delete per[pa.base];save()};
        tab.append(el("span","small",pa.label),sel)});
      dt.append(tab);lc.append(dt)}
    root.append(lc);
  }
  const card=el("section","card tight");card.append(el("h2",null,"Voice details"));
  const g=el("div","sd-grid");
  { const cur={new_airline:c.airline,new_icao:c.icao,new_voice:c.voice};
    g.append(field("Airline name","new_airline",cur,"Delta Air Lines"),field("Airline code (ICAO)","new_icao",cur,"DAL"),field("Character name (the voice)","new_voice",cur,"Aaron","wide")) }
  g.append(field("Voice artist","artist",c,"Your name"),field("Accent","accent",c,"British"),field("Short description","description",c,"Calm and collected",true),
           field("Support link (Ko-fi, Patreon...)","donate_url",c,"https://ko-fi.com/..."),field("Pack version","version",c,"1.0.0"),field("Also used by (other airline codes, e.g. SHT, CFE)","also_icaos",c,"SHT, CFE","wide"));
  card.append(g);
  { const known=(c.languages&&c.languages.known)||{},opts=(sel,none,skip)=>{const o0=el("option",null,none);o0.value="";sel.append(o0);Object.entries(known).forEach(([k,v])=>{if(k===skip)return;const o=el("option",null,v);o.value=k;sel.append(o)})};
    const gl=el("div","wide");gl.style.cssText="grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:4px";
    const pb=el("div","langbox"+(c.language?"":" req")),ps=el("select","voice"),sb=el("div","langbox"),ss=el("select","voice");
    pb.append(el("b",null,"Primary language (required)"),ps);opts(ps,"Choose a language...");ps.value=c.language||"";
    pb.append(el("div","small",c.language?"The language of this voice's main announcements. It is always said first.":"Required before anything can be recorded."));
    ps.onchange=async()=>{const r=await api("studio_details",{...pid(c),language:ps.value,effect:c.effect});if(r.ok)toast("Saved");else if(r.error)toast(r.error);await refreshNow()};
    sb.append(el("b",null,"Secondary language (optional)"),ss);opts(ss,"None",c.language);ss.value=c.secondary_language||"";ss.disabled=!c.language;
    sb.append(el("div","small","Adds a second set of announcements below, recorded in that language and said after the primary one, with a short pause between."));
    ss.onchange=async()=>{const r=await api("studio_secondary",{...pid(c),language:ss.value});if(r.error){toast(r.error);ss.value=c.secondary_language||""}else toast(ss.value?"Secondary language added below":"Secondary language removed (its recordings are kept)");await refreshNow()};
    gl.append(pb,sb);g.append(gl)}
  const lg=el("div","logorow"),im=el("img","logoimg");im.alt="Pack logo";
  im.src=c.logo?"/studio/logo?"+new URLSearchParams({icao:c.icao,voice:c.voice,t:TOKEN,v:Date.now()}):"/logo/"+c.icao+".png?t="+TOKEN+"&v="+(snap.logo_v||0);
  const lt=el("div",null);lt.append(el("div","nm2","Pack logo"),el("div","small",c.logo?"Shown in the pack list and the window header, and included in the exported zip.":"Optional. A png, jpg or webp - a square or wide logo on a transparent or light background works best."));
  const lf=el("input");lf.type="file";lf.accept="image/png,image/jpeg,image/webp";lf.style.display="none";
  const lb=el("button",null,c.logo?"Change logo":"Upload logo");lb.onclick=()=>lf.click();
  lf.onchange=async()=>{const f=lf.files[0];if(!f)return;if(f.size>5*1024*1024){toast("That image is too large (5 MB limit)",6000);return}
    const q=new URLSearchParams({icao:c.icao,voice:c.voice,slot:"@logo",filename:f.name,t:TOKEN});
    try{const r=await fetch("/studio/upload?"+q,{method:"POST",headers:{"X-Token":TOKEN},body:f});const j=await r.json();toast(j.error||"Logo saved",j.error?7000:3200)}catch(e){toast("Upload failed",7000)}
    await refreshNow()};
  const lr=el("button","ghost","Remove");lr.style.display=c.logo?"":"none";lr.onclick=async()=>{const r=await studio("logo_remove",pid(c));if(r.ok)toast("Logo removed")};
  lg.append(im,lt,lb,lr,lf);card.append(lg);
  const save=()=>{const d={...pid(c),effect:c.effect};g.querySelectorAll("input").forEach(i=>d[i.dataset.k]=i.value);return d};
  g.querySelectorAll("input").forEach(i=>i.onchange=async()=>{const r=await api("studio_details",save());if(r.ok)toast(/^new_/.test(i.dataset.k)?"Saved - press Save (green) to add it to your library under the new name":"Saved");else if(r.error)toast(r.error);studioSig="";await refreshNow()});
  const ef=el("div","radios");ef.style.marginTop="14px";
  [[true,"Cabin-PA sound on","Band-limited speaker voice with light compression, like the original announcements. Recommended."],[false,"Leave my recording as it is","Only the loudness is matched."]].forEach(([v,t,sub])=>{
    const r=el("div","radio"+(c.effect===v?" sel":""));r.append(el("b",null,t),el("span",null,sub));r.onclick=async()=>{if(c.effect===v)return;toast("Reprocessing...");await studio("details",{...save(),effect:v});toast("Reprocessed with "+(v?"the PA sound":"no effect"))};ef.append(r)});
  card.append(ef);
  { const lv=el("div","lvlrow");lv.style.cssText="margin-top:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap";
    const cb=el("button","ghost","Check clip volume"),msg=el("span","small","");
    cb.title="Measures every clip in this pack against the target loudness. Imported clips are only measured, never changed unless you ask.";
    const run=async(fix)=>{cb.disabled=true;msg.textContent=fix?"Matching volume...":"Measuring... (a few seconds per 100 clips)";
      const r=await api("studio_levels",{...pid(c),fix:fix});cb.disabled=false;studioSig="";
      if(r.error){msg.textContent=r.error;return}
      if(fix){msg.textContent="Matched "+r.fixed+" clip"+(r.fixed==1?"":"s")+" to "+r.target+" LUFS.";return}
      if(!r.off.length){msg.textContent="All "+r.checked+" clips are within 1 dB of "+r.target+" LUFS.";return}
      msg.textContent=r.off.length+" of "+r.checked+" clips are more than 1 dB from "+r.target+" LUFS (e.g. "+r.off.slice(0,3).map(o=>o.file.split("/").pop().replace(/\.ogg$/,"")+" "+o.lufs).join(", ")+").";
      const fx=el("button","ghost","Match volume");fx.title="Set those clips to "+r.target+" LUFS by volume alone (no PA sound is added, so clips that already have it keep their EQ)";fx.onclick=()=>run(true);lv.append(fx)};
    cb.onclick=()=>{lv.querySelectorAll("button:not(:first-child)").forEach(b=>b.remove());run(false)};lv.append(cb,msg);card.append(lv)}
  card.append(el("div","sd-meta","Record in a quiet room, about a hand's width from the microphone. Your untouched recording is always kept, so you can switch the effect on or off any time."));
  { // the details can be folded away; the choice is remembered
    const h=card.firstChild,body=el("div");[...card.children].slice(1).forEach(n=>body.append(n));card.append(body);
    let open=true;try{open=localStorage.getItem("paproDetails")!=="0"}catch(e){}
    h.classList.add("fold");const paint=()=>{h.dataset.open=open?"1":"0";body.style.display=open?"":"none"};paint();
    h.title="Show or hide the voice details";h.onclick=()=>{open=!open;paint();try{localStorage.setItem("paproDetails",open?"1":"0")}catch(e){}};
  }
  root.append(card);
  { // preview volume: only for listening in PAPro. The marker is the level the sim will play the pack at.
    const vc=el("section","card tight");const sim=100+(c.app_boost==null?30:c.app_boost);
    const head=el("div");head.style.cssText="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap";
    const t=el("h2",null,"Preview volume"),lab=el("span","small");head.append(t,lab);vc.append(head);
    vc.append(el("div","small","Only changes how loud you hear clips while testing here in PAPro - it does not change the pack or what plays in the sim. The marker is the level the sim will play your announcements at (your recordings plus the PA volume in Settings), so drag to it to hear them as they will sound."));
    const wrap=el("div","volwrap"),rg=el("input");rg.type="range";rg.min=0;rg.max=200;rg.step=5;rg.value=micPrefs().vol;
    const mk=el("div","volmark");mk.style.left="calc(9px + (100% - 18px) * "+sim/200+")";mk.title="Level in the sim: "+sim+"%";mk.append(el("span",null,"in the sim "+sim+"%"));
    wrap.append(rg,mk);vc.append(wrap);
    const match=el("button","ghost","Match the sim");match.style.cssText="margin-top:8px;padding:6px 12px";
    const paint=v=>{lab.textContent=v+"%"+(v===sim?" · same as in the sim":v>sim?" · louder than the sim":v===0?" · muted":"");match.style.display=v===sim?"none":""};paint(+rg.value);
    const set=v=>{rg.value=v;micSave({...micPrefs(),vol:v});paint(v);applyVol()};
    rg.oninput=()=>set(+rg.value);match.onclick=()=>set(sim);vc.append(match);
    root.append(vc);
  }
  if(c.upgrades&&c.upgrades.length){const b=el("section","card");b.style.cssText="margin-top:16px;display:flex;gap:14px;align-items:center;justify-content:space-between";
    b.append(el("div",null,c.upgrades.length+" clip"+(c.upgrades.length>1?"s use":" uses")+" Fenix's old file names (like CrewSeatsLanding). Upgrade them to the new names to use the new features."));
    const ub=el("button","primary","Review upgrade");ub.onclick=()=>{const l=$("#up-list");l.textContent="";c.upgrades.forEach(u=>{const r=el("div","facts");const d=el("div");d.append(el("i",null,u.old),el("span",null,"→ "+u.new));r.append(d);l.append(r)});
      $("#up-ok").onclick=async()=>{$("#m-up").classList.remove("show");const r=await studio("upgrade",pid(c));if(r.ok)toast("Renamed "+r.renamed+" clip"+(r.renamed==1?"":"s"))};$("#m-up").classList.add("show")};
    b.append(ub);root.append(b)}
  { // one master dropdown per language: the primary first, then the secondary; every category sits under its language
    const known=(c.languages&&c.languages.known)||{};
    const master=(cc,primary)=>{const key="@lang:"+cc.voice,d=el("details","master");d.open=openGroups.has(key)||primary&&!openGroups.has("@closed:"+key);
      d.ontoggle=()=>{if(d.open){openGroups.add(key);openGroups.delete("@closed:"+key)}else{openGroups.delete(key);openGroups.add("@closed:"+key)}};
      const sm=el("summary",null,known[cc.language]||cc.language||"Language");sm.append(el("span","pill "+(primary?"p":"s"),primary?"Primary":"Secondary"),el("span","count",cc.done+" / "+cc.total+" done"));d.append(sm);
      const body=el("div","inner");d.append(body);buildChecklist(cc,body);root.append(d)};
    if(!c.language){const w=el("section","card");w.style.margin="16px 0";w.append(el("h2",null,"Choose a primary language"),el("div","small","Pick the language this voice speaks under Voice details above. The checklist unlocks once a primary language is chosen."));root.append(w)}
    else{master(c,true);if(c.secondary)master(c.secondary,false)}
  }
  const bar2=el("div","sdbar");
  const dis=el("button","ghost danger",c.in_library?"Remove from packs in progress":"Abandon this pack");dis.title=c.in_library?"Removes this working copy only. The pack in your Airlines library stays.":"Deletes this project's recordings and details. Your Airlines library is not touched.";
  dis.onclick=()=>confirmDiscard(c);
  const fol=el("button","ghost","Open project folder");fol.onclick=()=>api("studio_folder",pid(c));
  const saveSig=JSON.stringify([c.icao,c.voice,c.rows.map(r=>r.id+(r.done?1:0)+(r.skipped?"s":"")+(r.seconds||"")),c.crew,c.landing_order,c.landing_off,c.boarding_order,c.boarding_off,c.descent_order,c.descent_city,c.time_period,c.airports,c.extras,c.service_types]);
  const use=el("button","save"+(savedSig===saveSig?" done":""),"Save");use.title=c.origin?"Save your changes to your library so PAPro and the Test tab use them":"Add this pack to your library as a voice for "+c.icao+" so PAPro and the Test tab use it";
  use.onclick=async()=>{const r=await studio("install",pid(c));if(r.ok){savedSig=saveSig;use.classList.add("done");toast(r.finished?"Saved to your library":"Added to your library as a voice for "+c.icao)}};
  const ex=el("button","primary","Export pack zip");ex.onclick=async()=>{const r=await studio("export",pid(c));if(r.ok)toast("Exported - release tag: "+(r.tag||"")+" (the folder is open)",9000)};
  bar2.append(dis,fol,use,ex);root.append(bar2);syncPlayer();
}

function renderJob(){
  const j=snap&&snap.studio&&snap.studio.job,m=$("#m-job");m.classList.toggle("show",!!j);if(!j)return;
  $("#job-t").textContent=j.label;const bar=$("#job-bar"),pr=bar.parentElement;
  pr.classList.toggle("wait",j.fraction==null);if(j.fraction!=null)bar.style.width=Math.max(4,Math.round(j.fraction*100))+"%";
}
$("#up-cancel").onclick=()=>$("#m-up").classList.remove("show");
async function tick(){
  try{
    const r=await fetch("/api/state",{headers:{"X-Token":TOKEN,"X-View":(view==="studio"&&!document.hidden)?"studio":""}}); snap=await r.json();
    $("#auto").classList.toggle("on",!!snap.auto); $("#ver").textContent="PAPro v"+(snap.version||"");
    renderHero(); renderKnob(); renderKeep(); renderUpd(); renderJob(); renderStudio(); renderPacks(); if($("#m-manage").classList.contains("show"))renderManage(); renderAliases(); renderWelcome(); renderPrompt();
    if(importWatch){const busy=(snap.packs||[]).some(p=>p.task&&p.task.kind==="install");
      if(busy)importWatch.seen=true;
      if(!busy&&(snap.installs_done||0)>importWatch.n){importWatch=null;setView("main");toast("Pack imported",4000)}   // finished: back to the Dashboard
      else if(!busy&&(importWatch.seen||Date.now()-importWatch.t0>900000))importWatch=null}   // it failed: stay, the message says why
  }catch(e){}
  setTimeout(tick,(snap&&(snap.busy||snap.prompt||(snap.studio&&snap.studio.job)))?400:1500);
}
tick();
</script>
</body>
</html>
"""
