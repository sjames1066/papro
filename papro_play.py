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
import time
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger("papro")

CHUNK = 1024


LAST_ERROR = ""   # why the last list_output_devices() came back empty ("missing" = library not installed)


def list_output_devices() -> list[dict]:
    """Sound outputs on this PC: [{"name", "index", "default"}]. Windows lists every device once per audio system (MME,
    DirectSound, WASAPI...); only one system is shown - WASAPI when there is one - so each device appears once.
    Returns [] when no audio library / driver is available."""
    global LAST_ERROR
    LAST_ERROR = ""
    try:
        import sounddevice as sd

        apis = sd.query_hostapis()
        devices = sd.query_devices()
    except ImportError:
        LAST_ERROR = "missing"
        log.warning("The sounddevice library is not installed in this copy of PAPro - no audio outputs to list")
        return []
    except Exception as exc:
        LAST_ERROR = str(exc)[:200]
        log.warning("No audio outputs listed: %s", exc)
        return []
    wanted = next((i for i, a in enumerate(apis) if "wasapi" in a["name"].lower()), None)
    if wanted is None:
        try:
            wanted = sd.default.hostapi if sd.default.hostapi >= 0 else 0
        except Exception:
            wanted = 0
    try:
        default_index = apis[wanted].get("default_output_device", -1)
    except Exception:
        default_index = -1
    out, seen = [], set()
    for i, d in enumerate(devices):
        if d.get("hostapi") != wanted or int(d.get("max_output_channels", 0)) <= 0 or d["name"] in seen:
            continue
        seen.add(d["name"])
        out.append({"name": d["name"], "index": i, "default": i == default_index})
    return out


def find_device(name: str) -> int | None:
    """Index of the output called `name`, or None (use the Windows default) if empty or no longer there."""
    if not name:
        return None
    return next((d["index"] for d in list_output_devices() if d["name"] == name), None)


def test_tone(rate: int = 44100) -> tuple[np.ndarray, int]:
    """A short two-note chime (like the cabin 'ding-dong') for checking the output device."""
    def note(freq: float, seconds: float) -> np.ndarray:
        t = np.arange(int(rate * seconds)) / rate
        return (np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(4 * np.pi * freq * t)) * np.exp(-3.2 * t) * 0.5

    data = np.concatenate([note(784, 0.55), note(622, 0.8)]).astype("float32")
    return data.reshape(-1, 1), rate


class SoundDeviceBackend:
    """Real output through sounddevice (PortAudio). open() -> a stream with write(frames). `device` is an output's
    name (as in list_output_devices) or ""/None for the Windows default."""

    def __init__(self, device=None):
        self.device = device

    def open(self, rate: int, channels: int):
        import sounddevice as sd

        index = find_device(self.device) if isinstance(self.device, str) else self.device
        try:
            return self._open(sd, rate, channels, index)
        except Exception:
            if index is None:
                raise
            log.warning("Could not open the chosen output - using the default instead")
            return self._open(sd, rate, channels, None)

    @staticmethod
    def _open(sd, rate, channels, index):
        """Opens the device at its own rate and channel count; the clip is converted to that (see _Converting)."""
        info = sd.query_devices(index if index is not None else sd.default.device[1])
        dev_rate = int(info.get("default_samplerate") or rate)
        dev_ch = max(1, min(2, int(info.get("max_output_channels") or 2)))
        stream = sd.OutputStream(samplerate=dev_rate, channels=dev_ch, dtype="float32", device=index)
        stream.start()
        log.info("Audio output: %s at %d Hz, %d ch (clip %d Hz, %d ch)", info.get("name"), dev_rate, dev_ch, rate, channels)
        return _Converting(stream, rate, channels, dev_rate, dev_ch)


class _Converting:
    """An output stream plus convert_all(): a whole clip in the device's rate and channels."""

    def __init__(self, stream, rate, channels, dev_rate, dev_ch):
        self.stream, self.rate, self.channels, self.dev_rate, self.dev_ch = stream, rate, channels, dev_rate, dev_ch

    def convert_all(self, data):
        data = np.asarray(data, dtype="float32")
        if self.rate != self.dev_rate and len(data):
            n = len(data)
            m = max(1, int(round(n * self.dev_rate / self.rate)))
            spec = np.fft.rfft(data, axis=0)
            out = np.zeros((m // 2 + 1, data.shape[1]), dtype=spec.dtype)
            k = min(len(spec), len(out))
            out[:k] = spec[:k]
            data = (np.fft.irfft(out, n=m, axis=0) * (m / n)).astype("float32")
        if data.shape[1] != self.dev_ch:
            data = np.repeat(data[:, :1], self.dev_ch, axis=1) if data.shape[1] == 1 else data[:, :self.dev_ch]
        return np.ascontiguousarray(data)

    def write(self, data):
        return self.stream.write(np.ascontiguousarray(data, dtype="float32"))

    def stop(self):
        self.stream.stop()

    def abort(self):
        self.stream.abort()

    def close(self):
        self.stream.close()


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

    def play_data(self, name: str, data: np.ndarray, rate: int) -> None:
        """Queue sound that is already in memory (the test chime)."""
        self.q.put((name, data, rate))

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
            self.playing = path[0] if isinstance(path, tuple) else path.name
            try:
                self._play_one(path)
            except Exception:
                log.exception("Could not play %s", path)
            finally:
                self.playing = None

    def _play_one(self, path) -> None:
        data, rate = (path[1], path[2]) if isinstance(path, tuple) else self.loader(path)
        stream = self.backend.open(rate, data.shape[1])
        if hasattr(stream, "convert_all"):
            data = stream.convert_all(data)
        try:
            for start in range(0, len(data), CHUNK):
                if self.stop_flag.is_set():
                    break
                level = max(0.0, min(2.0, float(self.gain())))
                chunk = data[start:start + CHUNK] * level
                stream.write(np.ascontiguousarray(np.clip(chunk, -1.0, 1.0), dtype="float32"))
        finally:
            try:
                if self.stop_flag.is_set() and hasattr(stream, "abort"):
                    stream.abort()   # skipped: cut the sound now instead of letting the buffered audio play out
                else:
                    stream.stop()
                stream.close()
            except Exception:
                pass


class MusicLoop:
    """The boarding music: one file played over and over on its own stream (so announcements can play on top of it)
    until stop() - which fades it out. Level and mute come from `gain()` like the announcements."""

    FADE_S = 2.0

    def __init__(self, gain: Callable[[], float], backend=None, loader=load_clip):
        self.gain, self.backend, self.loader = gain, backend or SoundDeviceBackend(), loader
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self.pinged = time.monotonic()
        self.duck: Callable[[], bool] | None = None   # True while an announcement is playing: the music goes quiet for it
        self._duck_level = 1.0

    def ping(self) -> None:
        """Called about once a second while the sim is sending data; the music stops by itself if the calls stop (sim closed)."""
        self.pinged = time.monotonic()

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive() and not self.stop_flag.is_set()

    def start(self, path: Path) -> None:
        if self.running():
            return
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(Path(path), self.stop_flag), daemon=True)
        self.thread.start()

    def stop(self, fade: bool = True) -> None:
        self.stop_flag.set()
        self.fade = fade

    fade = True

    def _run(self, path: Path, stop: threading.Event) -> None:
        try:
            data, rate = self.loader(path)
            if not len(data):
                return
            stream = self.backend.open(rate, data.shape[1])
            if hasattr(stream, "convert_all"):
                data, rate = stream.convert_all(data), stream.dev_rate
        except Exception:
            log.exception("Could not start the boarding music %s", path)
            return
        fade_total = int(rate * self.FADE_S)
        faded = 0
        try:
            pos = 0
            while True:
                pos = 0 if pos >= len(data) else pos
                while pos < len(data):
                    start = pos
                    level = max(0.0, min(2.0, float(self.gain())))
                    try:
                        ducked = bool(self.duck()) if self.duck else False
                    except Exception:
                        ducked = False
                    now = time.monotonic()
                    if ducked:   # one sound at a time: the music is silent the moment an announcement is playing
                        self._duck_level, self._duck_until = 0.0, now + 0.8   # ...and stays silent through the gap between queued clips
                    elif now >= getattr(self, "_duck_until", 0.0):
                        self._duck_level = min(1.0, self._duck_level + CHUNK / rate / 1.5)   # then comes back gently
                    paused = ducked or now < getattr(self, "_duck_until", 0.0)   # paused for a PA: hold the place in the track
                    if paused and not stop.is_set():
                        stream.write(np.zeros((CHUNK, data.shape[1]), dtype="float32"))
                        continue
                    chunk = data[start:start + CHUNK] * (level * self._duck_level)
                    pos += CHUNK
                    if not stop.is_set() and time.monotonic() - self.pinged > 8.0:
                        stop.set()
                    if stop.is_set():
                        if not self.fade:
                            return
                        ramp = 1.0 - (faded + np.arange(len(chunk))) / fade_total
                        chunk = chunk * np.clip(ramp, 0.0, 1.0)[:, None]
                        faded += len(chunk)
                    stream.write(np.ascontiguousarray(np.clip(chunk, -1.0, 1.0), dtype="float32"))
                    if stop.is_set() and faded >= fade_total:
                        return
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


def music_file(folder: Path) -> Path | None:
    """The merged boarding music in the built pack, if there is one."""
    p = Path(folder) / "BoardingMusic.ogg"
    return p if p.is_file() else None


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
