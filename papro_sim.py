"""Watch MSFS (through SimConnect) for the moment you have landed and slowed down, and read the local time there.

Everything that decides "landed and decelerated" is in LandingDetector, which knows nothing about the sim, so it can
be tested on its own. SimWatcher feeds it from SimConnect.
"""

from __future__ import annotations

import logging
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


SUPPORTED_MAKERS = ("fenix",)   # aircraft whose own PA sounds PAPro can replace (the Fenix A320 reads its announcements from a folder)


def classify_aircraft(title: str) -> dict | None:
    """What the sim says is loaded -> {"title": ..., "supported": bool}. Supported = PAs can be played in the sim by
    swapping the aircraft's own announcement files (the Fenix A32X series)."""
    title = " ".join(str(title or "").replace("\x00", " ").split())
    if not title or title.upper().startswith("ATCCOM."):   # an unresolved text key (the sim answered mid-load), not a name
        return None
    low = title.lower()
    return {"title": title, "supported": any(m in low for m in SUPPORTED_MAKERS)}


class SimWatcher:
    """Background thread: connects to MSFS when it is running and calls
    on_event("takeoff") or on_event("landed", "HH:MM") (local time where you landed)."""

    def __init__(self, on_event, running, stop: threading.Event, slow_kt: float = 60.0, on_aircraft=None, on_telemetry=None):
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
            "beacon": g("LIGHT_BEACON", "LIGHT_BEACON_ON"),
            "landing_light": g("LIGHT_LANDING", "LIGHT_LANDING_ON"),
            "seatbelt": (lambda p: p if p is not None else g("CABIN_SEATBELTS_ALERT_SWITCH"))(self._pmdg_seatbelt()),
            "eng_running": any(known) if known else None,
            "fmgc_phase": self._lvar(requests, "L:A32NX_FMGC_FLIGHT_PHASE"),
            "lat": g("PLANE_LATITUDE"), "lon": g("PLANE_LONGITUDE"),
            "heading": g("PLANE_HEADING_DEGREES_TRUE", "PLANE_HEADING_DEGREES_MAGNETIC"),
            "door_open": self._doors_open(requests),
            "gsx_boarding": self._lvar(requests, "L:FSDT_GSX_BOARDING_STATE"),      # 5 = boarding, 6 = done
            "gsx_deboarding": self._lvar(requests, "L:FSDT_GSX_DEBOARDING_STATE"),  # 5 = deboarding
        }

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
                        info = {"title": "Unnamed aircraft", "supported": False}
                else:
                    self._unnamed = 0
                self._report_aircraft(info)
                if ground is None or agl is None or speed is None:
                    self.stop.wait(1)
                    continue
                if self.on_telemetry:
                    try:
                        self.on_telemetry(self.read_telemetry(requests, bool(ground), float(agl), float(speed)))
                    except Exception:
                        log.exception("Telemetry callback failed")
                event = self.detector.feed(bool(ground), float(agl), float(speed))
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
