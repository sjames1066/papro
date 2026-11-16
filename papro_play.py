"""Plays PAPro's announcements itself, for aircraft that can't read them from a folder (PMDG and others).

Player: a queue of clips played one after another through the default (or chosen) output device. Level and mute come
from `gain()`, read continuously while playing - so turning the knob changes a PA already in progress, and "muted"
keeps the clip running silently (the announcement is over when it would have been over). The audio backend is
injectable so the logic can be tested without a sound card.
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger("papro")

CHUNK = 1024


class SoundDeviceBackend:
    """Real output through sounddevice (PortAudio). open() -> a stream with write(frames)."""

    def __init__(self, device=None):
        self.device = device

    def open(self, rate: int, channels: int):
        import sounddevice as sd

        stream = sd.OutputStream(samplerate=rate, channels=channels, dtype="float32", device=self.device or None)
        stream.start()
        return stream


def load_clip(path: Path) -> tuple[np.ndarray, int]:
    import soundfile as sf

    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    return data, rate


class Player:
    def __init__(self, gain: Callable[[], float], backend=None, loader=load_clip):
        self.gain, self.backend, self.loader = gain, backend or SoundDeviceBackend(), loader
        self.q: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.playing: str | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def play(self, path: Path) -> None:
        self.q.put(Path(path))

    def clear(self) -> None:
        """Drop what is waiting and cut the clip in progress."""
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.stop_flag.set()

    def close(self) -> None:
        self.clear()
        self.q.put(None)

    def idle(self) -> bool:
        return self.playing is None and self.q.empty()

    def _run(self) -> None:
        while True:
            path = self.q.get()
            if path is None:
                return
            self.stop_flag.clear()
            self.playing = path.name
            try:
                self._play_one(path)
            except Exception:
                log.exception("Could not play %s", path)
            finally:
                self.playing = None

    def _play_one(self, path: Path) -> None:
        data, rate = self.loader(path)
        stream = self.backend.open(rate, data.shape[1])
        try:
            for start in range(0, len(data), CHUNK):
                if self.stop_flag.is_set():
                    break
                level = max(0.0, min(2.0, float(self.gain())))
                chunk = data[start:start + CHUNK] * level
                stream.write(np.ascontiguousarray(np.clip(chunk, -1.0, 1.0), dtype="float32"))
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


# -- which file is which announcement ---------------------------------------------------------------------
# engine key -> (file base name, tag wanted in the name or None)
KEY_FILES = {
    "welcome": ("BoardingWelcome", "CLOCK"),
    "boarding_complete": ("BoardingComplete", None),
    "arm_doors": ("ArmDoors", None),
    "pre_safety": ("PreSafetyBriefing", None),
    "safety": ("SafetyBriefing", None),
    "takeoff_seats": ("CrewTakeSeats", "PreFlight"),
    "takeoff_dim": ("CabinDim", "PreFlight"),
    "takeoff_secured": ("CabinSecured", "PreFlight"),
    "after_takeoff": ("AfterTakeoff", None),
    "seatbelt_off": ("SeatbeltSignOff", None),
    "cabin_service": ("CabinService", None),
    "seatbelt_on": ("SeatbeltSignOn", None),
    "descent": ("DescentStarted", None),
    "landing_seats": ("CrewTakeSeats", "Descent"),
    "landing_dim": ("CabinDim", "Descent"),
    "landing_secured": ("CabinSecured", "Descent"),
    "after_landing": ("AfterLanding", None),
    "disarm_doors": ("DisarmDoors", None),
    "deboard_start": ("DeboardingStarted", None),
    "deboard_complete": ("DeboardingComplete", None),
}


def resolve(folder: Path, key: str, clock: str = "") -> Path | None:
    """The file in the built pack for this announcement, or None if the pack has none."""
    want = KEY_FILES.get(key)
    if not want or not folder.is_dir():
        return None
    base, tag = want
    tag = (clock if tag == "CLOCK" else tag or "").lower()
    found = []
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".ogg":
            continue
        stem = p.stem
        head = stem.split("[", 1)[0].split("~", 1)[0]
        if head != base:
            continue
        tags = stem.lower()
        if "refueling" in tags:
            continue
        found.append((p, tags))
    if not found:
        return None
    if tag:
        for p, tags in found:
            if f"[{tag}" in tags or f",{tag}" in tags:
                return p
    plain = [p for p, tags in found if "[" not in tags]
    if tag and not plain:   # a tagged PA (Descent/PreFlight) must not fall back to another phase's version
        others = {"preflight", "descent"} - {tag}
        found = [(p, t) for p, t in found if not any(o in t for o in others)]
    return (plain or [p for p, _ in found] or [None])[0]
