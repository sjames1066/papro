"""Which cabin announcement is due, worked out from what the sim reports - for aircraft whose PAs PAPro plays itself.

PhaseEngine knows nothing about the sim or about sound: feed() it a reading about once a second and it calls
fire(key) when an announcement is due. Each one is said once per flight (a few can repeat after a pause). The rules
are deliberately simple and tuned at the top of the file, so they can be refined flight by flight. A reading may lack
a value (None) - the aircraft doesn't report it - and then the rule falls back to something coarser or waits.

Keys: welcome, boarding_complete, arm_doors, pre_safety, safety, takeoff_seats, takeoff_dim, takeoff_secured,
after_takeoff, seatbelt_off, cabin_service, seatbelt_on, descent, landing_seats, landing_dim, landing_secured,
after_landing, disarm_doors, deboard_start, deboard_complete.
"""

from __future__ import annotations

from typing import Callable

WELCOME_AFTER_S = 15          # parked, engines off for this long -> the welcome aboard
ARM_DOORS_AFTER_S = 8         # after "boarding complete"
SAFETY_SPEED_KT = 2.0         # moving (pushback / taxi) after boarding complete -> safety briefing
LINEUP_MAX_KT = 35.0          # landing light on at or below this ground speed, before takeoff -> take seats / lights / cabin secure
AFTER_TAKEOFF_AGL_FT = 1500.0
SEATBELT_OFF_MIN_AGL_FT = 3000.0
SEATBELT_FALLBACK_ALT_FT = 10000.0   # no seatbelt switch reading: the signs go off climbing through this
CABIN_SERVICE_AFTER_S = 480   # after the seatbelt signs go off
CRUISE_ALT_FT = 20000.0       # above this we count as having been at cruise
DESCENT_VS_FPM = -700.0
DESCENT_BELOW_MAX_FT = 3000.0 # this far below the highest altitude of the flight
LANDING_PREP_AGL_FT = 10000.0 # descending through this: cabin crew take your seats for landing
SEATBELT_ON_COOLDOWN_S = 600
DEBOARD_AFTER_S = 20          # parked after landing for this long -> doors / deboarding
DEBOARD_COMPLETE_AFTER_S = 120


class PhaseEngine:
    def __init__(self, fire: Callable[[str], None]):
        self.fire_cb = fire
        self.reset()

    def reset(self) -> None:
        """A new flight: everything can be said again."""
        self.done: set[str] = set()
        self.t_idle = None          # since when parked with engines off
        self.t_seatbelt_off = None
        self.t_deboard = None
        self.t_parked_after = None
        self.t_boarding_done = None
        self.t_last_seatbelt_on = -1e9
        self.flown = False          # has been properly airborne this flight
        self.landed = False
        self.max_alt = 0.0
        self.cruise_seen = False
        self.prev_beacon = None
        self.prev_brake = None
        self.prev_seatbelt = None
        self.flight_clock = None

    # -- helpers ---------------------------------------------------------------------------------------
    def _fire(self, key: str) -> None:
        if key not in self.done:
            self.done.add(key)
            self.fire_cb(key)

    @staticmethod
    def _truthy(value):
        return None if value is None else bool(value)

    # -- the sim says ----------------------------------------------------------------------------------
    def landed_event(self, t: float) -> None:
        """The landing detector says we are down and slow: the landing PA, then wait to be parked."""
        if "after_landing" not in self.done and self.flown:
            self.landed = True
            self._fire("after_landing")
            self.t_parked_after = None

    def feed(self, r: dict) -> None:
        t = float(r.get("t", 0.0))
        ground = bool(r.get("on_ground"))
        agl = float(r.get("agl") or 0.0)
        gs = float(r.get("gs") or 0.0)
        vs = r.get("vs")
        alt = float(r.get("alt") or 0.0)
        brake = self._truthy(r.get("parking_brake"))
        beacon = self._truthy(r.get("beacon"))
        landing_light = self._truthy(r.get("landing_light"))
        seatbelt = self._truthy(r.get("seatbelt"))
        engines = self._truthy(r.get("eng_running"))

        self.max_alt = max(self.max_alt, alt)
        if alt >= CRUISE_ALT_FT:
            self.cruise_seen = True

        # ---- on the ground before the flight -------------------------------------------------------------
        if ground and not self.flown:
            parked = gs < 1.0 and engines is not True
            if parked and "boarding_complete" not in self.done:
                self.t_idle = self.t_idle if self.t_idle is not None else t
                if t - self.t_idle >= WELCOME_AFTER_S:
                    self._fire("welcome")
            else:
                self.t_idle = None
            started = (beacon is True and self.prev_beacon is not True) if beacon is not None \
                else (brake is False and self.prev_brake is True)
            if started and "welcome" in self.done:
                self._fire("boarding_complete")
                self.t_boarding_done = t
            if self.t_boarding_done is not None and t - self.t_boarding_done >= ARM_DOORS_AFTER_S:
                self._fire("arm_doors")
            if "arm_doors" in self.done and gs >= SAFETY_SPEED_KT:
                self._fire("pre_safety")
                self._fire("safety")
            if landing_light is True and gs <= LINEUP_MAX_KT and "safety" in self.done:
                self._fire("takeoff_seats")
                self._fire("takeoff_dim")
                self._fire("takeoff_secured")
        # ---- in the air --------------------------------------------------------------------------------
        if not ground:
            if agl >= AFTER_TAKEOFF_AGL_FT and (vs is None or vs > 0) and not self.cruise_seen_descending(vs, alt):
                if not self.flown:
                    self.flown = True
                self._fire("after_takeoff")
            if self.flown:
                off_edge = seatbelt is False and self.prev_seatbelt is True and agl >= SEATBELT_OFF_MIN_AGL_FT
                off_fallback = seatbelt is None and alt >= SEATBELT_FALLBACK_ALT_FT and "seatbelt_off" not in self.done
                if off_edge or off_fallback:
                    self._fire("seatbelt_off")
                    self.t_seatbelt_off = t
                if self.t_seatbelt_off is not None and t - self.t_seatbelt_off >= CABIN_SERVICE_AFTER_S:
                    self._fire("cabin_service")
                if seatbelt is True and self.prev_seatbelt is False and "seatbelt_off" in self.done \
                        and t - self.t_last_seatbelt_on >= SEATBELT_ON_COOLDOWN_S:
                    self.t_last_seatbelt_on = t
                    if self.cruise_seen and "descent" in self.done:
                        pass   # the descent PA already covers it
                    else:
                        self.fire_cb("seatbelt_on")
                if self.cruise_seen and vs is not None and vs <= DESCENT_VS_FPM and alt <= self.max_alt - DESCENT_BELOW_MAX_FT:
                    self._fire("descent")
                if self.cruise_seen and "descent" in self.done and agl <= LANDING_PREP_AGL_FT:
                    self._fire("landing_seats")
                    self._fire("landing_dim")
                    self._fire("landing_secured")
                elif not self.cruise_seen and vs is not None and vs <= -300 and agl <= LANDING_PREP_AGL_FT and agl > 500 and self.flown \
                        and "seatbelt_off" in self.done:
                    self._fire("descent")   # a short flight that never reached 20,000 ft
                    self._fire("landing_seats")
                    self._fire("landing_dim")
                    self._fire("landing_secured")
        # ---- parked after landing ----------------------------------------------------------------------
        if ground and self.landed:
            parked = gs < 1.0 and engines is not True
            if brake is True and engines is not True:
                parked = True
            if parked:
                self.t_parked_after = self.t_parked_after if self.t_parked_after is not None else t
                if t - self.t_parked_after >= DEBOARD_AFTER_S:
                    self._fire("disarm_doors")
                    self._fire("deboard_start")
                    self.t_deboard = self.t_deboard if self.t_deboard is not None else t
                if self.t_deboard is not None and t - self.t_deboard >= DEBOARD_COMPLETE_AFTER_S:
                    self._fire("deboard_complete")
            else:
                self.t_parked_after = None

        self.prev_beacon, self.prev_brake, self.prev_seatbelt = beacon, brake, seatbelt

    def cruise_seen_descending(self, vs, alt) -> bool:
        """Is this the way down rather than the way up? (Only the climb triggers the after-takeoff PA.)"""
        return self.cruise_seen and vs is not None and vs < 0


# ---------------------------------------------------------------------------------------------------------
# The phase of flight shown on the Dashboard (any aircraft). Taken from the aircraft's own flight computer when it
# reports one (reported_phase), otherwise worked out from the same sim readings the announcements use.

PHASES = ["Preflight", "Taxi out", "Takeoff", "Climb", "Cruise", "Descent", "Approach", "Landing", "Taxi in", "Arrived"]

# L:A32NX_FMGC_FLIGHT_PHASE (FlyByWire) numbering; other aircraft reporting this way would need their own table
FMGC_PHASES = {0: "Preflight", 1: "Takeoff", 2: "Climb", 3: "Cruise", 4: "Descent", 5: "Approach", 6: "Approach", 7: "Arrived"}


class PhaseTracker:
    """feed() the ~1 Hz readings; phase() is the current name, source() is "aircraft" or "estimated"."""

    CONFIRM = 3   # the same answer this many readings in a row before the display changes

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.current = None
        self.src = "estimated"
        self.flown = False
        self.cruised = False
        self.max_alt = 0.0
        self.t_arrived = None
        self._cand, self._n = None, 0

    def phase(self):
        return self.current

    def source(self) -> str:
        return self.src

    def estimate(self, r: dict) -> str:
        ground = bool(r.get("on_ground"))
        gs = float(r.get("gs") or 0.0)
        agl = float(r.get("agl") or 0.0)
        alt = float(r.get("alt") or 0.0)
        vs = r.get("vs")
        vs = float(vs) if vs is not None else 0.0
        self.max_alt = max(self.max_alt, alt)
        if not ground:
            self.flown = self.flown or agl > 300
            if alt >= CRUISE_ALT_FT:
                self.cruised = True
            if not self.cruised and not self.flown_far(agl):
                return "Takeoff" if vs > 200 and agl < 1500 else "Climb"
            if vs >= 300 and not self.descending(alt):
                return "Climb"
            if vs <= -300 or self.descending(alt):
                return "Approach" if agl < 3000 and self.cruised_or_long() else "Descent"
            return "Cruise" if alt >= 10000 else "Climb"
        if not self.flown:
            if gs >= 40:
                return "Takeoff"
            return "Taxi out" if gs >= 1 else "Preflight"
        if gs >= 40:
            return "Landing"
        if gs >= 1:
            return "Taxi in"
        return "Arrived"

    def flown_far(self, agl: float) -> bool:
        return agl >= 1500

    def descending(self, alt: float) -> bool:
        return self.cruised and alt < self.max_alt - 1500

    def cruised_or_long(self) -> bool:
        return self.cruised or self.max_alt > 8000

    def feed(self, r: dict, reported: str | None = None) -> None:
        if reported:
            name, src = reported, "aircraft"
            self.estimate(r)   # keep the flown / cruised memory going
        else:
            name, src = self.estimate(r), "estimated"
        t = float(r.get("t", 0.0))
        # a minute parked after arriving: the next flight starts from the beginning
        if name == "Arrived":
            self.t_arrived = self.t_arrived if self.t_arrived is not None else t
            if t - self.t_arrived > 300 and self.current == "Arrived" and r.get("eng_running") is True:
                self.reset()
                name = "Preflight"
        else:
            self.t_arrived = None
        if name == self._cand:
            self._n += 1
        else:
            self._cand, self._n = name, 1
        if self.current is None or (name != self.current and self._n >= self.CONFIRM) or src != self.src:
            self.current, self.src = name, src
