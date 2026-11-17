"""PAPro Studio: record or upload the pieces of an airline pack, give them the cabin-PA sound, set them to the
loudness of the original Fenix announcements and export a ready-to-share pack zip.

No GUI code here; papro.py calls these functions and papro_ui.py shows them. A project lives in
<studio>/<ICAO>/<voice>/ with raw/ (what was recorded or uploaded, untouched), pack/ (the processed pack, laid out like
Airlines\\<ICAO>) and project.json.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import statistics
import sys
import zipfile
from pathlib import Path

import papro_audio

DEFAULT_TARGET = -18.9     # LUFS every clip is set to (tested in the sim against the original Fenix announcements)
PACKS_REPO = "sjames1066/fcac-packs"   # where exported packs are published as releases (tag = the zip name without .zip)
EFFECT_VERSION = 3         # bump when the PA sound changes: projects are re-processed once when opened
MAX_UPLOAD = 60 * 1024 * 1024
AUDIO_EXT = {".wav", ".ogg", ".mp3", ".m4a", ".flac", ".webm", ".opus", ".aac"}
AIRPORT = re.compile(r"^[A-Z0-9]{4}(_[A-Z0-9]{1,8})?$")
MUSIC_NAME = re.compile(r"^\w[\w .&'()-]{0,59}$")
AIRCRAFT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .\-/]{0,29}$")
AIRCRAFT_PRESETS = ["Airbus A319", "Airbus A320", "Airbus A321", "Airbus A330", "Airbus A350", "Airbus A380", "Boeing 737",
                    "Boeing 737 MAX", "Boeing 777", "Boeing 787", "Embraer 175", "Embraer 195"]
# Aircraft-specific PAs: a custom take of a PA that only plays for some aircraft types (ICAO type designators). The types are
# part of the file name - SafetyBriefing@A320+A319.ogg - so they travel with the pack. ALL = every aircraft.
TYPED_BASES = {"SafetyBriefing": "Safety demonstration", "PreSafetyBriefing": "Pre-safety briefing",
               "Modular_Welcome": "Boarding welcome - the rest"}
TYPED_RE = re.compile(r"^(?P<base>[A-Za-z_]+)@(?P<types>[A-Za-z0-9+]+)(?:~v(?P<n>\d+))?$")
TYPE_CODE = re.compile(r"^[A-Z0-9]{2,4}$")
ICAO_TYPES = frozenset("""
A124 A140 A148 A158 A19N A20N A21N A225 A306 A30B A310 A318 A319 A320 A321 A332 A333 A337 A338 A339 A342 A343 A345
A346 A359 A35K A388 A3ST A748 AC68 AC90 AN12 AN24 AN26 AN28 AN30 AN32 AN72 AT43 AT44 AT45 AT46 AT72 AT73 AT75 AT76 ATP
B190 B212 B37M B38M B39M B3XM B461 B462 B463 B701 B703 B712 B720 B721 B722 B731 B732 B733 B734 B735 B736 B737 B738
B739 B741 B742 B743 B744 B748 B74D B74R B74S B752 B753 B762 B763 B764 B772 B773 B778 B779 B77L B77W B788 B789 B78X
BA11 BCS1 BCS3 BE20 BE55 BE58 BE99 BE9L BELF BLCF BN2P C130 C152 C172 C182 C208 C210 C25B C25C C500 C510 C525 C550
C56X C680 C700 C750 C919 CL2T CL30 CL44 CL60 CONC CONI CRJ1 CRJ2 CRJ7 CRJ9 CRJX D228 D328 DA40 DA42 DC10 DC3 DC6 DC85
DC86 DC87 DC91 DC92 DC93 DC94 DC95 DH2T DH8A DH8B DH8C DH8D DH8E DHC2 DHC3 DHC4 DHC6 DHC7 DOVE E110 E120 E135 E145
E170 E175 E190 E195 E290 E295 E35L E50P E545 E550 E55P E75L E75S F100 F27 F28 F2TH F50 F70 F900 FA50 FA7X G159 G280
GL5T GL7T GLEX GLF4 GLF5 GLF6 H25B H25C HERN I114 IL18 IL62 IL76 IL86 IL96 J328 JS31 JS32 JS41 L101 L188 L410 LJ35
LJ45 LJ60 MD11 MD81 MD82 MD83 MD87 MD88 MD90 N262 P28A P28B P68 PA31 PA44 PA46 PC12 PC24 PC6T RJ1H RJ70 RJ85 S210 S58T
S601 S61 S76 S92 SB20 SC7 SF34 SH33 SH36 SR22 SU25 SU95 T134 T144 T154 T204 TBM9 TRIS Y12 YK40 YK42 YS11
""".split())   # ICAO aircraft type designators (Doc 8643) for airliners, regional, turboprop and common GA types
AIRCRAFT_TYPES = sorted(ICAO_TYPES)


def typed_stem(base: str, types: list[str], n: int = 1) -> str:
    return f"{base}@{'+'.join(types)}" + (f"~v{n}" if n > 1 else "")


def parse_typed(stem: str):
    """'SafetyBriefing@A320+A319~v2' -> ('SafetyBriefing', ['A320', 'A319'], 2); None for anything else."""
    m = TYPED_RE.match(stem)
    if not m or m["base"] not in TYPED_BASES:
        return None
    return m["base"], [t.upper() for t in m["types"].split("+") if t], int(m["n"] or 1)


def clean_types(types, strict: bool = True) -> list[str]:
    """Validated, de-duplicated type list. ALL on its own means every aircraft; at least one type is required.
    strict: only real ICAO type designators are accepted (a pack from someone else is read leniently)."""
    out: list[str] = []
    for t in types or []:
        t = str(t).strip().upper()
        if not t:
            continue
        if t != "ALL" and (not TYPE_CODE.match(t) or (strict and t not in ICAO_TYPES)):
            raise StudioError(f"'{t}' is not an ICAO aircraft type (use codes like A320, B738 or B38M).")
        if t not in out:
            out.append(t)
    if not out or "ALL" in out:
        return ["ALL"]
    return sorted(out)[:12]


LANGUAGES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese", "nl": "Dutch",
             "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish", "is": "Icelandic", "pl": "Polish", "cs": "Czech",
             "sk": "Slovak", "hu": "Hungarian", "ro": "Romanian", "bg": "Bulgarian", "el": "Greek", "tr": "Turkish", "ru": "Russian",
             "uk": "Ukrainian", "ar": "Arabic", "he": "Hebrew", "hi": "Hindi", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
             "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay", "tl": "Filipino"}


def clean_language(code) -> str:
    code = str(code or "").strip().lower()
    if code and code not in LANGUAGES:
        raise StudioError(f"'{code}' is not a language PAPro knows.")
    return code


EXTRA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")

GENERAL = [  # (file name, label, what to say). The file names are Fenix's announcement types; every one is optional except the modular pair.
    ("Modular_Welcome", "Welcome - part 2: after the airport name", "Everything in the welcome PA after the destination: seatbelts, safety, enjoy your flight..."),
    ("Modular_Landing", "Landing - part 2: after the local time", "Everything in the landing PA after the local time: cabin crew seats, seatbelt sign, belongings, thank you..."),
    ("BoardingWelcome[Morning]", "While boarding - welcome, morning (generic)", "Generic welcome aboard, good morning. Played every 2-6 min during boarding."),
    ("BoardingWelcome[Afternoon]", "While boarding - welcome, afternoon (generic)", "Generic welcome aboard, good afternoon."),
    ("BoardingWelcome[Evening]", "While boarding - welcome, evening (generic)", "Generic welcome aboard, good evening."),
    ("BoardingWelcome[Night]", "While boarding - welcome, night (generic)", "Generic welcome aboard for boardings between midnight and 6 am."),
    ("BoardingWelcome[Refueling]", "While boarding - welcome, refuelling (generic)", "Generic welcome aboard while the aircraft is being refuelled."),
    ("BoardingComplete", "Boarding is complete", "The purser announces that boarding has completed."),
    ("ArmDoors", "Doors to automatic (arm doors)", "Cabin crew, arm doors and cross-check."),
    ("DisarmDoors", "Doors to manual (disarm doors)", "Cabin crew, disarm doors and cross-check."),
    ("PreSafetyBriefing", "Safety briefing - introduction", "The opening of the safety briefing, before the demonstration."),
    ("SafetyBriefing", "Safety briefing - demonstration", "The safety demonstration."),
    ("CrewTakeSeats[PreFlight]", "Before takeoff - crew, take your seats", "Cabin crew, take your seats for takeoff. (Tagged [PreFlight]: used before takeoff.)"),
    ("CrewTakeSeats[Descent][Approach]", "Before landing - crew, take your seats", "Cabin crew, take your seats for landing. (Tagged [Descent][Approach]: used before landing.)"),
    ("CabinDim[PreFlight]", "Before takeoff - cabin lights dimmed", "We will dim the cabin lights for takeoff. (Tagged [PreFlight]: used before takeoff.)"),
    ("CabinDim[Descent][Approach]", "Before landing - cabin lights dimmed", "We will dim the cabin lights for landing. (Tagged [Descent][Approach]: used before landing.)"),
    ("CabinSecured[PreFlight]", "Before takeoff - cabin secure report", "Interphone to the cockpit: cabin secure for takeoff. (Tagged [PreFlight].)"),
    ("CabinSecured[Descent][Approach]", "Before landing - cabin secure report", "Interphone to the cockpit: cabin secure for landing. (Tagged [Descent][Approach].)"),
    ("AfterTakeoff", "After takeoff - welcome and flight details", "PA after takeoff, once past acceleration altitude."),
    ("SeatbeltSignOn", "Seatbelt sign switched on", "The seatbelt signs have been switched on."),
    ("SeatbeltSignOff", "Seatbelt sign switched off", "The seatbelt signs have been switched off."),
    ("CabinService", "Cabin service begins", "Cabin service is about to begin."),
    ("DescentStarted", "Descent has started", "We have started our descent."),
    ("AfterLanding", "After landing - generic (no airport name)", "Generic 'welcome to our destination', used where an airport has no recording."),
    ("DeboardingStarted", "Deboarding has started", "Passengers may now disembark."),
    ("DeboardingComplete", "Deboarding is complete", "Deboarding has completed."),
    ("Decompression", "Emergency - oxygen masks (decompression)", "Oxygen masks have deployed - emergency descent instructions."),
]
GREETINGS = [("Welcome_Morning", "Welcome - part 1: good morning", "Ladies and gentlemen, welcome on board this [airline] flight to..."),
             ("Welcome_Afternoon", "Welcome - part 1: good afternoon", "Ladies and gentlemen, welcome on board this [airline] flight to..."),
             ("Welcome_Evening", "Welcome - part 1: good evening", "Ladies and gentlemen, welcome on board this [airline] flight to..."),
             ("Welcome_Night", "Welcome - part 1: good evening, night (optional)", "Ladies and gentlemen, welcome on board this [airline] flight to... (for boardings between midnight and 6 am; without it the evening greeting is used)"),
             ("WelcomeTo", "Landing - part 1: 'welcome to...' (before the airport name)", "Ladies and gentlemen, welcome to... (the airport name is added next)"),
             ("LocalTime", "Landing - part 1: 'where the local time is...'", "...where the local time is")]
HOURS = ["twelve", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven"]
MINUTES = {0: "o'clock", 5: "oh five", 10: "ten", 15: "fifteen", 20: "twenty", 25: "twenty-five", 30: "thirty",
           35: "thirty-five", 40: "forty", 45: "forty-five", 50: "fifty", 55: "fifty-five"}


# PAs that can have extra takes; PAPro picks one at random each flight. Every announcement in the main folder, except the
# ones PAPro builds itself from parts (the BoardingWelcome set and the generic AfterLanding): vary Modular_Welcome /
# Modular_Landing for those.
VARIANT_BASES = ("Modular_Welcome", "Modular_Landing", "Welcome_Morning", "Welcome_Afternoon", "Welcome_Evening", "Welcome_Night",
                 "Fuel_Tag") + tuple(stem for stem, _, _ in GENERAL[2:] if stem != "AfterLanding")
VARIANT_FOLDERS = {"Welcome_Morning": "time", "Welcome_Afternoon": "time", "Welcome_Evening": "time", "Welcome_Night": "time",
                   "Fuel_Tag": "fuel"}   # where the takes of a PA live when it is not in main


def variant_folder(base: str) -> str:
    return VARIANT_FOLDERS.get(base, "main")
MODE_SLOTS = ("CabinService", "AfterTakeoff", "SafetyBriefing", "Decompression")  # Prerecorded by default; "Manual" adds [Manual] to the file name
MODES = ("Prerecorded", "Manual")


# Fenix's old announcement names -> the new type and the flight-phase tags that keep the takeoff / landing split.
# The mapping is inferred from the names and the trigger table in the Fenix developer guide (it publishes no list).
BEFORE_TAKEOFF = ["PreFlight"]
BEFORE_LANDING = ["Descent", "Approach"]
LEGACY = {
    "CrewSeatsTakeoff": ("CrewTakeSeats", BEFORE_TAKEOFF), "CrewSeatsLanding": ("CrewTakeSeats", BEFORE_LANDING),
    "CallCabinSecureTakeoff": ("CabinSecured", BEFORE_TAKEOFF), "CallCabinSecureLanding": ("CabinSecured", BEFORE_LANDING),
    "CabinDimTakeoff": ("CabinDim", BEFORE_TAKEOFF),
    "FastenSeatbelt": ("SeatbeltSignOn", []), "DescentSeatbelts": ("DescentStarted", []),
    "DisembarkStarted": ("DeboardingStarted", []),
}


LOGO_MAX = 5 * 1024 * 1024


def logo_path(root: Path, project: dict) -> Path:
    return project_dir(root, project["icao"], project["voice"]) / "logo.png"


def set_logo(root: Path, project: dict, body: bytes) -> None:
    """Keep the pack's logo as a PNG (at most 512 px). PAPro shows it in the pack list and the window header."""
    import io
    from PIL import Image
    if not body or len(body) > LOGO_MAX:
        raise StudioError("That image is empty or too large (5 MB limit).")
    try:
        image = Image.open(io.BytesIO(body))
        image.load()
    except Exception:
        raise StudioError("That is not an image PAPro can read. Use a png, jpg or webp.")
    image = image.convert("RGBA")
    image.thumbnail((512, 512))
    path = logo_path(root, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")


def remove_logo(root: Path, project: dict) -> None:
    logo_path(root, project).unlink(missing_ok=True)


def raw_takes(raw_dir: Path, slot: dict) -> list[Path]:
    """The raw recordings of a slot. Compared by name, not by glob: stems like CrewTakeSeats[PreFlight] contain [ ]."""
    prefix = f"{slot['folder']}__{slot['stem']}"
    return [p for p in raw_dir.iterdir() if p.is_file() and p.stem == prefix] if raw_dir.is_dir() else []


def base_of(stem: str) -> str:
    return re.split(r"[\[~]", stem, maxsplit=1)[0]


def legacy_renames(folder: Path) -> list[tuple[str, str]]:
    """(old file name, new file name) for every legacy-named clip in a folder; skips ones whose new name is taken."""
    if not folder.is_dir():
        return []
    taken = {p.name.lower() for p in folder.iterdir()}
    out = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".ogg":
            continue
        base = base_of(path.stem)
        if base not in LEGACY:
            continue
        new_base, tags = LEGACY[base]
        rest = path.stem[len(base):]                       # existing [tags] and ~custom name
        head, tilde, name = rest.partition("~")
        extra = "".join(f"[{t}]" for t in tags if f"[{t.lower()}]" not in head.lower())
        new = f"{new_base}{head}{extra}{tilde}{name}.ogg"
        if new.lower() not in taken:
            out.append((path.name, new))
    return out


class StudioError(Exception):
    pass


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip()).strip("_")[:40] or "Voice"


def project_dir(root: Path, icao: str, voice: str) -> Path:
    return root / icao / slug(voice)


def discard(root: Path, icao: str, voice: str) -> None:
    """Delete a project (recordings, processed clips, details). Never touches the Airlines library."""
    folder = project_dir(root, icao, voice)
    if not (folder / "project.json").is_file() or root.resolve() not in folder.resolve().parents:
        raise StudioError("That project does not exist.")
    shutil.rmtree(folder)
    parent = folder.parent
    if parent != root and not any(parent.iterdir()):
        parent.rmdir()


def list_projects(root: Path) -> list[dict]:
    found = []
    for file in sorted(root.glob("*/*/project.json")) if root.is_dir() else []:
        try:
            info = json.loads(file.read_text(encoding="utf-8"))
            found.append({"icao": info["icao"], "voice": info["voice"], "airline": info.get("airline", "")})
        except (OSError, ValueError, KeyError):
            continue
    return found


def load(root: Path, icao: str, voice: str) -> dict:
    path = project_dir(root, icao, voice) / "project.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise StudioError("That project does not exist.")


def save(root: Path, project: dict) -> None:
    folder = project_dir(root, project["icao"], project["voice"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "project.json").write_text(json.dumps(project, indent=2), encoding="utf-8")


def create(root: Path, icao: str, airline: str, voice: str) -> dict:
    icao, voice, airline = icao.strip().upper(), voice.strip(), airline.strip()
    if not re.fullmatch(r"[A-Z0-9]{3}", icao):
        raise StudioError("The airline code must be 3 letters or digits, like BAW.")
    if not voice or not airline:
        raise StudioError("Give the airline name and a name for this voice.")
    try:
        return load(root, icao, voice)
    except StudioError:
        pass
    project = {"icao": icao, "airline": airline[:80], "voice": voice[:40], "artist": "", "credit_url": "", "donate_url": "",
               "accent": "", "description": "", "version": "1.0.0", "airports": [], "extras": [], "music": [],
               "effect": True, "slots": {}}
    save(root, project)
    return project


# --------------------------------------------------------------------------- airport names
_NAMES = None


def airport_name(code: str) -> str:
    """'London Heathrow' for EGLL (or EGLL_T5); '' when the airport is not in airport_names.json. Data: OurAirports (public domain)."""
    global _NAMES
    if _NAMES is None:
        _NAMES = {}
        for base in (getattr(sys, "_MEIPASS", None), Path(__file__).parent):
            f = Path(base) / "airport_names.json" if base else None
            if f and f.is_file():
                try:
                    _NAMES = json.loads(f.read_text(encoding="utf-8"))
                    break
                except (OSError, ValueError):
                    pass
    return _NAMES.get(code.split("_")[0].upper(), "")


# --------------------------------------------------------------------------- the checklist
def slot_list(project: dict) -> list[dict]:
    icao = project["icao"]
    out = []

    def add(group, folder, stem, label, script, required=False, out_name=None, part=None):
        slot_id = f"{folder}/{stem}"
        modes = list(MODES) if folder == "main" and stem in MODE_SLOTS else None
        mode = project.get("modes", {}).get(slot_id, MODES[0]) if modes else None
        tag = "[Manual]" if mode == "Manual" else ""
        out.append({"id": slot_id, "group": group, "folder": folder, "stem": stem, "label": label,
                    "script": script, "required": required, "file": f"{out_name or stem}{tag}.ogg",
                    "modes": modes, "mode": mode, "part": part})

    def add_variations(stem, group, label, script, part=None, folder="main"):
        out[-1]["variantable"] = True
        for n in project.get("variants", {}).get(stem, []):
            add(group, folder, f"{stem}~v{n}", f"{label} - variation {n}", script, part=part)
            out[-1].update({"variant_of": stem, "variant_n": n})

    W1, W2 = "Part 1 - the greeting (ends on the start of the destination; the airport name follows)", "Part 2 - the rest of the welcome (after the airport name)"
    L1, L2 = "Part 1 - the arrival (welcome to, airport name, local time)", "Part 2 - the rest of the landing (after the time)"
    for stem, label, script in GREETINGS:
        if stem.startswith("Welcome_"):
            add("BoardingWelcome", "time", stem, label, script, required=stem != "Welcome_Night", part=W1)
            add_variations(stem, "BoardingWelcome", label, script, W1, "time")
    add("BoardingWelcome", "main", "Modular_Welcome", GENERAL[0][1], GENERAL[0][2], required=True, part=W2)
    add_variations("Modular_Welcome", "BoardingWelcome", GENERAL[0][1], GENERAL[0][2], W2)
    for stem, label, script in GREETINGS:
        if not stem.startswith("Welcome_"):
            add("After Landing", "time", stem, label, script, required=True, part=L1)
    for i, word in enumerate(HOURS):
        n = i or 12
        add("After Landing", "time", f"Hour_{n:02d}", f"Local time - hour {n}", word.capitalize(), required=True, part=L1)
    for minute, word in MINUTES.items():
        add("After Landing", "time", f"Min_{minute:02d}", f"Local time - minutes :{minute:02d}", word.capitalize(), required=True, part=L1)
    add("After Landing", "time", "AM", "Local time - AM (optional)", "A.M. - said after the minutes, e.g. 'four thirty-five A.M.'", part=L1)
    add("After Landing", "time", "PM", "Local time - PM (optional)", "P.M. - said after the minutes, e.g. 'four thirty-five P.M.'", part=L1)
    add("After Landing", "main", "Modular_Landing", GENERAL[1][1], GENERAL[1][2], required=True, part=L2)
    add_variations("Modular_Landing", "After Landing", GENERAL[1][1], GENERAL[1][2], L2)
    for stem, label, script in GENERAL[2:]:
        add("Other announcements", "main", stem, label, script)
        if stem in VARIANT_BASES:
            add_variations(stem, "Other announcements", label, script)
    for code in project.get("airports", []):
        name = airport_name(code)
        add("Airports", "airports", f"{icao}_{code}", f"{code}: {name}" if name else f"Airport name - {code}",
            "Say airport name", required=False)
    if project.get("aircraft_on"):   # optional: the aircraft type, so a PA can say "this Airbus A320"
        for name in project.get("aircraft", []):
            add("Aircraft names", "aircraft", f"{icao}_{slug(name)}", name, f'"{name}"', required=False)
    add("Refuelling", "fuel", "Fuel_Tag", "While refuelling - tag-on",
        "Added to the welcome while the aircraft is being refuelled, e.g. 'We are currently refuelling, so please keep your seatbelt unfastened...'")
    add_variations("Fuel_Tag", "Refuelling", "While refuelling - tag-on", "Added to the welcome while the aircraft is being refuelled.", None, "fuel")
    for name in project.get("music", []):
        add("Boarding music", "main", f"BoardingMusic~{name}", f"Track: {name}",
            "Upload a track you have the rights to use. It is shown in the cabin crew panel under this name; no PA sound is applied.")
        out[-1]["music"] = True
    for entry in project.get("typed", []):   # custom takes for particular aircraft types
        stem = typed_stem(entry["base"], entry["types"], entry.get("n", 1))
        shown = "all aircraft" if entry["types"] == ["ALL"] else ", ".join(entry["types"])
        in_safety = entry["base"] in SAFETY_TYPED   # these sit right under the normal safety briefing PA they replace
        label = next((lab for stem0, lab, _ in GENERAL if stem0 == entry["base"]), TYPED_BASES[entry["base"]]) if in_safety else TYPED_BASES[entry["base"]]
        add("Safety briefing" if in_safety else "Other announcements", "main", stem, f"{label} ({shown})",
            "this announcement as it should sound on the aircraft types you choose. It replaces the normal one on those types.")
        out[-1].update({"typed": True, "base": entry["base"], "types": entry["types"], "n": entry.get("n", 1)})
    for name in project.get("extras", []):
        add("Bolt-ons", "extra", name, f"Bolt-on: {name}", "A short announcement that is sometimes added to the welcome.")
    return arrange(out)


# The checklist in the order the announcements are heard on a flight. (group, the file-name bases that belong to it.)
PHASES = [
    ("Airports", ()), ("Aircraft names", ()),
    ("Boarding", ("Welcome_", "Modular_Welcome", "BoardingWelcome", "Fuel_Tag")),
    ("Boarding music", ()), ("Bolt-ons", ()),
    ("Boarding complete & doors", ("BoardingComplete", "ArmDoors")),
    ("Safety briefing", ("PreSafetyBriefing", "SafetyBriefing")),
    ("Before takeoff", ("CrewTakeSeats[PreFlight]", "CabinDim[PreFlight]", "CabinSecured[PreFlight]")),
    ("Climb & cruise", ("AfterTakeoff", "SeatbeltSignOff", "CabinService", "SeatbeltSignOn")),
    ("Descent & approach", ("DescentStarted", "CrewTakeSeats[Descent]", "CabinDim[Descent]", "CabinSecured[Descent]")),
    ("After Landing", ("WelcomeTo", "LocalTime", "Hour_", "Min_", "AM", "PM", "Modular_Landing", "AfterLanding")),
    ("Deboarding", ("DisarmDoors", "DeboardingStarted", "DeboardingComplete")),
    ("Emergency", ("Decompression",)),
]
PHASE_NAMES = [name for name, _ in PHASES]
BOARDING_LOOP = "Generic Boarding [no destination file]"
REFUEL_PART = "While refuelling - the tag-on"
LANDING_FALLBACK = "Generic After Landing"


SAFETY_TYPED = ("PreSafetyBriefing", "SafetyBriefing")   # aircraft versions of these are listed in the Safety briefing group


def phase_of(slot: dict) -> tuple[str, int]:
    """(phase name, position inside it) for a slot, from its file-name base."""
    if slot.get("typed") and slot.get("base") in SAFETY_TYPED:   # introduction, its aircraft versions, demonstration, its aircraft versions
        return "Safety briefing", 0.5 if slot["base"] == "PreSafetyBriefing" else 1.5
    if slot["group"] == "Safety briefing" and slot["stem"] in SAFETY_TYPED:
        return "Safety briefing", SAFETY_TYPED.index(slot["stem"])
    if slot["group"] in PHASE_NAMES and slot["group"] not in ("BoardingWelcome", "After Landing", "Other announcements"):
        return slot["group"], 0
    stem = slot["stem"]
    base = re.sub(r"~v\d+$", "", stem)
    if slot.get("typed"):   # an aircraft version sits right under the PA it replaces
        base = slot["base"]
    for index, (name, bases) in enumerate(PHASES):
        for pos, b in enumerate(bases):
            if base == b or base.startswith(b + "[") or (b.endswith("_") and base.startswith(b)) or (b.endswith("]") and base.startswith(b[:-1])):
                if name == "After Landing" and slot["folder"] == "time" and base.startswith(("Welcome_",)):
                    continue
                return name, pos
    legacy = {"CrewTakeSeats": 0, "CabinDim": 1, "CabinSecured": 2}   # untagged: the takeoff versions
    if base_of(stem) in legacy:
        return "Before takeoff", legacy[base_of(stem)]
    return slot["group"], 99


def arrange(slots: list[dict]) -> list[dict]:
    """Put the checklist in flight order and give every slot its phase as its group."""
    for sl in slots:
        name, pos = phase_of(sl)
        if name == "Boarding":
            if sl["stem"].startswith("BoardingWelcome["):
                sl["part"] = BOARDING_LOOP
            elif re.sub(r"~v\d+$", "", sl["stem"]) == "Fuel_Tag":
                sl["part"] = REFUEL_PART
        elif name == "After Landing" and sl["stem"] == "AfterLanding":
            sl["part"] = LANDING_FALLBACK
        sl["group"], sl["_pos"] = name, pos
    order = {name: i for i, name in enumerate(PHASE_NAMES)}
    slots.sort(key=lambda sl: (order.get(sl["group"], 98), sl["_pos"]))   # stable: equal positions (hours, minutes...) keep their order
    for sl in slots:
        sl.pop("_pos", None)
    return slots


def hour_order(slots: list[dict]) -> list[dict]:
    hours = sorted((s for s in slots if s["stem"].startswith("Hour_")), key=lambda s: int(s["stem"][5:]))
    it = iter(hours)
    return [next(it) if s["stem"].startswith("Hour_") else s for s in slots]


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def parse_script(project: dict, text: str) -> tuple[dict, int]:
    """Read an uploaded script into {slot id: text}. Accepts JSON ({"time/Hour_04": "four"}) or text lines like
    "Hour 4: Four" / "Hour_04 - Four" / "time/Hour_04 = Four", matching a slot's id, file name or label.
    Returns (scripts, number of lines that matched nothing)."""
    slots = slot_list(project)
    lookup = {}
    for slot in slots:
        for name in (slot["id"], slot["stem"], slot["label"]):
            lookup.setdefault(_key(name), slot["id"])
    found: dict[str, str] = {}
    pairs = []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            pairs = [(str(k), str(v)) for k, v in data.items()]
    except ValueError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for sep in re.finditer(r"\s*(?::|=|\t| - | – )\s*", line):  # the first split whose left side names a clip
                if _key(line[:sep.start()]) in lookup and line[sep.end():].strip():
                    pairs.append((line[:sep.start()], line[sep.end():].strip()))
                    break
            else:
                pairs.append(("", line))
    skipped = 0
    for name, words in pairs:
        slot_id = lookup.get(_key(name))
        if slot_id:
            found[slot_id] = words[:1000]
        else:
            skipped += 1
    return found, skipped


def find_slot(project: dict, slot_id: str) -> dict:
    for slot in slot_list(project):
        if slot["id"] == slot_id:
            return slot
    raise StudioError("Unknown recording slot.")


def variants_of(pack: Path, slot: dict, known: set[str] = frozenset()) -> list[str]:
    """Tagged copies of an announcement in the pack, e.g. CrewTakeSeats[PreFlight].ogg for the CrewTakeSeats slot."""
    folder = pack / slot["folder"]
    if slot["folder"] != "main" or not folder.is_dir():
        return []
    return [p.name for p in sorted(folder.iterdir())
            if p.is_file() and p.suffix.lower() == ".ogg" and p.name != slot["file"] and p.name not in known
            and base_of(p.stem) == base_of(slot["stem"])
            and {t.lower() for t in re.findall(r"\[(.*?)\]", slot["stem"])} <= {t.lower() for t in re.findall(r"\[(.*?)\]", p.stem)}
            and not slot["stem"].startswith("BoardingWelcome")]


def status(root: Path, project: dict) -> list[dict]:
    folder = project_dir(root, project["icao"], project["voice"])
    done = project.get("slots", {})
    rows = []
    all_slots = hour_order(slot_list(project))
    known = {s["file"] for s in all_slots}
    for slot in all_slots:
        info = done.get(slot["id"])
        have = (folder / "pack" / slot["folder"] / slot["file"]).is_file()
        if have and info is None:
            info = {"imported": True}   # a clip that arrived by renaming
        have = have and info is not None
        variants = variants_of(folder / "pack", slot, known)
        if not have and variants:
            have, info = True, {"imported": True}
        slot = {**slot, "script": project.get("scripts", {}).get(slot["id"], slot["script"]),
                "custom": slot["id"] in project.get("scripts", {})}
        rows.append({**slot, "music": bool(slot.get("music")), "skipped": bool(not have and slot["id"] in project.get("skipped", [])), "done": have, "seconds": info.get("seconds") if have else None,
                     "lufs": info.get("lufs_out") if have else None, "imported": bool(have and info.get("imported")), "variants": variants})
    alphabetical = {"Boarding music", "Bolt-ons"}  # the clock stays in clock order; everything else is in flight order
    first = {}
    for i, r in enumerate(rows):
        first.setdefault(r["group"], i)
    def order(r):
        if r["group"] in alphabetical:
            return r["label"].lower()
        if r["group"] == "Airports":   # by airport, welcome aboard before landing
            return r["stem"].split("_", 1)[-1]
        return 0
    rows.sort(key=lambda r: (first[r["group"]], order(r)))
    return rows


# --------------------------------------------------------------------------- loudness reference
def reference_loudness(files: list[Path], progress=None) -> tuple[float, int]:
    """The loudness of the original Fenix announcements: the median of their integrated loudness -> (LUFS, how many
    were measured). Falls back to DEFAULT_TARGET."""
    values = []
    for i, path in enumerate(files[:12]):
        if progress:
            progress(i / max(1, min(len(files), 12)))
        try:
            value = papro_audio.file_loudness(path)
        except Exception:
            continue
        if value is not None and value > -50:
            values.append(value)
    if not values:
        return DEFAULT_TARGET, 0
    return round(statistics.median(values), 1), len(values)


# --------------------------------------------------------------------------- recording / processing
def add_airport(root: Path, project: dict, code: str) -> None:
    code = code.strip().upper().replace(" ", "_")
    if not AIRPORT.match(code):
        raise StudioError("Use the airport's 4-letter ICAO code, like EGLL. A terminal variant can follow: EGLL_T5.")
    if code not in project["airports"]:
        project["airports"].append(code)
        project["airports"].sort()
    save(root, project)


def add_airports(root: Path, project: dict, text: str) -> tuple[list[str], list[str]]:
    """Add every airport in a pasted list (separated by commas, semicolons, spaces or new lines).
    Returns (added, skipped); codes that are not valid ICAO codes are skipped rather than failing the whole list."""
    added: list[str] = []
    skipped: list[str] = []
    for raw in re.split(r"[,;\s]+", text.upper()):
        code = raw.strip(".")
        if not code:
            continue
        if not AIRPORT.match(code):
            skipped.append(code)
        elif code not in project["airports"] and code not in added:
            added.append(code)
    if added:
        project["airports"] = sorted(project["airports"] + added)
        save(root, project)
    return added, skipped


def remove_airport(root: Path, project: dict, code: str) -> None:
    """Take an airport off the checklist, together with anything recorded for it (recorded or not)."""
    slot_id = f"airports/{project['icao']}_{code}"
    try:
        delete_slot(root, project, slot_id)
    except StudioError:
        pass
    project["airports"] = [c for c in project["airports"] if c != code]
    project.get("scripts", {}).pop(slot_id, None)
    if slot_id in project.get("skipped", []):
        project["skipped"].remove(slot_id)
    save(root, project)


def set_aircraft_on(root: Path, project: dict, on: bool) -> None:
    project["aircraft_on"] = bool(on)
    save(root, project)


def add_variant(root: Path, project: dict, base: str) -> int:
    """Add another take of a PA (PAPro picks one at random for each flight). Returns its number (2, 3, ...)."""
    if base not in VARIANT_BASES:
        raise StudioError("That announcement cannot have variations.")
    taken = project.setdefault("variants", {}).setdefault(base, [])
    if len(taken) >= 8:
        raise StudioError("That is plenty of variations - 8 is the most.")
    n = max(taken, default=1) + 1
    taken.append(n)
    save(root, project)
    return n


def remove_variant(root: Path, project: dict, base: str, n: int) -> None:
    slot_id = f"{variant_folder(base)}/{base}~v{n}"
    try:
        delete_slot(root, project, slot_id)
    except StudioError:
        pass
    taken = project.get("variants", {}).get(base, [])
    if n in taken:
        taken.remove(n)
    project.get("scripts", {}).pop(slot_id, None)
    if slot_id in project.get("skipped", []):
        project["skipped"].remove(slot_id)
    save(root, project)


def add_typed(root: Path, project: dict, base: str, types=None) -> dict:
    """A new aircraft-specific PA. Returns its entry."""
    if base not in TYPED_BASES:
        raise StudioError("That announcement cannot be made aircraft-specific.")
    types = clean_types(types or ["ALL"])
    entries = project.setdefault("typed", [])
    if len(entries) >= 60:
        raise StudioError("That is plenty of aircraft PAs - 60 is the most.")
    n = 1
    while any(e["base"] == base and e["types"] == types and e.get("n", 1) == n for e in entries):
        n += 1
    entry = {"base": base, "types": types, "n": n}
    entries.append(entry)
    save(root, project)
    return entry


def _typed_entry(project: dict, slot_id: str) -> dict:
    stem = slot_id.split("/", 1)[-1]
    parsed = parse_typed(stem)
    if not parsed:
        raise StudioError("That is not an aircraft PA.")
    for e in project.get("typed", []):
        if (e["base"], e["types"], e.get("n", 1)) == parsed:
            return e
    raise StudioError("That aircraft PA does not exist.")


def set_typed_types(root: Path, project: dict, slot_id: str, types) -> str:
    """Change which aircraft types a PA applies to: its files are renamed to match. Returns the new slot id."""
    entry = _typed_entry(project, slot_id)
    types = clean_types(types)
    if types == entry["types"]:
        return slot_id
    old_stem = typed_stem(entry["base"], entry["types"], entry.get("n", 1))
    n = 1
    while any(e is not entry and e["base"] == entry["base"] and e["types"] == types and e.get("n", 1) == n for e in project["typed"]):
        n += 1
    new_stem = typed_stem(entry["base"], types, n)
    folder = project_dir(root, project["icao"], project["voice"])
    for d, prefix in ((folder / "raw", "main__"), (folder / "pack" / "main", "")):
        if d.is_dir():
            for path in list(d.iterdir()):
                if path.is_file() and path.stem == prefix + old_stem:
                    path.rename(path.with_name(prefix + new_stem + path.suffix))
    old_id, new_id = f"main/{old_stem}", f"main/{new_stem}"
    for key in ("slots", "scripts"):
        if old_id in project.get(key, {}):
            project[key][new_id] = project[key].pop(old_id)
    project["skipped"] = [new_id if i == old_id else i for i in project.get("skipped", [])]
    entry["types"], entry["n"] = types, n
    save(root, project)
    return new_id


def remove_typed(root: Path, project: dict, slot_id: str) -> None:
    entry = _typed_entry(project, slot_id)
    try:
        delete_slot(root, project, slot_id)
    except StudioError:
        pass
    project["typed"] = [e for e in project["typed"] if e is not entry and e != entry]
    project.get("scripts", {}).pop(slot_id, None)
    project["skipped"] = [i for i in project.get("skipped", []) if i != slot_id]
    save(root, project)


def add_aircraft(root: Path, project: dict, text: str) -> tuple[list[str], list[str]]:
    """Add aircraft names (one per line, or separated by commas or semicolons). Returns (added, skipped)."""
    added: list[str] = []
    skipped: list[str] = []
    have = {slug(n).lower() for n in project.get("aircraft", [])}
    for raw in re.split(r"[,;\n]+", text):
        name = " ".join(raw.split())
        if not name:
            continue
        if not AIRCRAFT_NAME.match(name):
            skipped.append(name)
        elif slug(name).lower() not in have:
            have.add(slug(name).lower())
            added.append(name)
    if added:
        project["aircraft"] = project.get("aircraft", []) + added
        project["aircraft_on"] = True
        save(root, project)
    return added, skipped


def remove_aircraft(root: Path, project: dict, name: str) -> None:
    """Take an aircraft name off the list together with its recording."""
    slot_id = f"aircraft/{project['icao']}_{slug(name)}"
    try:
        delete_slot(root, project, slot_id)
    except StudioError:
        pass
    project["aircraft"] = [n for n in project.get("aircraft", []) if n != name]
    project.get("scripts", {}).pop(slot_id, None)
    if slot_id in project.get("skipped", []):
        project["skipped"].remove(slot_id)
    save(root, project)


def add_music(root: Path, project: dict, name: str) -> None:
    name = name.strip()
    if not MUSIC_NAME.match(name):
        raise StudioError("Name the track with letters, digits, spaces or . & ' ( ) - (e.g. Departure lounge).")
    if name.lower() not in [n.lower() for n in project.setdefault("music", [])]:
        project["music"].append(name)
    save(root, project)


def remove_music(root: Path, project: dict, name: str) -> None:
    project["music"] = [n for n in project.get("music", []) if n != name]
    save(root, project)


def add_extra(root: Path, project: dict, name: str) -> None:
    name = name.strip()
    if not EXTRA_NAME.match(name):
        raise StudioError("Name the bolt-on with letters, digits, spaces, - or _ (e.g. Nut allergy).")
    name = name.replace(" ", "_")
    if name not in project["extras"]:
        project["extras"].append(name)
    save(root, project)


def remove_extra(root: Path, project: dict, name: str) -> None:
    project["extras"] = [n for n in project["extras"] if n != name]
    save(root, project)


def store_raw(root: Path, project: dict, slot_id: str, body: bytes, filename: str, target: float) -> dict:
    """Keep the uploaded or recorded take and process it. Returns the slot's measurements."""
    slot = find_slot(project, slot_id)
    ext = Path(filename).suffix.lower()
    if ext not in AUDIO_EXT:
        raise StudioError("Use a wav, mp3, ogg, m4a, flac or webm file.")
    if not body or len(body) > MAX_UPLOAD:
        raise StudioError("That file is empty or too large (60 MB limit).")
    folder = project_dir(root, project["icao"], project["voice"])
    raw = folder / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for old in raw_takes(raw, slot):
        old.unlink()
    path = raw / f"{slot['folder']}__{slot['stem']}{ext}"
    path.write_bytes(body)
    return process_slot(root, project, slot, target)


def process_slot(root: Path, project: dict, slot: dict, target: float) -> dict:
    folder = project_dir(root, project["icao"], project["voice"])
    raws = raw_takes(folder / "raw", slot)
    if not raws:
        raise StudioError("Nothing recorded for that yet.")
    try:
        if slot.get("music"):
            result = papro_audio.convert_music(raws[0], folder / "pack" / slot["folder"] / slot["file"])
        else:
            result = papro_audio.process_clip(raws[0], folder / "pack" / slot["folder"] / slot["file"], target,
                                             bool(project.get("effect", True)))
    except papro_audio.AudioError as exc:
        raise StudioError(str(exc))
    result["target"] = target
    project["skipped"] = [i for i in project.get("skipped", []) if i != slot["id"]]
    project.setdefault("slots", {})[slot["id"]] = result
    save(root, project)
    return result


def unprocessed(root: Path, project: dict) -> list[dict]:
    """Slots whose recording was saved but never turned into a clip (for example after a crash or an older bug)."""
    raw = project_dir(root, project["icao"], project["voice"]) / "raw"
    pack = project_dir(root, project["icao"], project["voice"]) / "pack"
    return [sl for sl in slot_list(project)
            if sl["id"] not in project.get("slots", {}) and not (pack / sl["folder"] / sl["file"]).is_file() and raw_takes(raw, sl)]


def reprocess_all(root: Path, project: dict, target: float) -> int:
    count = 0
    for slot in slot_list(project):
        if slot["id"] in project.get("slots", {}) and not slot.get("music"):
            try:
                process_slot(root, project, slot, target)
                count += 1
            except StudioError:
                pass
    return count


def set_mode(root: Path, project: dict, slot_id: str, mode: str) -> None:
    """Prerecorded (plain file name) or Manual (the file gets [Manual]). Renames a clip that is already processed."""
    slot = find_slot(project, slot_id)
    if not slot["modes"] or mode not in MODES:
        raise StudioError("That announcement has no mode choice.")
    pack = project_dir(root, project["icao"], project["voice"]) / "pack" / slot["folder"]
    old = pack / slot["file"]
    project.setdefault("modes", {})[slot_id] = mode
    new = pack / find_slot(project, slot_id)["file"]
    if old.is_file() and old != new:
        new.unlink(missing_ok=True)
        old.rename(new)
    save(root, project)


def delete_slot(root: Path, project: dict, slot_id: str) -> None:
    slot = find_slot(project, slot_id)
    folder = project_dir(root, project["icao"], project["voice"])
    for name in variants_of(folder / "pack", slot, {s["file"] for s in slot_list(project)}):
        (folder / "pack" / slot["folder"] / name).unlink(missing_ok=True)
    for path in raw_takes(folder / "raw", slot):
        path.unlink()
    (folder / "pack" / slot["folder"] / slot["file"]).unlink(missing_ok=True)
    project.get("slots", {}).pop(slot_id, None)
    save(root, project)


def processed_file(root: Path, project: dict, slot_id: str) -> Path | None:
    slot = find_slot(project, slot_id)
    path = project_dir(root, project["icao"], project["voice"]) / "pack" / slot["folder"] / slot["file"]
    return path if path.is_file() else None


def legacy_moves(pack: Path) -> list[tuple[str, str]]:
    """Old dep clips (just the airport's name, when the pack has the modular welcome) -> the airports folder."""
    dep, airports = pack / "dep", pack / "airports"
    modular = [pack / "main" / "Modular_Welcome.ogg", pack / "time" / "Welcome_Rest.ogg", pack / "parts" / "Welcome_Rest.ogg"]
    if not dep.is_dir() or not any(p.is_file() for p in modular):
        return []
    have = {p.name.lower() for p in airports.iterdir()} if airports.is_dir() else set()
    return [(f"dep/{p.name}", f"airports/{p.name}") for p in sorted(dep.iterdir())
            if p.is_file() and p.suffix.lower() == ".ogg" and p.name.lower() not in have]


def upgrade_plan(root: Path, project: dict) -> list[tuple[str, str]]:
    pack = project_dir(root, project["icao"], project["voice"]) / "pack"
    return [(f"main/{a}", f"main/{b}") for a, b in legacy_renames(pack / "main")] + legacy_moves(pack)


def upgrade_preview(root: Path, project: dict) -> list[dict]:
    return [{"old": a, "new": b} for a, b in upgrade_plan(root, project)]


def upgrade_names(root: Path, project: dict) -> int:
    """Rename / move the legacy clips in the project's copy (never the library) to the new Fenix names and folders."""
    pack = project_dir(root, project["icao"], project["voice"]) / "pack"
    done = 0
    for old, new in upgrade_plan(root, project):
        (pack / new).parent.mkdir(parents=True, exist_ok=True)
        (pack / old).rename(pack / new)
        project.setdefault("replaced", []).append(old)
        done += 1
    airports = pack / "airports"
    for path in sorted(airports.iterdir()) if airports.is_dir() else []:  # moved clips become airports in the checklist
        match = re.match(rf"^{re.escape(project['icao'])}_([A-Z0-9]{{4}}(?:_[A-Z0-9]{{1,8}})?)$", path.stem, re.I)
        if match and match[1].upper() not in project["airports"]:
            project["airports"].append(match[1].upper())
    project["airports"].sort()
    save(root, project)
    return done


# --------------------------------------------------------------------------- export
def voice_json(project: dict) -> dict:
    data = {"icao": project.get("icao", ""), "airline": project.get("airline", ""), "name": project["voice"], "artist": project.get("artist", ""), "credit_url": project.get("credit_url", ""),
            "donate_url": project.get("donate_url", ""), "accent": project.get("accent", ""),
            "description": project.get("description", "")}
    if project.get("language"):
        data["language"] = project["language"]
    if project.get("also_icaos"):
        data["also_icaos"] = list(project["also_icaos"])
    out = {k: v for k, v in data.items() if v}
    if project.get("local_time") is False:
        out["local_time"] = False
    return out


def clean_also(value, own: str = "") -> list[str]:
    """"sht, CFE EFW" (or a list) -> ["CFE", "EFW", "SHT"]: other airline codes that use this pack, 3 letters/digits each."""
    parts = re.split(r"[\s,;]+", value) if isinstance(value, str) else list(value or [])
    out = []
    for p in parts:
        p = str(p).strip().upper()
        if not p:
            continue
        if not re.fullmatch(r"[A-Z0-9]{3}", p):
            raise StudioError(f"'{p}' is not a 3-character airline code (like SHT).")
        if p != own.upper() and p not in out:
            out.append(p)
    return sorted(out)


def set_skip(root: Path, project: dict, slot_id: str, skip: bool) -> None:
    """Mark an announcement as skipped (you are not recording it): it counts as done for the checklist and the export."""
    find_slot(project, slot_id)
    skipped = [i for i in project.get("skipped", []) if i != slot_id]
    if skip:
        skipped.append(slot_id)
    project["skipped"] = skipped
    save(root, project)


def unskip_all(root: Path, project: dict) -> int:
    """Put every skipped announcement back on the checklist. Returns how many were skipped."""
    n = len([r for r in status(root, project) if r["skipped"]])
    project["skipped"] = []
    save(root, project)
    return n


def missing(root: Path, project: dict) -> list[str]:
    return [s["label"] for s in status(root, project) if s["required"] and not s["done"] and not s["skipped"]]


PA_BASES = []   # (file-name base, plain-English label) of every PA that can be set to play its languages in its own order
for _stem, _label, _ in GENERAL:
    _base = re.split(r"[\[~]", _stem, maxsplit=1)[0]
    if not _base.startswith("Modular_") and _base not in [b for b, _ in PA_BASES]:
        PA_BASES.append((_base, {"BoardingWelcome": "Welcome while boarding", "AfterLanding": "Landing", "CrewTakeSeats": "Crew, take your seats",
                                 "CabinDim": "Cabin lights dimmed", "CabinSecured": "Cabin secure report"}.get(_base, _label)))


def export(root: Path, project: dict, out_dir: Path, languages: dict | None = None) -> dict:
    """Write <ICAO>_<Voice>.zip (laid out like Airlines\\<ICAO>, ready for the library or for make_pack/catalog) and
    a catalog entry. Raises StudioError if required recordings are missing."""
    todo = missing(root, project)
    if todo:
        raise StudioError("Still to record: " + ", ".join(todo[:6]) + (f" and {len(todo) - 6} more" if len(todo) > 6 else ""))
    if not any(s["folder"] == "airports" and s["done"] for s in status(root, project)):
        raise StudioError("Add at least one airport and record its name.")
    folder = project_dir(root, project["icao"], project["voice"]) / "pack"
    out_dir.mkdir(parents=True, exist_ok=True)
    version = str(project.get("version", "1.0.0")).strip() or "1.0.0"
    pack_id = f"{project['icao'].lower()}-{re.sub(r'[^a-z0-9]+', '-', project['voice'].lower()).strip('-') or 'voice'}"
    tag = f"{pack_id}-{version}"                  # the GitHub release tag, and the zip's name: ezy-sam-james-1.0.0
    zip_path = out_dir / f"{tag}.zip"
    inside = f"voices/{slug(project['voice'])}"   # the voice lives in its own folder, so several voices of one airline merge
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*.ogg")):
            archive.write(path, f"{inside}/{path.relative_to(folder).as_posix()}")
        archive.writestr(f"{inside}/voice.json", json.dumps(voice_json(project), indent=2))
        if project.get("scripts"):
            archive.writestr(f"{inside}/scripts.json", json.dumps(project["scripts"], indent=2, ensure_ascii=False))
        if project.get("skipped"):
            archive.writestr(f"{inside}/skipped.json", json.dumps(sorted(project["skipped"]), indent=2))
        if languages and languages.get("enabled"):   # the airline's language order travels with its packs
            archive.writestr("languages.json", json.dumps(languages, indent=2))
        archive.writestr("voice.json", json.dumps({"icao": project["icao"], "airline": project.get("airline", ""),
                                                   **({"also_icaos": list(project["also_icaos"])} if project.get("also_icaos") else {}),
                                                   **({"local_time": False} if project.get("local_time") is False else {})}, indent=2))
        if logo_path(root, project).is_file():
            archive.write(logo_path(root, project), "logo.png")
    has_logo = logo_path(root, project).is_file()
    if has_logo:   # also saved beside the zip: attach it to the release too, so Download Packs can show the logo
        shutil.copyfile(logo_path(root, project), out_dir / f"{tag}.png")
    entry = {"id": pack_id, "icao": project["icao"], "name": project["airline"], "voice": project["voice"], "version": version,
             "url": f"https://github.com/{PACKS_REPO}/releases/download/{tag}/{zip_path.name}",
             "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
             "size": zip_path.stat().st_size,
             "voices": [{k: v for k, v in voice_json(project).items() if k in ("name", "artist", "accent", "description")}]}
    if project.get("also_icaos"):
        entry["also_icaos"] = list(project["also_icaos"])
    if has_logo:
        entry["logo"] = f"https://github.com/{PACKS_REPO}/releases/download/{tag}/{tag}.png"
    entry_path = zip_path.with_suffix(".catalog.json")
    entry_path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    return {"zip": str(zip_path), "entry": str(entry_path), "size": entry["size"], "tag": tag}


IMPORT_FOLDERS = ("main", "time", "parts", "extra", "airports", "aircraft", "dep", "dest", "fuel")


def _import_logo(root: Path, project: dict, src: Path) -> None:
    """Bring the airline's logo (Airlines\\<ICAO>\\logo.png, beside the voices folder) into a project that has none."""
    if logo_path(root, project).is_file():
        return
    for folder in (src, src.parent.parent if src.parent.name.lower() == "voices" else None):
        for name in ("logo.png", "logo.jpg", "logo.jpeg", "logo.webp"):
            path = folder / name if folder else None
            if path and path.is_file():
                try:
                    set_logo(root, project, path.read_bytes())
                    return
                except (StudioError, OSError):
                    continue


def import_pack(root: Path, icao: str, airline: str, src: Path, fallback_name: str, progress=None) -> dict:
    """Open an existing voice folder (Airlines\\<ICAO> itself or voices\\<Name>) for editing: its clips are copied
    into a new project, untouched, and show as done. Opens the existing project if there already is one."""
    info = {}
    try:
        info = json.loads((src / "voice.json").read_text(encoding="utf-8-sig"))
        if not isinstance(info, dict):
            info = {}
    except (OSError, ValueError):
        pass
    voice = str(info.get("name") or fallback_name).strip()[:40] or fallback_name
    try:
        existing = load(root, icao, voice)
        _import_logo(root, existing, src)   # an earlier import may have left the logo behind
        return existing
    except StudioError:
        pass
    files = []
    for folder in IMPORT_FOLDERS:
        base = src / folder
        if base.is_dir():
            files += [(p, "time" if folder == "parts" else folder) for p in sorted(base.iterdir())
                      if p.is_file() and p.suffix.lower() == ".ogg"]
    if not files:
        raise StudioError("That pack has no sound files to edit.")
    project = create(root, icao, airline or icao, voice)
    for key, limit in (("artist", 80), ("accent", 40), ("description", 200)):
        project[key] = str(info.get(key, "")).strip()[:limit]
    if info.get("local_time") is False:
        project["local_time"] = False
    try:
        also = info.get("also_icaos")
        if not also:   # the airline-level voice.json sits beside the voices folder
            outer = json.loads((src.parent.parent / "voice.json").read_text(encoding="utf-8-sig")) if src.parent.name.lower() == "voices" else {}
            also = outer.get("also_icaos") if isinstance(outer, dict) else None
        project["also_icaos"] = clean_also(also or [], icao)
    except (StudioError, OSError, ValueError):
        project["also_icaos"] = []
    if str(info.get("language", "")).strip().lower() in LANGUAGES:
        project["language"] = str(info["language"]).strip().lower()
    for key in ("credit_url", "donate_url"):
        value = str(info.get(key, "")).strip()[:300]
        project[key] = value if re.match(r"https?://", value, re.I) else ""
    for path, folder in files:  # work out the airports, bolt-ons and music tracks it contains
        stem = path.stem
        match = re.match(rf"^{re.escape(icao)}_([A-Z0-9]{{4}}(?:_[A-Z0-9]{{1,8}})?)$", stem, re.I)
        if folder == "airports" and match and match[1].upper() not in project["airports"]:
            project["airports"].append(match[1].upper())
        elif folder == "aircraft" and stem.lower().startswith(icao.lower() + "_") and len(stem) > len(icao) + 1:
            name = stem[len(icao) + 1:].replace("_", " ")
            if name not in project.setdefault("aircraft", []):
                project["aircraft"].append(name)
            project["aircraft_on"] = True
        elif folder == "extra" and EXTRA_NAME.match(stem) and stem not in project["extras"]:
            project["extras"].append(stem)
        elif re.match(rf"^({'|'.join(re.escape(b) for b in VARIANT_BASES)})~v\d+$", stem) and folder == variant_folder(stem.split("~v")[0]):
            base, n = stem.split("~v")
            if int(n) not in project.setdefault("variants", {}).setdefault(base, []):
                project["variants"][base].append(int(n))
                project["variants"][base].sort()
        elif folder == "main" and parse_typed(stem):
            base, types, n = parse_typed(stem)
            try:
                entry = {"base": base, "types": clean_types(types, False), "n": n}
            except StudioError:
                continue
            if entry not in project.setdefault("typed", []):
                project["typed"].append(entry)
        elif folder == "main" and stem.startswith("BoardingMusic~") and MUSIC_NAME.match(stem[14:]):
            project["music"].append(stem[14:])
    project["airports"].sort()
    pack = project_dir(root, icao, voice) / "pack"
    for i, (path, folder) in enumerate(files):
        if progress:
            progress(i / len(files))
        (pack / folder).mkdir(parents=True, exist_ok=True)
        name = path.name
        if folder == "main" and parse_typed(path.stem):   # types in a canonical order (sorted), so the clip matches its entry
            base, types, n = parse_typed(path.stem)
            try:
                name = typed_stem(base, clean_types(types, False), n) + path.suffix
            except StudioError:
                continue
        shutil.copyfile(path, pack / folder / name)
    for stem in MODE_SLOTS:  # a clip already called SafetyBriefing[Manual].ogg keeps that mode
        if (pack / "main" / f"{stem}[Manual].ogg").is_file() and not (pack / "main" / f"{stem}.ogg").is_file():
            project.setdefault("modes", {})[f"main/{stem}"] = "Manual"
    for slot in slot_list(project):
        if (pack / slot["folder"] / slot["file"]).is_file():
            project["slots"][slot["id"]] = {"imported": True}
    try:  # the scripts that were saved with the pack
        scripts = json.loads((src / "scripts.json").read_text(encoding="utf-8-sig"))
        if isinstance(scripts, dict):
            known = {slot["id"] for slot in slot_list(project)}
            project["scripts"] = {k: v[:4000] for k, v in scripts.items() if k in known and isinstance(v, str) and v.strip()}
    except (OSError, ValueError):
        pass
    try:  # the clips that were skipped (not recorded on purpose) when the pack was saved
        skipped = json.loads((src / "skipped.json").read_text(encoding="utf-8-sig"))
        if isinstance(skipped, list):
            known = {slot["id"] for slot in slot_list(project)}
            project["skipped"] = [i for i in skipped if isinstance(i, str) and i in known and i not in project["slots"]]
    except (OSError, ValueError):
        pass
    project["origin"] = str(src)
    save(root, project)
    _import_logo(root, project, src)
    return project


LEVEL_TOLERANCE = 1.0   # LU: a clip within this of the target counts as level


def check_levels(root: Path, project: dict, target: float, fix: bool = False, progress=None) -> dict:
    """Measure the loudness of every speech clip in the pack (music is left alone). Clips more than LEVEL_TOLERANCE from
    the target are listed; with fix=True they are set to the target by gain alone - the PA sound is not applied again, so
    clips that already have it (imported ones) keep their EQ. Imported clips get their measured loudness remembered, so
    the checklist can show it."""
    pack = project_dir(root, project["icao"], project["voice"]) / "pack"
    by_file = {(s["folder"], s["file"]): s for s in slot_list(project)}
    files = [p for p in sorted(pack.rglob("*.ogg")) if not p.name.startswith("BoardingMusic~")] if pack.is_dir() else []
    off, fixed, errors, measured = [], 0, 0, 0
    for i, path in enumerate(files):
        if progress:
            progress(i / max(1, len(files)))
        try:
            lufs = papro_audio.file_loudness(path)
            if lufs is None or lufs < -60:
                continue
            measured += 1
            if abs(lufs - target) > LEVEL_TOLERANCE:
                if fix:
                    lufs = papro_audio.level_clip(path, target) or lufs
                    fixed += 1
                else:
                    off.append({"file": f"{path.parent.name}/{path.name}", "lufs": round(lufs, 1)})
        except Exception:
            errors += 1
            continue
        slot = by_file.get((path.parent.name, path.name))
        info = project.get("slots", {}).get(slot["id"]) if slot else None
        if info is not None and (info.get("imported") or fix):
            info["lufs_out"] = round(lufs, 1)
    save(root, project)
    return {"checked": measured, "off": off, "fixed": fixed, "errors": errors, "target": target}


def install_to_library(root: Path, project: dict, airline_dir: Path) -> Path:
    """Copy the project into Airlines\\<ICAO>\\voices\\<Voice> (or back over the pack it was opened from) so PAPro can
    use it straight away."""
    origin = Path(project["origin"]) if project.get("origin") else None
    dest = origin if origin and origin.is_dir() else airline_dir / "voices" / slug(project["voice"])
    src = project_dir(root, project["icao"], project["voice"]) / "pack"
    if not src.is_dir() or not any(src.rglob("*.ogg")):
        raise StudioError("Record something first.")
    shutil.copytree(src, dest, dirs_exist_ok=True)
    if origin and dest == origin:  # the old-named copies of upgraded clips are replaced, not left beside the new ones
        for old in project.get("replaced", []):
            (dest / (old if "/" in old else f"main/{old}")).unlink(missing_ok=True)
        project["replaced"] = []
        save(root, project)
    music = sorted((src / "main").glob("BoardingMusic*.ogg"))
    if music:  # boarding music is the airline's, not a voice's: it goes where PAPro copies the general PAs from
        (airline_dir / "main").mkdir(parents=True, exist_ok=True)
        for path in music:
            shutil.copyfile(path, airline_dir / "main" / path.name)
    (dest / "voice.json").write_text(json.dumps(voice_json(project), indent=2), encoding="utf-8")
    try:   # the pack's own local-time choice travels with it
        path = airline_dir / "settings.json"
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        data = data if isinstance(data, dict) else {}
        data["local_time"] = project.get("local_time") is not False
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        pass
    if project.get("scripts"):
        (dest / "scripts.json").write_text(json.dumps(project["scripts"], indent=2, ensure_ascii=False), encoding="utf-8")
    skipped_file = dest / "skipped.json"
    if project.get("skipped"):   # the clips you chose not to record are remembered with the pack
        skipped_file.write_text(json.dumps(sorted(project["skipped"]), indent=2), encoding="utf-8")
    else:
        skipped_file.unlink(missing_ok=True)
    if logo_path(root, project).is_file():   # the logo belongs to the airline, so it goes beside its folders
        airline_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(logo_path(root, project), airline_dir / "logo.png")
    return dest
