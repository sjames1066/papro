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
CRUISE_ALT_FT = 20000.0       # above this we count as having been at cruise
DESCENT_VS_FPM = -700.0
DESCENT_BELOW_MAX_FT = 3000.0 # this far below the highest altitude of the flight
LANDING_PREP_AGL_FT = 10000.0 # descending through this: cabin crew take your seats for landing
SEATBELT_ON_COOLDOWN_S = 600
SEATBELT_ON_MAX_DESCENT_FPM = 300.0   # the turbulence PA only when not descending faster than this
DEBOARD_AFTER_S = 5           # parked after landing for this long -> doors / deboarding
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
        self.gsx_boarding_seen = False
        self.joined = False         # the first reading has not been looked at yet (see join())

    # -- helpers ---------------------------------------------------------------------------------------
    def _fire(self, key: str) -> None:
        if key not in self.done:
            self.done.add(key)
            self.fire_cb(key)

    @staticmethod
    def _truthy(value):
        return None if value is None else bool(value)

    def join(self, r: dict, phase: str | None = None) -> None:
        """Called once with the first reading, before feed(): PAPro was opened (or the sim connected) part way through a flight.
        Everything that would already have been said is marked done without playing, so opening PAPro in the air does not
        start a welcome / safety / after-takeoff announcement."""
        self.joined = True
        ground = bool(r.get("on_ground"))
        gs = float(r.get("gs") or 0.0)
        vs = r.get("vs")
        alt = float(r.get("alt") or 0.0)
        before = ("welcome", "boarding_complete", "arm_doors", "pre_safety", "safety")
        takeoff = ("takeoff_seats", "takeoff_dim", "takeoff_secured", "after_takeoff")
        if not ground:
            self.done.update(before + takeoff)
            self.flown = True
            self.max_alt = alt
            if alt >= SEATBELT_FALLBACK_ALT_FT or phase in ("Climb", "Cruise", "Descent", "Approach") \
                    or (vs is not None and float(vs) < -300):
                self.done.update(("seatbelt_off", "cabin_service"))
            if vs is not None and float(vs) < -300:
                self.done.add("descent")   # already on the way down: no "we have started our descent"
        elif phase in ("Taxi in", "Deboarding", "Flight complete"):
            self.done.update(before + takeoff + ("seatbelt_off", "cabin_service", "descent", "landing_seats", "landing_dim",
                                                 "landing_secured", "after_landing"))
            self.flown = self.landed = True
            if gs < 1.0 and r.get("eng_running") is not True:   # already parked: all of it is over, say nothing
                self.done.update(("disarm_doors", "deboard_start", "deboard_complete"))
        elif gs >= SAFETY_SPEED_KT or phase in ("Pushback", "Taxi out"):
            self.done.update(before)    # already moving: the boarding PAs are over
        # Opened part way through: say the announcement for the closest phase (just that one), not the ones already behind you
        agl = float(r.get("agl") or 0.0)
        closest: tuple[str, ...] = ()
        if not ground:
            if phase == "Approach" or (vs is not None and float(vs) < -300 and agl <= LANDING_PREP_AGL_FT):
                closest = ("landing_seats", "landing_dim", "landing_secured")
            elif phase == "Descent" or (vs is not None and float(vs) < -300):
                closest = ("descent",)
            elif phase == "Cruise" or alt >= CRUISE_ALT_FT:   # (a fresh start has no history, so the height says where we are)
                closest = ("cabin_service",)
            elif agl < SEATBELT_OFF_MIN_AGL_FT and (vs is None or float(vs) > 0):
                closest = ("after_takeoff",)
            else:
                closest = ()
        elif phase in ("Taxi in", "Deboarding", "Flight complete"):
            closest = () if "deboard_complete" in self.done else ("after_landing",)
        elif phase == "Pushback":
            closest = ("boarding_complete",)
        elif gs >= SAFETY_SPEED_KT or phase == "Taxi out":
            closest = ("pre_safety", "safety")
        for key in closest:
            self.done.discard(key)
            self._fire(key)

    # -- the sim says ----------------------------------------------------------------------------------
    def boarding_done(self, t: float) -> None:
        """The Boarding Complete button: the announcement now, then the doors, safety briefing and so on follow as usual."""
        if not self.flown and "boarding_complete" not in self.done:
            self.done.add("welcome")
            self._fire("boarding_complete")
            self.t_boarding_done = t

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

        # ---- GSX boarding finished or aborted -> Boarding complete ---------------------------------------
        gsx_b = r.get("gsx_boarding") if getattr(self, "use_gsx", True) else None
        if gsx_b == GSX_PERFORMING:
            self.gsx_boarding_seen = True
        elif getattr(self, "gsx_boarding_seen", False) and gsx_b is not None and ground and not self.flown:
            self.gsx_boarding_seen = False
            self.boarding_done(t)
        # ---- on the ground before the flight -------------------------------------------------------------
        if ground and not self.flown:
            parked = gs < 1.0 and engines is not True
            if parked and "boarding_complete" not in self.done:
                self.t_idle = self.t_idle if self.t_idle is not None else t
                if t - self.t_idle >= WELCOME_AFTER_S and getattr(self, "boarding_ok", True):
                    self._fire("welcome")
            else:
                self.t_idle = None
            started = (beacon is True and self.prev_beacon is not True) if beacon is not None \
                else (brake is False and self.prev_brake is True)
            if started and "welcome" in self.done:
                self._fire("boarding_complete")
                self.t_boarding_done = t
            if self.t_boarding_done is not None and t - self.t_boarding_done >= ARM_DOORS_AFTER_S \
                    and (r.get("door_open") is not True or gs >= SAFETY_SPEED_KT):   # wait until every door is shut (moving: no more waiting)
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
                if off_edge:   # only when the aircraft's seatbelt sign can be read (no guessing from altitude)
                    self._fire("seatbelt_off")
                    self.t_seatbelt_off = t
                if getattr(self, "cruise_reached", False) and "descent" not in self.done:   # at cruise altitude (set from the phase tracker)
                    self._fire("cabin_service")
                if seatbelt is True and self.prev_seatbelt is False and "seatbelt_off" in self.done \
                        and t - self.t_last_seatbelt_on >= SEATBELT_ON_COOLDOWN_S:
                    self.t_last_seatbelt_on = t
                    if self.cruise_seen and "descent" in self.done:
                        pass   # the descent PA already covers it
                    elif getattr(self, "cruise_reached", False) and (vs is None or vs > -SEATBELT_ON_MAX_DESCENT_FPM):
                        self.fire_cb("seatbelt_on")   # turbulence: sign switched on at cruise, not descending
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
                    self._fire("disarm_doors")   # every engine is off (one running engine, e.g. single-engine taxi, is not parked)
                    gsx_d = r.get("gsx_deboarding") if getattr(self, "use_gsx", True) else None
                    try:
                        gsx_d = int(gsx_d) if gsx_d is not None else None
                    except (TypeError, ValueError):
                        gsx_d = None
                    if gsx_d is None or gsx_d >= 5 or "deboard_start" in self.done \
                            or (not self.gsx_boarding_seen and t - self.t_parked_after >= 60):   # with GSX: wait for it to begin deboarding
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
# The phase of flight shown on the Dashboard (any aircraft). Worked out from the sim readings below, and it only ever
# moves forward: nothing goes back (pushback -> boarding, say) until a new flight is loaded (reset()).
#
#   Boarding           GSX says boarding is under way (GSX integration on), or the "Start boarding" button was pressed
#   Boarding complete  GSX says boarding is done, or the parking brake comes off / the beacon goes on
#   Pushback           moving backwards (more than 110 degrees off the nose, judged after 10 m) slower than 12 kt
#   Taxi out           ground speed 5 kt or more (stopping at a holding point keeps it)
#   Initial climb      ground speed over 60 kt and airborne for 30 s
#   Climb              altitude has been increasing for 3 minutes
#   Cruise             at the planned cruise altitude (SimBrief), or level for 5 minutes
#   Descent            3000 ft below the highest altitude of the flight
#   Approach           descending within 25 NM of the destination
#   Taxi in            landed and ground speed under 60 kt
#   Deboarding         a door is open, or GSX says deboarding is under way

import math

PHASES = ["Preflight", "Boarding", "Boarding complete", "Pushback", "Taxi out", "Initial climb", "Climb", "Cruise",
          "Descent", "Approach", "Taxi in", "Deboarding", "Flight complete"]

# L:A32NX_FMGC_FLIGHT_PHASE (FlyByWire) numbering - kept for the probe report only
FMGC_PHASES = {0: "Preflight", 1: "Takeoff", 2: "Climb", 3: "Cruise", 4: "Descent", 5: "Approach", 6: "Approach", 7: "Arrived"}

PUSHBACK_MAX_KT = 12.0
PUSHBACK_BACKWARD_DEG = 110.0
PUSHBACK_MIN_MOVE_M = 10.0
TAXI_MIN_KT = 5.0
PUSHBACK_FORWARD_KT = 2.0       # moving forward this fast after a pushback -> Taxi out
INITIAL_CLIMB_KT = 60.0
INITIAL_CLIMB_AIRBORNE_S = 30.0
CLIMB_INCREASING_S = 180.0
CLIMB_VS_FPM = 200.0
CLIMB_GAP_S = 45.0               # a pause in the climb (level-off for a step) this short does not restart the 3 minutes
CRUISE_LEVEL_S = 300.0
CRUISE_LEVEL_BAND_FT = 150.0
CRUISE_MATCH_FT = 300.0
DESCENT_DROP_FT = 3000.0
APPROACH_NM = 25.0
AT_ORIGIN_NM = 5.0                 # on the ground this close to the departure airport (and far from the destination): the flight is yet to start
NOT_FLOWN_MAX_ALT_FT = 3000.0     # ...and it has never climbed above this: it has not flown
SEED_AT_DESTINATION_NM = 5.0     # opened on the ground within this of the destination airport: landed, taxiing in
LANDED_KT = 60.0
GSX_PERFORMING, GSX_COMPLETED = 5, 6
FLIGHT_COMPLETE_S = 180.0         # parked, engines off, nobody deboarding for this long -> Flight complete
PARKED_DEBOARD_S = 20.0          # stopped with the engines off this long after landing -> Deboarding, even with no GSX / door signal


def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 3440.065 * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def position_degrees(lat, lon, heading):
    """The sim library answers in radians (some builds in degrees): work out which and return degrees."""
    if lat is None or lon is None:
        return None, None, None
    lat, lon = float(lat), float(lon)
    if abs(lat) <= math.pi / 2 + 0.01 and abs(lon) <= math.pi + 0.01:   # radians
        return math.degrees(lat), math.degrees(lon), None if heading is None else math.degrees(float(heading)) % 360
    return lat, lon, None if heading is None else float(heading) % 360


class PhaseTracker:
    """feed() the ~1 Hz readings; phase() is the current name. Readings: t, on_ground, agl, gs (kt), alt, vs, lat, lon,
    heading (the sim's own units - see position_degrees), parking_brake, beacon, door_open, gsx_boarding, gsx_deboarding."""

    def __init__(self):
        self.use_gsx = True
        self.dest = None          # (lat, lon) of the destination airport, degrees
        self.origin = None        # (lat, lon) of the departure airport, degrees
        self.cruise_ft = None     # planned cruise altitude
        self.reset()

    def reset(self) -> None:
        """A new flight: start again from Preflight."""
        self.current = None
        self.rank = 0
        self.manual_boarding_flag = False
        self.manual_complete_flag = False
        self.gsx_was_boarding = False
        self.flown = False            # has been airborne at some point this flight
        self.air_since = None
        self.max_alt = 0.0
        self.inc_start = self.last_inc = None
        self.level_alt = self.level_since = None
        self.anchor = None            # (lat, lon) where movement is being measured from
        self.move_back = False
        self.prev_brake = None
        self.dist_nm = None

    def reset_to_preflight(self) -> None:
        """The Reset button: back to Preflight. A beacon that is already on does not count as "boarding complete" again."""
        self.reset()
        self.current = "Preflight"
        self.beacon_wait = True

    # -- outside help ----------------------------------------------------------------------------------
    def start_boarding(self) -> None:
        """The "Start boarding" button (aircraft with no GSX to say so)."""
        self.manual_boarding_flag = True

    def seed(self, r: dict) -> None:
        """Opened part way through a flight, so there is no history to work the phase out from: take it from one reading."""
        at_dep = None   # on the ground within reach of the departure airport (None: departure not known)
        if r.get("on_ground") and self.origin and r.get("lat") is not None:
            la, lo, _ = position_degrees(r.get("lat"), r.get("lon"), r.get("heading"))
            at_dep = la is not None and haversine_nm(la, lo, self.origin[0], self.origin[1]) <= AT_ORIGIN_NM
        if r.get("on_ground") and r.get("eng_all") is True and (at_dep if at_dep is not None else not (self.dist_nm is not None and self.dist_nm <= SEED_AT_DESTINATION_NM)):
            if self.rank <= PHASES.index("Taxi out") and not self.flown:   # both engines running on the ground: on the way out
                self.rank = PHASES.index("Taxi out")
                self.current = "Taxi out"
            return
        if r.get("on_ground") and self.origin and r.get("lat") is not None:
            lat0, lon0, _ = position_degrees(r.get("lat"), r.get("lon"), r.get("heading"))
            if lat0 is not None and haversine_nm(lat0, lon0, self.origin[0], self.origin[1]) <= AT_ORIGIN_NM:
                return   # parked at the departure airport: Preflight, whatever the destination says
        if r.get("on_ground"):   # no history: on the ground at the destination means we have landed (taxiing in), anywhere else it is the start
            if self.dist_nm is not None and self.dist_nm <= SEED_AT_DESTINATION_NM and self.rank <= PHASES.index("Taxi out") and not self.flown:
                self.flown = True
                parked = float(r.get("gs") or 0.0) < 1.0 and r.get("eng_running") is not True
                self.rank = PHASES.index("Flight complete" if parked else "Taxi in")
                self.current = "Flight complete" if parked else "Taxi in"
            return
        if self.rank > 0:
            return
        alt = float(r.get("alt") or 0.0)
        agl = float(r.get("agl") or 0.0)
        vs = float(r.get("vs") or 0.0)
        R = {n: i for i, n in enumerate(PHASES)}
        self.flown, self.max_alt = True, alt
        self.air_since = float(r.get("t", 0.0)) - INITIAL_CLIMB_AIRBORNE_S
        if vs < -300:
            near = self.dist_nm is not None and self.dist_nm <= APPROACH_NM
            name = "Approach" if near else "Descent"
        elif alt >= CRUISE_ALT_FT or (self.cruise_ft and abs(alt - self.cruise_ft) <= CRUISE_MATCH_FT):
            name = "Cruise"
        elif agl > 3000 or alt > 10000:
            name = "Climb"
        else:
            name = "Initial climb"
        self.rank = R[name]
        self.current = name

    def complete_boarding(self) -> None:
        """The "Boarding Complete" button (manual boarding)."""
        self.manual_complete_flag = True

    def set_flight(self, dest, cruise_ft) -> None:
        self.dest, self.cruise_ft = dest, cruise_ft

    def phase(self):
        return self.current

    def source(self) -> str:
        return "estimated"

    # -- helpers ---------------------------------------------------------------------------------------
    def _parked_since(self, t: float) -> float:
        """Seconds the aircraft has been stopped with the engines off (starts counting on the first call)."""
        if getattr(self, "parked_t", None) is None:
            self.parked_t = t
        return t - self.parked_t

    def _go(self, name: str) -> None:
        i = PHASES.index(name)
        if i > self.rank:
            self.rank = i

    def _backward(self, r, heading, lat, lon, gs) -> bool:
        """Is the aircraft moving backwards? Judged from where it went (at least 10 m), not from the speed sign."""
        if lat is None or heading is None:
            return self.move_back
        if self.anchor is None:
            self.anchor = (lat, lon)
            return self.move_back
        dy = (lat - self.anchor[0]) * 111320.0
        dx = (lon - self.anchor[1]) * 111320.0 * math.cos(math.radians(lat))
        if math.hypot(dx, dy) >= PUSHBACK_MIN_MOVE_M:
            course = bearing_deg(self.anchor[0], self.anchor[1], lat, lon)
            off = abs((course - heading + 180) % 360 - 180)
            self.move_back = off > PUSHBACK_BACKWARD_DEG
            self.anchor = (lat, lon)
        elif gs < 0.5:
            self.move_back = False if self.rank < PHASES.index("Pushback") else self.move_back
        return self.move_back

    @staticmethod
    def _int(v):
        try:
            return None if v is None else int(v)
        except (TypeError, ValueError):
            return None

    # -- the readings ----------------------------------------------------------------------------------
    def feed(self, r: dict, reported=None) -> None:
        t = float(r.get("t", 0.0))
        ground = bool(r.get("on_ground"))
        gs = float(r.get("gs") or 0.0)
        alt = float(r.get("alt") or 0.0)
        agl = float(r.get("agl") or 0.0)
        vs = r.get("vs")
        vs = float(vs) if vs is not None else 0.0
        lat, lon, heading = position_degrees(r.get("lat"), r.get("lon"), r.get("heading"))
        brake = None if r.get("parking_brake") is None else bool(r.get("parking_brake"))
        beacon = None if r.get("beacon") is None else bool(r.get("beacon"))
        if beacon is False:
            self.beacon_wait = False   # after a manual reset the beacon only counts once it has been seen off
        gsx_b = self._int(r.get("gsx_boarding")) if self.use_gsx else None
        gsx_d = self._int(r.get("gsx_deboarding")) if self.use_gsx else None
        R = {n: i for i, n in enumerate(PHASES)}

        if lat is not None and self.dest:
            self.dist_nm = haversine_nm(lat, lon, self.dest[0], self.dest[1])
        else:
            self.dist_nm = None
        self.origin_nm = haversine_nm(lat, lon, self.origin[0], self.origin[1]) if lat is not None and self.origin else None
        if ground and self.flown and self.origin_nm is not None and self.origin_nm <= AT_ORIGIN_NM and self.max_alt < NOT_FLOWN_MAX_ALT_FT \
                and (self.dist_nm is None or self.dist_nm > AT_ORIGIN_NM):
            # on the ground at the departure airport, far from the destination, and never really climbed: this is the start of the
            # flight (a bad first reading made it look flown), so Preflight - not Taxi in / Deboarding
            self.flown = False
            if self.rank >= PHASES.index("Initial climb") or self.rank >= PHASES.index("Taxi in"):
                self.rank = 0
            self.corrected = True    # the announcements marked as already said must be given back (see the PAPro loop)
            self.parked_t = None
        self.max_alt = max(self.max_alt, alt) if not ground else self.max_alt

        if ground:
            self.air_since = None
            if self.flown:
                # landed: slow enough -> Taxi in; a door open / GSX deboarding -> Deboarding
                if gs < LANDED_KT:
                    self._go("Taxi in")
                engines_off = r.get("eng_running") is False or (r.get("eng_running") is None and brake is True)
                parked = gs < 1.0 and engines_off and brake is not False
                if self.rank >= R["Taxi in"] and (r.get("door_open") or (gsx_d is not None and gsx_d >= GSX_PERFORMING)
                                                  or (parked and self._parked_since(t) >= PARKED_DEBOARD_S)):
                    self._go("Deboarding")
                if self.rank >= R["Deboarding"] and parked and not (gsx_d is not None and gsx_d >= GSX_PERFORMING) \
                        and self._parked_since(t) >= FLIGHT_COMPLETE_S:
                    self._go("Flight complete")
                if not parked:
                    self.parked_t = None
            else:
                # before the flight
                if gsx_b == GSX_PERFORMING:
                    self.gsx_was_boarding = True
                if gsx_b == GSX_PERFORMING or self.manual_boarding_flag:
                    self._go("Boarding")
                if getattr(self, "gsx_was_boarding", False) and gsx_b is not None and gsx_b != GSX_PERFORMING:
                    self.manual_complete_flag = True   # GSX finished or aborted the boarding
                released = self.prev_brake is True and brake is False
                if gsx_b == GSX_COMPLETED or (beacon is True and not getattr(self, "beacon_wait", False)) or released or self.manual_complete_flag:
                    self._go("Boarding complete")
                if self.rank >= R["Boarding complete"]:
                    back = self._backward(r, heading, lat, lon, gs)
                    if back and 0.0 < gs < PUSHBACK_MAX_KT:
                        self._go("Pushback")
                    elif not back and (gs >= TAXI_MIN_KT or (self.rank == R["Pushback"] and gs >= PUSHBACK_FORWARD_KT)):
                        self._go("Taxi out")   # after the pushback, a slow roll forward is already taxiing
                elif gs >= TAXI_MIN_KT:   # no boarding signals seen at all, but it is taxiing
                    self._go("Taxi out")
        else:
            self.air_since = self.air_since if self.air_since is not None else t
            self.flown = self.flown or agl > 50
            if gs > INITIAL_CLIMB_KT and t - self.air_since >= INITIAL_CLIMB_AIRBORNE_S:
                self._go("Initial climb")
            if self.rank >= R["Initial climb"]:
                # climb: altitude increasing for 3 minutes (short pauses allowed)
                if vs > CLIMB_VS_FPM:
                    self.last_inc = t
                    self.inc_start = self.inc_start if self.inc_start is not None else t
                elif self.last_inc is not None and t - self.last_inc > CLIMB_GAP_S:
                    self.inc_start = None
                if self.inc_start is not None and t - self.inc_start >= CLIMB_INCREASING_S:
                    self._go("Climb")
                # cruise: the planned altitude, or level for 5 minutes
                level_ok = False
                if self.level_alt is None or abs(alt - self.level_alt) > CRUISE_LEVEL_BAND_FT:
                    self.level_alt, self.level_since = alt, t
                elif t - self.level_since >= CRUISE_LEVEL_S:
                    level_ok = True
                at_plan = bool(self.cruise_ft) and alt >= self.cruise_ft - CRUISE_MATCH_FT
                if (at_plan or level_ok) and self.rank >= R["Initial climb"] and agl > 1000:
                    self._go("Cruise")
                # descent / approach
                if self.max_alt - alt >= DESCENT_DROP_FT:
                    self._go("Descent")
                if self.rank >= R["Initial climb"] and vs < -CLIMB_VS_FPM and agl > 0:
                    if self.dist_nm is not None and self.dist_nm <= APPROACH_NM and self.max_alt - alt >= 500:
                        self._go("Approach")
                    elif self.dist_nm is None and self.rank >= R["Descent"] and agl < 5000:
                        self._go("Approach")
        self.prev_brake = brake if brake is not None else self.prev_brake
        self.current = PHASES[self.rank]

    # -- what the Dashboard may offer ------------------------------------------------------------------
    def can_start_boarding(self) -> bool:
        return self.rank < PHASES.index("Boarding") and not self.flown

    def can_complete_boarding(self) -> bool:
        return self.manual_boarding_flag and self.rank == PHASES.index("Boarding") and not self.flown

    def can_disarm_doors(self) -> bool:
        return self.rank == PHASES.index("Taxi in")
