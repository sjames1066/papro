"""`papro.exe --probe-aircraft [NAME ...]`: with MSFS running and the aircraft loaded, write probe-report.txt listing
what the aircraft answers to through SimConnect, so the PA-knob and the flight-phase triggers can be mapped for it.
Extra names (e.g. L:SomeKnob) can be given on the command line and are tried as well. Read-only: changes nothing."""

from __future__ import annotations

import time
from pathlib import Path

STANDARD = [
    "TITLE", "ATC_MODEL", "ATC_TYPE", "ATC_AIRLINE", "SIM_ON_GROUND", "PLANE_ALT_ABOVE_GROUND", "GROUND_VELOCITY",
    "VERTICAL_SPEED", "INDICATED_ALTITUDE", "PLANE_ALTITUDE", "BRAKE_PARKING_POSITION", "BRAKE_PARKING_INDICATOR",
    "LIGHT_BEACON", "LIGHT_LANDING", "LIGHT_STROBE", "LIGHT_NAV", "CABIN_SEATBELTS_ALERT_SWITCH",
    "CABIN_NO_SMOKING_ALERT_SWITCH", "GENERAL_ENG_COMBUSTION:1", "GENERAL_ENG_COMBUSTION:2", "EXIT_OPEN:0",
    "EXIT_OPEN:1", "AUDIO_PANEL_VOLUME", "COM_VOLUME", "LOCAL_TIME", "FLAPS_HANDLE_INDEX", "GEAR_HANDLE_POSITION",
]
# LVAR names tried for the captain's PA volume / selector - guesses to be confirmed from the report
LVARS = [
    "L:PMDG_PA_Volume", "L:switch_PA_volume", "L:SWITCH_AUDIO_PA", "L:AUDIO_PA_VOLUME", "L:PMDG_AUDIO_PA",
    "L:AudioPanel_PA", "L:PA_KNOB", "L:PA_VOLUME", "L:PA_Volume", "L:ACP_CAPT_PA_VOL", "L:ACP_CAPT_PA_KNOB",
]


def run(extra: list[str], out: Path) -> str:
    lines = [f"PAPro aircraft probe - {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    try:
        from SimConnect import AircraftRequests, SimConnect
    except Exception as exc:
        lines.append(f"SimConnect library not available: {exc}")
        out.write_text("\n".join(lines), encoding="utf-8")
        return "\n".join(lines)
    try:
        sm = SimConnect()
        requests = AircraftRequests(sm, _time=200)
    except Exception as exc:
        lines.append(f"Could not connect to MSFS: {exc}  (start MSFS, load the aircraft in a flight, then run this again)")
        out.write_text("\n".join(lines), encoding="utf-8")
        return "\n".join(lines)
    lines.append("Connected to MSFS.")
    for name in STANDARD + LVARS + list(extra):
        try:
            value = requests.get(name)
            if isinstance(value, (bytes, bytearray)):
                value = bytes(value).decode("utf-8", "replace")
            lines.append(f"{name:40} = {value!r}")
        except Exception as exc:
            lines.append(f"{name:40} ! {type(exc).__name__}: {exc}")
    lines.append("")
    lines.append("Now move the captain's PA knob / switch, run this again with the names that changed, and send both reports.")
    out.write_text("\n".join(lines), encoding="utf-8")
    return "\n".join(lines)
