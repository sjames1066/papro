r"""
PAPro - Cabin announcements for MSFS: system tray app for custom MSFS 2024 cabin announcements.

* Reads the airline and destination of your latest SimBrief flight plan.
* Takes the matching recording  <AIRLINE>_<ICAO>.ogg  from  Airlines\<AIRLINE>\airports (older packs: dep | dest | fuel)  and
  puts it in your Fenix airline pack, replacing the generic announcement files.
* Airlines\<AIRLINE>\main holds the airline's general PAs (SafetyBriefing, ArmDoors...); installing a pack puts them there.
* Downloads and installs airline packs from an online catalog into the same Airlines folder, with one-click uninstall.

Command line (handy for testing; with no arguments the tray app starts):
    python papro.py --community              show the detected MSFS 2024 Community folder
    python papro.py --add-airline BAW AAL    create Airlines\BAW\main|time|extra|airports|fuel (and AAL)
    python papro.py --list-packs             list packs in the catalog
    python papro.py --install BAW            download + install a pack
    python papro.py --uninstall BAW          remove an installed pack (restores any pack that was there before)
    python papro.py --restore BAW            put the airline's original generic announcements back
    python papro.py --once                   one update from SimBrief
    python papro.py --once --airline BAW --dest BIKF     skip SimBrief, force a flight
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import math
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import url2pathname

import requests

try:
    import papro_audio  # joins announcement pieces (needs numpy + soundfile)
except Exception:  # missing libraries must not stop the rest of the app
    papro_audio = None
import papro_studio  # Pack Studio (record / upload, PA sound, loudness, export); imports papro_audio itself

APP_NAME = "PAPro - Cabin announcements for MSFS"
VERSION = "1.1.2"
BASE_DIR = (
    Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
)
def _data_dir() -> Path:
    env = os.environ.get("PAPRO_DATA") or os.environ.get("FCAC_DATA")
    if env:
        return Path(env)
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        new, old = base / "PAPro", base / "FCAC"
    else:
        new, old = Path.home() / ".papro", Path.home() / ".fcac"
    if old.is_dir() and not new.exists():   # this app used to be called FCAC: carry its recordings, packs and state over
        try:
            old.rename(new)
        except OSError:
            try:
                shutil.copytree(old, new)
            except OSError:
                return old
    return new


DATA_DIR = _data_dir()

CONFIG_PATH = Path(os.environ.get("PAPRO_CONFIG") or os.environ.get("FCAC_CONFIG") or DATA_DIR / "config.json")


def _migrate_settings() -> None:
    """Settings, the aliases and the Airlines library live in PAPro's data folder (AppData\\Local\\PAPro), so they stay
    when the program is reinstalled or updated. Bring them over the first time from where older versions kept them:
    next to the program, or in the old FCAC program folder. Nothing is overwritten."""
    if os.environ.get("PAPRO_CONFIG") or os.environ.get("FCAC_CONFIG") or os.environ.get("PAPRO_DATA") or os.environ.get("FCAC_DATA"):
        return   # tests / a deliberately chosen place
    sources = [BASE_DIR]
    if getattr(sys, "frozen", False):
        sources.append(BASE_DIR.parent / "FCAC")
    for old in sources:
        if old == DATA_DIR or not old.is_dir():
            continue
        try:
            cfg_old = old / "config.json"
            if cfg_old.is_file() and not CONFIG_PATH.exists():
                text = cfg_old.read_text(encoding="utf-8").replace("sjames1066/fcac/", "sjames1066/papro/")
                CONFIG_PATH.write_text(text, encoding="utf-8")
            alias_old = old / "aliases.json"
            if alias_old.is_file() and not (DATA_DIR / "aliases.json").exists():
                shutil.copyfile(alias_old, DATA_DIR / "aliases.json")
            if (old / "Airlines").is_dir() and not (DATA_DIR / "Airlines").exists():
                shutil.move(str(old / "Airlines"), str(DATA_DIR / "Airlines"))
        except OSError:
            pass


DATA_DIR.mkdir(parents=True, exist_ok=True)
_migrate_settings()
ALIASES_PATH = CONFIG_PATH.with_name("aliases.json")
DEFAULT_ALIASES = {"SHT": "BAW", "CFE": "BAW", "EFW": "BAW"}  # BA Shuttle, BA CityFlyer, BA EuroFlyer -> the BAW pack


GENERIC_DIR = DATA_DIR / "generic"    # untouched copy of an airline's generic announcements (saved before PAPro changes them)
BACKUPS_DIR = DATA_DIR / "backups"    # packs that were already in the Fenix folder before we installed ours
STATE_PATH = DATA_DIR / "state.json"
TEST_DIR = DATA_DIR / "test"            # scratch builds for the Test tab (never touches the Fenix folder or the real state)
_STATE_LOCAL = threading.local()        # a thread can point PAPro's state at another file (the Test tab's scratch builds)
STUDIO_DIR = DATA_DIR / "studio"        # Pack Studio projects (recordings, processed clips)
EXPORTS_DIR = DATA_DIR / "exports"      # packs exported from Pack Studio (next to "imports")
if (STUDIO_DIR / "exports").is_dir() and not EXPORTS_DIR.exists():   # older versions kept them inside the studio folder
    try:
        shutil.move(str(STUDIO_DIR / "exports"), str(EXPORTS_DIR))
    except OSError:
        pass
LOG_PATH = DATA_DIR / "announcer.log"
for _d in (DATA_DIR, GENERIC_DIR, BACKUPS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SIMBRIEF_URL = "https://www.simbrief.com/api/xml.fetcher.php"
STORE_PACKAGE = "Microsoft.Limitless_8wekyb3d8bbwe"   # MSFS 2024, Microsoft Store / Xbox app
STEAM_APPID = "2537590"                               # MSFS 2024 on Steam
KOFI_URL = "https://ko-fi.com/sjames1066"
MAIN_FOLDER = "main"                                  # Airlines\<ICAO>\main: the airline's general PAs (SafetyBriefing...)
PARTS_FOLDER = "time"                                 # Airlines\<ICAO>\time: greetings + local-time clips joined into the PAs
LEGACY_PARTS = "parts"                                # the old name of the time folder, still read
EXTRA_FOLDER = "extra"                                # Airlines\<ICAO>\extra: bolt-on announcements (nut allergy, Wi-Fi down...)
VOICES_FOLDER = "voices"                              # Airlines\<ICAO>\voices\<Name>: another voice artist's set of recordings
DEFAULT_VOICE = "Default"                             # the recordings directly in Airlines\<ICAO>
AIRPORTS_FOLDER = "airports"                          # Airlines\<ICAO>\airports: <AIRLINE>_<ICAO>.ogg, just the airport's name
ZIP_FOLDERS = (MAIN_FOLDER, PARTS_FOLDER, LEGACY_PARTS, EXTRA_FOLDER, VOICES_FOLDER, AIRPORTS_FOLDER, "dep", "dest", "fuel")    # the only top-level folders read from a pack zip
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024

DEFAULT_CONFIG = {
    "simbrief_username": "",
    "simbrief_userid": "",
    "catalog_url": "https://raw.githubusercontent.com/sjames1066/papro/main/catalog.json",
    "community_dir": "",              # leave empty to auto-detect
    "airlines_dir": "",               # your recordings library; empty = an 'Airlines' folder next to the app
    "announcements_folder": "fnx-airline-packs",
    "airline_override": "",           # force an airline ICAO instead of using the SimBrief plan
    # Airlines that share another airline's recordings. Edit aliases.json (next to the app) instead of this.
    "airline_aliases": {},
    "auto_poll": True,
    "poll_interval_seconds": 60,
    "notifications": False,
    "minimize_to_tray": False,        # minimizing the PAPro window hides it in the tray instead of the taskbar
    "close_to_tray": False,           # closing the PAPro window: False = PAPro quits, True = it keeps running in the tray
    "pa_boost": 30,                   # percent louder than the recordings when PAPro puts a PA into the Fenix folder (0 = untouched)
    "extra_chance": 25,               # percent of flights that get one random bolt-on from the extra folder (0 = never)
    "announce_local_time": True,      # say "where the local time is ..." in the landing PA (needs the parts pieces)
    "landing_speed_knots": 60,        # the landing PA is built once you have landed and slowed to this ground speed
    "join_style": "flow",             # "flow": pieces are cropped tight and run into each other; "pauses": a short gap between them
    "segment_pause_ms": 60,           # pause between the pieces of a joined PA (see the parts folder)
    "pa_knob_on": True,               # the on-screen PA knob (aircraft whose PAs PAPro plays itself): pushed up = PAs audible
    "pa_knob_volume": 70,             # ... and how loud (0-100)
    "lan_access": True,               # also answer on this PC's private network / VPN addresses (EFBx on another device)
    "gsx_integration": True,          # read GSX's boarding / deboarding state for the flight phase
    "pa_playback": True,              # play the PAs in PAPro itself for aircraft that can't use the Fenix folder
    "pa_output_device": "",           # sound output for those PAs ("" = the Windows default)
    "quit_with_msfs": True,           # when started with MSFS: close PAPro when the sim closes
    # Which pack files each folder replaces. Variants (e.g. BoardingWelcome[Morning].ogg) are found in the pack.
    "mappings": [
        {"folder": "dep", "base": "BoardingWelcome", "exclude_tags": ["Refueling"], "default_name": "BoardingWelcome"},
        {"folder": "dest", "base": "AfterLanding", "default_name": "AfterLanding"},
        {"folder": "fuel", "base": "BoardingWelcome", "require_tags": ["Refueling"],
         "default_name": "BoardingWelcome[Refueling]"},
    ],
}

log = logging.getLogger("papro")

DEBUG = False


class _DebugBuffer(logging.Handler):
    """Keeps the last few log lines for the red debug panel."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{time.strftime('%H:%M:%S')}  {record.getMessage()}")
        del self.lines[:-60]


DEBUG_BUFFER = _DebugBuffer()
log.addHandler(DEBUG_BUFFER)
log.setLevel(logging.INFO)


class AppError(Exception):
    """An error with a message that is safe to show the user."""


# --------------------------------------------------------------------------- config / state
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        raise AppError(f"Created {CONFIG_PATH.name} - please edit it (SimBrief name, catalog_url).")
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AppError(f"config.json is not valid JSON: {exc}") from exc
    return {**DEFAULT_CONFIG, **user}


def set_config_value(key: str, value) -> None:
    """Change one setting in config.json, keeping everything else the user has there."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else dict(DEFAULT_CONFIG)
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError(f"config.json could not be read: {exc}") from exc
    data[key] = value
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_aliases(cfg: dict) -> dict[str, str]:
    """Airlines that use another airline's recordings, e.g. {"SHT": "BAW"}: a flight with airline SHT uses
    Airlines\\BAW and fills the Fenix pack folder named SHT. Edit aliases.json next to the app (created on first run)."""
    if not ALIASES_PATH.exists():
        try:
            ALIASES_PATH.write_text(json.dumps(DEFAULT_ALIASES, indent=2) + "\n", encoding="utf-8")
        except OSError:
            log.warning("Could not create %s", ALIASES_PATH)
    try:
        data = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        raise AppError(f"aliases.json could not be read ({exc}) - it should look like {{\"SHT\": \"BAW\"}}.") from exc
    merged = {**(cfg.get("airline_aliases") or {}), **data}
    return {str(k).strip().upper(): str(v).strip().upper() for k, v in merged.items() if str(k).strip() and str(v).strip()}


# --------------------------------------------------------------------------- starting with Windows / with MSFS
EXE_XML_NAME = "PAPro"                       # name of our entry in MSFS's EXE.xml
MSFS_PROCESS = "FlightSimulator2024.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> tuple[str, str]:
    """(program, arguments) that start this app: the .exe itself, or pythonw + papro.py."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--background"
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    program = windowless if exe.name.lower() == "python.exe" and windowless.exists() else exe
    return str(program), f'"{Path(__file__).resolve()}" --background'


def msfs_config_dirs() -> list[Path]:
    """Folders that hold MSFS 2024's EXE.xml (next to UserCfg.opt): Microsoft Store and/or Steam edition."""
    found = []
    local, roaming = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    if local:
        found.append(Path(local) / "Packages" / STORE_PACKAGE / "LocalCache")
    if roaming:
        found.append(Path(roaming) / "Microsoft Flight Simulator 2024")
    return [p for p in found if p.is_dir()]


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ADDON_BLOCK = re.compile(r"[ \t]*<Launch\.Addon>.*?</Launch\.Addon>[ \t]*\r?\n?", re.DOTALL | re.IGNORECASE)
_EXE_XML_NEW = (
    '<?xml version="1.0" encoding="Windows-1252"?>\r\n<SimBase.Document Type="Launch" version="1,0">\r\n'
    "\t<Descr>Launch</Descr>\r\n\t<Filename>EXE.xml</Filename>\r\n\t<Disabled>False</Disabled>\r\n"
    "\t<Launch.ManualLoad>False</Launch.ManualLoad>\r\n</SimBase.Document>\r\n"
)


def _is_ours(block: str) -> bool:
    return re.search(rf"<Name>\s*({EXE_XML_NAME}|FCAC)\s*</Name>", block, re.IGNORECASE) is not None   # FCAC: the old name


def exe_xml_without_ours(text: str) -> str:
    return _ADDON_BLOCK.sub(lambda m: "" if _is_ours(m.group(0)) else m.group(0), text)


def exe_xml_with_ours(text: str, program: str, args: str) -> str:
    """EXE.xml text with our <Launch.Addon> entry added (any older copy of it replaced). Other entries are kept."""
    text = exe_xml_without_ours(text)
    block = (
        "\t<Launch.Addon>\r\n\t\t<Disabled>False</Disabled>\r\n\t\t<ManualLoad>False</ManualLoad>\r\n"
        f"\t\t<Name>{EXE_XML_NAME}</Name>\r\n\t\t<Path>{_xml_escape(program)}</Path>\r\n"
        + (f"\t\t<CommandLine>{_xml_escape(args)}</CommandLine>\r\n" if args else "")
        + "\t\t<NewConsole>False</NewConsole>\r\n\t</Launch.Addon>\r\n"
    )
    close = re.search(r"[ \t]*</SimBase\.Document>", text, re.IGNORECASE)
    if not close:
        raise AppError("EXE.xml has an unexpected layout - PAPro left it alone.")
    return text[: close.start()] + block + text[close.start():]


def _read_exe_xml(path: Path) -> str:
    return path.read_bytes().decode("cp1252", errors="replace") if path.exists() else ""


def _write_exe_xml(path: Path, text: str) -> None:
    if path.exists():  # keep a copy of what was there before we first touched it
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, BACKUPS_DIR / f"EXE.xml.{time.strftime('%Y%m%d-%H%M%S')}.bak")
    path.write_bytes(text.encode("cp1252", errors="xmlcharrefreplace"))


def set_msfs_autostart(enabled: bool) -> list[Path]:
    """Add or remove PAPro in MSFS's EXE.xml, so the sim starts it. Returns the files that were changed."""
    dirs = msfs_config_dirs()
    if enabled and not dirs:
        raise AppError("Could not find MSFS 2024's settings folder (run the sim once, then try again).")
    program, args = launch_command()
    changed = []
    for folder in dirs:
        path = folder / "EXE.xml"
        old = _read_exe_xml(path)
        if enabled:
            new = exe_xml_with_ours(old or _EXE_XML_NEW, program, args)
        elif _is_ours(old):
            new = exe_xml_without_ours(old)
        else:
            continue
        if new != old:
            try:
                _write_exe_xml(path, new)
            except OSError as exc:
                raise AppError(f"Could not write {path}: {exc}") from exc
            changed.append(path)
    return changed


def msfs_autostart_enabled() -> bool:
    return any(_is_ours(_read_exe_xml(d / "EXE.xml")) for d in msfs_config_dirs())


def set_windows_autostart(enabled: bool) -> None:
    try:
        import winreg
    except ImportError as exc:
        raise AppError("Starting with Windows only works on Windows.") from exc
    program, args = launch_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            for name in ("PAPro", "FCAC"):   # FCAC: what an older version called its entry
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            if enabled:
                winreg.SetValueEx(key, "PAPro", 0, winreg.REG_SZ, f'"{program}" {args}'.strip())
    except OSError as exc:
        raise AppError(f"Could not change the Windows startup entry: {exc}") from exc


def windows_autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            for name in ("PAPro", "FCAC"):
                try:
                    winreg.QueryValueEx(key, name)
                    return True
                except FileNotFoundError:
                    continue
            return False
    except (ImportError, OSError):
        return False


def current_autostart() -> str:
    """What is actually set up right now: "msfs", "windows" or "off"."""
    if msfs_autostart_enabled():
        return "msfs"
    return "windows" if windows_autostart_enabled() else "off"


def apply_autostart(mode: str) -> str:
    """Make `mode` ("off", "windows", "msfs") the only way PAPro starts by itself."""
    if mode not in ("off", "windows", "msfs"):
        raise AppError("Unknown startup option.")
    if mode != "windows" and sys.platform.startswith("win"):
        set_windows_autostart(False)
    if mode != "msfs":
        set_msfs_autostart(False)
    if mode == "windows":
        set_windows_autostart(True)
    elif mode == "msfs":
        set_msfs_autostart(True)
    return mode


def msfs_running() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {MSFS_PROCESS}", "/NH", "/FO", "CSV"], capture_output=True,
                             text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return MSFS_PROCESS.lower() in out.lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


def save_aliases(aliases: dict[str, str]) -> None:
    ALIASES_PATH.write_text(json.dumps(dict(sorted(aliases.items())), indent=2) + "\n", encoding="utf-8")


def load_state() -> dict:
    try:
        return json.loads((getattr(_STATE_LOCAL, "path", None) or STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    try:
        (getattr(_STATE_LOCAL, "path", None) or STATE_PATH).write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        log.exception("Could not save state")


# --------------------------------------------------------------------------- MSFS Community folder
def registry_hints() -> dict[str, bool]:
    """Best effort: which MSFS 2024 editions does the Windows registry say are installed for this user?"""
    hints = {"store": False, "steam": False}
    if not sys.platform.startswith("win"):
        return hints
    try:
        import winreg
    except ImportError:
        return hints
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Valve\Steam\Apps\{STEAM_APPID}") as key:
            try:
                hints["steam"] = bool(winreg.QueryValueEx(key, "Installed")[0])
            except OSError:
                hints["steam"] = True
    except OSError:
        pass
    packages = (r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion"
                r"\AppModel\Repository\Packages")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, packages) as key:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(key, index)
                except OSError:
                    break
                if name.startswith("Microsoft.Limitless_"):
                    hints["store"] = True
                    break
                index += 1
    except OSError:
        pass
    return hints


def community_from_usercfg(usercfg: Path) -> Path | None:
    """UserCfg.opt's InstalledPackagesPath is where MSFS keeps its package tree (and so Community)."""
    try:
        text = usercfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(r'^\s*InstalledPackagesPath\s+"([^"]+)"', text, re.MULTILINE)
    if not match:
        return None
    root = Path(match.group(1))
    for candidate in (root / "Community", root / "Packages" / "Community"):
        if candidate.is_dir():
            return candidate
    return None


def find_community_dir(cfg: dict) -> Path:
    override = str(cfg.get("community_dir", "")).strip()
    if override:
        path = Path(override)
        if path.is_dir():
            return path
        raise AppError(f"community_dir not found: {path}")

    editions = []  # (edition, UserCfg.opt, default Community folder)
    local, roaming = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    if local:
        store = Path(local) / "Packages" / STORE_PACKAGE / "LocalCache"
        editions.append(("store", store / "UserCfg.opt", store / "Packages" / "Community"))
    if roaming:
        steam = Path(roaming) / "Microsoft Flight Simulator 2024"
        editions.append(("steam", steam / "UserCfg.opt", steam / "Packages" / "Community"))
    hints = registry_hints()
    editions.sort(key=lambda e: not hints.get(e[0], False))  # editions the registry knows about go first

    for _edition, usercfg, default in editions:
        found = community_from_usercfg(usercfg)
        if found:
            return found
        if default.is_dir():
            return default
    raise AppError("Could not find the MSFS 2024 Community folder. Set community_dir in config.json.")


def airline_packs_dir(cfg: dict) -> Path:
    return find_community_dir(cfg) / str(cfg["announcements_folder"]) / "AirlinePacks"


# --------------------------------------------------------------------------- SimBrief
EFBX_SIMBRIEF_URL = "http://localhost:35810/efbx/simbrief"
_EFBX = {"down_until": 0.0, "logged": False, "plan": None, "plan_until": 0.0}


def _find_plan(node, depth: int = 0):
    """The SimBrief plan inside whatever EFBx returned: the JSON itself, or a dict somewhere inside it that has a destination."""
    if isinstance(node, dict):
        dest = node.get("destination")
        if isinstance(dest, dict) and str(dest.get("icao_code", "")).strip():
            return node
        if depth < 4:
            for value in node.values():
                found = _find_plan(value, depth + 1)
                if found is not None:
                    return found
    elif isinstance(node, list) and depth < 4:
        for value in node[:5]:
            found = _find_plan(value, depth + 1)
            if found is not None:
                return found
    return None


def efbx_plan(cfg: dict) -> dict | None:
    """The latest SimBrief plan from EFBx running on this PC (http://localhost:35810/efbx/simbrief), or None when EFBx is not
    running or has no plan, so PAPro falls back to SimBrief itself. A miss is remembered for 30 seconds so PAPro never waits on it."""
    if not cfg.get("efbx_simbrief", True) or time.monotonic() < _EFBX["down_until"]:
        return None
    if _EFBX["plan"] is not None and time.monotonic() < _EFBX["plan_until"]:   # the Dashboard asks every second: answer from the last reply
        return _EFBX["plan"]
    url = str(cfg.get("efbx_simbrief_url") or EFBX_SIMBRIEF_URL)
    try:
        resp = requests.get(url, timeout=2)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")
        plan = _find_plan(resp.json())
        if plan is None:
            raise ValueError("no plan in the reply")
    except (requests.RequestException, ValueError) as exc:
        _EFBX["down_until"] = time.monotonic() + 30
        if not _EFBX["logged"]:
            _EFBX["logged"] = True
            log.info("EFBx SimBrief not used (%s): using your SimBrief account instead", exc)
        return None
    _EFBX["plan"], _EFBX["plan_until"] = plan, time.monotonic() + 5
    if _EFBX["logged"]:
        log.info("Using the SimBrief plan from EFBx")
        _EFBX["logged"] = False
    return plan


def fetch_flight(cfg: dict) -> dict:
    """Return {'airline','icao','name'} (airline ICAO, destination ICAO) from the latest SimBrief plan."""
    data = efbx_plan(cfg)   # EFBx already has your SimBrief plan: ask it first (no username needed)
    if data is not None:
        return _flight_from_plan(data)
    params = {"json": "1"}
    if str(cfg.get("simbrief_userid", "")).strip():
        params["userid"] = str(cfg["simbrief_userid"]).strip()
    elif str(cfg.get("simbrief_username", "")).strip():
        params["username"] = str(cfg["simbrief_username"]).strip()
    else:
        raise AppError("Enter your SimBrief username: right-click the tray icon -> Set SimBrief username.")

    try:
        resp = requests.get(SIMBRIEF_URL, params=params, timeout=15)
    except requests.RequestException as exc:
        raise AppError(f"Could not reach SimBrief: {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    status = str(data.get("fetch", {}).get("status", ""))
    if resp.status_code != 200 or (status and status.lower() != "success"):
        raise AppError(f"SimBrief: {status or resp.status_code} (check your username and that a plan exists).")

    return _flight_from_plan(data)


def _flight_from_plan(data: dict) -> dict:
    """The flight PAPro needs from a SimBrief plan (the API's JSON, from SimBrief itself or from EFBx)."""
    dest = data.get("destination") or {}
    icao = str(dest.get("icao_code", "")).strip().upper()
    if not icao:
        raise AppError("SimBrief plan has no destination.")
    return {
        "icao": icao,
        "origin": str((data.get("origin") or {}).get("icao_code", "")).strip().upper(),
        "name": str(dest.get("name", "")).strip(),
        "airline": str((data.get("general") or {}).get("icao_airline", "")).strip().upper(),
        "arrival": arrival_clock(data),
        "aircraft": str((data.get("aircraft") or {}).get("icaocode", "")).strip().upper(),   # ICAO type, e.g. A320
        "dest_lat": _number(dest.get("pos_lat")), "dest_lon": _number(dest.get("pos_long")),
        "cruise_ft": _number((data.get("general") or {}).get("initial_altitude")),
        **flight_labels(data),
    }


# airline ICAO -> IATA, for showing "BA1336" when SimBrief only gives the ICAO code (a common few; SimBrief's own wins)
IATA = {"BAW": "BA", "SHT": "BA", "CFE": "BA", "EFW": "BA", "AAL": "AA", "DAL": "DL", "UAL": "UA", "SWA": "WN",
        "DLH": "LH", "AFR": "AF", "KLM": "KL", "RYR": "FR", "EZY": "U2", "UAE": "EK", "QTR": "QR", "ETD": "EY",
        "VIR": "VS", "IBE": "IB", "SAS": "SK", "FIN": "AY", "SWR": "LX", "AUA": "OS", "THY": "TK", "QFA": "QF",
        "ANZ": "NZ", "SIA": "SQ", "CPA": "CX", "JAL": "JL", "ANA": "NH", "KAL": "KE", "ACA": "AC", "WJA": "WS",
        "JBU": "B6", "ASA": "AS", "NKS": "NK", "FFT": "F9", "WZZ": "W6", "VLG": "VY", "TAP": "TP", "EIN": "EI",
        "NAX": "DY", "TVF": "TO", "EWG": "EW", "AZA": "AZ", "ITY": "AZ", "LOT": "LO", "CSA": "OK", "ELY": "LY"}


def flight_labels(data: dict) -> dict:
    """{"number": "BA1336", "callsign": "SHT12Q"} for the flight card (either may be empty)."""
    general, atc = data.get("general") or {}, data.get("atc") or {}
    icao = str(general.get("icao_airline", "")).strip().upper()
    digits = str(general.get("flight_number", "")).strip().upper()
    iata = str(general.get("iata_airline") or general.get("airline_iata") or IATA.get(icao, "")).strip().upper()
    callsign = str(atc.get("callsign") or general.get("callsign") or "").strip().upper()
    number = f"{iata or icao}{digits}" if digits else ""
    return {"number": number, "callsign": callsign}


def _number(value) -> float | None:
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def arrival_clock(data: dict) -> str | None:
    """The local time at the destination when the plan has you landing, as "HH:MM" rounded to 5 minutes.
    Landing time (UTC, SimBrief times.sched_on) + the destination's UTC offset. None when SimBrief doesn't say."""
    times, dest = data.get("times") or {}, data.get("destination") or {}
    moment = next((n for n in (_number(times.get(k)) for k in ("sched_on", "est_on", "sched_in", "est_in")) if n), None)
    offset = next((n for n in (_number(dest.get(k)) for k in ("timezone", "utc_offset", "tz_offset", "gmt_offset"))
                   if n is not None), None)
    if moment is None or offset is None:
        log.info("No arrival local time: SimBrief gave landing=%s, destination offset=%s", moment, offset)
        return None
    if abs(offset) > 24:  # seconds rather than hours
        offset /= 3600
    minutes = int(round((moment / 60 + offset * 60) / 5.0)) * 5  # nearest 5 minutes
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# --------------------------------------------------------------------------- pack catalog
def _is_http(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def _local_path(url: str) -> Path:
    return Path(url2pathname(urlparse(url).path)) if url.lower().startswith("file:") else Path(url)


APP_LATEST: dict = {}   # filled in by load_catalog from the catalog's "app" entry


def _version_tuple(text: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:4]) or (0,)


def app_update() -> dict | None:
    """The newer PAPro the catalog announces, if there is one."""
    if APP_LATEST and _version_tuple(APP_LATEST["version"]) > _version_tuple(VERSION):
        info = dict(APP_LATEST)
        info["auto"] = bool(info.get("installer")) and bool(getattr(sys, "frozen", False))   # the installed program (not the Python source) can update itself
        info.pop("installer", None)
        info.pop("sha256", None)
        return info
    return None


def load_catalog(cfg: dict) -> list[dict]:
    """Catalog JSON: {"packs": [{"icao","name","version","url","sha256"?,"size"?}]}. URL may be relative."""
    url = str(cfg.get("catalog_url", "")).strip()
    if not url or "YOUR-" in url:
        raise AppError("Set catalog_url in config.json to your pack catalog (see README).")
    try:
        if _is_http(url):
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        else:
            data = json.loads(_local_path(url).read_text(encoding="utf-8"))
    except (requests.RequestException, OSError, ValueError) as exc:
        raise AppError(f"Could not load the pack catalog: {exc}") from exc

    app = data.get("app")   # {"version": "1.0.2", "url": "https://...", "notes": "..."}: the newest PAPro
    APP_LATEST.clear()
    if isinstance(app, dict) and str(app.get("version", "")).strip():
        link = str(app.get("url", "")).strip()
        installer = str(app.get("installer", "")).strip()
        digest = str(app.get("sha256", "")).strip().lower()
        # only a GitHub download with a checksum can be installed automatically; anything else falls back to the release page
        auto = bool(re.match(r"https://github\.com/[^/]+/[^/]+/releases/download/[^?#]+\.exe$", installer)) and bool(re.fullmatch(r"[0-9a-f]{64}", digest))
        APP_LATEST.update({"version": str(app["version"]).strip()[:20], "url": link if _is_http(link) else "",
                           "notes": str(app.get("notes", "")).strip()[:200],
                           "installer": installer if auto else "", "sha256": digest if auto else ""})
    packs = []
    for entry in data.get("packs", []):
        icao = str(entry.get("icao", "")).strip().upper()
        raw_url = str(entry.get("url", "")).strip()
        if not re.fullmatch(r"[A-Z0-9]{3}", icao) or not raw_url:
            continue
        if _is_http(raw_url):
            resolved = raw_url
        elif _is_http(url):
            resolved = urljoin(url, raw_url)
        else:
            raw = _local_path(raw_url)
            resolved = str(raw if raw.is_absolute() else _local_path(url).parent / raw)
        voice = str(entry.get("voice", "")).strip()[:40]
        entry_id = re.sub(r"[^a-z0-9-]+", "-", str(entry.get("id", "")).strip().lower()).strip("-") or \
            (f"{icao.lower()}-" + re.sub(r"[^a-z0-9]+", "-", voice.lower()).strip("-") if voice else icao.lower())
        packs.append({
            "id": entry_id,
            "voice": voice,
            "icao": icao,
            "name": str(entry.get("name", icao)),
            "version": str(entry.get("version", "")),
            "sha256": str(entry.get("sha256", "")).lower(),
            "url": resolved,
            "size": int(entry["size"]) if str(entry.get("size", "")).isdigit() else 0,
            "logo": str(entry.get("logo", "")).strip() if _is_http(str(entry.get("logo", "")).strip()) else "",
            "voices": [
                {k: str(v.get(k, "")).strip()[:200] for k in ("name", "artist", "accent", "description")}
                for v in entry.get("voices", []) if isinstance(v, dict) and str(v.get("name", "")).strip()
            ][:12],
        })
    return sorted(packs, key=lambda p: p["icao"])


def pack_identity(zip_path: Path, filename: str = "") -> tuple[str, str]:
    """Which airline a pack zip is for: (ICAO, name), read from the voice.json inside it (its "icao" and "airline"),
    else from the file name (EZY.zip, EZY_pack.zip)."""
    data: dict = {}
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = {n.replace("\\", "/"): n for n in archive.namelist()}
            for key in ("voice.json", *sorted(k for k in names if k.endswith("/voice.json") and k.count("/") <= 2)):
                if key in names:
                    try:
                        found = json.loads(archive.read(names[key]).decode("utf-8-sig"))
                    except (ValueError, KeyError):
                        continue
                    if isinstance(found, dict) and str(found.get("icao", "")).strip():
                        data = found
                        break
    except (zipfile.BadZipFile, OSError) as exc:
        raise AppError(f"That is not a readable pack zip: {exc}") from exc
    code = str(data.get("icao", "")).strip().upper()
    if not code:
        stem = Path(filename.replace("\\", "/")).name
        stem = stem[:-4] if stem.lower().endswith(".zip") else stem
        match = re.match(r"^([A-Za-z0-9]{3})(?:[_\-. ].*)?$", stem)
        code = match[1].upper() if match else ""
    if not re.fullmatch(r"[A-Z0-9]{3}", code):
        raise AppError("This pack does not say which airline it is. Its voice.json needs an \"icao\" entry, like \"icao\": \"BAW\".")
    return code, str(data.get("airline", "")).strip()[:80]


def resolve_pack_url(url: str, icao: str = "") -> str:
    """A link the user pasted -> a direct .zip link. Accepts a direct .zip, a GitHub file page, or a GitHub repository /
    releases page (the zip attached to that release, else the latest one)."""
    url = url.strip()
    if not _is_http(url):
        raise AppError("Paste a link that starts with https://")
    clean = url.split("#")[0].split("?")[0].rstrip("/")
    if clean.lower().endswith(".zip"):
        blob = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", clean)
        return f"https://raw.githubusercontent.com/{blob[1]}/{blob[2]}/{blob[3]}" if blob else url
    m = re.match(r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/releases(?:/tag/([^/]+)|/latest)?)?$", clean)
    if not m:
        raise AppError("Use a link to a .zip file, or to a GitHub repository or release page.")
    owner, repo, tag = m[1], m[2], m[3]
    api = f"https://api.github.com/repos/{owner}/{repo}/releases/" + (f"tags/{tag}" if tag else "latest")
    try:
        resp = requests.get(api, timeout=20, headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException as exc:
        raise AppError(f"Could not reach GitHub: {exc}") from exc
    if resp.status_code == 404:
        raise AppError("GitHub has no such release (is the repository public, and does it have a release?).")
    if not resp.ok:
        raise AppError(f"GitHub answered {resp.status_code}.")
    assets = [a for a in resp.json().get("assets", []) if str(a.get("name", "")).lower().endswith(".zip")]
    if not assets:
        raise AppError("That GitHub release has no .zip file attached.")
    if icao:
        assets.sort(key=lambda a: not str(a["name"]).upper().startswith(icao.upper()))
    return str(assets[0]["browser_download_url"])


def download_pack(entry: dict, dest_file: Path, progress=None) -> None:
    digest = hashlib.sha256()
    url = entry["url"]
    try:
        if _is_http(url):
            with requests.get(url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                done = 0
                with open(dest_file, "wb") as out:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        done += len(chunk)
                        if done > MAX_DOWNLOAD_BYTES:
                            raise AppError("Pack is larger than 1 GB - refusing to download.")
                        out.write(chunk)
                        digest.update(chunk)
                        if progress and total:
                            progress(done, total)
        else:
            shutil.copyfile(_local_path(url), dest_file)
            digest.update(dest_file.read_bytes())
    except (requests.RequestException, OSError) as exc:
        raise AppError(f"Download failed: {exc}") from exc
    expected = entry.get("sha256", "")
    if expected and digest.hexdigest() != expected:
        raise AppError("Download failed its checksum - the file is corrupt or has been changed.")


def extract_pack(zip_path: Path, out_dir: Path) -> None:
    """Extract only main/, dep/, dest/, fuel/ from the zip, refusing anything that escapes out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    root = out_dir.resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
                if info.is_dir() or not parts or (parts[0] not in ZIP_FOLDERS and parts not in (["voice.json"], ["logo.png"], ["scripts.json"], ["languages.json"])):
                    continue
                if any(p == ".." or ":" in p for p in parts):
                    raise AppError("Pack zip contains an unsafe path - refusing to install it.")
                target = out_dir.joinpath(*parts)
                if root not in target.resolve().parents:
                    raise AppError("Pack zip contains an unsafe path - refusing to install it.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as exc:
        raise AppError("The downloaded pack is not a valid zip file.") from exc
    mains = [out_dir / MAIN_FOLDER, *sorted(out_dir.glob(f"{VOICES_FOLDER}/*/{MAIN_FOLDER}"))]
    if not any(m.is_dir() and any(p.is_file() for p in m.iterdir()) for m in mains):
        raise AppError(f"The pack zip has no '{MAIN_FOLDER}' folder with announcement files in it.")


# --------------------------------------------------------------------------- the Airlines library
def airlines_root(cfg: dict) -> Path:
    configured = str(cfg.get("airlines_dir", "")).strip()
    return Path(configured) if configured else DATA_DIR / "Airlines"


def airline_folder(cfg: dict, icao: str) -> Path:
    return airlines_root(cfg) / icao.upper()


def check_airline_code(icao: str) -> str:
    icao = icao.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3}", icao):
        raise AppError(f"'{icao}' is not a 3-character airline code (like BAW).")
    return icao


def ensure_airline_folders(cfg: dict, icao: str) -> tuple[Path, bool]:
    """Create Airlines\\<ICAO>\\main, time, extra, airports, fuel if they are missing. Returns (folder, newly_created)."""
    icao = check_airline_code(icao)
    base = airline_folder(cfg, icao)
    created = not base.is_dir()
    try:
        for folder in (MAIN_FOLDER, PARTS_FOLDER, EXTRA_FOLDER, AIRPORTS_FOLDER, "fuel"):
            (base / folder).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AppError(f"Could not create {base}: {exc}") from exc
    return base, created


def has_ogg(folder: Path) -> bool:
    return folder.is_dir() and any(p.suffix.lower() == ".ogg" for p in folder.iterdir() if p.is_file())


def adopt_aliases(cfg: dict, icao: str, staging: Path) -> list[str]:
    """A pack can say which other airline codes use it ("also_icaos" in its voice.json): add those as aliases of this
    airline. A code that has a pack of its own, or an alias you already set, is left alone."""
    codes: list[str] = []
    for path in [staging / "voice.json", *sorted(staging.glob(f"{VOICES_FOLDER}/*/voice.json"))]:
        try:
            found = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(found, dict) and isinstance(found.get("also_icaos"), list):
            codes += [str(c).strip().upper() for c in found["also_icaos"]]
    added = []
    try:
        aliases = load_aliases(cfg)
        if aliases.get(icao):
            return []
        for code in dict.fromkeys(codes):
            if not re.fullmatch(r"[A-Z0-9]{3}", code) or code == icao or code in aliases or airline_folder(cfg, code).is_dir():
                continue
            aliases[code] = icao
            added.append(code)
        if added:
            save_aliases(aliases)
            state = load_state()
            state.pop("last", None)
            save_state(state)
    except (AppError, OSError):
        log.exception("Could not add the pack's alias airlines")
    return added


def install_pack(cfg: dict, entry: dict, progress=None) -> str:
    """progress(text, fraction) is called as it goes; fraction is 0..1 (or None when unknown)."""
    """Install a catalog pack: its files go into Airlines\\<ICAO> (never over your own recordings) and its generic
    files into the Fenix AirlinePacks folder. A pack that was already in the Fenix folder is set aside and restored
    when you uninstall."""
    icao = entry["icao"]
    fenix_dir = airline_packs_dir(cfg) / icao
    lib_dir = airline_folder(cfg, icao)
    state = load_state()
    installed = state.setdefault("installed", {})
    record = installed.get(icao) or {}
    ours = set(record.get("files", []))

    def report(text: str, fraction: float | None = None) -> None:
        if progress:
            progress(text, fraction)

    def downloading(done: int, total: int) -> None:
        report(f"Downloading  {done / 1e6:.0f} of {total / 1e6:.0f} MB", 0.8 * done / total)

    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp:
        zip_path = Path(tmp) / f"{icao}.zip"
        report("Starting download", 0.0)
        download_pack(entry, zip_path, downloading)
        report("Verified - unpacking", 0.82)
        staging = Path(tmp) / "extract"
        extract_pack(zip_path, staging)
        report("Installing files", 0.88)
        try:   # the pack's own default for the local time (from its voice.json), unless you already chose
            said = json.loads((staging / "voice.json").read_text(encoding="utf-8-sig")).get("local_time")
            if isinstance(said, bool) and "local_time" not in pack_settings(cfg, icao):
                set_pack_setting(cfg, icao, "local_time", said)
        except (OSError, ValueError, AttributeError):
            pass
        also_added = adopt_aliases(cfg, icao, staging)
        if also_added:
            log.info("%s pack: %s now use its recordings", icao, ", ".join(also_added))

        try:
            # 1. Library: add the pack's files. Files you put there yourself are never overwritten.
            added, skipped = [], 0
            files = sorted(p for p in staging.rglob("*") if p.is_file())
            for number, src in enumerate(files, 1):
                if number % 20 == 0:
                    report("Installing files", 0.88 + 0.1 * number / len(files))
                rel = src.relative_to(staging).as_posix()
                dst = lib_dir / rel
                if dst.exists() and rel not in ours:
                    skipped += 1
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                added.append(rel)
            ensure_airline_folders(cfg, icao)

            # 2. Fenix folder: the pack's generic announcements.
            backup = record.get("backup")
            if fenix_dir.exists():
                if record:
                    shutil.rmtree(fenix_dir)  # upgrade of our own install
                else:  # a pack we didn't install is there: set it aside so uninstall can put it back
                    backup_path = BACKUPS_DIR / f"{icao}-{time.strftime('%Y%m%d-%H%M%S')}"
                    shutil.move(str(fenix_dir), str(backup_path))
                    backup = str(backup_path)
            fenix_dir.parent.mkdir(parents=True, exist_ok=True)
            generic = lib_dir / MAIN_FOLDER
            source = generic if has_ogg(generic) else staging / MAIN_FOLDER
            if not source.is_dir():   # a pack that only has voices\\<Name>\\main: use the first voice's general announcements
                voice_mains = sorted((staging / VOICES_FOLDER).glob(f"*/{MAIN_FOLDER}")) if (staging / VOICES_FOLDER).is_dir() else []
                if not voice_mains:
                    raise AppError("This pack has no general announcements (a main folder).")
                source = voice_mains[0]
            shutil.copytree(source, fenix_dir, ignore=shutil.ignore_patterns("Modular_*", "modular_*"))
        except OSError as exc:
            raise AppError(f"Could not install {icao} (is MSFS using the files?): {exc}") from exc

    report("Finishing", 0.99)
    installed[icao] = {
        "name": entry.get("name", icao),
        "version": entry.get("version", ""),
        "backup": backup,
        "files": sorted(set(added) | {rel for rel in ours if (lib_dir / rel).is_file()}),  # what uninstall may remove
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "catalog": {**record.get("catalog", {}), **({entry["id"]: entry.get("version", "")} if entry.get("id") else {})},
    }
    state.pop("last", None)  # force the next update to apply the destination
    save_state(state)
    log.info("Installed pack %s v%s (%d files, %d of yours kept)", icao, entry.get("version", ""), len(added), skipped)
    shown = str(entry.get("version") or "").strip()
    if shown in ("", "imported"):   # an imported pack has no catalog version: use the one stored with the pack, if any
        try:
            shown = library_version(cfg, icao)
        except (OSError, AppError):
            shown = ""
    version = f" v{shown}" if shown[:1].isdigit() else ""
    note = f" ({skipped} of your own files kept)" if skipped else ""
    voice = f" ({entry['voice']})" if entry.get("voice") else ""
    return f"Installed {icao} - {entry.get('name', icao)}{voice}{version}{note}"


AIRLINE_NAMES = {
    "EZY": "easyJet", "BAW": "British Airways", "RYR": "Ryanair", "DLH": "Lufthansa", "AFR": "Air France", "KLM": "KLM",
    "UAE": "Emirates", "QTR": "Qatar Airways", "ETD": "Etihad Airways", "THY": "Turkish Airlines", "SAS": "Scandinavian Airlines",
    "VIR": "Virgin Atlantic", "TOM": "TUI Airways", "TUI": "TUI fly", "WZZ": "Wizz Air", "VLG": "Vueling", "IBE": "Iberia",
    "SWR": "Swiss", "AUA": "Austrian", "BEL": "Brussels Airlines", "FIN": "Finnair", "ICE": "Icelandair", "EIN": "Aer Lingus",
    "NAX": "Norwegian", "NOZ": "Norwegian", "TAP": "TAP Air Portugal", "AEE": "Aegean Airlines", "LOT": "LOT Polish Airlines",
    "AAL": "American Airlines", "DAL": "Delta Air Lines", "UAL": "United Airlines", "SWA": "Southwest Airlines", "JBU": "JetBlue",
    "ASA": "Alaska Airlines", "ACA": "Air Canada", "WJA": "WestJet", "QFA": "Qantas", "ANZ": "Air New Zealand", "SIA": "Singapore Airlines",
    "CPA": "Cathay Pacific", "JAL": "Japan Airlines", "ANA": "All Nippon Airways", "KAL": "Korean Air", "THA": "Thai Airways",
    "EVA": "EVA Air", "CCA": "Air China", "CES": "China Eastern", "CSN": "China Southern", "ETH": "Ethiopian Airlines",
    "SAA": "South African Airways", "MSR": "EgyptAir", "SVA": "Saudia", "GIA": "Garuda Indonesia", "AIC": "Air India", "VOZ": "Virgin Australia",
    "SHT": "BA Shuttle", "EXS": "Jet2", "JET": "Jet2", "LOG": "Loganair", "EJU": "easyJet Europe", "BCS": "European Air Transport",
}


def airline_display_name(cfg: dict, icao: str, record: dict | None = None, catalog_name: str = "") -> str:
    """Best name for an airline folder: what the pack said, then a voice.json, a Pack Studio project, a known airline."""
    for name in (catalog_name, (record or {}).get("name", "")):
        if name and name.upper() != icao.upper():
            return name
    try:
        folder = airline_folder(cfg, icao)
        for path in [folder / "voice.json", *sorted(folder.glob("voices/*/voice.json"))]:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and str(data.get("airline", "")).strip().upper() not in ("", icao.upper()):
                return str(data["airline"]).strip()
    except OSError:
        pass
    try:
        for proj in papro_studio.list_projects(STUDIO_DIR):
            if str(proj.get("icao", "")).upper() == icao.upper() and str(proj.get("airline", "")).strip().upper() not in ("", icao.upper()):
                return str(proj["airline"])
    except Exception:
        pass
    return AIRLINE_NAMES.get(icao.upper(), icao.upper())


def library_voice_items(cfg: dict, icao: str) -> list[dict]:
    """The voices of an airline in your library: [{"key", "name"}] (key is what Pack Studio opens, "Default" is the airline's own folder)."""
    try:
        folder = airline_folder(cfg, icao)
        found = list_voices(folder)
        if not found and has_ogg(folder / MAIN_FOLDER):  # an airline with only general PAs
            found = {DEFAULT_VOICE: folder}
        return [{"key": key, "name": voice_info(path, key)["name"]} for key, path in found.items()]
    except (OSError, AppError):
        return []


def library_voices(cfg: dict, icao: str) -> list[str]:
    return [v["name"] for v in library_voice_items(cfg, icao) if v["key"] != DEFAULT_VOICE]


def library_version(cfg: dict, icao: str) -> str:
    """The newest pack version recorded in the voice.json files of an airline in your library ("" when none says)."""
    folder = airline_folder(cfg, icao)
    found = []
    try:
        for path in [folder / "voice.json", *sorted((folder / VOICES_FOLDER).glob("*/voice.json"))]:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            version = str(data.get("version", "")).strip() if isinstance(data, dict) else ""
            if version:
                found.append(version)
    except OSError:
        pass
    return max(found, key=_version_tuple) if found else ""


def files_in_use(folder: Path) -> bool:
    """Is the sim (Fenix) holding any sound file in this folder open right now? Windows refuses to open a file for writing
    while another program is playing it."""
    try:
        for path in folder.rglob("*.ogg"):
            try:
                with open(path, "r+b"):
                    pass
            except PermissionError:
                return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def check_not_in_use(cfg: dict, icao: str) -> None:
    folder = airline_packs_dir(cfg) / icao
    if folder.is_dir() and files_in_use(folder):
        raise AppError(f"The {icao} announcements are being played by the sim right now - try again when the announcement has finished.")


def remove_voice(cfg: dict, icao: str, key: str) -> str:
    """Take one voice out of an airline: its folder is moved to the PAPro backups folder, not deleted."""
    icao = check_airline_code(icao)
    folder = airline_folder(cfg, icao) / VOICES_FOLDER / key
    if not key or key == DEFAULT_VOICE or Path(key).name != key or not folder.is_dir():
        raise AppError("That voice cannot be removed here.")
    if len(list_voices(airline_folder(cfg, icao))) <= 1:   # the last voice takes the whole pack with it
        check_not_in_use(cfg, icao)
    try:
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(BACKUPS_DIR / f"{icao}-voice-{key}-{time.strftime('%Y%m%d-%H%M%S')}"))
    except OSError as exc:
        raise AppError(f"Could not remove the voice (is MSFS using the files?): {exc}") from exc
    state = load_state()
    if state.get("voices", {}).get(icao) == key:
        state["voices"].pop(icao)
    state.pop("last", None)
    save_state(state)
    log.info("Removed voice %s from %s (kept in backups)", key, icao)
    if not list_voices(airline_folder(cfg, icao)):   # that was the last voice: nothing of the pack is left to keep
        try:
            return uninstall_pack(cfg, icao)
        except AppError:
            pass
    return f"Removed the {key} voice from {icao}"


def _remove_library_folder(lib_dir: Path, icao: str) -> None:
    """A folder with no sound files is simply deleted; one with recordings is moved to the backups, never lost."""
    if any(lib_dir.rglob("*.ogg")):
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(lib_dir), str(BACKUPS_DIR / f"{icao}-recordings-{time.strftime('%Y%m%d-%H%M%S')}"))
    else:
        shutil.rmtree(lib_dir)


def uninstall_pack(cfg: dict, icao: str) -> str:
    """Remove what install_pack added (your own recordings stay) and restore any pack that was there before."""
    icao = check_airline_code(icao)
    check_not_in_use(cfg, icao)
    state = load_state()
    record = state.get("installed", {}).get(icao)
    if not record:  # recordings you made or copied in yourself: set the folder aside (in backups) rather than deleting it
        lib_dir = airline_folder(cfg, icao)
        if not lib_dir.is_dir():
            raise AppError(f"The {icao} pack is not installed.")
        try:
            _remove_library_folder(lib_dir, icao)
            shutil.rmtree(GENERIC_DIR / icao, ignore_errors=True)
        except OSError as exc:
            raise AppError(f"Could not remove {icao} (is MSFS using the files?): {exc}") from exc
        state.pop("last", None)
        save_state(state)
        log.info("Removed library folder %s (kept in backups)", icao)
        return f"Removed {icao} (a copy of the recordings is in the PAPro backups folder)"
    try:
        fenix_dir = airline_packs_dir(cfg) / icao
        if fenix_dir.exists():
            shutil.rmtree(fenix_dir)
        backup = record.get("backup")
        restored = False
        if backup and Path(backup).is_dir():
            shutil.move(backup, str(fenix_dir))
            restored = True

        lib_dir = airline_folder(cfg, icao)
        for rel in record.get("files", []):
            (lib_dir / rel).unlink(missing_ok=True)
        for folder in sorted((p for p in lib_dir.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            try:
                folder.rmdir()  # only removes folders that are now empty
            except OSError:
                pass
        try:
            lib_dir.rmdir()
        except OSError:
            pass
        shutil.rmtree(GENERIC_DIR / icao, ignore_errors=True)
        # Whatever is left (a logo, voice.json, recordings added from Pack Studio) belongs to the one voice this pack was:
        # take the pack out completely, so it doesn't stay in the list needing a second Uninstall. Several voices left
        # means other voices of yours are there: those stay (remove them one by one).
        if lib_dir.is_dir() and len(list_voices(lib_dir)) <= 1:
            _remove_library_folder(lib_dir, icao)
    except OSError as exc:
        raise AppError(f"Could not uninstall {icao} (is MSFS using the files?): {exc}") from exc
    del state["installed"][icao]
    state.pop("last", None)
    save_state(state)
    log.info("Uninstalled pack %s", icao)
    return f"Uninstalled {icao}" + (" (your previous pack was restored)" if restored else "")


# --------------------------------------------------------------------------- applying a destination
def pack_tags(filename: str) -> set[str]:
    return {t.lower() for t in re.findall(r"\[([^\]]+)\]", filename)}


def target_names(pristine: Path | None, mapping: dict, also: Path | None = None) -> list[str]:
    """Pack files this mapping replaces: every variant of the base name that passes the tag rules."""
    require = {t.lower() for t in mapping.get("require_tags", [])}
    exclude = {t.lower() for t in mapping.get("exclude_tags", [])}
    names = []
    files = sorted(pristine.iterdir()) if pristine else []
    if also and also.is_dir():   # a voice's own generics can add variants the original pack lacks (e.g. [Night])
        files += sorted(also.iterdir())
    for path in files:
        if not path.is_file() or path.suffix.lower() != ".ogg" or path.name in names:
            continue
        if re.split(r"[\[~]", path.stem, maxsplit=1)[0] != mapping["base"]:
            continue
        tags = pack_tags(path.name)
        if require <= tags and not (exclude & tags):
            names.append(path.name)
    return names or [f"{mapping['default_name']}.ogg"]


def boost_percent(cfg: dict) -> int:
    try:
        return max(0, min(100, int(cfg.get("pa_boost", 30))))
    except (TypeError, ValueError):
        return 30


def boosted_copy(src: Path, dst: Path, percent: int) -> bool:
    """Write a louder copy of an .ogg. False = could not (no audio library...), so the caller copies it unchanged."""
    if papro_audio is None or not papro_audio.have_backend():
        return False
    try:
        papro_audio.boost(src, dst, percent)
        return True
    except (papro_audio.AudioError, OSError, ValueError) as exc:
        log.warning("Could not make %s louder (%s) - copied as recorded", src.name, exc)
        dst.unlink(missing_ok=True)
        return False


def boost_stamp(src: Path, percent: int) -> str:
    info = src.stat()
    return f"{info.st_mtime_ns}:{info.st_size}:{percent}"


def merged_music(tracks: list[Path]) -> Path | None:
    """The boarding tracks joined into one file (kept in the data folder and rebuilt only when a track changes).
    One track is used as it is. None if joining is not possible."""
    if len(tracks) == 1:
        return tracks[0]
    if papro_audio is None or not papro_audio.have_backend():
        return None
    stamp = hashlib.sha1("|".join(f"{t.name}:{t.stat().st_size}:{int(t.stat().st_mtime)}" for t in tracks).encode()).hexdigest()[:12]
    out = DATA_DIR / "music" / f"BoardingMusic-{stamp}.ogg"
    if not out.is_file():
        try:
            for old in out.parent.glob("BoardingMusic-*.ogg") if out.parent.is_dir() else ():
                old.unlink()
            papro_audio.merge_tracks(tracks, out)
        except (papro_audio.AudioError, OSError) as exc:
            log.warning("Could not merge the boarding music: %s", exc)
            return None
    out2 = out.with_name("BoardingMusic.ogg")
    shutil.copyfile(out, out2)
    return out2


def sync_needed(src: Path, dst: Path, percent: int) -> bool:
    """Is the Fenix copy of a general PA out of date? (A boosted copy never equals its source, so it is tracked by stamp.)"""
    if not dst.is_file():
        return True
    if not percent or src.name.startswith("BoardingMusic"):
        return not filecmp.cmp(src, dst, shallow=False)
    return load_state().get("boosted", {}).get(str(dst)) != boost_stamp(src, percent)


def install_file(src: Path, dst: Path, boost: int = 0) -> None:
    """Copy via a temp file + atomic replace (louder by `boost` percent when asked). Retries because the sim may briefly
    lock the file."""
    tmp = dst.with_name(dst.name + ".tmp")
    made = bool(boost) and not src.name.startswith("BoardingMusic") and boosted_copy(src, tmp, boost)
    last: Exception | None = None
    for _ in range(6):
        try:
            if not made:
                shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
            state = load_state()
            stamps = state.setdefault("boosted", {})
            if made and src.is_file():
                stamps[str(dst)] = boost_stamp(src, boost)
            else:
                stamps.pop(str(dst), None)
            save_state(state)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(1)
    tmp.unlink(missing_ok=True)
    raise AppError(f"{dst.name} is locked (is the sim playing it?): {last}")


def generic_source(cfg: dict, airline: str, fenix_dir: Path) -> Path | None:
    """Where the airline's ORIGINAL announcements are kept, so an airport without a recording can go back to them.

    Order: Airlines\\<ICAO>\\main (the airline's general PAs; a catalog pack installs them there), then PAPro's saved
    snapshot. The snapshot is taken from the Fenix pack the first time PAPro is about to change it.
    """
    library_main = airline_folder(cfg, airline) / MAIN_FOLDER
    if has_ogg(library_main):
        return library_main
    snapshot = GENERIC_DIR / airline
    if not has_ogg(snapshot) and has_ogg(fenix_dir):
        snapshot.mkdir(parents=True, exist_ok=True)
        for path in fenix_dir.iterdir():
            if path.is_file() and path.suffix.lower() == ".ogg":
                shutil.copy2(path, snapshot / path.name)
        log.info("Saved a copy of the original %s announcements", airline)
    return snapshot if has_ogg(snapshot) else None


STANDARD = "Standard"  # label for the plain <AIRLINE>_<ICAO>.ogg recording (no terminal/variant suffix)


def find_clips(lib_dir: Path, folder: str, airline: str, dest_icao: str) -> dict[str, Path]:
    """Recordings for a destination in one folder, keyed by variant label (upper case; "" = the plain file).
    BAW_EGLL.ogg -> "",  BAW_EGLL_T5.ogg -> "T5",  BAW_EGLL_Gen.ogg -> "GEN". Matching ignores case."""
    pattern = re.compile(rf"{re.escape(airline)}_{re.escape(dest_icao)}(?:_([A-Z0-9]+))?\.ogg", re.IGNORECASE)
    clips: dict[str, Path] = {}
    folder_path = lib_dir / folder
    if folder_path.is_dir():
        for path in sorted(folder_path.iterdir()):
            match = pattern.fullmatch(path.name)
            if match and path.is_file():
                clips[(match.group(1) or "").upper()] = path
    return clips


def variant_options(all_clips: dict[str, dict[str, Path]]) -> list[str]:
    """Every variant label offered for a destination across dep/dest/fuel, plain first then alphabetical."""
    return sorted({label for clips in all_clips.values() for label in clips}, key=lambda label: (label != "", label))


def variant_name(label: str) -> str:
    return label or STANDARD


CHOOSER = None  # set by the tray app: asks in the PAPro window which recording to use


def choose_default(title: str, prompt: str, options: list[str]) -> tuple[str, bool]:
    """No window available (command line): take the first option."""
    return options[0], False


def pick_named(airline: str, base_name: str, options: list[str], choose) -> str:
    """Which named version of a PA (Cabin service: Domestic / International...) to use. The user is asked once for each flight; a
    second language of the same pack reuses the answer."""
    flight = load_state().get("flight") or {}
    key = f"{airline}/{base_name}/{flight.get('number', '')}/{flight.get('origin', '')}/{flight.get('dest', '')}"
    state = load_state()
    picks = state.setdefault("named_pick", {})
    if picks.get(key) in options:
        return picks[key]
    label = papro_studio.NAMED_BASES.get(base_name[:-4], base_name[:-4])
    answer, _ = choose(f"{airline} - {label}", f"This pack has more than one {label.lower()} announcement. Which one would you like for this flight?", options)
    picked = answer if answer in options else options[0]
    state = load_state()
    picks = state.setdefault("named_pick", {})
    if len(picks) > 20:
        picks.clear()
    picks[key] = picked
    save_state(state)
    return picked


_DIVERSION = threading.local()   # .on while building the landing PA for an airport that is not the planned destination


def pick_variant(airline: str, dest_icao: str, options: list[str], choose) -> str:
    """Return the variant label to use. The user is always asked when an airport has more than one recording. The one
    exception is a diversion (landed somewhere else than planned): the generic recording is used if there is one -
    the plain file, else _Gen - and only when there is none is the user asked."""
    if getattr(_DIVERSION, "on", False):
        for generic in ("", "GEN"):
            if generic in options:
                log.info("Diversion to %s: using the generic recording", dest_icao)
                return generic
    names = {variant_name(label): label for label in options}
    answer, _ = choose(
        f"{airline} to {dest_icao}",
        f"There is more than one recording for {dest_icao}. Which would you like?",
        list(names),
    )
    return names.get(answer, options[0])


TIME_OF_DAY = ("morning", "afternoon", "evening", "night")   # Fenix tags: night 00-06, morning 06-12, afternoon 12-18, evening 18-24


def list_voices(lib_dir: Path) -> dict[str, Path]:
    """Voices of an airline: "Default" (the recordings in Airlines\\<ICAO> itself, if any) and one per folder in
    Airlines\\<ICAO>\\voices that has sound files. Each voice has its own parts / dep / dest / fuel."""
    voices: dict[str, Path] = {}
    if any(has_ogg(lib_dir / folder) for folder in (PARTS_FOLDER, LEGACY_PARTS, EXTRA_FOLDER, AIRPORTS_FOLDER, "dep", "dest", "fuel")):
        voices[DEFAULT_VOICE] = lib_dir
    root = lib_dir / VOICES_FOLDER
    if root.is_dir():
        for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if path.is_dir() and any(path.rglob("*.ogg")):
                voices[path.name] = path
    if DEFAULT_VOICE in voices and len(voices) > 1:  # the same voice both in the airline folder and in voices\<Name> (an imported pack
        names = {voice_info(p, k)["name"].strip().lower() for k, p in voices.items() if k != DEFAULT_VOICE}  # beside a Studio copy)
        if voice_info(lib_dir, DEFAULT_VOICE)["name"].strip().lower() in names:
            del voices[DEFAULT_VOICE]
    return voices


def voice_info(path: Path, key: str) -> dict:
    """What the window shows for a voice: voice.json in its folder ({"name","artist","credit_url","accent",
    "description"}, all optional). Without it, the folder name is the name."""
    info = {"id": key, "name": key, "artist": "", "credit_url": "", "donate_url": "", "accent": "", "description": "", "language": ""}
    try:
        data = json.loads((path / "voice.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return info
    if isinstance(data, dict):
        for field in ("name", "artist", "credit_url", "donate_url", "accent", "description"):
            value = str(data.get(field, "")).strip()[:200]
            if value:
                info[field] = value
        lang = str(data.get("language", "")).strip().lower()
        if lang in papro_studio.LANGUAGES:
            info["language"] = lang
    if not _is_http(info["credit_url"]):
        info["credit_url"] = ""  # "TBC" and the like: nothing to link to
    if not _is_http(info["donate_url"]):
        info["donate_url"] = ""
    return info


def choose_voice(source: str, lib_dir: Path, voice: str | None = None) -> tuple[Path, str, int]:
    """Which voice's recordings to use: (folder, name, how many voices there are). `voice` forces one; otherwise the
    one picked in the window (a name, or "random" for a different one each flight), else the first."""
    voices = list_voices(lib_dir)
    if not voices:
        return lib_dir, "", 0
    if voice in voices:
        name = voice
    else:
        wanted = load_state().get("voices", {}).get(source)
        name = random.choice(list(voices)) if wanted == "random" else (wanted if wanted in voices else next(iter(voices)))
    return voices[name], name, len(voices)


def _find_piece(base: Path, names: list[str], subs: tuple[str, ...]) -> Path | None:
    for sub in subs:
        folder = base / sub
        if folder.is_dir():
            files = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
            for name in names:
                if f"{name}.ogg".lower() in files:
                    return files[f"{name}.ogg".lower()]
    return None


def part_file(base: Path, name: str) -> Path | None:
    """A greeting or time piece, e.g. Hour_04.ogg, from Airlines\\<ICAO>\\time (case-insensitive)."""
    return _find_piece(base, [name], (PARTS_FOLDER, LEGACY_PARTS))


VARIANT_RE = re.compile(r"^(?P<base>.+?)~v(?P<n>\d+)\.ogg$", re.IGNORECASE)   # AfterTakeoff~v2.ogg: another take of AfterTakeoff.ogg


TYPED_RE = re.compile(r"^(?P<base>[A-Za-z_]+)@(?P<types>[A-Za-z0-9+]+)(?:~v\d+)?(?P<tags>(?:\[[^\]]*\])*)\.ogg$", re.IGNORECASE)
_AIRCRAFT_LOCAL = threading.local()   # the Test tab builds for a chosen aircraft type without touching the real flight


def typed_info(name: str):
    """'SafetyBriefing@A320+A319~v2.ogg' -> ('SafetyBriefing.ogg', {'A320', 'A319'}): the file name it replaces, and the
    aircraft types it is for ('ALL' = every aircraft). None for an ordinary file."""
    m = TYPED_RE.match(name)
    if not m:
        return None
    return f"{m['base']}{m['tags']}.ogg", {t.upper() for t in m["types"].split("+") if t}


def current_aircraft_type() -> str:
    """ICAO type of the aircraft in the flight plan (e.g. A320), or "" when unknown."""
    override = getattr(_AIRCRAFT_LOCAL, "value", None)
    if override is not None:
        return override.upper()
    return str((load_state().get("flight") or {}).get("aircraft", "")).upper()


def typed_choice(key: str, generic: list[Path], typed: list[Path]) -> Path | None:
    """Pick the take of a PA for this aircraft. Takes made for the aircraft's own type win; takes marked ALL join the
    ordinary ones. One is chosen at random once per flight."""
    ac = current_aircraft_type()
    specific = [p for p in typed if ac and ac in typed_info(p.name)[1]]
    if specific:
        return choose_variant(f"{key}@{ac}", specific)
    pool = generic + [p for p in typed if "ALL" in typed_info(p.name)[1]]
    return choose_variant(key, pool) if pool else None


def is_variant(name: str) -> bool:
    return bool(VARIANT_RE.match(name)) and not name.lower().startswith("boardingmusic") and not typed_info(name)


def choose_variant(key: str, options: list[Path]) -> Path:
    """One of several takes of the same PA, rolled once per flight (so rebuilding the PA keeps the same one)."""
    if len(options) < 2:
        return options[0]
    state = load_state()
    picks = state.setdefault("main_pick", {})
    chosen = next((p for p in options if p.name == picks.get(key)), None)
    if chosen is None:
        chosen = random.choice(options)
        picks[key] = chosen.name
        save_state(state)
        log.info("Variation for %s: %s", key, chosen.stem)
    return chosen


def modular_piece(base: Path, kind: str) -> Path | None:
    """The rest of a modular PA: Modular_Welcome.ogg or Modular_Landing.ogg (in main, next to the generic PAs)."""
    found = _find_piece(base, [f"Modular_{kind}", f"{kind}_Rest"], (MAIN_FOLDER, PARTS_FOLDER, LEGACY_PARTS))
    if not found:
        return None
    takes = [found] + sorted(p for p in found.parent.iterdir() if p.is_file() and is_variant(p.name)
                             and VARIANT_RE.match(p.name)["base"].lower() == found.stem.lower())
    typed = sorted(p for p in found.parent.iterdir() if p.is_file() and typed_info(p.name)
                   and typed_info(p.name)[0].lower() == found.name.lower())   # Modular_Welcome@A320.ogg: only for some aircraft
    return typed_choice(f"{base.parent.name}/{base.name}/{found.stem}", takes, typed) or found


def is_modular(name: str) -> bool:
    return name.lower().startswith("modular_")


def take_choice(key: str, base: Path | None, folders, stem: str | None = None) -> Path | None:
    """`base` or one of its extra takes (<stem>~v2.ogg ... in `folders`), rolled once per flight like the other variations.
    With no base file the takes alone are used."""
    stem = stem or (base.stem if base else "")
    takes = [base] if base else []
    for folder in folders:
        if folder is None or not Path(folder).is_dir():
            continue
        for path in sorted(Path(folder).iterdir()):
            match = VARIANT_RE.match(path.name) if path.is_file() else None
            if match and match["base"].lower() == stem.lower() and is_variant(path.name) and path.name not in {t.name for t in takes}:
                takes.append(path)
    return choose_variant(key, takes) if takes else None


def fuel_tag(base: Path) -> Path | None:
    """The tag-on said while refuelling: the first sound file in fuel that isn't an airport clip (BAW_EGLL.ogg), or one of
    its extra takes (Fuel_Tag~v2.ogg)."""
    folder = base / "fuel"
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix.lower() == ".ogg" and not re.match(r"[A-Za-z0-9]{3}_[A-Za-z0-9]{4}", path.name) \
                    and not is_variant(path.name):
                return take_choice(f"{base.parent.name}/{base.name}/{path.stem}", path, [folder])
    return None


def pick_extra(cfg: dict, base: Path, key: str) -> Path | None:
    """Now and then a random bolt-on from the extra folder (nut allergy, Wi-Fi unavailable...), chosen once per
    flight so rebuilding the PA (voice change, refuelling) keeps the same one."""
    folder = base / EXTRA_FOLDER
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".ogg") if folder.is_dir() else []
    state = load_state()
    used = state.setdefault("extra_used", {})
    if key not in used:
        chance = max(0, min(100, int(cfg.get("extra_chance", 25))))
        used[key] = random.choice(files).name if files and random.random() * 100 < chance else ""
        save_state(state)
    return next((p for p in files if p.name == used[key]), None)


BOARDING_PA_MIN_S, BOARDING_PA_MAX_S = 180, 420     # the boarding announcement repeats this often while boarding
PLAYBACK_DIR = DATA_DIR / "playback"          # where PAs are built for aircraft that PAPro plays itself
_PLAYBACK = {"on": False}                      # set from the sim: the loaded aircraft is not a supported (Fenix) one
KNOB = {"on": True, "volume": 70}              # the on-screen PA knob, mirrored here for the audio thread


def clear_playback() -> None:
    """Remove the PAs built for an earlier flight, so PAPro plays nothing when this flight has no pack."""
    try:
        for path in PLAYBACK_DIR.rglob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)
    except OSError:
        log.warning("Could not clear the playback folder")


def playback_target() -> Path | None:
    return PLAYBACK_DIR if _PLAYBACK["on"] else None


def knob_gain() -> float:
    return (max(0, min(100, int(KNOB["volume"]))) / 100.0) if KNOB["on"] else 0.0


def clock_tag() -> str:
    hour = time.localtime().tm_hour
    return "night" if hour < 6 else "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"


def voice_setting(voice_dir: Path, key: str):
    """A value from the voice's voice.json (None if there is none)."""
    try:
        data = json.loads((voice_dir / "voice.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data.get(key) if isinstance(data, dict) else None


def crew_settings(voice_dir: Path) -> dict | None:
    """The crew introduction a voice was made with (voice.json "crew": the order of the pieces and the flight attendants per
    aircraft type), or None when the voice has none."""
    try:
        data = json.loads((voice_dir / "voice.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    crew = data.get("crew") if isinstance(data, dict) else None
    return crew if isinstance(crew, dict) and crew.get("order") else None


def build_crew_intro(voice_dir: Path, airline: str, origin: str, aircraft: str, out: Path, state_key: str) -> bool:
    """Build the pre-safety briefing from pieces: intro parts, the number of flight attendants (the clock's hour clips), a base
    and the crew's names, in the order the pack set. The aircraft type gives the number; the base is the departure airport if
    one was recorded, else a random one; each flight attendant gets a different random name. The picks are made once per
    flight. Returns False (the ordinary PreSafetyBriefing stays) when the pieces are not there."""
    crew = crew_settings(voice_dir)
    folder = voice_dir / "crew"
    if not crew or not folder.is_dir() or papro_audio is None or not papro_audio.have_backend():
        return False
    count = int((crew.get("fa") or {}).get(aircraft.upper(), 0) or 0)
    if not count and ("number" in crew["order"] or "names" in crew["order"]):
        return False   # no number for this aircraft type: say the pack's ordinary briefing
    state = load_state()
    pick_all = state.setdefault("crew_pick", {})
    pick = pick_all.get(state_key) or {}
    bases = {f.stem[5:].upper(): f for f in folder.glob("Base_*.ogg")}
    names = {f.stem[5:]: f for f in folder.glob("Name_*.ogg")}
    if not pick or pick.get("n") != count:
        base = origin.upper() if origin.upper() in bases else (random.choice(sorted(bases)) if bases else "")
        chosen = random.sample(sorted(names), min(count, len(names))) if names else []
        pick = {"n": count, "base": base, "names": chosen}
        pick_all.clear()
        pick_all[state_key] = pick
        save_state(state)
        log.info("Crew introduction: %d crew, base %s, names %s", count, pick["base"] or "-", ", ".join(pick["names"]) or "-")
    pieces: list[Path] = []
    for item in crew["order"]:   # every piece of the introduction must be there; if not, the ordinary briefing is used
        if item.startswith("intro:"):
            f = folder / f"Intro_{item[6:]}.ogg"
            if not f.is_file():
                return False
            pieces.append(f)
        elif item == "number":
            f = part_file(voice_dir, f"Hour_{count:02d}")
            if not f:
                return False
            pieces.append(f)
        elif item == "base":
            if pick.get("base") not in bases:
                return False
            pieces.append(bases[pick["base"]])
        elif item == "names":
            most = max([int(v) for v in (crew.get("fa") or {}).values() if str(v).isdigit()] or [1])
            if len(names) < most:   # enough names for the biggest crew in the pack, so every aircraft type is covered
                return False
            files = [names[n] for n in pick.get("names", []) if n in names]
            if not files:
                return False
            joined = out.with_name("crew_names.ogg")
            papro_audio.join(files, joined, 220, False)
            pieces.append(joined)
    if not pieces:
        return False
    papro_audio.join(pieces, out, 60, True)
    return True


def welcome_pieces(lib_dir: Path, clip: Path, tag: str, tail: list[Path] | None = None) -> list[Path] | None:
    """Good morning/afternoon/evening + destination + rest of the welcome aboard (+ fuel tag-on, + a bolt-on), or None
    if a piece is missing."""
    name = f"Welcome_{tag.capitalize()}"
    if not part_file(lib_dir, name) and tag == "night":
        name = "Welcome_Evening"
    base_file = part_file(lib_dir, name)
    greeting = take_choice(f"{lib_dir.parent.name}/{lib_dir.name}/{name}", base_file,
                           [lib_dir / PARTS_FOLDER, lib_dir / LEGACY_PARTS], name) if base_file else None
    pieces = [greeting, clip, modular_piece(lib_dir, "Welcome")]
    return [*pieces, *(tail or [])] if all(pieces) else None


def round_clock(arrival: str) -> str:
    """The PAs only have minutes in steps of five (Min_00, Min_05 ... Min_55): 10:01-02 -> 10:00, 10:03-04 -> 10:05,
    10:06-07 -> 10:05, 10:08-09 -> 10:10, and 10:58 -> 11:00 (the hour moves on, wrapping at midnight)."""
    try:
        hour, minute = (int(x) for x in arrival.split(":"))
    except ValueError:
        return arrival
    minute = int(round(minute / 5.0 + 1e-9)) * 5   # 2 -> 0, 3 -> 5 (the half-way point goes up)
    if minute >= 60:
        minute, hour = 0, (hour + 1) % 24
    return f"{hour:02d}:{minute:02d}"


def landing_pieces(lib_dir: Path, clip: Path, arrival: str | None, want_time: bool = True,
                   lead: Path | None = None) -> tuple[list[Path] | None, str]:
    """[Welcome to] + <destination> + "local time is HH:MM" + rest of the landing PA. `lead` is the "welcome to" clip when
    `clip` is only the airport's name (the old dest clips already include it).
    Returns (pieces or None, a note about the time that could not be added)."""
    rest = modular_piece(lib_dir, "Landing")
    if not rest:
        return None, ""
    pieces, note = ([lead, clip] if lead else [clip]), ""
    if not want_time:
        pass  # switched off in Settings
    elif arrival:
        arrival = round_clock(arrival)   # the spoken time is to the nearest five minutes
        hour, minute = arrival.split(":")
        twelve = int(hour) % 12 or 12  # PAs use the 12-hour clock: 14:35 -> "two thirty-five"
        needed = [part_file(lib_dir, f"Hour_{twelve:02d}") or part_file(lib_dir, f"Hour_{hour}")]
        if minute != "00" or part_file(lib_dir, "Min_00"):
            needed.append(part_file(lib_dir, f"Min_{minute}"))
        if all(needed):
            meridiem = part_file(lib_dir, "PM" if int(hour) >= 12 else "AM")  # optional "AM" / "PM" clip after the minutes
            pieces += [p for p in (part_file(lib_dir, "LocalTime"), *needed, meridiem) if p]
        else:
            note = f"local time {arrival} skipped (a Hour_{twelve:02d} / Min_{minute} piece is missing)"
    else:
        note = "local time skipped (SimBrief gave no arrival time)"
    return pieces + [rest], note


LANGUAGES_FILE = "languages.json"   # Airlines\\<ICAO>\\languages.json: {"enabled": true, "order": ["de", "en"], "per_pa": {"SafetyBriefing": ["en", "de"]}}


def load_languages(lib_dir: Path) -> dict:
    """The airline's language settings (see LANGUAGES_FILE), cleaned: {"enabled", "order", "per_pa"}."""
    out = {"enabled": False, "order": [], "per_pa": {}}
    try:
        data = json.loads((lib_dir / LANGUAGES_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    known = papro_studio.LANGUAGES
    out["enabled"] = bool(data.get("enabled"))
    out["order"] = [c for c in dict.fromkeys(str(x).lower() for x in data.get("order") or []) if c in known]
    per = data.get("per_pa") if isinstance(data.get("per_pa"), dict) else {}
    for base, order in per.items():
        if isinstance(order, list) and re.fullmatch(r"[A-Za-z_]{2,40}", str(base)):
            clean = [c for c in dict.fromkeys(str(x).lower() for x in order) if c in known]
            if clean:
                out["per_pa"][str(base)] = clean
    return out


def save_languages(lib_dir: Path, enabled: bool, order: list, per_pa: dict) -> dict:
    lib_dir.mkdir(parents=True, exist_ok=True)
    data = {"enabled": bool(enabled), "order": order, "per_pa": per_pa}
    (lib_dir / LANGUAGES_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return load_languages(lib_dir)


def language_voices(lib_dir: Path) -> dict[str, list[str]]:
    """{language code: [voice keys]} for the voices of an airline that say what language they are in."""
    found: dict[str, list[str]] = {}
    for key, path in list_voices(lib_dir).items():
        lang = voice_info(path, key)["language"]
        if lang:
            found.setdefault(lang, []).append(key)
    return found


def language_plan(lib_dir: Path, voice: str | None) -> list[tuple[str, str]] | None:
    """[(language, voice key), ...] in announcement order when the airline speaks several languages, else None. A language
    with several voices uses the chosen voice if it speaks that language, otherwise the first."""
    cfg_l = load_languages(lib_dir)
    if not cfg_l["enabled"]:
        return None
    avail = language_voices(lib_dir)
    plan = []
    for code in cfg_l["order"]:
        voices = avail.get(code)
        if voices:
            plan.append((code, voice if voice in voices else voices[0]))
    return plan if len(plan) >= 2 else None


_MULTI_ACTIVE = threading.local()


def apply_destination(cfg: dict, airline: str, dest_icao: str, choose=None, variant: str | None = None,
                      arrival: str | None = None, only: set[str] | None = None, voice: str | None = None,
                      target_dir: Path | None = None) -> str:
    """Apply a destination. An airline with several languages is built once per language and the languages are
    played one after the other (in the order set in Pack Studio); everything else is the plain single-voice build."""
    if not getattr(_MULTI_ACTIVE, "on", False):
        try:
            source = check_airline_code(load_aliases(cfg).get(check_airline_code(airline), airline))
            plan = language_plan(airline_folder(cfg, source), voice)
        except AppError:
            plan = None
        if plan:
            return _apply_multilingual(cfg, airline, dest_icao, plan, choose, variant, arrival, only, voice, target_dir)
    return _apply_single(cfg, airline, dest_icao, choose, variant, arrival, only, voice, target_dir)


def _apply_multilingual(cfg, airline, dest_icao, plan, choose, variant, arrival, only, voice, target_dir) -> str:
    """Build every language into its own scratch folder with the normal pipeline, then join the languages one after the
    other, PA by PA, into the Fenix folder."""
    airline = check_airline_code(airline)
    source = check_airline_code(load_aliases(cfg).get(airline, airline))
    lib_dir = airline_folder(cfg, source)
    langs = load_languages(lib_dir)
    boost = boost_percent(cfg)
    fenix_dir = target_dir or airline_packs_dir(cfg) / airline
    generic_source(cfg, airline, fenix_dir)   # saves the original announcements before the first change, as a single-language build does
    flat = {**cfg, "pa_boost": 0}   # each language is built untouched; the volume boost is applied once, to the joined result
    fenix_dir.mkdir(parents=True, exist_ok=True)
    messages, built_dirs = [], {}
    _MULTI_ACTIVE.on = True
    try:
        with tempfile.TemporaryDirectory(dir=DATA_DIR) as work:
            chosen = variant
            for code, vkey in plan:
                sub = Path(work) / code
                messages.append(_apply_single(flat, airline, dest_icao, choose, chosen, arrival, only, vkey, sub))
                built_dirs[code] = sub
                if chosen is None:   # a terminal / variant picked for the first language applies to the others too
                    chosen = load_state().get("variants", {}).get(f"{airline}_{dest_icao}")
            state = load_state()
            state.setdefault("voice_used", {})[f"{airline}_{dest_icao}"] = voice or plan[0][1]
            save_state(state)
            only_bases = {m["base"] for m in cfg["mappings"] if only and m["folder"] in only}
            names = sorted({p.name for d in built_dirs.values() for p in d.iterdir()
                            if p.is_file() and p.suffix.lower() == ".ogg"})
            pause = int(cfg.get("language_pause_ms", 2000))
            joined = 0
            for name in names:
                base = re.split(r"[\[~]", Path(name).stem, maxsplit=1)[0]
                if only and base not in only_bases:
                    continue
                order = langs["per_pa"].get(base) or langs["order"]
                files = [built_dirs[c] / name for c in order if c in built_dirs and (built_dirs[c] / name).is_file()]
                if name.startswith("BoardingMusic"):
                    files = files[:1]
                unique = []
                for f in files:   # a PA both languages left as the original generic file is said once
                    if not any(papro_audio.same_audio(f, u) for u in unique):
                        unique.append(f)
                if not unique:
                    continue
                if len(unique) == 1:
                    install_file(unique[0], fenix_dir / name, boost)
                    continue
                if papro_audio is None or not papro_audio.have_backend():
                    raise AppError("Joining languages needs the numpy and soundfile libraries (use the latest papro.exe).")
                out = Path(work) / ("joined_" + name)
                try:
                    papro_audio.join(unique, out, pause, False)
                except (papro_audio.AudioError, OSError) as exc:
                    raise AppError(f"Could not join the languages for {name}: {exc}") from exc
                install_file(out, fenix_dir / name, boost)
                joined += 1
    finally:
        _MULTI_ACTIVE.on = False
    names_txt = " then ".join(papro_studio.LANGUAGES.get(c, c) for c, _ in plan)
    first = messages[0]
    return f"{first.split(' - ')[0]} - languages: {names_txt}" + (APPLIED_MARK if APPLIED_MARK in " ".join(messages) else "")


def _apply_single(cfg: dict, airline: str, dest_icao: str, choose=None, variant: str | None = None,
                  arrival: str | None = None, only: set[str] | None = None, voice: str | None = None,
                  target_dir: Path | None = None) -> str:
    """Apply a destination. If several recordings exist for it (terminals etc.) `choose` is asked which to use
    (`variant` skips the question)."""
    airline = check_airline_code(airline)  # the airline in the Fenix livery, e.g. SHT
    aliases = load_aliases(cfg)
    source = check_airline_code(aliases.get(airline, airline))  # whose recordings to use, e.g. BAW
    lib_dir, created = ensure_airline_folders(cfg, source)
    voice_dir, voice_name, voice_count = choose_voice(source, lib_dir, voice)  # clips and parts come from the voice
    log.info("Applying %s to %s: recordings %s, voice %s, planned landing time %s%s", airline, dest_icao, source,
             voice_name or "-", arrival or "none", f", only {sorted(only)}" if only else "")
    boost = boost_percent(cfg)
    fenix_dir = target_dir or airline_packs_dir(cfg) / airline   # target_dir: a scratch folder (the Test tab)
    if source == airline or not has_ogg(lib_dir / MAIN_FOLDER):
        pristine = generic_source(cfg, airline, fenix_dir)
    else:
        pristine = lib_dir / MAIN_FOLDER  # SHT's pack is built from BAW's general PAs
    if not pristine and target_dir and has_ogg(voice_dir / MAIN_FOLDER):
        pristine = voice_dir / MAIN_FOLDER   # Test tab: a voice with its own full set of PAs needs no installed Fenix pack
    if not pristine:
        # Never create an empty/partial pack: Fenix would pick it over its default pack and lose announcements.
        extra = f" Created {lib_dir} for your recordings." if created else ""
        raise AppError(
            f"No Fenix pack for {airline} found in {fenix_dir.parent} - install one first "
            f"(or put its general PA .ogg files in {lib_dir / MAIN_FOLDER}).{extra}"
        )
    if not fenix_dir.is_dir():  # e.g. the folder was deleted: put the complete generic pack back first
        fenix_dir.mkdir(parents=True)
        for path in pristine.iterdir():
            if path.is_file() and path.suffix.lower() == ".ogg" and not is_modular(path.name) and not is_variant(path.name) and not typed_info(path.name):
                install_file(path, fenix_dir / path.name, boost)

    # Your general PAs in Airlines\<ICAO>\main are the source of truth for everything PAPro doesn't swap per
    # destination (SafetyBriefing, ArmDoors...): copy any that are new or changed into the Fenix pack.
    synced = 0
    voice_main = voice_dir / MAIN_FOLDER
    own_main = voice_main if voice_main != lib_dir / MAIN_FOLDER and has_ogg(voice_main) else None   # a voice with its own full set of PAs
    if not only and (pristine == lib_dir / MAIN_FOLDER or own_main):
        managed = {name for m in cfg["mappings"] for name in target_names(pristine, m, own_main)}
        sources: dict[str, Path] = {}
        named: dict[str, dict[str, Path]] = {}   # installed name -> {version name: path}
        typed_files: dict[str, dict[str, Path]] = {}   # installed name -> {file name: path}, for takes made for some aircraft only
        for folder in (lib_dir / MAIN_FOLDER if pristine == lib_dir / MAIN_FOLDER else None, own_main):   # the voice's files win
            if folder is None:
                continue
            for path in sorted(folder.iterdir()):
                if path.is_file() and path.suffix.lower() == ".ogg" and typed_info(path.name) and not is_modular(path.name):
                    typed_files.setdefault(typed_info(path.name)[0], {})[path.name] = path   # aircraft-specific takes
                elif path.is_file() and path.suffix.lower() == ".ogg" and papro_studio.NAMED_FILE.match(path.name):
                    m = papro_studio.NAMED_FILE.match(path.name)   # CabinService~Domestic.ogg: one of the named versions of a PA
                    named.setdefault(f"{m['base']}.ogg", {})[m["name"]] = path
                elif path.is_file() and path.suffix.lower() == ".ogg" and path.name not in managed and not is_modular(path.name) \
                        and not is_variant(path.name):
                    sources[path.name] = path
        # extra takes of a PA (AfterTakeoff~v2.ogg): one of them is picked at random for each flight and installed under
        # the PA's normal name. A voice with its own takes of a PA uses only those.
        takes: dict[str, dict[str, Path]] = {}
        for folder in (lib_dir / MAIN_FOLDER if pristine == lib_dir / MAIN_FOLDER else None, own_main):
            if folder is None:
                continue
            mine = {}
            for path in sorted(folder.iterdir()):
                match = VARIANT_RE.match(path.name) if path.is_file() and is_variant(path.name) else None
                if match and f"{match['base']}.ogg" not in managed:
                    mine.setdefault(f"{match['base']}.ogg", {})[path.name] = path
            for base_name, found in mine.items():
                takes[base_name] = found if folder == own_main else {**takes.get(base_name, {}), **found}
        for base_name, found in takes.items():
            if base_name in sources or any(n.lower() == base_name.lower() for n in sources):
                sources[base_name] = choose_variant(f"{airline}/{voice_dir.name}/{base_name}", [sources[base_name], *found.values()])
        for name, found in typed_files.items():   # a take for this flight's aircraft type replaces the ordinary PA
            if name in managed:
                continue
            chosen_take = typed_choice(f"{airline}/{voice_dir.name}/{name}", [sources[name]] if name in sources else [], list(found.values()))
            if chosen_take:
                sources[name] = chosen_take
        music = sorted(n for n in sources if n.startswith("BoardingMusic"))
        if music:   # all the boarding tracks become ONE BoardingMusic.ogg (it plays on repeat until boarding is complete)
            tracks = [sources.pop(n) for n in music]
            merged = merged_music(tracks)
            if merged:
                sources["BoardingMusic.ogg"] = merged
            else:
                sources.update({t.name: t for t in tracks})
        for stale in fenix_dir.glob("BoardingMusic~*.ogg") if "BoardingMusic.ogg" in sources else ():
            try:
                stale.unlink()   # older versions of PAPro installed each track separately
            except OSError:
                pass
        for base_name, versions in named.items():   # e.g. Domestic / International cabin service: the user picks one for this flight
            options = dict(versions)
            if base_name in sources and "Standard" not in options:
                options = {"Standard": sources[base_name], **options}
            if len(options) > 1:
                picked = pick_named(airline, base_name, list(options), choose or CHOOSER or choose_default)
                sources[base_name] = options[picked]
            elif options:
                sources[base_name] = next(iter(options.values()))
        for name, path in sources.items():
            dst = fenix_dir / name
            if sync_needed(path, dst, boost):
                install_file(path, dst, boost)
                synced += 1

    if not only and crew_settings(voice_dir):   # the pre-safety briefing is built for this flight's aircraft (crew number, base, names)
        flight_now = load_state().get("flight") or {}
        with tempfile.TemporaryDirectory(dir=DATA_DIR) as crew_work:
            crew_out = Path(crew_work) / "PreSafetyBriefing.ogg"
            try:
                if build_crew_intro(voice_dir, airline, str(flight_now.get("origin", "")), current_aircraft_type(), crew_out,
                                    f"{airline}/{voice_dir.name}/{flight_now.get('number', '')}/{flight_now.get('origin', '')}/{dest_icao}"):
                    install_file(crew_out, fenix_dir / "PreSafetyBriefing.ogg", boost)
            except (papro_audio.AudioError, OSError) as exc:
                log.warning("Could not build the crew introduction: %s", exc)

    all_clips = {m["folder"]: find_clips(voice_dir, m["folder"], source, dest_icao) for m in cfg["mappings"]}
    all_clips[AIRPORTS_FOLDER] = find_clips(voice_dir, AIRPORTS_FOLDER, source, dest_icao)  # just the airport's name
    options = variant_options(all_clips)
    chosen = ""
    if variant is not None:
        chosen = variant.upper().strip()
        if chosen == STANDARD.upper():
            chosen = ""
    elif len(options) == 1:
        chosen = options[0]
    elif len(options) > 1:
        chosen = pick_variant(airline, dest_icao, options, choose or CHOOSER or choose_default)

    state = load_state()
    state.setdefault("voice_used", {})[f"{airline}_{dest_icao}"] = voice_name
    save_state(state)
    if len(options) > 1 and variant is None:  # remember which one, so a later rebuild (landing) needn't ask
        state = load_state()
        state.setdefault("variants", {})[f"{airline}_{dest_icao}"] = chosen
        save_state(state)

    if not only and voice_setting(voice_dir, "descent_city"):   # "We've begun our initial descent into <airport>. Please ensure..."
        part1 = _find_piece(voice_dir, ["Modular_DescentStart"], (MAIN_FOLDER,))
        part2 = modular_piece(voice_dir, "DescentRest")
        names_clips = all_clips.get(AIRPORTS_FOLDER) or {}
        city = names_clips.get("") or names_clips.get(chosen) or next(iter(names_clips.values()), None)
        if part1 and part2 and city and papro_audio is not None and papro_audio.have_backend():
            with tempfile.TemporaryDirectory(dir=DATA_DIR) as descent_work:
                descent_out = Path(descent_work) / "DescentStarted.ogg"
                try:
                    papro_audio.join([part1, city, part2], descent_out, 60, True)
                    install_file(descent_out, fenix_dir / "DescentStarted.ogg", boost)
                    log.info("Descent announcement built with %s", dest_icao)
                except (papro_audio.AudioError, OSError) as exc:
                    log.warning("Could not build the descent announcement: %s", exc)

    summary, custom, notes = [], 0, []
    pause = int(cfg.get("segment_pause_ms", 60))
    flow = cfg.get("join_style", "flow") != "pauses"
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as work:
        for mapping in cfg["mappings"]:
            if only and mapping["folder"] not in only:
                continue
            folder = mapping["folder"]
            welcome_mode = bool(modular_piece(voice_dir, "Welcome"))
            # Modular welcome: the dep clip is only the destination name, and the same clip (+ the fuel tag-on) builds
            # the refuelling variant. Modular landing: the dest clip is "welcome to <destination>".
            names_only = all_clips[AIRPORTS_FOLDER] or all_clips["dep"]   # the airport's name: airports folder, else old dep clips
            lead = part_file(voice_dir, "WelcomeTo")                       # "Ladies and gentlemen, welcome to"
            landing_from_name = folder == "dest" and bool(all_clips[AIRPORTS_FOLDER]) and bool(lead)
            if welcome_mode and folder in ("dep", "fuel"):
                clips = names_only
            elif landing_from_name:
                clips = all_clips[AIRPORTS_FOLDER]
            else:
                clips = all_clips[folder]
            clip = clips.get(chosen) or clips.get("")  # the chosen variant, else the plain file, else generic
            joined = (folder in ("dep", "fuel") and welcome_mode) or (folder == "dest" and modular_piece(voice_dir, "Landing"))
            extra = pick_extra(cfg, voice_dir, f"{airline}_{dest_icao}") if joined and folder != "dest" else None
            if extra:
                log.info("Bolt-on for this flight: %s", extra.stem)
            built = 0
            for name in target_names(pristine, mapping, own_main):
                dst = fenix_dir / name
                generic = own_main / name if own_main and (own_main / name).is_file() else (pristine / name if pristine else None)
                if generic and generic.is_file():   # extra takes of the generic PA (BoardingWelcome[Morning]~v2.ogg)
                    generic = take_choice(f"{airline}/{voice_dir.name}/{name}", generic, [own_main, pristine], Path(name).stem) or generic
                pieces = None
                if joined and clip:
                    if folder == "dest":
                        pieces, note = landing_pieces(voice_dir, clip, arrival, local_time_on(cfg, source),
                                                      lead if landing_from_name else None)
                        if note and note not in notes:
                            notes.append(note)
                    else:
                        tags = pack_tags(name) & set(TIME_OF_DAY)
                        tag = next(iter(tags)) if tags else (clock_tag() if folder == "fuel" else None)
                        tail = ([fuel_tag(voice_dir)] if folder == "fuel" else []) + [extra]
                        pieces = welcome_pieces(voice_dir, clip, tag, [t for t in tail if t]) if tag else None
                if pieces:
                    out = Path(work) / name
                    if papro_audio is None or not papro_audio.have_backend():
                        raise AppError("Joining PA pieces needs the numpy and soundfile libraries "
                                       "(run Setup PAPro.bat again, or use the latest papro.exe).")
                    try:
                        papro_audio.join(pieces, out, pause, flow)
                    except (papro_audio.AudioError, OSError) as exc:
                        raise AppError(f"Could not join the pieces for {name}: {exc}") from exc
                    install_file(out, dst, boost)
                    log.info("Built %s = %s", name, " + ".join(p.stem for p in pieces))
                    built += 1
                elif clip and not joined:
                    install_file(clip, dst, boost)
                    built += 1
                elif generic and generic.is_file():
                    install_file(generic, dst, boost)  # no recording (or no pieces) for this airport: back to the generic file
                else:
                    dst.unlink(missing_ok=True)
            custom += bool(built)
            summary.append(f"{mapping['folder']}: {'custom' if built else 'generic'}")

    shown = f"{dest_icao} {variant_name(chosen)}" if len(options) > 1 or chosen else dest_icao
    log.info("Folders: %s", ", ".join(summary))
    message = f"{airline} to {shown}" + (f" using {source} recordings" if source != airline else "") + (APPLIED_MARK if custom else "")
    if voice_count > 1:
        message += f" - voice: {voice_info(voice_dir, voice_name)['name']}"
    if notes:
        message += " - " + "; ".join(notes)
    if synced:
        message += f" - updated {synced} general PA file(s) from main"
    if created:
        message += f" - created {lib_dir}, add your recordings there"
    elif not custom:
        message += f" - no recordings for {source}_{dest_icao} yet"
    return message


PACK_SETTINGS = "settings.json"   # Airlines\\<ICAO>\\settings.json: this pack's own settings, e.g. {"local_time": true}


def pack_settings(cfg: dict, code: str) -> dict:
    """The settings kept with an airline pack (after any alias), {} if it has none."""
    try:
        source = load_aliases(cfg).get(code, code)
        data = json.loads((airline_folder(cfg, source) / PACK_SETTINGS).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (AppError, OSError, ValueError):
        return {}


def set_pack_setting(cfg: dict, code: str, key: str, value) -> None:
    folder = airline_folder(cfg, code)
    data = pack_settings(cfg, code) if code not in load_aliases(cfg) else {}
    data[key] = value
    folder.mkdir(parents=True, exist_ok=True)
    (folder / PACK_SETTINGS).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def local_time_on(cfg: dict, code: str) -> bool:
    """Does this pack say the local time in the landing PA? The pack's own setting; packs without one use the old global
    setting (config.json), which defaults to yes."""
    value = pack_settings(cfg, code).get("local_time")
    return bool(value) if isinstance(value, bool) else bool(cfg.get("announce_local_time", True))


def callsign_airline(cfg: dict, code: str, callsign: str) -> str | None:
    """The airline in the callsign (SHT2A -> SHT) when it differs from the SimBrief airline (BAW) and has a pack of its
    own or an alias - the Fenix livery reads the folder of the callsign's airline, so that folder needs filling too."""
    m = re.match(r"([A-Z]{3})\d", (callsign or "").strip().upper())
    if not m or m.group(1) == code:
        return None
    other = m.group(1)
    try:
        if not airline_has_pack(cfg, other):
            return None
        aliased = load_aliases(cfg).get(other)
        if aliased != code and not (airline_packs_dir(cfg) / other).is_dir():
            return None
    except Exception:
        return None
    return other


_COORDS = None


def airport_coords() -> dict:
    global _COORDS
    if _COORDS is None:
        _COORDS = {}
        for base in (getattr(sys, "_MEIPASS", None), Path(__file__).parent):
            f = Path(base) / "airport_coords.json" if base else None
            if f and f.is_file():
                try:
                    _COORDS = json.loads(f.read_text(encoding="utf-8"))
                    break
                except (OSError, ValueError):
                    pass
    return _COORDS


NEAREST_MAX_KM = 30.0   # touched down further than this from every known airport: keep the planned one


def nearest_airport(lat: float, lon: float, max_km: float = NEAREST_MAX_KM) -> str | None:
    """ICAO of the closest airport in the built-in list (airport_coords.json), or None if none is within max_km."""
    best, best_d = None, max_km
    cos = math.cos(math.radians(lat))
    for icao, (a, o) in airport_coords().items():
        if abs(a - lat) > 0.6:
            continue
        d = 111.2 * math.hypot(a - lat, (o - lon) * cos)
        if d < best_d:
            best, best_d = icao, d
    return best


def rebuild_landing(cfg: dict, arrival: str | None, position: tuple[float, float] | None = None) -> str:
    """You have landed and slowed down: rebuild the landing PA with the local time the sim reported (HH:MM)."""
    state = load_state()
    flight = state.get("flight") or {}
    airline, dest = flight.get("airline"), flight.get("dest")
    if not airline or not dest:
        raise AppError("No flight is set up, so there is no landing PA to build.")
    planned = dest
    near = nearest_airport(*position) if position else None
    diverted = bool(near and near != dest)
    if diverted:
        log.info("Touched down at %s, not the planned %s - building the landing PA for %s", near, dest, near)
        dest = near
    if not local_time_on(cfg, airline):
        return f"Landed at {dest}"
    if not arrival:
        return f"Landed at {dest} - no local time from the sim, kept the PA as it was"
    if not airline_has_pack(cfg, airline):
        return f"Landed at {dest} - no {airline} airline pack installed"
    stored = state.get("variants", {}).get(f"{airline}_{dest}")
    if diverted:
        stored = None   # the choice made for the planned airport means nothing here
    _DIVERSION.on = diverted
    try:
        message = apply_destination(cfg, airline, dest, choose=CHOOSER or choose_default, variant=stored, arrival=arrival, only={"dest"},
                                    voice=state.get("voice_used", {}).get(f"{airline}_{planned}") or None,
                                    target_dir=playback_target())
    finally:
        _DIVERSION.on = False
    stored = load_state().get("variants", {}).get(f"{airline}_{dest}")
    other = None if _PLAYBACK["on"] else callsign_airline(cfg, airline, flight.get("callsign", ""))
    if other:
        try:
            apply_destination(cfg, other, dest, choose=choose_default, variant=stored, arrival=arrival, only={"dest"},
                              voice=state.get("voice_used", {}).get(f"{other}_{dest}") or state.get("voice_used", {}).get(f"{airline}_{dest}") or None)
        except AppError:
            log.exception("Could not rebuild the %s landing PA", other)
    state = load_state()
    state["live"] = f"{airline}_{planned}"  # the next SimBrief check must not undo this
    save_state(state)
    return f"Landed - {message}"


def restore_generic(cfg: dict, icao: str) -> str:
    """Put an airline's original generic announcements back in the Fenix folder."""
    icao = check_airline_code(icao)
    fenix_dir = airline_packs_dir(cfg) / icao
    source = generic_source(cfg, icao, fenix_dir)
    if not source:
        raise AppError(f"No saved original {icao} announcements yet (PAPro saves them the first time it changes a pack).")
    fenix_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.iterdir()):
        if path.is_file() and path.suffix.lower() == ".ogg" and not is_modular(path.name) and not is_variant(path.name) and not typed_info(path.name):
            install_file(path, fenix_dir / path.name)
    state = load_state()
    state.pop("last", None)
    save_state(state)
    return f"Restored the original {icao} announcements"


APPLIED_MARK = " - your announcements are in place"   # the dashboard turns this into a green flight card


def airline_has_pack(cfg: dict, code: str) -> bool:
    """Is there anything to play for this airline (its recordings, after any alias)? If not PAPro does nothing: it creates
    no folders and sends nothing to the Fenix folder."""
    try:
        folder = airline_folder(cfg, load_aliases(cfg).get(code, code))
        return folder.is_dir() and (bool(list_voices(folder)) or has_ogg(folder / MAIN_FOLDER))
    except (AppError, OSError):
        return False


def update(cfg: dict, force: bool = False, airline: str | None = None, dest: str | None = None,
           variant: str | None = None) -> tuple[bool, str]:
    """Look up the flight (or use the given one) and apply it. Returns (changed, message)."""
    flight = {"icao": dest.upper(), "airline": "", "arrival": None, "number": "", "callsign": "", "aircraft": ""} if dest \
        else fetch_flight(cfg)
    code = (airline or cfg.get("airline_override") or flight["airline"] or "").strip().upper()
    if not code:
        raise AppError("The SimBrief plan has no airline ICAO - set airline_override in config.json.")
    code = check_airline_code(code)
    if not re.fullmatch(r"[A-Z0-9]{3,4}", flight["icao"]):
        raise AppError(f"'{flight['icao']}' is not a valid airport code.")
    key = f"{code}_{flight['icao']}_{flight.get('arrival') or ''}_{flight.get('aircraft') or ''}{'_pb' if _PLAYBACK['on'] else ''}"  # a new landing time or aircraft rebuilds the PAs
    state = load_state()
    shown = {"airline": code, "dest": flight["icao"], "origin": flight.get("origin", ""), "name": flight.get("name", ""),
             "number": flight.get("number", ""), "callsign": flight.get("callsign", ""), "aircraft": flight.get("aircraft", ""),
             "dest_lat": flight.get("dest_lat"), "dest_lon": flight.get("dest_lon"), "cruise_ft": flight.get("cruise_ft")}
    if state.get("flight") != shown:
        state["flight"] = shown
        save_state(state)
    if not force and state.get("live") == f"{code}_{flight['icao']}":
        return False, f"{code} to {flight['icao']}: landing PA built from the sim's local time"
    if not force and state.get("last") == key:
        return False, f"Unchanged: {code} to {flight['icao']}"
    if not airline_has_pack(cfg, code):   # no pack (no recordings) for this airline: leave everything alone
        state = load_state()
        state["applied"] = False
        state.pop("last", None)
        save_state(state)
        clear_playback()   # PAs left from an earlier flight (another airline's pack) must not play for this one
        return False, f"{code} to {flight['icao']}: no {code} airline pack installed"
    message = apply_destination(cfg, code, flight["icao"], variant=variant, arrival=flight.get("arrival"),
                                target_dir=playback_target())
    other = None if _PLAYBACK["on"] else callsign_airline(cfg, code, flight.get("callsign", ""))
    if other:
        try:
            message += f"; also {apply_destination(cfg, other, flight['icao'], variant=variant, arrival=flight.get('arrival'))}"
        except AppError as exc:
            log.warning("Could not fill the %s pack: %s", other, exc)
    state = load_state()
    state["last"] = key
    state["loaded"] = [c for c in (code, other) if c]   # the airline codes whose PAs were just filled (for the tray hover text)
    state["applied"] = APPLIED_MARK in message
    state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return True, message


# --------------------------------------------------------------------------- tray app
def _bold_sans(text: str) -> str:
    """Windows tray menus can't bold a normal item, so use the mathematical bold sans-serif letters."""
    return "".join(chr(0x1D5D4 + ord(c) - 65) if "A" <= c <= "Z" else chr(0x1D5EE + ord(c) - 97) if "a" <= c <= "z" else c for c in text)


ACTIVE_BOLD = _bold_sans("Active")


def make_icon_image():
    from papro_icon import make_icon

    return make_icon(64)


def open_path(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        log.exception("Could not open %s", path)


# --------------------------------------------------------------------------- airline logos
LOGO_DIR = DATA_DIR / "logos"
_logo_cache: dict = {}
_PALETTES = [((107, 47, 214), (206, 58, 128)), ((206, 58, 128), (255, 146, 24)), ((60, 90, 220), (107, 47, 214)),
             ((20, 150, 160), (60, 90, 220)), ((255, 146, 24), (214, 60, 60)), ((120, 60, 200), (40, 130, 210))]


def _png_bytes(img) -> bytes:
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _font(size: int):
    import base64
    import io

    from PIL import ImageFont

    try:
        from papro_fonts import INTER

        return ImageFont.truetype(io.BytesIO(base64.b64decode(INTER["Bold"])), size)
    except Exception:
        pass
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def logo_tile(source) -> "Image.Image":
    """Any logo image -> a 256px rounded white tile with the logo centred (logos are drawn for light backgrounds)."""
    from PIL import Image, ImageDraw

    size = 256
    logo = source.convert("RGBA")
    logo.thumbnail((int(size * 0.74), int(size * 0.74)), Image.LANCZOS)
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.23), fill=255)
    tile.paste((255, 255, 255, 255), (0, 0), mask)
    tile.alpha_composite(logo, ((size - logo.width) // 2, (size - logo.height) // 2))
    return tile


def monogram(icao: str) -> bytes:
    """Stand-in 'logo' when no image was supplied: the ICAO code on a gradient tile (colour picked from the code)."""
    from PIL import Image, ImageChops, ImageDraw

    size = 256
    first, second = _PALETTES[sum(ord(c) * (i + 3) for i, c in enumerate(icao)) % len(_PALETTES)]
    vertical = Image.linear_gradient("L").resize((size, size))
    t = ImageChops.add(vertical, vertical.rotate(90), scale=2)
    tile = Image.composite(Image.new("RGB", (size, size), second), Image.new("RGB", (size, size), first), t).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.23), fill=255)
    tile.putalpha(mask)
    draw = ImageDraw.Draw(tile)
    font = _font(92 if len(icao) <= 3 else 72)
    box = draw.textbbox((0, 0), icao, font=font)
    draw.text(((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1] - 2), icao,
              font=font, fill=(255, 255, 255, 255))
    return _png_bytes(tile)


def cache_remote_logo(entry: dict) -> bool:
    """Download the catalog's logo image for a pack (once). Returns True if a new logo was stored."""
    from PIL import Image
    import io

    url, icao = entry.get("logo", ""), entry["icao"]
    target = LOGO_DIR / f"{icao}.png"
    if not url or target.exists():
        return False
    try:
        resp = requests.get(url, timeout=20, stream=True)
        resp.raise_for_status()
        data = resp.raw.read(3 * 1024 * 1024 + 1, decode_content=True)
        if len(data) > 3 * 1024 * 1024:
            return False
        image = Image.open(io.BytesIO(data))
        image.load()
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_png_bytes(logo_tile(image)))
        return True
    except Exception:
        log.warning("Could not fetch the logo for %s", icao)
        return False


def logo_png(cfg: dict, icao: str) -> bytes:
    """Logo for an airline: your own Airlines/<ICAO>/logo.png, else the pack's logo, else a monogram tile."""
    from PIL import Image

    try:
        icao = load_aliases(cfg).get(icao, icao)
    except AppError:
        pass
    try:
        folder = airline_folder(cfg, icao)
        for name in ("logo.png", "logo.jpg", "logo.jpeg", "logo.webp"):
            path = folder / name
            if path.is_file():
                key = (str(path), path.stat().st_mtime_ns)
                if key not in _logo_cache:
                    _logo_cache[key] = _png_bytes(logo_tile(Image.open(path)))
                return _logo_cache[key]
    except Exception:
        log.warning("Could not read your logo for %s", icao)
    cached = LOGO_DIR / f"{icao}.png"
    if cached.is_file():
        return cached.read_bytes()
    if ("mono", icao) not in _logo_cache:
        _logo_cache[("mono", icao)] = monogram(icao)
    return _logo_cache[("mono", icao)]


# --------------------------------------------------------------------------- the PAPro window (see papro_ui.py)
class UIMixin:
    """What the window shows and does. Needs: lock, catalog, catalog_error, status, auto, wake, set_status(),
    refresh_catalog(), refresh_menu(), poll_once(), run_task()."""

    def _ui_init(self) -> None:
        import papro_ui

        self.tasks: dict[str, dict] = {}
        self.prompt: dict | None = None
        self.logo_version = 0
        self.installs_done = 0
        self.updating = False
        self.ui = papro_ui.UIServer(self, DATA_DIR / "ui-profile")

    def lan_enabled(self) -> bool:
        return True   # always on: other devices on the private network / VPN (EFBx, an iPad) can open PAPro

    def open_window(self) -> None:
        self.ui.open_window()

    # data for the page
    def ui_snapshot(self) -> dict:
        try:
            cfg = load_config()
        except AppError:
            cfg = dict(DEFAULT_CONFIG)
        state = load_state()
        installed = state.get("installed", {})
        packs = []
        known_names = {e["icao"]: e["name"] for e in self.catalog}
        voices_cache: dict[str, set] = {}

        def have_entry(entry: dict) -> str | None:
            """The installed version of this catalog entry (None = not installed). A pack installed before voices had
            their own entries counts for the entry with no voice."""
            record = installed.get(entry["icao"])
            if entry.get("voice"):   # a voice that is already in your library (installed, imported or made in the Studio) is installed
                have_voices = voices_cache.setdefault(entry["icao"], {v.lower() for v in library_voices(cfg, entry["icao"])})
                if entry["voice"].lower() in have_voices:
                    return ((record or {}).get("catalog", {}).get(entry["id"])) or entry["version"]
            if not record:
                return None
            if "catalog" in record:
                return record["catalog"].get(entry["id"])
            return record.get("version", "") if not entry.get("voice") else None

        # airlines on this PC: installed from the catalog / imported, or just sitting in your library
        codes = set(installed)
        try:
            for folder in airlines_root(cfg).iterdir():
                if folder.is_dir() and re.fullmatch(r"[A-Za-z0-9]{3}", folder.name) and any(folder.rglob("*.ogg")):
                    codes.add(folder.name.upper())
        except OSError:
            pass
        for code in sorted(codes):
            have = installed.get(code)
            entries = [e for e in self.catalog if e["icao"] == code]
            update = any(have_entry(e) not in (None, e["version"]) for e in entries)
            packs.append({"icao": code, "key": code, "name": airline_display_name(cfg, code, have, known_names.get(code, "")),
                          "version": ((have or {}).get("version") if (have or {}).get("version") not in (None, "", "imported") else "") or library_version(cfg, code) or "Your recordings",
                          "voices": library_voices(cfg, code), "voice_items": library_voice_items(cfg, code), "local_time": local_time_on(cfg, code), "size_mb": 0,
                          "installed": True, "own": not have, "update": update, "task": self.tasks.get(code),
                          "update_id": next((e["id"] for e in entries if have_entry(e) not in (None, e["version"])), "")})
        shown = {p["icao"] for p in packs}
        for entry in self.catalog:   # everything that can be downloaded (what you already have is marked)
            have = have_entry(entry)
            packs.append({"icao": entry["icao"], "key": "dl-" + entry["id"], "id": entry["id"], "catalog": True, "name": entry["name"],
                          "version": entry["version"], "voices": [v["name"] for v in entry.get("voices", [])] or ([entry["voice"]] if entry.get("voice") else []),
                          "size_mb": round(entry["size"] / 1048576) if entry.get("size") else 0, "installed": have is not None,
                          "update": have not in (None, entry["version"]),
                          "task": self.tasks.get(entry["id"])})
        for key, task in list(self.tasks.items()):   # a pack being imported that is not in the catalog yet
            if key not in {p["key"] for p in packs} and key not in codes and "dl-" + key not in {p["key"] for p in packs}:
                packs.append({"icao": key, "key": key, "name": task.get("name") or key, "version": "", "size_mb": 0,
                              "installed": False, "update": False, "task": task})
        seen = {p["icao"] for p in packs}
        prompt = self.prompt
        try:
            aliases = load_aliases(cfg)
        except AppError:
            aliases = {}
        sources = {p["icao"] for p in packs}
        try:
            root = airlines_root(cfg)
            sources |= {p.name.upper() for p in root.iterdir() if p.is_dir() and re.fullmatch(r"[A-Za-z0-9]{3}", p.name)}
        except OSError:
            pass
        flight = state.get("flight") or None
        voices = None
        if flight and flight.get("airline"):
            source = aliases.get(flight["airline"], flight["airline"])
            try:
                names = list(list_voices(airline_folder(cfg, source)))
            except (OSError, AppError):
                names = []
            if names:
                found = list_voices(airline_folder(cfg, source))
                voices = {"source": source, "options": [voice_info(found[n], n) for n in names], "selected": state.get("voices", {}).get(source) or names[0],
                          "used": state.get("voice_used", {}).get(f"{flight['airline']}_{flight.get('dest', '')}", "")}
        company = ""
        if flight and flight.get("airline"):  # "British Airways" for BAW, and for SHT which uses the BAW pack
            names = {p["icao"]: p["name"] for p in packs}
            company = names.get(flight["airline"]) or names.get(aliases.get(flight["airline"], ""), "")
        return {
            "version": VERSION,
            "address": self.ui.url() if getattr(self.ui, "httpd", None) else "",
            "company": company,
            "voices": voices,
            "hide_reminder": bool(cfg.get("hide_reminder")),
            "welcome": self._welcome_info(cfg, state),
            "welcome_info": self._welcome_info(cfg, {}),
            "studio": self.studio_snapshot(cfg),
            "settings": {
                "close_to_tray": bool(cfg.get("close_to_tray", False)),
                "minimize_to_tray": bool(cfg.get("minimize_to_tray", False)),
                "extra_chance": int(cfg.get("extra_chance", 25)),
                "pa_boost": boost_percent(cfg),
                "lan": {"on": True, "addresses": [f"http://{ip}:{self.ui.httpd.server_address[1]}/" for ip in self.ui.private_ips()] if getattr(self.ui, "httpd", None) and self.ui.lan_on else []},
                "gsx": bool(cfg.get("gsx_integration", True)),
                "audio": {"device": str(cfg.get("pa_output_device") or ""), "playback": bool(cfg.get("pa_playback", True))},
                "pa_knob": {"on": bool(cfg.get("pa_knob_on", True)), "volume": max(0, min(100, int(cfg.get("pa_knob_volume", 70))))},
                "join_style": "pauses" if cfg.get("join_style") == "pauses" else "flow",
                "autostart": current_autostart(),
                "quit_with_msfs": bool(cfg.get("quit_with_msfs", True)),
                "windows": sys.platform.startswith("win"),
            },
            "aliases": aliases,
            "alias_sources": sorted(set(sources)),
            "library": str(airlines_root(cfg)),
            "setup_needed": not (str(cfg.get("simbrief_username", "")).strip() or str(cfg.get("simbrief_userid", "")).strip() or efbx_plan(cfg) is not None),
            "username": cfg.get("simbrief_username", ""),
            "flight": state.get("flight"),
            "message": "" if self._quiet_status(state) else self.status.replace(APPLIED_MARK, "") if APPLIED_MARK not in self.status else "",
            "applied": APPLIED_MARK in self.status or (self._quiet_status(state) and bool(state.get("applied"))),
            "updated": state.get("updated", ""),
            "auto": self.auto,
            "busy": self.lock.locked() or bool(self.tasks),
            "packs": packs,
            "catalog_error": self.catalog_error,
            "app_update": app_update(),
            "aircraft": getattr(self, "aircraft", None),
            "phase": self.phase_info(),
            "update_checked": int(getattr(self, "update_checked", 0) * 1000),
            "logo_v": self.logo_version,
            "installs_done": self.installs_done,
            "prompt": {k: prompt[k] for k in ("id", "title", "text", "options")} if prompt else None,
        }

    def _quiet_status(self, state: dict) -> bool:
        """Routine 'nothing to do' statuses are not worth showing: the card's colour already says it."""
        return self.status.startswith("Unchanged:") or "landing PA built from the sim" in self.status

    def _welcome_info(self, cfg: dict, state: dict) -> dict | None:
        """First run only: what PAPro is going to change, so the user isn't surprised."""
        if state.get("welcomed"):
            return None
        try:
            fenix = str(airline_packs_dir(cfg))
        except AppError:
            fenix = ""
        return {"fenix": fenix, "originals": str(GENERIC_DIR), "recordings": str(airlines_root(cfg))}

    def import_download(self, zip_url: str) -> None:
        """Download a pack from a link, then install it. A card called 'Importing a pack' shows the progress until we know the airline."""
        key = "IMPORT"
        if key in self.tasks:
            return
        task = {"kind": "install", "text": "Starting download", "frac": 0.0, "name": "Importing a pack"}
        self.tasks[key] = task
        path = self.import_stage_path()
        try:
            def progress(done: int, total: int) -> None:
                task["text"], task["frac"] = f"Downloading  {done / 1e6:.0f} of {total / 1e6:.0f} MB", 0.8 * done / total
            download_pack({"url": zip_url, "sha256": ""}, path, progress)
            icao, name = pack_identity(path, zip_url.split("?")[0].rsplit("/", 1)[-1])
        except AppError as exc:
            path.unlink(missing_ok=True)
            self.tasks.pop(key, None)
            self.set_status(str(exc), notify=True)
            return
        self.tasks.pop(key, None)
        self.start_pack_task("install", icao, {"icao": icao, "name": name or airline_display_name(load_config(), icao),
                                               "version": "imported", "url": str(path), "sha256": "", "size": 0, "cleanup": str(path)})

    def import_stage_path(self) -> Path:
        folder = DATA_DIR / "imports"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"import-{int(time.time() * 1000)}.zip"

    def import_local(self, path: Path, filename: str = "") -> dict:
        """A pack zip already on disk at `path`: read which airline it is for and install it like a catalog pack."""
        try:
            icao, name = pack_identity(path, filename)
        except AppError as exc:
            path.unlink(missing_ok=True)
            return {"error": str(exc)}
        self.start_pack_task("install", icao, {"icao": icao, "name": name or airline_display_name(load_config(), icao),
                                               "version": "imported", "url": str(path), "sha256": "", "size": 0, "cleanup": str(path)})
        return {"ok": True, "icao": icao}

    def library_logo_set(self, icao: str, body: bytes) -> dict:
        """The logo the user picked on the dashboard: kept as Airlines\\<ICAO>\\logo.png (it wins over any other logo)."""
        import io
        from PIL import Image
        try:
            folder = airline_folder(load_config(), check_airline_code(icao))
            if not folder.is_dir():
                return {"error": "Install or import this pack first."}
            if not body or len(body) > 5 * 1024 * 1024:
                return {"error": "That image is empty or too large (5 MB limit)."}
            try:
                image = Image.open(io.BytesIO(body))
                image.load()
            except Exception:
                return {"error": "That is not an image PAPro can read. Use a png, jpg or webp."}
            image = image.convert("RGBA")
            image.thumbnail((512, 512))
            for old in ("logo.jpg", "logo.jpeg", "logo.webp"):
                (folder / old).unlink(missing_ok=True)
            image.save(folder / "logo.png", "PNG")
        except (AppError, OSError) as exc:
            return {"error": str(exc)}
        self.logo_version += 1
        return {"ok": True}

    def ui_logo(self, icao: str) -> tuple[bytes, str]:
        try:
            return logo_png(load_config(), check_airline_code(icao)), "image/png"
        except (AppError, OSError):
            return monogram(icao[:4] or "?"), "image/png"

    def ui_icon(self) -> bytes:
        from papro_icon import make_icon

        return _png_bytes(make_icon(128))

    def window_minimized(self) -> None:
        """'Hide to the tray' on minimize: close the window (the app keeps running; the tray icon opens it again)."""
        try:
            if not load_config().get("minimize_to_tray"):
                return
        except AppError:
            return
        window = self.ui.window
        if window is not None and window.poll() is None:
            window.terminate()

    def window_closed(self) -> None:
        """The page says it is going away. Unless "keep running in the tray" is on in Settings, closing the window quits
        PAPro. A page reload also says this, so only quit if the window's browser process has really ended."""
        try:
            if load_config().get("close_to_tray"):
                return
        except AppError:
            return

        def check() -> None:
            window = self.ui.window
            if window is not None and window.poll() is not None:
                log.info("The window was closed: quitting (Settings: keep running in the tray is off)")
                self.quit()

        threading.Timer(1.5, check).start()

    # things the page can ask for
    # ------------------------------------------------------------------ Test tab
    TEST_LABELS = {stem: label for stem, label, _ in papro_studio.GENERAL}

    def test_options(self) -> dict:
        try:
            return self._test_options()
        except Exception as exc:
            log.exception("Test options failed")
            return {"error": f"Could not read your library: {type(exc).__name__}: {exc}", "airlines": []}

    def _test_options(self) -> dict:
        """Every airline in your library that has a voice: its voices and the airports recorded for them."""
        cfg = load_config()
        root = airlines_root(cfg)
        installed = load_state().get("installed", {})
        airlines = []
        for folder in sorted(root.iterdir()) if root.is_dir() else []:
            if not folder.is_dir():
                continue
            code = folder.name.upper()
            voices = list_voices(folder)
            if not voices or not re.fullmatch(r"[A-Z0-9]{2,4}", code):
                continue
            ports: dict[str, set[str]] = {}
            for key, vdir in voices.items():
                for f in list((vdir / AIRPORTS_FOLDER).glob("*.ogg")) if (vdir / AIRPORTS_FOLDER).is_dir() else []:
                    parts = f.stem.split("_")
                    if len(parts) >= 2 and parts[0].upper() == code:
                        ports.setdefault(key, set()).add(parts[1].upper())
            types: set[str] = set()   # aircraft types that have their own PAs in this airline's voices
            for vdir in voices.values():
                if (vdir / MAIN_FOLDER).is_dir():
                    for f in (vdir / MAIN_FOLDER).glob("*@*.ogg"):
                        info = typed_info(f.name)
                        if info:
                            types |= info[1] - {"ALL"}
            airlines.append({"icao": code, "name": airline_display_name(cfg, code, installed.get(code), ""), "aircraft_types": sorted(types),
                             "voices": [{"key": k, "name": voice_info(p, k)["name"], "airports": sorted(ports.get(k, []))}
                                        for k, p in voices.items()]})
        return {"airlines": airlines, "all_types": papro_studio.AIRCRAFT_TYPES}

    def test_build(self, data: dict) -> dict:
        """Build the whole set of PAs for one airline / voice / destination in a scratch folder, with the real pipeline."""
        try:
            icao = check_airline_code(str(data.get("icao", "")))
            dest = str(data.get("dest", "")).upper().strip()
            if not re.fullmatch(r"[A-Z0-9]{3,4}", dest):
                return {"error": "Choose a destination airport."}
            arrival = str(data.get("arrival", "")).strip() or None
            if arrival:   # 14:35, 14.35, 1435 or 935 (24-hour clock)
                m = re.fullmatch(r"(\d{1,2})[:.]?(\d{2})", arrival)
                if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                    return {"error": "Local time must be a 24-hour time like 1435 or 14:35."}
                arrival = f"{int(m.group(1))}:{m.group(2)}"
            cfg = load_config()
            voices = list_voices(airline_folder(cfg, icao))
            voice = str(data.get("voice", ""))
            if voice not in voices:
                return {"error": "That voice is not in your library."}
            aircraft = str(data.get("aircraft", "")).upper().strip()
            if aircraft and aircraft not in papro_studio.ICAO_TYPES:
                return {"error": f"'{aircraft}' is not an ICAO aircraft type (use codes like A320, B738 or B38M)."}
        except AppError as exc:
            return {"error": str(exc)}
        key = hashlib.sha1(f"{icao}|{voice}|{dest}|{arrival}|{aircraft}|{boost_percent(cfg)}|{cfg.get('join_style')}".encode()).hexdigest()[:12]
        sandbox = TEST_DIR / key
        pack = sandbox / "pack"
        message = ""
        if data.get("rebuild") or not pack.is_dir():
            shutil.rmtree(sandbox, ignore_errors=True)
            sandbox.mkdir(parents=True)
            _STATE_LOCAL.path = sandbox / "state.json"   # roll variations / bolt-ons here, not in the real state
            _AIRCRAFT_LOCAL.value = aircraft              # the aircraft type to build for ("" = no type: generic PAs)
            try:
                message = apply_destination(cfg, icao, dest, variant=STANDARD, arrival=arrival, voice=voice, target_dir=pack)
            except AppError as exc:
                shutil.rmtree(sandbox, ignore_errors=True)
                return {"error": str(exc)}
            except Exception as exc:   # show what went wrong in the Test tab instead of a bare "internal error"
                log.exception("Test build failed")
                shutil.rmtree(sandbox, ignore_errors=True)
                return {"error": f"Test build failed: {type(exc).__name__}: {exc}"}
            finally:
                _STATE_LOCAL.path = None
                _AIRCRAFT_LOCAL.value = None
            TEST_DIR.mkdir(parents=True, exist_ok=True)
            old = sorted((p for p in TEST_DIR.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)[:-10]
            for p in old:
                shutil.rmtree(p, ignore_errors=True)
        triggers = []
        for f in sorted(pack.glob("*.ogg"), key=lambda p: p.name.lower()):
            stem = f.stem
            base = stem.split("[")[0]
            tags = re.findall(r"\[(.*?)\]", stem)
            label = self.TEST_LABELS.get(stem) or self.TEST_LABELS.get(base) or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base)
            if stem not in self.TEST_LABELS and not tags and self.TEST_LABELS.get(base + "[PreFlight]"):
                label = self.TEST_LABELS[base + "[PreFlight]"]   # an untagged CabinDim / CabinSecured / CrewTakeSeats is the takeoff one
            elif stem not in self.TEST_LABELS and tags:
                label += " (" + ", ".join(tags) + ")"
            group, pos = papro_studio.phase_of({"group": "", "stem": stem, "folder": "main"})   # flight order, like Pack Studio
            if group == "":
                group = "Other"
            if base == "BoardingWelcome" and tags:
                label = "Welcome aboard - " + ", ".join(tags).lower()
            elif base == "BoardingWelcome":
                label = "Welcome aboard"
            elif base == "AfterLanding":
                label = "Landing"
            order = papro_studio.PHASE_NAMES.index(group) if group in papro_studio.PHASE_NAMES else 98
            tag_rank = next((i for i, w in enumerate(("morning", "afternoon", "evening", "night", "refueling")) if tags and tags[0].lower() == w), 9)
            triggers.append({"file": f.name, "label": label, "group": group, "_o": (order, pos, tag_rank, f.name.lower())})
        triggers.sort(key=lambda t: t.pop("_o"))
        return {"ok": True, "key": key, "message": message, "triggers": triggers}

    def test_audio(self, key: str, file: str) -> bytes | None:
        if not re.fullmatch(r"[0-9a-f]{12}", key) or file != Path(file).name or not file.lower().endswith(".ogg"):
            return None
        path = TEST_DIR / key / "pack" / file
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError:
            return None

    def ui_action(self, name: str, data: dict) -> dict:
        if name == "install":
            wanted = str(data.get("id") or data.get("icao", "")).strip().lower()
            entry = next((e for e in self.catalog if e["id"] == wanted), None) or \
                next((e for e in self.catalog if e["icao"].lower() == wanted), None)
            if not entry:
                return {"error": "That pack is not in the catalog - refresh the list."}
            self.start_pack_task("install", entry["icao"], entry)
        elif name == "import_url":
            try:
                zip_url = resolve_pack_url(str(data.get("url", "")))
            except AppError as exc:
                return {"error": str(exc)}
            threading.Thread(target=self.import_download, args=(zip_url,), daemon=True).start()
        elif name == "pack_local_time":   # a pack's own choice: say the local time in its landing PA, or leave it out
            cfg = load_config()
            try:
                code = check_airline_code(str(data.get("icao", "")))
                if not airline_folder(cfg, code).is_dir():
                    return {"error": "That pack is not installed."}
                set_pack_setting(cfg, code, "local_time", bool(data.get("on")))
            except (AppError, OSError) as exc:
                return {"error": str(exc)}
            state = load_state()   # rebuild the landing PA with / without the time on the next check
            state.pop("last", None)
            state.pop("live", None)
            save_state(state)
            self.wake.set()
        elif name == "remove_voice":
            try:
                message = remove_voice(load_config(), str(data.get("icao", "")), str(data.get("voice", "")))
            except AppError as exc:
                return {"error": str(exc)}
            self.set_status(message)
        elif name == "uninstall":
            self.start_pack_task("uninstall", check_airline_code(str(data.get("icao", ""))))
        elif name == "update":
            self.run_task("Updating", self.do_update)
        elif name == "voice":
            chosen = str(data.get("voice", ""))
            flight = load_state().get("flight") or {}
            try:
                cfg = load_config()
                source = load_aliases(cfg).get(flight.get("airline", ""), flight.get("airline", ""))
                names = list(list_voices(airline_folder(cfg, source)))
            except (AppError, OSError):
                return {"error": "No flight set up yet."}
            if chosen != "random" and chosen not in names:
                return {"error": "Unknown voice."}
            state = load_state()
            state.setdefault("voices", {})[source] = chosen
            state.pop("live", None)
            save_state(state)
            self.run_task("Switching voice", self.do_update)
        elif name == "auto":
            self.auto = bool(data.get("on"))
            self.refresh_menu()
            self.wake.set()
        elif name == "username":
            who = str(data.get("name", "")).strip()
            if not re.fullmatch(r"[\w.\- ]{1,60}", who):
                return {"error": "That doesn't look like a SimBrief username."}
            set_config_value("simbrief_username", who)
            set_config_value("simbrief_userid", "")  # an old numeric ID would otherwise win over the new name
            state = load_state()
            state.pop("last", None)
            save_state(state)
            self.set_status(f"SimBrief username set to {who}")
            self.poll_once(force=True) if not self.lock.locked() else self.wake.set()
        elif name in ("alias_add", "alias_remove"):
            alias = str(data.get("alias", "")).strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{3}", alias):
                return {"error": "An airline code is 3 letters or digits, like SHT."}
            cfg = load_config()
            try:
                aliases = load_aliases(cfg)
            except AppError as exc:
                return {"error": str(exc)}
            if name == "alias_add":
                source = str(data.get("source", "")).strip().upper()
                if not re.fullmatch(r"[A-Z0-9]{3}", source):
                    return {"error": "Pick the airline whose recordings it should use (3 letters, like BAW)."}
                if source == alias:
                    return {"error": "An airline can't be an alias of itself."}
                if aliases.get(source):
                    return {"error": f"{source} is itself an alias of {aliases[source]} - pick {aliases[source]} instead."}
                aliases[alias] = source
            else:
                aliases.pop(alias, None)
            try:
                save_aliases(aliases)
            except OSError as exc:
                return {"error": f"Could not save aliases.json: {exc}"}
            state = load_state()
            state.pop("last", None)
            save_state(state)
            self.set_status(f"{alias} {'now uses ' + aliases[alias] + ' recordings' if name == 'alias_add' else 'alias removed'}")
            self.wake.set()
        elif name == "pa_knob":   # the on-screen PA knob: push (audible / muted) and volume
            if "on" in data:
                set_config_value("pa_knob_on", bool(data["on"]))
            if "volume" in data:
                try:
                    set_config_value("pa_knob_volume", max(0, min(100, int(data["volume"]))))
                except (TypeError, ValueError):
                    return {"error": "The volume must be a number from 0 to 100."}
            self.sync_knob()
        elif name in ("phase_boarding", "phase_disarm"):
            self.phase_action(name.split("_", 1)[1])
        elif name == "audio_devices":   # the sound outputs on this PC (listed when Settings opens, not with every refresh)
            import papro_play

            devices = [{"name": d["name"], "default": d["default"]} for d in papro_play.list_output_devices()]
            return {"ok": True, "devices": devices, "problem": papro_play.LAST_ERROR}
        elif name == "audio_test":   # a chime through the chosen output, at the knob's volume
            from papro_play import Player, SoundDeviceBackend, find_device, test_tone

            device = str(data.get("device") or "")
            if device and find_device(device) is None:
                return {"error": "That output is not available right now - pick another or refresh the list."}
            self.sync_knob()
            level = max(0.25, min(1.0, KNOB["volume"] / 100.0))
            if getattr(self, "test_player", None) is not None:
                self.test_player.close()
            self.test_player = Player(lambda: level, SoundDeviceBackend(device))
            chime, rate = test_tone()
            self.test_player.play_data("test chime", chime, rate)
        elif name == "settings":
            mode = data.get("autostart")
            if mode not in ("off", "windows", "msfs"):
                return {"error": "Unknown setting."}
            try:
                if mode != current_autostart():
                    apply_autostart(mode)
                set_config_value("close_to_tray", bool(data.get("close_to_tray")))
                set_config_value("minimize_to_tray", bool(data.get("minimize_to_tray")))
                set_config_value("quit_with_msfs", bool(data.get("quit_with_msfs", True)))
                if "gsx" in data:
                    set_config_value("gsx_integration", bool(data["gsx"]))
                if "audio_device" in data:
                    set_config_value("pa_output_device", str(data.get("audio_device") or "")[:200])
                if "audio_playback" in data and bool(data["audio_playback"]) != bool(load_config().get("pa_playback", True)):
                    set_config_value("pa_playback", bool(data["audio_playback"]))
                    self.aircraft_event(self.aircraft)   # switch playback on / off for the aircraft already loaded
                chance = int(data.get("extra_chance", 25))
                if chance in (0, 25, 50) and chance != int(load_config().get("extra_chance", 25)):
                    set_config_value("extra_chance", chance)
                    state = load_state()
                    state.pop("last", None)
                    state.pop("extra_used", None)  # roll again for this flight
                    state.pop("main_pick", None)
                    save_state(state)
                    self.wake.set()
                style = "pauses" if data.get("join_style") == "pauses" else "flow"
                if style != ("pauses" if load_config().get("join_style") == "pauses" else "flow"):
                    set_config_value("join_style", style)
                    state = load_state()
                    state.pop("last", None)   # rebuild the joined announcements
                    save_state(state)
                    self.wake.set()
                boost = int(data.get("pa_boost", 30))
                if boost in (0, 15, 30, 50) and boost != boost_percent(load_config()):
                    set_config_value("pa_boost", boost)
                    state = load_state()
                    state.pop("last", None)   # rebuild everything at the new volume
                    save_state(state)
                    self.wake.set()
            except (AppError, OSError) as exc:
                return {"error": str(exc)}
            self.set_status("Settings saved")
        elif name == "hide_reminder":
            set_config_value("hide_reminder", bool(data.get("hide", True)))
        elif name == "welcomed":
            state = load_state()
            state["welcomed"] = True
            save_state(state)
        elif name == "minimized":
            self.window_minimized()
        elif name == "closing":  # the window was closed: quit if that is what the user chose
            self.window_closed()
        elif name == "refresh":
            self.refresh_catalog()
        elif name == "answer":
            prompt = self.prompt
            if prompt and prompt["id"] == data.get("id") and data.get("choice") in prompt["options"]:
                prompt["answer"] = (data["choice"], False)
                prompt["event"].set()
        elif name.startswith("studio_"):
            return self.studio_action(name[7:], data)
        elif name == "open":
            if data.get("what") == "browser":   # the same page in a normal browser tab (the address changes every time PAPro starts)
                import webbrowser

                webbrowser.open(self.ui.url())
            else:
                self._open_folder(str(data.get("what", "")))
        elif name == "test_options":
            r = self.test_options()
            return r if r.get("error") else {"ok": True, **r}
        elif name == "test_build":
            return self.test_build(data)
        elif name == "check_update":   # re-read catalog.json now: the newest PAPro and the pack list
            try:
                self.catalog = load_catalog(load_config())
                self.catalog_error = ""
            except AppError as exc:
                return {"error": str(exc)}
            self.update_checked = time.time()
            if any([cache_remote_logo(e) for e in self.catalog]):
                self.logo_version += 1
            self.refresh_menu()
            latest = app_update()
            return {"ok": True, "version": VERSION, "latest": latest["version"] if latest else ""}
        else:
            return {"error": "unknown action"}
        return {"ok": True}

    # Pack Studio ---------------------------------------------------------
    def language_snapshot(self, project: dict) -> dict:
        """The airline's language settings and the languages its voices speak, for Pack Studio's Languages panel."""
        lib = airline_folder(load_config(), project["icao"])
        langs = load_languages(lib)
        avail = {code: list(keys) for code, keys in language_voices(lib).items()}
        shown = {k: voice_info(p, k)["name"] for k, p in list_voices(lib).items()}
        mine = project.get("language", "")
        if mine and project["voice"] not in avail.get(mine, []):   # this voice, even before it is saved to the library
            avail.setdefault(mine, []).append(project["voice"])
        order = [c for c in langs["order"] if c in avail] + [c for c in avail if c not in langs["order"]]
        return {"enabled": langs["enabled"], "order": order, "per_pa": langs["per_pa"],
                "available": [{"code": c, "name": papro_studio.LANGUAGES.get(c, c), "voices": [shown.get(k, k) for k in avail[c]]} for c in avail],
                "known": papro_studio.LANGUAGES, "pas": [{"base": b, "label": l} for b, l in papro_studio.PA_BASES]}

    def studio_target(self, cfg: dict, icao: str) -> tuple[float, int, str]:
        """Loudness (LUFS) every clip is set to -> (LUFS, 0, ""). A fixed level, no measuring."""
        return papro_studio.DEFAULT_TARGET, 0, ""

    def studio_job(self, label: str | None, fraction: float | None = None) -> None:
        """What the Studio is busy with, shown as a loading bar in the window."""
        self._studio_job = {"label": label, "fraction": fraction} if label else None

    def _measure_target(self, cfg: dict, icao: str, cache: dict) -> tuple[float, int, str]:
        sources = [("this airline's own announcements", airline_folder(cfg, icao) / MAIN_FOLDER), ("saved original announcements", GENERIC_DIR / icao)]
        try:
            sources.append(("your Fenix airline packs", airline_packs_dir(cfg)))
        except (AppError, OSError):
            pass
        result = (papro_studio.DEFAULT_TARGET, 0, "")
        for label, folder in sources:
            try:
                files = sorted(p for p in folder.rglob("*.ogg") if not is_modular(p.name)) if folder.is_dir() else []
            except OSError:
                files = []
            if files:
                step = max(1, len(files) // 10)
                value, count = papro_studio.reference_loudness(files[::step], lambda f: self.studio_job("Measuring the original announcements", f))
                if count:
                    result = (value, count, label)
                    break
        cache[icao] = result
        return result

    def studio_voices(self, folder: Path) -> dict[str, Path]:
        """Voices that can be opened for editing: the usual ones, plus an airline folder that only has general PAs."""
        found = list_voices(folder)
        if not found and has_ogg(folder / MAIN_FOLDER):
            found = {DEFAULT_VOICE: folder}
        return found

    def project_in_library(self, cfg: dict, icao: str, voice: str) -> bool:
        """Is this project already a working pack in the Airlines library (opened from it, or installed from Pack Studio)?
        Then the project is only a working copy and can be removed without losing the pack."""
        try:
            project = papro_studio.load(STUDIO_DIR, icao, voice)
            origin = Path(project["origin"]) if project.get("origin") else None
            if origin and origin.is_dir():
                return True
            return (airline_folder(cfg, icao) / "voices" / papro_studio.slug(voice) / "voice.json").is_file()
        except (papro_studio.StudioError, AppError, OSError):
            return False

    def studio_snapshot(self, cfg: dict) -> dict:
        current = getattr(self, "studio_current", None)
        projects = [{**p, "in_library": self.project_in_library(cfg, p["icao"], p["voice"])} for p in papro_studio.list_projects(STUDIO_DIR)]
        out = {"projects": projects, "current": None, "folder": str(STUDIO_DIR),
               "job": getattr(self, "_studio_job", None)}
        if not current:  # packs already in the library that can be opened for editing
            out["library"] = []
            try:
                for folder in sorted(airlines_root(cfg).iterdir(), key=lambda p: p.name.lower()):
                    if folder.is_dir() and re.fullmatch(r"[A-Za-z0-9]{3}", folder.name):
                        for key, path in self.studio_voices(folder).items():
                            info = voice_info(path, key)
                            out["library"].append({"icao": folder.name.upper(), "voice": key, "name": info["name"], "artist": info["artist"]})
            except OSError:
                pass
        if current:
            try:
                project = papro_studio.load(STUDIO_DIR, *current)
            except papro_studio.StudioError:
                return out
            out["current"] = self._studio_current(cfg, project)
        return out

    def _studio_current(self, cfg: dict, project: dict, with_secondary: bool = True) -> dict:
        """Everything the Pack Studio page shows for one voice project (and, for the primary, its secondary language)."""
        target, count, label = papro_studio.DEFAULT_TARGET, 0, ""
        rows = papro_studio.status(STUDIO_DIR, project)
        info = {
            "icao": project["icao"], "airline": project["airline"], "voice": project["voice"],
            "artist": project.get("artist", ""), "credit_url": project.get("credit_url", ""),
            "donate_url": project.get("donate_url", ""), "accent": project.get("accent", ""),
            "also_icaos": ", ".join(project.get("also_icaos", [])), "local_time": project.get("local_time") is not False,
            "description": project.get("description", ""), "version": project.get("version", "1.0.0"),
            "effect": bool(project.get("effect", True)), "music_level": papro_studio.music_level(project), "airports": project["airports"], "aircraft": project.get("aircraft", []), "aircraft_on": bool(project.get("aircraft_on")),
            "app_boost": boost_percent(cfg), "languages": self.language_snapshot(project), "language": project.get("language", ""), "aircraft_presets": papro_studio.AIRCRAFT_PRESETS, "typed_bases": papro_studio.TYPED_BASES, "aircraft_types": papro_studio.AIRCRAFT_TYPES, "extras": project["extras"], "music": project.get("music", []), "crew": papro_studio.crew_config(project), "descent_city": bool(project.get("descent_city")), "service_types": project.get("service_types", []),
            "logo": papro_studio.logo_path(STUDIO_DIR, project).is_file(), "has_script": bool(project.get("scripts")), "origin": bool(project.get("origin")), "in_library": self.project_in_library(cfg, project["icao"], project["voice"]), "upgrades": papro_studio.upgrade_preview(STUDIO_DIR, project), "target_pending": False, "rows": rows, "missing": len(papro_studio.missing(STUDIO_DIR, project)),
            "target": target, "target_count": count, "target_source": label,
            "done": sum(1 for r in rows if r["done"] or r["skipped"]), "total": len(rows)}
        if with_secondary:
            sec = papro_studio.secondary_of(STUDIO_DIR, project)
            info["secondary_language"] = (project.get("secondary") or {}).get("language", "")
            info["secondary"] = self._studio_current(cfg, sec, False) if sec else None
        return info

    def studio_snapshot_current(self, cfg: dict, project: dict) -> dict:
        return self._studio_current(cfg, project)

    def studio_project(self, data: dict) -> dict:
        return papro_studio.load(STUDIO_DIR, check_airline_code(str(data.get("icao", ""))), str(data.get("voice", "")))

    def studio_upload(self, icao: str, voice: str, slot: str, filename: str, body: bytes) -> dict:
        try:
            project = papro_studio.load(STUDIO_DIR, check_airline_code(icao), voice)
            if slot == "@logo":
                papro_studio.set_logo(STUDIO_DIR, project, body)
                return {"ok": True}
            target = self.studio_target(load_config(), project["icao"])[0]
            return {"ok": True, **papro_studio.store_raw(STUDIO_DIR, project, slot, body, filename, target)}
        except (papro_studio.StudioError, AppError) as exc:
            return {"error": str(exc)}

    def studio_logo(self, icao: str, voice: str) -> bytes | None:
        try:
            path = papro_studio.logo_path(STUDIO_DIR, papro_studio.load(STUDIO_DIR, check_airline_code(icao), voice))
            return path.read_bytes() if path.is_file() else None
        except (papro_studio.StudioError, AppError, OSError):
            return None

    def studio_audio(self, icao: str, voice: str, slot: str) -> bytes | None:
        try:
            path = papro_studio.processed_file(STUDIO_DIR, papro_studio.load(STUDIO_DIR, check_airline_code(icao), voice), slot)
            return path.read_bytes() if path else None
        except (papro_studio.StudioError, AppError, OSError):
            return None

    def studio_action(self, name: str, data: dict) -> dict:
        try:
            if name == "create":
                project = papro_studio.create(STUDIO_DIR, str(data.get("icao", "")), str(data.get("airline", "")), str(data.get("voice", "")))
                self.studio_current = (project["icao"], project["voice"])
                return {"ok": True}
            if name == "import":
                icao = check_airline_code(str(data.get("icao", "")))
                cfg = load_config()
                found = self.studio_voices(airline_folder(cfg, icao))
                key = str(data.get("voice", ""))
                if key not in found:
                    return {"error": "That pack is no longer in your library."}
                names = {e["icao"]: e["name"] for e in self.catalog}
                if getattr(self, "_studio_job", None):
                    return {"error": "Still busy - wait for the loading bar to finish."}
                self.studio_job("Opening the pack - copying its clips", 0.0)

                def work() -> None:
                    try:
                        project = papro_studio.import_pack(STUDIO_DIR, icao, names.get(icao, icao), found[key], key,
                                                          lambda f: self.studio_job("Opening the pack - copying its clips", f))
                        self.studio_current = (project["icao"], project["voice"])
                    except (papro_studio.StudioError, OSError) as exc:
                        log.warning("Could not open pack for editing: %s", exc)
                        self.set_status(f"Could not open that pack: {exc}", notify=False)
                    finally:
                        self.studio_job(None)

                threading.Thread(target=work, daemon=True).start()
                return {"ok": True}
            if name == "open":
                project = self.studio_project(data)
                self.studio_current = (project["icao"], project["voice"])
                try:   # recordings that were saved but never processed (a crash, or an old bug) are finished now
                    todo = papro_studio.unprocessed(STUDIO_DIR, project)
                    if todo:
                        target = self.studio_target(load_config(), project["icao"])[0]
                        for slot in todo:
                            try:
                                papro_studio.process_slot(STUDIO_DIR, project, slot, target)
                            except papro_studio.StudioError:
                                pass
                except Exception:
                    log.exception("Could not finish unprocessed recordings")
                try:   # the PA sound was changed since this pack was last processed: redo the clips from your untouched recordings
                    if project.get("effect", True) and project.get("fx") != papro_studio.EFFECT_VERSION:
                        if any(papro_studio.raw_takes(papro_studio.project_dir(STUDIO_DIR, project["icao"], project["voice"]) / "raw", s)
                               for s in papro_studio.slot_list(project)):
                            papro_studio.reprocess_all(STUDIO_DIR, project, papro_studio.DEFAULT_TARGET)
                        project["fx"] = papro_studio.EFFECT_VERSION
                        papro_studio.save(STUDIO_DIR, project)
                except Exception:
                    log.exception("Could not re-process with the new PA sound")
                return {"ok": True}
            if name == "discard":
                project = self.studio_project(data)
                papro_studio.discard(STUDIO_DIR, project["icao"], project["voice"])
                if getattr(self, "studio_current", None) == (project["icao"], project["voice"]):
                    self.studio_current = None
                return {"ok": True}
            if name == "close":
                self.studio_current = None
                return {"ok": True}
            project = self.studio_project(data)
            cfg = load_config()
            if name == "details":
                for key, limit in (("artist", 80), ("accent", 40), ("description", 200), ("version", 20)):
                    if key in data:
                        project[key] = str(data[key]).strip()[:limit]
                for key in ("credit_url", "donate_url"):
                    if key in data:
                        value = str(data[key]).strip()[:300]
                        if value and not _is_http(value):
                            return {"error": "Links must start with http:// or https://"}
                        project[key] = value
                if "local_time" in data:
                    project["local_time"] = bool(data["local_time"])
                if "also_icaos" in data:
                    try:
                        project["also_icaos"] = papro_studio.clean_also(str(data["also_icaos"]), project["icao"])
                    except papro_studio.StudioError as exc:
                        return {"error": str(exc)}
                if "language" in data:
                    project["language"] = papro_studio.clean_language(data["language"])
                    if not project["language"]:
                        project.pop("language")
                    if (project.get("secondary") or {}).get("language") == project.get("language"):
                        project.pop("secondary", None)   # the primary became the same language: no secondary any more
                effect = bool(data.get("effect", project.get("effect", True)))
                changed = effect != bool(project.get("effect", True))
                project["effect"] = effect
                papro_studio.save(STUDIO_DIR, project)
                if changed:
                    papro_studio.reprocess_all(STUDIO_DIR, project, self.studio_target(cfg, project["icao"])[0])
                if not project.get("primary_voice"):
                    papro_studio.sync_secondary(STUDIO_DIR, project)
            elif name == "music_level":   # how loud the boarding music is, as a percent of the announcements (50 = half as loud)
                try:
                    project["music_level"] = max(5, min(100, int(data.get("level"))))
                except (TypeError, ValueError):
                    return {"error": "The music volume must be a number from 5 to 100."}
                count = papro_studio.reprocess_music(STUDIO_DIR, project, self.studio_target(cfg, project["icao"])[0])
                return {"ok": True, "tracks": count}
            elif name == "secondary":   # the secondary language (second set of recordings); "" removes it
                try:
                    papro_studio.set_secondary(STUDIO_DIR, project, str(data.get("language", "")))
                except papro_studio.StudioError as exc:
                    return {"error": str(exc)}
            elif name == "levels":   # check (or, with fix, set) the volume of the pack's clips
                try:
                    result = papro_studio.check_levels(STUDIO_DIR, project, self.studio_target(cfg, project["icao"])[0], bool(data.get("fix")))
                except Exception as exc:
                    return {"error": f"Could not check the clips: {exc}"}
                return {"ok": True, **result}
            elif name == "upgrade":
                count = papro_studio.upgrade_names(STUDIO_DIR, project)
                return {"ok": True, "renamed": count}
            elif name == "mode":
                papro_studio.set_mode(STUDIO_DIR, project, str(data.get("slot", "")), str(data.get("mode", "")))
            elif name == "script" and data.get("slot"):  # a script for one clip
                slot = papro_studio.find_slot(project, str(data["slot"]))
                words = str(data.get("text", "")).strip()[:4000]
                scripts = project.setdefault("scripts", {})
                if words:
                    scripts[slot["id"]] = words
                else:
                    scripts.pop(slot["id"], None)
                papro_studio.save(STUDIO_DIR, project)
                return {"ok": True}
            elif name == "script":
                if data.get("clear"):
                    project.pop("scripts", None)
                    skipped = 0
                else:
                    found, skipped = papro_studio.parse_script(project, str(data.get("text", ""))[:60000])
                    if not found:
                        return {"error": "No lines in that script matched a clip. Use lines like  Hour 4: Four  or  time/Hour_04: Four."}
                    project.setdefault("scripts", {}).update(found)
                papro_studio.save(STUDIO_DIR, project)
                return {"ok": True, "matched": len(project.get("scripts", {})), "skipped": skipped}
            elif name == "variant":
                base = str(data.get("base", ""))
                if data.get("remove"):
                    papro_studio.remove_variant(STUDIO_DIR, project, base, int(data.get("n", 0)))
                else:
                    papro_studio.add_variant(STUDIO_DIR, project, base)
            elif name == "lang_save":
                lib = airline_folder(cfg, project["icao"])
                order = [str(c).lower() for c in data.get("order") or []]
                per_pa = data.get("per_pa") if isinstance(data.get("per_pa"), dict) else {}
                for code in set(order) | {str(c).lower() for o in per_pa.values() if isinstance(o, list) for c in o}:
                    papro_studio.clean_language(code)
                known_bases = {b for b, _ in papro_studio.PA_BASES}
                per_pa = {b: [str(c).lower() for c in o] for b, o in per_pa.items() if b in known_bases and isinstance(o, list) and o}
                save_languages(lib, bool(data.get("enabled")), order, per_pa)
            elif name == "typed_add":
                entry = papro_studio.add_typed(STUDIO_DIR, project, str(data.get("base", "")), data.get("types"))
                return {"ok": True, "slot": "main/" + papro_studio.typed_stem(entry["base"], entry["types"], entry.get("n", 1))}
            elif name == "typed_types":
                return {"ok": True, "slot": papro_studio.set_typed_types(STUDIO_DIR, project, str(data.get("slot", "")), data.get("types"))}
            elif name == "typed_remove":
                papro_studio.remove_typed(STUDIO_DIR, project, str(data.get("slot", "")))
            elif name == "aircraft":
                if "on" in data:
                    papro_studio.set_aircraft_on(STUDIO_DIR, project, bool(data["on"]))
                elif data.get("remove"):
                    papro_studio.remove_aircraft(STUDIO_DIR, project, str(data.get("code", "")))
                else:
                    added, skipped = papro_studio.add_aircraft(STUDIO_DIR, project, str(data.get("code", ""))[:2000])
                    if not added:
                        return {"error": "Not an aircraft name: " + ", ".join(skipped[:5])} if skipped else {"ok": True, "added": 0}
                    return {"ok": True, "added": len(added), "skipped": skipped}
            elif name == "airport":
                if data.get("remove"):
                    papro_studio.remove_airport(STUDIO_DIR, project, str(data.get("code", "")))
                    papro_studio.sync_secondary(STUDIO_DIR, project)
                else:
                    added, skipped = papro_studio.add_airports(STUDIO_DIR, project, str(data.get("code", ""))[:5000])
                    papro_studio.sync_secondary(STUDIO_DIR, project)
                    if not added and not skipped:
                        return {"ok": True, "added": 0}
                    if not added:
                        return {"error": "Not an airport code: " + ", ".join(skipped[:8]) + ". Use 4-letter ICAO codes like EGLL, separated by commas, semicolons or spaces."}
                    return {"ok": True, "added": len(added), "skipped": skipped}
            elif name == "crew":   # the crew introduction (pre-safety briefing built from pieces): on/off, parts, order, crew per type
                fa = None
                if "fa" in data:
                    fa = {}
                    for k, v in (data["fa"] or {}).items():
                        k = str(k).upper().strip()
                        if k:
                            try:
                                fa[k] = max(1, min(papro_studio.MAX_CREW, int(v)))
                            except (TypeError, ValueError):
                                return {"error": f"The number of flight attendants for {k} must be a number from 1 to {papro_studio.MAX_CREW}."}
                papro_studio.set_crew(STUDIO_DIR, project, data.get("on"), data.get("order"), fa)
                papro_studio.sync_secondary(STUDIO_DIR, project)
            elif name == "descent_city":   # the descent announcement says the destination
                project["descent_city"] = bool(data.get("on"))
                if not project["descent_city"]:
                    project.pop("descent_city")
                papro_studio.save(STUDIO_DIR, project)
                papro_studio.sync_secondary(STUDIO_DIR, project)
            elif name == "service":   # a named version of the cabin service (Domestic, International...)
                if data.get("remove"):
                    papro_studio.remove_service(STUDIO_DIR, project, str(data.get("name", "")))
                else:
                    papro_studio.add_service(STUDIO_DIR, project, str(data.get("name", "")))
                papro_studio.sync_secondary(STUDIO_DIR, project)
            elif name == "crew_section":   # add / delete a piece of the introduction (intro part, number, base, names)
                if data.get("remove"):
                    papro_studio.remove_crew_section(STUDIO_DIR, project, str(data.get("item", "")))
                else:
                    papro_studio.add_crew_section(STUDIO_DIR, project, str(data.get("item", "")))
                papro_studio.sync_secondary(STUDIO_DIR, project)
            elif name == "crew_base":
                if data.get("remove"):
                    papro_studio.remove_crew_base(STUDIO_DIR, project, str(data.get("code", "")).upper())
                else:
                    added, skipped = papro_studio.add_crew_base(STUDIO_DIR, project, str(data.get("code", ""))[:500])
                    papro_studio.sync_secondary(STUDIO_DIR, project)
                    if skipped and not added:
                        return {"error": "Use 4-letter airport codes like KATL, separated by commas or spaces."}
                    return {"ok": True, "added": len(added), "skipped": skipped}
                papro_studio.sync_secondary(STUDIO_DIR, project)
            elif name == "crew_name":
                if data.get("remove"):
                    papro_studio.remove_crew_name(STUDIO_DIR, project, str(data.get("name", "")))
                else:
                    papro_studio.add_crew_name(STUDIO_DIR, project, str(data.get("name", "")))
                papro_studio.sync_secondary(STUDIO_DIR, project)
            elif name == "crew_random":
                return {"ok": True, **papro_studio.random_crew_name(project)}
            elif name == "music":
                (papro_studio.remove_music if data.get("remove") else papro_studio.add_music)(STUDIO_DIR, project, str(data.get("name", "")))
            elif name == "extra":
                (papro_studio.remove_extra if data.get("remove") else papro_studio.add_extra)(STUDIO_DIR, project, str(data.get("name", "")))
            elif name == "delete":
                papro_studio.delete_slot(STUDIO_DIR, project, str(data.get("slot", "")))
            elif name == "export":
                result = papro_studio.export(STUDIO_DIR, project, EXPORTS_DIR,
                                             load_languages(airline_folder(cfg, project["icao"])))
                open_path(Path(result["zip"]).parent)
                return {"ok": True, **result}
            elif name == "install":
                lib = airline_folder(cfg, project["icao"])
                dest = papro_studio.install_to_library(STUDIO_DIR, project, lib)
                sec = papro_studio.secondary_of(STUDIO_DIR, project)
                if sec is not None and any((papro_studio.project_dir(STUDIO_DIR, sec["icao"], sec["voice"]) / "pack").rglob("*.ogg")):
                    papro_studio.install_to_library(STUDIO_DIR, sec, lib)   # both languages, said one after the other
                    old = load_languages(lib)
                    save_languages(lib, True, [project.get("language", ""), sec["language"]], old["per_pa"])
                self.logo_version += 1
                state = load_state()
                state.pop("last", None)
                save_state(state)
                self.wake.set()
                finished = bool(project.get("origin"))
                if finished:  # edits to a pack that came from your library are saved: it is no longer a pack in progress
                    papro_studio.discard(STUDIO_DIR, project["icao"], project["voice"])
                    self.studio_current = None
                return {"ok": True, "path": str(dest), "finished": finished}
            elif name == "skip":
                papro_studio.set_skip(STUDIO_DIR, project, str(data.get("slot", "")), bool(data.get("skip", True)), "silent" if data.get("mode") == "silent" else "default")
            elif name == "unskip_all":
                return {"ok": True, "count": papro_studio.unskip_all(STUDIO_DIR, project)}
            elif name == "logo_remove":
                papro_studio.remove_logo(STUDIO_DIR, project)
            elif name == "folder":
                folder = papro_studio.project_dir(STUDIO_DIR, project["icao"], project["voice"])
                open_path(folder)
            else:
                return {"error": "unknown action"}
        except (papro_studio.StudioError, AppError, OSError) as exc:
            return {"error": str(exc)}
        return {"ok": True}

    def _open_folder(self, what: str) -> None:
        try:
            cfg = load_config()
            if what == "airlines":
                target = airlines_root(cfg)
            elif what == "fenix":
                target = airline_packs_dir(cfg)
            elif what == "log":
                open_path(LOG_PATH)
                return
            elif what == "aliases":
                load_aliases(cfg)  # creates the file if it isn't there yet
                open_path(ALIASES_PATH)
                return
            elif what == "update":
                import webbrowser

                latest = app_update()
                if latest and latest.get("auto"):
                    threading.Thread(target=self.install_app_update, daemon=True).start()
                elif latest and latest.get("url"):
                    webbrowser.open(latest["url"])
                return
            elif what == "kofi":
                import webbrowser

                webbrowser.open(KOFI_URL)
                return
            else:
                return
            target.mkdir(parents=True, exist_ok=True)
            open_path(target)
        except (AppError, OSError) as exc:
            self.set_status(str(exc))

    # install / uninstall with progress
    def start_pack_task(self, kind: str, icao: str, entry: dict | None = None) -> None:
        key = (entry or {}).get("id") or icao if kind == "install" else icao   # a downloadable voice has its own card
        if key in self.tasks or (kind == "install" and icao in self.tasks):
            return
        task = {"kind": kind, "text": "Starting", "frac": 0.0 if kind == "install" else None, "name": (entry or {}).get("name", "")}
        self.tasks[key] = task

        last_step = [-1]

        def progress(text: str, fraction: float | None) -> None:
            task["text"], task["frac"] = text, fraction
            step = -1 if fraction is None else int(fraction * 10)  # status line only changes every 10%
            if step != last_step[0]:
                last_step[0] = step
                self.set_status(f"{icao}: {text}")

        def work() -> None:
            ran = False
            try:
                if not self.lock.acquire(blocking=False):
                    self.set_status("Busy - try again in a moment")
                    return
                ran = True
                try:
                    if kind == "install":
                        message = install_pack(load_config(), entry, progress)
                    else:
                        task.update(text="Removing files", frac=None)
                        message = uninstall_pack(load_config(), icao)
                    self.set_status(message, notify=True)
                    self.logo_version += 1   # a new pack may have brought its logo: make the window ask for it again
                    if kind == "install":
                        self.installs_done += 1   # the window watches this to return to the Dashboard after an import
                except AppError as exc:
                    self.set_status(str(exc), notify=True)
                except Exception as exc:
                    log.exception("Unexpected error")
                    self.set_status(f"Error: {exc}", notify=True)
                finally:
                    self.lock.release()
            finally:
                self.tasks.pop(key, None)
                if entry and entry.get("cleanup"):
                    Path(entry["cleanup"]).unlink(missing_ok=True)   # the copy of an imported zip
            if ran and kind == "install":
                self.poll_once(force=True)  # apply the current flight with the new files
            self.refresh_menu()

        threading.Thread(target=work, daemon=True).start()

    def ask_choice(self, title: str, text: str, options: list[str]) -> tuple[str, bool]:
        """Called while applying a flight whose airport has several recordings. Waits for the window's answer."""
        import secrets

        event = threading.Event()
        self.prompt = {"id": secrets.token_hex(4), "title": title, "text": text, "options": options,
                       "event": event, "answer": None}
        self.set_status(f"{title}: choose a recording in the PAPro window", notify=True)
        self.open_window()
        event.wait(timeout=600)
        prompt, self.prompt = self.prompt, None
        return (prompt or {}).get("answer") or (options[0], False)


def tray_tooltip(status: str) -> str:
    """The short text shown when you hover the tray icon: "PAPro - BAW to EGCC, updated 3 minutes ago"."""
    text = status.strip()
    try:
        state = load_state()
        loaded, dest = state.get("loaded") or [], (state.get("flight") or {}).get("dest", "")
        if state.get("applied") and loaded and dest and (text.startswith("Unchanged:") or "last updated" in text or APPLIED_MARK in text):
            return f"PAs loaded for {'/'.join(loaded)} for {dest}"[:127]
    except Exception:
        pass
    for sep in (" - your announcements", " - updated ", " - landing PA", ", planned landing", "; also "):
        if sep in text and not text.startswith(sep.strip()):
            text = text.split(sep, 1)[0]
    ago = re.search(r"last updated (.+)$", status)
    if ago and "last updated" not in text:
        text = f"{text}, updated {ago.group(1)}"
    text = text.replace(" - last updated ", ", updated ")
    return f"PAPro - {text}"[:63]


class TrayApp(UIMixin):
    def __init__(self, show_window: bool = False) -> None:
        import pystray

        self.show_window = show_window
        self.aircraft = None   # {"title", "supported"} of the aircraft loaded in MSFS, or None
        self.pystray = pystray
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.status = "Starting..."
        self.auto = True
        self.catalog: list[dict] = []
        self.catalog_error = ""
        self.update_checked = 0.0
        self._ui_init()
        global CHOOSER
        CHOOSER = self.ask_choice
        self.icon = pystray.Icon(APP_NAME, make_icon_image(), APP_NAME, self.build_menu())

    # menu ---------------------------------------------------------------
    def build_menu(self):
        item, menu = self.pystray.MenuItem, self.pystray.Menu
        return menu(
            item("Open PAPro window", self.open_window, default=True),
            item(lambda _: tray_tooltip(self.status_text()).replace("PAPro - ", "", 1), None, enabled=False),
            menu.SEPARATOR,
            item("Update from SimBrief now", lambda: self.run_task("Updating", self.do_update)),
            item(lambda _: f"\u2714  {ACTIVE_BOLD}" if self.auto else "Off", self.toggle_auto),   # "✔  Active" in bold letters; plain "Off" when switched off
            menu.SEPARATOR,
            item("View log", lambda: open_path(LOG_PATH)),
            menu.SEPARATOR,
            item("Quit", self.quit),
        )

    def install_items(self):
        item = self.pystray.MenuItem
        installed = load_state().get("installed", {})
        rows = []
        for entry in self.catalog:
            current = installed.get(entry["icao"])
            have = (current.get("catalog", {}).get(entry["id"]) if current and "catalog" in current
                    else (current.get("version") if current and not entry.get("voice") else None))
            if have == entry["version"]:
                continue
            label = f"{entry['icao']} - {entry['name']}" + (f" ({entry['voice']})" if entry.get("voice") else "") + \
                f" v{entry['version']}" + (" (update)" if have is not None else "")
            rows.append(item(label, self._install_action(entry)))
        if rows:
            return rows
        text = self.catalog_error or ("All available packs are installed" if self.catalog else "Pack list not loaded")
        return [item(text[:90], None, enabled=False)]

    def uninstall_items(self):
        item = self.pystray.MenuItem
        installed = load_state().get("installed", {})
        rows = [
            item(f"{icao} - {rec.get('name', icao)} v{rec.get('version', '')}", self._uninstall_action(icao))
            for icao, rec in sorted(installed.items())
        ]
        return rows or [item("No packs installed", None, enabled=False)]

    def restore_items(self):
        """Airlines PAPro has saved original announcements for (or that have a 'main' folder of their own)."""
        item = self.pystray.MenuItem
        try:
            cfg = load_config()
            root = airlines_root(cfg)
            codes = {p.name for p in GENERIC_DIR.iterdir() if p.is_dir() and has_ogg(p)}
            if root.is_dir():
                codes |= {p.name.upper() for p in root.iterdir() if p.is_dir() and has_ogg(p / MAIN_FOLDER)}
        except (AppError, OSError):
            codes = set()
        rows = [item(code, self._restore_action(code)) for code in sorted(codes)]
        return rows or [item("Nothing to restore yet", None, enabled=False)]

    def _restore_action(self, icao: str):
        return lambda: self.run_task(f"Restoring {icao}", lambda: restore_generic(load_config(), icao))

    def open_airlines_folder(self) -> None:
        try:
            folder = airlines_root(load_config())
            folder.mkdir(parents=True, exist_ok=True)
            open_path(folder)
        except (AppError, OSError) as exc:
            self.set_status(str(exc), notify=True)

    def _install_action(self, entry: dict):
        return lambda: self.start_pack_task("install", entry["icao"], entry)

    def _uninstall_action(self, icao: str):
        return lambda: self.start_pack_task("uninstall", icao)

    def open_packs_folder(self) -> None:
        try:
            folder = airline_packs_dir(load_config())
            folder.mkdir(parents=True, exist_ok=True)
            open_path(folder)
        except AppError as exc:
            self.set_status(str(exc), notify=True)

    def msfs_watch(self) -> None:
        """Started by MSFS (Settings): close PAPro after the sim has been running and then closes."""
        seen, missing = False, 0
        while not self.stop.wait(10):
            try:
                if not (load_config().get("quit_with_msfs", True) and msfs_autostart_enabled()):
                    seen, missing = False, 0
                    continue
                if msfs_running():
                    seen, missing = True, 0
                elif seen:
                    missing += 1
                    if missing >= 2 and not (self.tasks or self.lock.locked()):
                        log.info("MSFS closed - quitting PAPro")
                        self.quit()
                        return
            except Exception:
                log.exception("MSFS watcher error")

    def aircraft_event(self, info: dict | None) -> None:
        """From the SimConnect watcher: the aircraft now loaded in the sim (None when the sim is closed)."""
        before = _PLAYBACK["on"]
        self.aircraft = info
        if not info and getattr(self, "tracker", None):
            self.tracker.reset()
            self.tracker.current = None   # the sim is closed: no phase to show
        if info:
            log.info("Aircraft: %s (%s)", info["title"], "PAs supported" if info["supported"] else "PAPro plays the PAs itself")
        try:
            _PLAYBACK["on"] = bool(info and not info["supported"] and load_config().get("pa_playback", True))
        except AppError:
            _PLAYBACK["on"] = False
        if _PLAYBACK["on"] != before:   # build the PAs where they are needed (the Fenix folder / PAPro's own playback folder)
            state = load_state()
            state.pop("last", None)   # rebuild for the new place (the landing time already taken from the sim is kept)
            save_state(state)
            self.wake.set()

    def phase_info(self):
        from papro_phases import PHASES

        tracker = getattr(self, "tracker", None)
        if not tracker or not tracker.phase() or not getattr(self, "aircraft", None):
            return None
        name = tracker.phase()
        playing = _PLAYBACK["on"]   # PAPro plays the PAs itself (not a Fenix): it has buttons the Fenix doesn't need
        return {"name": name, "source": tracker.source(), "index": PHASES.index(name) if name in PHASES else -1, "steps": PHASES,
                "can_board": bool(playing and tracker.can_start_boarding()), "can_disarm": bool(playing and tracker.can_disarm_doors())}

    def sync_knob(self) -> None:
        try:
            cfg = load_config()
            KNOB["on"] = bool(cfg.get("pa_knob_on", True))
            KNOB["volume"] = max(0, min(100, int(cfg.get("pa_knob_volume", 70))))
        except (AppError, TypeError, ValueError):
            pass

    def studio_seen(self) -> None:
        """The page says Pack Studio is open (about once a second): PAPro plays nothing for the next few seconds, so it never talks
        over someone who is recording. Whatever is playing right now is stopped."""
        now = time.monotonic()
        was_quiet = now < getattr(self, "studio_quiet_until", 0.0)
        self.studio_quiet_until = now + 6.0
        if not was_quiet and getattr(self, "music", None) is not None:
            self.music.stop(fade=False)
        if not was_quiet and getattr(self, "player", None) is not None:
            try:
                self.player.clear()
            except Exception:
                log.exception("Could not stop the playing PA")

    def release_held(self) -> None:
        """Pack Studio is closed again: play the announcements that came due meanwhile, in the order they came."""
        held = self.__dict__.get("held_keys")
        if not held or time.monotonic() < getattr(self, "studio_quiet_until", 0.0):
            return
        self.held_keys = []
        for key in held:
            log.info("Playing the held PA: %s", key)
            self.play_key(key)

    def play_key(self, key: str) -> None:
        """An announcement is due: find it in the pack built for this flight and queue it."""
        if time.monotonic() < getattr(self, "studio_quiet_until", 0.0):
            held = self.__dict__.setdefault("held_keys", [])
            if key not in held:
                held.append(key)
            log.info("PA due: %s - held until the Dashboard is open again (Pack Studio is open)", key)
            return
        path = None
        try:
            from papro_play import resolve

            path = resolve(PLAYBACK_DIR, key, clock_tag())
        except Exception:
            log.exception("Could not look up the %s announcement", key)
        if not path:
            log.info("PA due: %s - no recording in the pack", key)
            return
        log.info("PA due: %s -> %s", key, path.name)
        self.player_for().play(path)
        if key == "welcome":
            self.boarding_live = True
            self.next_boarding_pa = time.monotonic() + random.uniform(BOARDING_PA_MIN_S, BOARDING_PA_MAX_S)

    def player_for(self):
        from papro_play import Player, SoundDeviceBackend

        device = str(load_config().get("pa_output_device") or "")
        if getattr(self, "player", None) is not None and getattr(self, "player_device", None) != device:
            self.player.close()   # a different output was chosen in Settings
            self.player = None
        if getattr(self, "player", None) is None:
            self.sync_knob()
            self.player = Player(knob_gain, SoundDeviceBackend(device))
            self.player_device = device
        return self.player

    def _sync_tracker(self, tracker, now: float) -> None:
        """Every 20 s: the destination / cruise level of the loaded plan and the GSX setting. A different plan is a new
        flight, so the phase starts again from Preflight."""
        if now - getattr(self, "_tracker_sync", 0.0) < 20.0:
            return
        self._tracker_sync = now
        try:
            tracker.use_gsx = bool(load_config().get("gsx_integration", True))
            flight = load_state().get("flight") or {}
        except (AppError, OSError):
            return
        identity = (flight.get("airline"), flight.get("dest"), flight.get("number"), flight.get("aircraft"))
        if getattr(self, "_flight_identity", None) not in (None, identity) and identity != (None,) * 4:
            log.info("A different flight plan: the flight phase starts again")
            tracker.reset()
        self._flight_identity = identity
        lat, lon = flight.get("dest_lat"), flight.get("dest_lon")
        tracker.set_flight((lat, lon) if lat is not None and lon is not None else None, flight.get("cruise_ft"))

    def phase_action(self, what: str) -> None:
        """A Dashboard button: "boarding" (start the boarding phase) or "disarm" (the Disarm Doors PA)."""
        tracker = getattr(self, "tracker", None)
        if what == "boarding" and tracker:
            tracker.start_boarding()
        elif what == "disarm" and _PLAYBACK["on"]:
            engine = getattr(self, "phases", None)
            if engine is not None:
                engine._fire("disarm_doors")
            else:
                self.play_key("disarm_doors")

    def telemetry(self, reading: dict) -> None:
        """From the SimConnect watcher, about once a second: drives the phase engine for aircraft PAPro plays for."""
        from papro_phases import FMGC_PHASES, PhaseTracker

        tracker = getattr(self, "tracker", None)
        if tracker is None:
            tracker = self.tracker = PhaseTracker()
        self._sync_tracker(tracker, reading["t"])
        tracker.feed(reading)
        if not _PLAYBACK["on"]:
            return
        engine = getattr(self, "phases", None)
        if engine is None:
            from papro_phases import PhaseEngine

            engine = self.phases = PhaseEngine(self.play_key)
        self.release_held()
        if not engine.joined:   # PAPro opened part way through a flight: don't play what is already behind you
            engine.join(reading, tracker.phase())
        engine.feed(reading)
        self.track_flight_lock(reading)
        self.boarding_music(engine, tracker)
        if "deboard_complete" in engine.done:   # flight over: a minute later get ready for the next one
            self._reset_at = getattr(self, "_reset_at", None) or reading["t"] + 60
            if reading["t"] >= self._reset_at:
                engine.reset()
                self.held_keys = []
                self._reset_at = None

    def track_flight_lock(self, reading: dict) -> None:
        """While a flight is under way (airborne, landing, taxiing in) PAPro changes nothing for a new SimBrief plan - pilots often
        file the next flight in the air. The lock is released once the aircraft is parked with the engines shut down, and PAPro
        then fetches the new plan. Also remembers where the aircraft touched down (for the nearest-airport landing PA)."""
        self.last_telemetry = time.time()
        ground = bool(reading.get("on_ground"))
        if not ground:
            if not getattr(self, "flight_locked", False):
                log.info("In the air: SimBrief changes are ignored until you are parked with the engines off")
            self.flight_locked = True
            self.was_airborne = True
            return
        if getattr(self, "was_airborne", False):   # the first reading on the ground after flying: the touchdown point
            self.was_airborne = False
            lat, lon = reading.get("lat"), reading.get("lon")
            if lat is not None and lon is not None:
                self.touchdown_pos = (float(lat), float(lon))
        if not getattr(self, "flight_locked", False):
            return
        engines = reading.get("eng_running")
        brake = reading.get("parking_brake")
        stopped = float(reading.get("gs") or 0.0) < 1.0 and (engines is False or (engines is None and brake is True))
        if stopped:
            self.flight_locked = False
            log.info("Parked with the engines off - looking for the next SimBrief flight plan")
            state = load_state()
            state.pop("live", None)
            state.pop("last", None)
            save_state(state)
            self.wake.set()

    def flight_in_progress(self) -> bool:
        """True while the sim is feeding data and the flight has not ended with a parked, engines-off aircraft."""
        return bool(getattr(self, "flight_locked", False)) and time.time() - getattr(self, "last_telemetry", 0.0) < 20.0

    def boarding_music(self, engine, tracker) -> None:
        """Non-Fenix aircraft: the boarding music plays on repeat from when boarding starts until boarding is complete, and the
        boarding announcement is repeated every 3-7 minutes meanwhile."""
        from papro_phases import PHASES

        music = getattr(self, "music", None)
        over = "boarding_complete" in engine.done or engine.flown or tracker.rank >= PHASES.index("Boarding complete")
        started = getattr(self, "boarding_live", False) or tracker.phase() == "Boarding"
        quiet = time.monotonic() < getattr(self, "studio_quiet_until", 0.0)
        if over or not started:
            self.boarding_live = False
            if music is not None and music.running():
                music.stop()
            return
        if quiet:
            return
        from papro_play import MusicLoop, SoundDeviceBackend, music_file, resolve

        device = str(load_config().get("pa_output_device") or "")
        if music is None or getattr(self, "music_device", None) != device:
            if music is not None:
                music.stop(fade=False)
            self.sync_knob()
            music = self.music = MusicLoop(knob_gain, SoundDeviceBackend(device))
            self.music_device = device
        music.ping()
        track = music_file(PLAYBACK_DIR)
        if track and not music.running():
            log.info("Boarding music starts (plays until boarding is complete)")
            music.start(track)
        due = getattr(self, "next_boarding_pa", None)
        if due is None:
            self.next_boarding_pa = time.monotonic() + random.uniform(BOARDING_PA_MIN_S, BOARDING_PA_MAX_S)
        elif time.monotonic() >= due and "welcome" in engine.done:
            self.next_boarding_pa = time.monotonic() + random.uniform(BOARDING_PA_MIN_S, BOARDING_PA_MAX_S)
            path = resolve(PLAYBACK_DIR, "welcome", clock_tag())
            if path:
                log.info("Boarding announcement again")
                self.player_for().play(path)

    def sim_event(self, event: str, clock: str | None) -> None:
        """From the SimConnect watcher: "takeoff" (a new flight) or "landed" with the local time at the airport."""
        if event == "takeoff":
            log.info("Takeoff detected - new flight: landing time and extras start again")
            state = load_state()  # a new flight: the landing PA starts from the plan again and the extras roll again
            state.pop("live", None)
            state.pop("extra_used", None)
            state.pop("main_pick", None)
            state.pop("crew_pick", None)
            save_state(state)
            return
        with self.lock:  # wait for any update in progress
            try:
                self.set_status(rebuild_landing(load_config(), clock, getattr(self, "touchdown_pos", None)), notify=True)
            except AppError as exc:
                self.set_status(str(exc), notify=True)
            except Exception as exc:
                log.exception("Landing PA error")
                self.set_status(f"Error: {exc}", notify=True)
            if _PLAYBACK["on"] and getattr(self, "phases", None):
                self.phases.landed_event(time.time())

    def toggle_auto(self) -> None:
        self.auto = not self.auto
        self.wake.set()

    def install_app_update(self) -> None:
        """Download the new installer, check its checksum, run it silently (it closes PAPro, updates it and starts it again)."""
        if self.updating:
            return
        wanted = dict(APP_LATEST)
        if not (wanted.get("installer") and wanted.get("sha256")) or _version_tuple(wanted["version"]) <= _version_tuple(VERSION):
            return
        self.updating = True
        folder = Path(tempfile.mkdtemp(prefix="papro-update-"))
        setup = folder / f"PAPro-Setup-{wanted['version']}.exe"
        try:
            self.set_status(f"Downloading PAPro {wanted['version']} ...")
            last = [0.0]

            def progress(done: int, total: int) -> None:
                if time.time() - last[0] > 1:
                    last[0] = time.time()
                    self.set_status(f"Downloading PAPro {wanted['version']} ... {done * 100 // total}%")

            download_pack({"url": wanted["installer"], "sha256": wanted["sha256"]}, setup, progress)
            if setup.stat().st_size < 1_000_000:
                raise AppError("The downloaded update looks too small - not installing it.")
            self.set_status(f"Installing PAPro {wanted['version']} - PAPro will restart in a moment")
            time.sleep(1.5)
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                             creationflags=flags, close_fds=True)
            self.quit()
        except (AppError, OSError) as exc:
            log.error("App update failed: %s", exc)
            self.set_status(f"Could not update PAPro automatically: {exc}", notify=True)
            shutil.rmtree(folder, ignore_errors=True)
            self.updating = False

    def quit(self) -> None:
        try:
            if getattr(self, "player", None):
                self.player.close()
        except Exception:
            pass
        self.stop.set()
        self.wake.set()
        try:
            self.ui.close_window()   # the window is a separate browser process: it would otherwise stay open
        except Exception:
            log.exception("Could not close the window")
        self.icon.stop()

    # status -------------------------------------------------------------
    def status_text(self) -> str:
        """The status line, with "Unchanged: X to Y" shown as when it was last updated ("... - last updated 3 minutes ago")."""
        text = self.status
        if text.startswith("Unchanged: "):
            try:
                then = time.mktime(time.strptime(load_state().get("updated", ""), "%Y-%m-%d %H:%M:%S"))
                seconds = max(0, int(time.time() - then))
            except (ValueError, OverflowError):
                return text[11:]
            if seconds < 5:
                ago = "just now"
            else:
                for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
                    if seconds >= size:
                        n = seconds // size
                        ago = f"{n} {unit}{'' if n == 1 else 's'} ago"
                        break
            return f"{text[11:]} - last updated {ago}"[:120]
        return text

    def set_status(self, text: str, notify: bool = False) -> None:
        self.status = text[:120]
        log.info(text)
        try:
            self.icon.title = tray_tooltip(self.status_text())
            self.icon.update_menu()
            if notify and load_config().get("notifications", False):   # Windows pop-ups are off by default: PAPro works quietly in the background
                self.icon.notify(text if len(text) <= 250 else text[:247] + "...", APP_NAME)
        except Exception:  # tray backends can be flaky; never let that kill the worker
            log.exception("Tray update failed")

    # work ---------------------------------------------------------------
    def do_update(self) -> str:
        return update(load_config(), force=True)[1]

    def run_task(self, label: str, fn, then_update: bool = False) -> None:
        """Run fn in a background thread, one task at a time, and report the result in the tray."""
        def work() -> None:
            if not self.lock.acquire(blocking=False):
                self.set_status("Busy - try again in a moment")
                return
            try:
                self.set_status(f"{label}...")
                message = fn()
                self.set_status(message, notify=True)
            except AppError as exc:
                self.set_status(str(exc), notify=True)
            except Exception as exc:
                log.exception("Unexpected error")
                self.set_status(f"Error: {exc}", notify=True)
            finally:
                self.lock.release()
            if then_update:
                self.poll_once(force=True)
            self.refresh_menu()

        threading.Thread(target=work, daemon=True).start()

    def refresh_menu(self) -> None:
        try:
            self.icon.update_menu()
        except Exception:
            log.exception("Menu refresh failed")

    def refresh_catalog(self, notify: bool = False) -> None:
        def work() -> None:
            try:
                self.catalog = load_catalog(load_config())
                self.catalog_error = ""
                self.update_checked = time.time()
                if any([cache_remote_logo(e) for e in self.catalog]):
                    self.logo_version += 1
                if notify:
                    self.set_status(f"Pack list refreshed ({len(self.catalog)} available)", notify=True)
            except AppError as exc:
                self.catalog, self.catalog_error = [], str(exc)
                if notify:
                    self.set_status(str(exc), notify=True)
            self.refresh_menu()

        threading.Thread(target=work, daemon=True).start()

    def poll_once(self, force: bool = False) -> None:
        if not self.lock.acquire(blocking=False):
            return
        if not force and self.flight_in_progress() and load_state().get("last"):
            return   # in the air / taxiing in: a newly filed flight waits until the aircraft is parked with the engines off
        try:
            cfg = load_config()
            changed, message = update(cfg, force=force)
            self.set_status(message, notify=False)   # pack updates happen quietly in the background
        except AppError as exc:
            self.set_status(str(exc))
        except Exception as exc:
            log.exception("Unexpected error")
            self.set_status(f"Error: {exc}")
        finally:
            self.lock.release()

    def poll_loop(self) -> None:
        while not self.stop.is_set():
            interval = 60
            try:
                cfg = load_config()
                interval = max(15, int(cfg.get("poll_interval_seconds", 60)))
                if self.auto and cfg.get("auto_poll", True):
                    self.poll_once()
            except AppError as exc:
                self.set_status(str(exc))
            except Exception:
                log.exception("Poll loop error")
            self.wake.wait(interval)
            self.wake.clear()

    def run(self) -> None:
        try:
            airlines_root(load_config()).mkdir(parents=True, exist_ok=True)
        except (AppError, OSError):
            pass  # reported later when the first update runs
        try:   # the web page / network address must work right away, also when Windows started PAPro in the tray
            self.ui.ensure_server()
        except Exception:
            log.exception("Could not start PAPro's web server")
        self.refresh_catalog()
        self.sync_knob()
        try:
            cfg = load_config()
            needs_name = not (str(cfg.get("simbrief_username", "")).strip() or str(cfg.get("simbrief_userid", "")).strip())
        except AppError:
            needs_name = True  # first run: config.json was just created
        if self.show_window or needs_name or not load_state().get("welcomed"):
            self.open_window()  # started by hand, or first run: explain what PAPro changes, ask for the SimBrief username
        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self.msfs_watch, daemon=True).start()
        threading.Thread(target=_listen_for_show, args=(self,), daemon=True).start()
        try:
            import papro_sim

            papro_sim.SimWatcher(self.sim_event, msfs_running, self.stop,
                                float(load_config().get("landing_speed_knots", 60)), on_aircraft=self.aircraft_event,
                                on_telemetry=self.telemetry).start()
        except Exception:
            log.exception("Could not start the landing watcher")
        self.icon.run()


# --------------------------------------------------------------------------- entry point
_INSTANCE_LOCK = None
INSTANCE_PORT = 47653   # a loopback port that only the running copy of PAPro holds


def _claim_single_instance(show: bool = True) -> bool:
    """Only one PAPro at a time: a second copy would fight the first over the same files and be impossible to close.
    Starting PAPro again (by hand) just brings the running copy's window forward."""
    global _INSTANCE_LOCK
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", INSTANCE_PORT))
        sock.listen(2)
    except OSError:
        log.warning("PAPro is already running - this copy will exit")
        if not show:
            return False
        try:
            with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=3) as conn:
                conn.sendall(b"show\n")
            return False
        except OSError:
            pass
        text = "PAPro is already running. Look for its icon in the system tray (bottom right, under the ^ arrow)."
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, text, "PAPro", 0x40)
            except Exception:
                pass
        else:
            print(text)
        return False
    _INSTANCE_LOCK = sock
    return True


def _listen_for_show(app: "TrayApp") -> None:
    """A second start of PAPro knocks on our loopback port: open the window for it."""
    sock = _INSTANCE_LOCK
    while sock is not None and not app.stop.is_set():
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        with conn:
            try:
                conn.settimeout(2)
                if conn.recv(16).startswith(b"show"):
                    app.open_window()
            except Exception:
                log.exception("Could not open the window")


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--community", action="store_true", help="show the detected Community folder")
    parser.add_argument("--add-airline", nargs="+", metavar="ICAO", help="create Airlines\\<ICAO>\\main|time|extra|airports|fuel folders")
    parser.add_argument("--list-packs", action="store_true", help="list packs in the catalog")
    parser.add_argument("--install", metavar="ICAO", help="download and install an airline pack")
    parser.add_argument("--uninstall", metavar="ICAO", help="uninstall an airline pack")
    parser.add_argument("--restore", metavar="ICAO", help="put an airline's original generic announcements back")
    parser.add_argument("--once", action="store_true", help="update once and exit (no tray icon)")
    parser.add_argument("--probe-aircraft", nargs="*", metavar="NAME", help="with MSFS running: write probe-report.txt (what the loaded aircraft reports) for mapping its PA knob")
    parser.add_argument("--background", action="store_true", help="start in the tray without opening the window (used by the startup entries)")
    parser.add_argument("--airline", help="with --once: airline ICAO instead of the SimBrief plan's")
    parser.add_argument("--variant", help="with --once: which recording to use when a destination has several (e.g. T5, or Standard)")
    parser.add_argument("--dest", help="with --once: destination ICAO instead of asking SimBrief")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )

    try:  # if the program ever dies hard (a crash inside a native library), say where in crash.log
        import faulthandler
        faulthandler.enable(open(DATA_DIR / "crash.log", "a", encoding="utf-8"), all_threads=True)
    except Exception:
        pass

    if args.probe_aircraft is not None:
        import papro_probe

        print(papro_probe.run(args.probe_aircraft, DATA_DIR / "probe-report.txt"))
        print(f"\nSaved to {DATA_DIR / 'probe-report.txt'}")
        return 0
    cli = (args.community or args.add_airline or args.list_packs or args.install or args.uninstall
           or args.restore or args.once)
    if not cli:
        if not _claim_single_instance(show=not args.background):
            return 0
        shutil.rmtree(TEST_DIR, ignore_errors=True)   # scratch builds from the Test tab last one run
        try:  # startup entries written by older versions had no --background: rewrite them so MSFS starts PAPro quietly
            mode = current_autostart()
            if mode != "off":
                apply_autostart(mode)
        except Exception:
            log.exception("Could not refresh the startup entry")
        TrayApp(show_window=not args.background).run()
        return 0
    try:
        cfg = load_config()
        if args.community:
            print(find_community_dir(cfg))
        for code in args.add_airline or []:
            folder, created = ensure_airline_folders(cfg, code)
            print(f"{'Created' if created else 'Already there'}: {folder}")
        if args.list_packs:
            installed = load_state().get("installed", {})
            for p in load_catalog(cfg):
                mark = "installed" if p["icao"] in installed else ""
                print(f"{p['icao']}  {p['name']}  v{p['version']}  {mark}")
        if args.install:
            wanted = args.install.upper()
            entry = next((p for p in load_catalog(cfg) if p["icao"] == wanted), None)
            if not entry:
                raise AppError(f"{wanted} is not in the catalog.")
            print(install_pack(cfg, entry))
        if args.uninstall:
            print(uninstall_pack(cfg, args.uninstall))
        if args.restore:
            print(restore_generic(cfg, args.restore))
        if args.once:
            print(update(cfg, force=True, airline=args.airline, dest=args.dest, variant=args.variant)[1])
        return 0
    except AppError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
