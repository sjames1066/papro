"""Watch MSFS (through SimConnect) for the moment you have landed and slowed down, and read the local time there.

Everything that decides "landed and decelerated" is in LandingDetector, which knows nothing about the sim, so it can
be tested on its own. SimWatcher feeds it from SimConnect.
"""

from __future__ import annotations

import logging
import re
import threading
import time

log = logging.getLogger("papro")


class LandingDetector:
    """Feed it once a second. Returns "takeoff" the first time you are properly airborne, "landed" once when, after
    touching down, the ground speed falls to `slow_kt`, otherwise None. A touch-and-go (airborne again before slowing
    down) triggers nothing."""

    def __init__(self, slow_kt: float = 60.0, airborne_ft: float = 300.0):
        self.slow_kt, self.airborne_ft = slow_kt, airborne_ft
        self.flown = False      # has been properly airborne since the last landing
        self.touched = False    # on the ground again after that flight

    def feed(self, on_ground: bool, agl_ft: float, ground_kt: float) -> str | None:
        if not on_ground:
            self.touched = False
            if agl_ft >= self.airborne_ft and not self.flown:
                self.flown = True
                return "takeoff"
            return None
        if self.flown:
            self.touched = True
        if self.flown and self.touched and ground_kt <= self.slow_kt:
            self.flown = self.touched = False
            return "landed"
        return None


def round_clock(seconds: float, step_min: int = 5) -> str:
    """Seconds since local midnight -> "HH:MM" to the nearest `step_min` minutes."""
    minutes = int(round(seconds / 60 / step_min)) * step_min % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


FSLABS_MARKERS = ("fslabs", "fsl a3", "fsl_a3", "flightsimlabs", "flight sim labs")   # the FSLabs A32X: its PAs are airline-pack files too, played by the aircraft
SUPPORTED_MAKERS = ("fenix",)   # aircraft whose own PA sounds PAPro can replace (the Fenix A320 reads its announcements from a folder)


def classify_aircraft(title: str) -> dict | None:
    """What the sim says is loaded -> {"title": ..., "supported": bool}. Supported = PAs can be played in the sim by
    swapping the aircraft's own announcement files (the Fenix A32X series)."""
    title = " ".join(str(title or "").replace("\x00", " ").split())
    if not title or title.upper().startswith("ATCCOM."):   # an unresolved text key (the sim answered mid-load), not a name
        return None
    low = title.lower()
    maker = "fenix" if "fenix" in low else "fslabs" if any(m in low for m in FSLABS_MARKERS) else ""
    livery = ""
    if maker == "fslabs":   # "FSLabs A321-251NX - British Airways (G-NEOV)" -> the airline the loaded livery belongs to
        m = re.search(r"\s-\s(.+?)\s*(?:\([^)]*\))?\s*$", title)
        livery = m.group(1).strip() if m else ""
    return {"title": title, "supported": bool(maker), "maker": maker, "livery": livery}


class SimWatcher:
    """Background thread: connects to MSFS when it is running and calls
    on_event("takeoff") or on_event("landed", "HH:MM") (local time where you landed)."""

    def __init__(self, on_event, running, stop: threading.Event, slow_kt: float = 60.0, on_aircraft=None, on_telemetry=None, seatbelt_lvars=None):
        self.seatbelt_lvars = seatbelt_lvars   # optional callable -> extra L: variable names for the seatbelt sign (config.json: seatbelt_lvars)
        self.on_event, self.running, self.stop = on_event, running, stop
        self.on_telemetry = on_telemetry
        self.on_aircraft, self._aircraft = on_aircraft, None
        self.detector = LandingDetector(slow_kt)
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _report_aircraft(self, info) -> None:
        """Report a change of aircraft only once it has been seen three times in a row: the sim sometimes answers with
        nothing or a placeholder for a second, and that must not look like swapping aircraft."""
        if info != getattr(self, "_candidate", None):
            self._candidate, self._seen = info, 0
        self._seen = getattr(self, "_seen", 0) + 1
        if info != self._aircraft and (self._seen >= 3 or (info is None and not self.running())):
            self._aircraft = info
            if self.on_aircraft:
                try:
                    self.on_aircraft(info)
                except Exception:
                    log.exception("Aircraft callback failed")

    def _read_title(self, requests) -> str:
        for name in ("TITLE",) if self._aircraft else ("TITLE", "ATC_MODEL"):   # ATC_MODEL only to name an aircraft with no title
            try:
                value = requests.get(name)
            except Exception:
                continue
            if isinstance(value, (bytes, bytearray)):
                value = bytes(value).decode("utf-8", "replace")
            if value and str(value).strip():
                if name == "TITLE":
                    self._title_low = str(value).lower()
                return str(value)
        return ""

    def _get(self, requests, *names):
        """First name the aircraft answers to, or None (the value is not reported / the name is unknown)."""
        for name in names:
            try:
                value = requests.get(name)
            except Exception:
                continue
            if value is not None:
                return value
        return None

    # -- PMDG 737 seatbelt selector (PMDG SDK data area; needs EnableDataBroadcast=1 in PMDG's 737 options ini) ----
    PMDG_AREA = b"PMDG_NG3_Data"
    PMDG_SEATBELT_OFFSET = 0x649F - 0x6420   # COMM_FastenBeltsSelector: FSUIPC offset 0x649F, the data starts at 0x6420
    PMDG_WINDOW = 32                          # bytes read around it, so the log can show whether the right byte moves
    _PMDG_IDS = (0x50500001, 0x50500002, 0x50500003, 0x50500004)   # area, definition, datum, request (our own numbers)

    def _pmdg_setup(self, requests) -> None:
        """Ask SimConnect for the PMDG data window (once per connection). Silent when it isn't there."""
        self._pmdg = {"data": None, "t": 0.0, "last": None, "ok": False}
        try:
            import ctypes
            from ctypes import wintypes

            sm = requests.sm
            dll = sm.dll.SimConnect
            area, define, datum, request = self._PMDG_IDS
            D = wintypes.DWORD
            dll.SimConnect_MapClientDataNameToID.argtypes = [wintypes.HANDLE, ctypes.c_char_p, D]
            dll.SimConnect_AddToClientDataDefinition.argtypes = [wintypes.HANDLE, D, D, D, ctypes.c_float, D]
            dll.SimConnect_RequestClientData.argtypes = [wintypes.HANDLE, D, D, D, D, D, D, D, D]
            for fn in (dll.SimConnect_MapClientDataNameToID, dll.SimConnect_AddToClientDataDefinition, dll.SimConnect_RequestClientData):
                fn.restype = ctypes.c_long
            start = max(0, self.PMDG_SEATBELT_OFFSET - self.PMDG_WINDOW // 2)
            self._pmdg["start"] = start
            h = sm.hSimConnect
            if dll.SimConnect_MapClientDataNameToID(h, self.PMDG_AREA, area) < 0:
                return
            if dll.SimConnect_AddToClientDataDefinition(h, define, start, self.PMDG_WINDOW, 0.0, datum) < 0:
                return
            orig = sm.my_dispatch_proc
            state = self._pmdg

            def dispatch(pData, cbData, ctx, _orig=orig, _state=state):
                try:
                    if pData.contents.dwID == 16:   # SIMCONNECT_RECV_ID_CLIENT_DATA
                        base = ctypes.addressof(pData.contents)
                        if ctypes.c_uint32.from_address(base + 12).value == request:   # dwRequestID
                            _state["data"] = bytes((ctypes.c_ubyte * self.PMDG_WINDOW).from_address(base + 40))
                            _state["t"] = time.time()
                        return
                except Exception:
                    return
                _orig(pData, cbData, ctx)

            sm.my_dispatch_proc_rd = sm.dll.DispatchProc(dispatch)
            self._pmdg_keep = dispatch
            # period 4 = every second; flags 0 = always (not only on change)
            if dll.SimConnect_RequestClientData(h, area, request, define, 4, 0, 0, 0, 0) >= 0:
                state["ok"] = True
        except Exception as exc:
            log.info("PMDG seatbelt reading unavailable: %s", exc)

    def _pmdg_seatbelt(self):
        """True = selector ON, False = AUTO or OFF, None = no PMDG data (not a PMDG, or SDK broadcast is off)."""
        st = getattr(self, "_pmdg", None)
        if not st or not st["ok"] or st["data"] is None or time.time() - st["t"] > 5.0:
            return None
        data = st["data"]
        value = data[self.PMDG_SEATBELT_OFFSET - st["start"]]
        if value != st["last"]:
            names = {0: "OFF", 1: "AUTO", 2: "ON"}
            log.info("PMDG seatbelt selector byte (offset %d) = %d (%s); window: %s", self.PMDG_SEATBELT_OFFSET, value,
                     names.get(value, "?"), data.hex(" "))
            st["last"] = value
        return None if value > 2 else value == 2

    def read_telemetry(self, requests, ground: bool, agl: float, speed: float) -> dict:
        g = lambda *n: self._get(requests, *n)   # noqa: E731
        engines = [g(f"GENERAL_ENG_COMBUSTION:{i}") for i in (1, 2)]
        known = [e for e in engines if e is not None]
        return {
            "t": time.time(), "on_ground": ground, "agl": agl, "gs": speed,
            "vs": g("VERTICAL_SPEED"), "alt": g("INDICATED_ALTITUDE", "PLANE_ALTITUDE") or 0.0,
            "parking_brake": g("BRAKE_PARKING_POSITION", "BRAKE_PARKING_INDICATOR"),
            "beacon": self._beacon(requests, g),
            "landing_light": g("LIGHT_LANDING", "LIGHT_LANDING_ON"),
            "seatbelt": self._seatbelt(requests, g),
            "no_smoking": self._no_smoking(requests, g),   # 0 = off, 1 = on, 2 = auto (None: the aircraft does not report it)
            "sim_local_s": g("LOCAL_TIME"),   # seconds since midnight, local time at the aircraft
            "eng_running": any(known) if known else None,
            "eng_all": all(known) if len(known) >= 2 else None,   # both engines running
            "fmgc_phase": self._lvar(requests, "L:A32NX_FMGC_FLIGHT_PHASE"),
            "lat": g("PLANE_LATITUDE"), "lon": g("PLANE_LONGITUDE"),
            "heading": g("PLANE_HEADING_DEGREES_TRUE", "PLANE_HEADING_DEGREES_MAGNETIC"),
            "door_open": self._doors_open(requests),
            "gsx_boarding": self._lvar(requests, "L:FSDT_GSX_BOARDING_STATE"),      # 5 = boarding, 6 = done
            "gsx_deboarding": self._lvar(requests, "L:FSDT_GSX_DEBOARDING_STATE"),  # 5 = deboarding
        }

    FSS_BEACON_LVAR = "L:FSS_EXX_OVHD_EXLT_RED_BCN_SWITCH"
    FSS_SEATBELT_LVAR = "L:FSS_EXX_OVHD_PSGR_FSTN_BELT_SWITCH"
    IFLY_SEATBELT_LVAR = "L:VC_Fasten_Belts_SW_VAL"   # iFly 737 MAX overhead switch: 0 = OFF, 10 = AUTO, 20 = ON (it does not set the standard variable)

    # FSLabs A32X overhead switches (0 = OFF, 10 = AUTO, 20 = ON). The no-smoking name is read off the aircraft in the behaviours
    # debugger; the seatbelt one follows the same pattern (both spellings are tried).
    FSL_SEATBELT_LVARS = ("L:VC_OVHD_SIGNS_Seatbelts_Switch", "L:VC_OVHD_SIGNS_SEATBELTS_SWITCH")
    FSL_NOSMOKING_LVAR = "L:VC_OVHD_SIGNS_NoSmoking_Switch"

    def _seatbelt(self, requests, g):
        """The seatbelt sign: PMDG 737's selector, the iFly 737 MAX's own switch, any L: variables named in config.json
        (seatbelt_lvars), then the standard sim variable. True / False, or None when nothing reports it."""
        value = self._pmdg_seatbelt()
        if value is not None:
            return value
        title = getattr(self, "_title_low", "")
        # an L: variable the aircraft doesn't have reads as 0, not "missing", so only look at the ones that belong to this aircraft
        if "fsl" in title or "flightsimlabs" in title:   # FSLabs: the name's spelling is not certain, so take the larger of the two
            raws = [self._lvar(requests, n) for n in self.FSL_SEATBELT_LVARS]
            raws = [float(r) for r in raws if r is not None]
            if raws:
                raw = max(raws)
                if self.__dict__.setdefault("_sb_seen", {}).get("fsl") != raw:
                    log.info("FSLabs seatbelt switch = %s", raw)
                    self._sb_seen["fsl"] = raw
                return raw >= 15   # 0 = OFF, 10 = AUTO, 20 = ON
        elif "ifly" in title:
            raw = self._lvar(requests, self.IFLY_SEATBELT_LVAR)
            if raw is not None:
                return float(raw) >= 15
        elif "fss" in title and ("e1" in title or "embraer" in title):   # FlightSim Studio E-Jets: 0 = OFF, 1 = ON
            raw = self._lvar(requests, self.FSS_SEATBELT_LVAR)
            if raw is not None:
                if self.__dict__.setdefault("_sb_seen", {}).get("fss") != raw:
                    log.info("FSS E-Jet seatbelt switch = %s", raw)
                    self._sb_seen["fss"] = raw
                return float(raw) >= 0.5   # reads 0 / 1 in the sim (not 0 / 100)
        try:
            extra = [str(n) for n in (self.seatbelt_lvars() if self.seatbelt_lvars else []) if str(n).strip()]
        except Exception:
            extra = []
        for name in extra:
            raw = self._lvar(requests, name if name.startswith("L:") else f"L:{name}")
            if raw is not None:
                return float(raw) >= 0.5
        return g("CABIN_SEATBELTS_ALERT_SWITCH")

    def _beacon(self, requests, g):
        """The anti-collision beacon: the FSS E-Jets' own switch (0 = OFF, 1 = ON; their lights don't answer the standard
        variable), else the standard sim variable."""
        title = getattr(self, "_title_low", "")
        if "fss" in title and ("e1" in title or "embraer" in title):
            raw = self._lvar(requests, self.FSS_BEACON_LVAR)
            if raw is not None:
                seen = self.__dict__.setdefault("_sb_seen", {})
                if seen.get("fssb") != raw:
                    log.info("FSS E-Jet beacon switch = %s", raw)
                    seen["fssb"] = raw
                return float(raw) >= 0.5   # reads 0 / 1 in the sim (not 0 / 100)
        return g("LIGHT_BEACON", "LIGHT_BEACON_ON")

    def _no_smoking(self, requests, g):
        """The no-smoking sign: the FSLabs switch (20 = ON, 10 = AUTO, which is still lit, 0 = OFF), else the standard variable."""
        title = getattr(self, "_title_low", "")
        raw = self._lvar(requests, self.FSL_NOSMOKING_LVAR) if ("fsl" in title or "flightsimlabs" in title) else None   # other aircraft read 0 here
        if raw is not None:
            seen = self.__dict__.setdefault("_sb_seen", {})
            if seen.get("ns") != raw:
                log.info("No-smoking switch %s = %s", self.FSL_NOSMOKING_LVAR, raw)
                seen["ns"] = raw
            return float(raw) >= 5
        return g("CABIN_NO_SMOKING_ALERT_SWITCH")

    def _doors_open(self, requests):
        """True when any exit reports open; None if the aircraft reports none of them."""
        seen = False
        for i in range(0, 6):
            try:
                value = requests.get(f"EXIT_OPEN:{i}")
            except Exception:
                continue
            if value is None:
                continue
            seen = True
            if float(value) > 0.05:
                return True
        return False if seen else None

    def _lvar(self, requests, name):
        """An aircraft's / add-on's own variable (an L: var), or None. The SimConnect library has no names for these, so
        the request is built by hand (the library would read "L:FSDT_..." as an indexed name and return nothing)."""
        try:
            from SimConnect.RequestList import Request

            cache = self.__dict__.setdefault("_lvar_reqs", {})
            req = cache.get(name)
            if req is None:
                req = cache[name] = Request((name.encode(), b"number"), requests.sm, _time=500)
            value = req.value
        except Exception:
            return None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def loop(self) -> None:
        try:
            from SimConnect import AircraftRequests, SimConnect
        except Exception as exc:
            log.info("Live landing time unavailable (SimConnect library missing): %s", exc)
            return
        sm = requests = None
        while not self.stop.is_set():
            try:
                if sm is None:
                    if not self.running():
                        self._report_aircraft(None)
                        self.stop.wait(15)
                        continue
                    sm = SimConnect()
                    requests = AircraftRequests(sm, _time=500)
                    log.info("Connected to MSFS (SimConnect)")
                    self._pmdg_setup(requests)
                    self.detector = LandingDetector(self.detector.slow_kt)
                info = classify_aircraft(self._read_title(requests))
                ground = requests.get("SIM_ON_GROUND")
                agl = requests.get("PLANE_ALT_ABOVE_GROUND")
                speed = requests.get("GROUND_VELOCITY")
                # some add-on aircraft (e.g. FSLabs) never give a usable TITLE: once flight data is flowing, carry on as
                # "an aircraft PAPro plays the PAs for" instead of waiting for a name that never comes
                if info is None and ground is not None and agl is not None and speed is not None:
                    self._unnamed = getattr(self, "_unnamed", 0) + 1
                    if self._unnamed >= 8:
                        info = {"title": "Unnamed aircraft", "supported": False, "maker": ""}
                else:
                    self._unnamed = 0
                self._report_aircraft(info)
                if ground is None or agl is None or speed is None:
                    # connected while MSFS was still loading (menu / no aircraft yet): the connection can stay dead after
                    # the aircraft loads, so drop it and connect again
                    self._dead = getattr(self, "_dead", 0) + 1
                    if self._dead >= 15:
                        self._dead = 0
                        log.info("SimConnect gave no flight data - reconnecting")
                        try:
                            sm.exit()
                        except Exception:
                            pass
                        sm = requests = None
                        self._lvar_reqs = {}
                        self.stop.wait(3)
                        continue
                    self.stop.wait(1)
                    continue
                self._dead = 0
                if self.on_telemetry:
                    try:
                        self.on_telemetry(self.read_telemetry(requests, bool(ground), float(agl), float(speed)))
                    except Exception:
                        log.exception("Telemetry callback failed")
                event = self.detector.feed(bool(ground), float(agl), float(speed))
                # on short final (500 ft after having been well above it): an early landing PA with the real local time, for
                # aircraft whose PAs are swapped files (FSLabs may play its After Landing PA before the aircraft has slowed down)
                if not ground:
                    if float(agl) > 1500:
                        self._final_done = False
                        self._was_high = True
                    elif float(agl) <= 500 and getattr(self, "_was_high", False) and not getattr(self, "_final_done", False):
                        self._final_done = True
                        local = requests.get("LOCAL_TIME")
                        if local is not None:
                            clock = round_clock(float(local))
                            log.info("500 ft or less on final - local time here is %s", clock)
                            self.on_event("final", clock)
                else:
                    self._was_high = False
                if event == "takeoff":
                    self.on_event("takeoff", None)
                elif event == "landed":
                    local = requests.get("LOCAL_TIME")
                    if local is None:
                        log.warning("Landed, but MSFS did not give the local time")
                        self.on_event("landed", None)
                    else:
                        clock = round_clock(float(local))
                        log.info("Landed and slowed down - local time here is %s (sim says %.0f s since midnight)", clock, float(local))
                        self.on_event("landed", clock)
                self.stop.wait(1)
            except Exception as exc:  # sim closed, connection lost...: try again later
                log.info("SimConnect: %s - will retry", exc)
                sm = requests = None
                self._report_aircraft(None)
                self.stop.wait(10)
