"""User settings management.

Settings stored as JSON in the user's home directory.
"""

import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from pickhero import jsonfile
from pickhero.validation import integer, number, text

def _jsonable(value):
    """Coerce anything json.dump chokes on into a plain Python number.

    Levels, frequencies and thresholds arrive from numpy as float32 or
    float64. They look like floats and compare like floats, and json.dump
    refuses them.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


CONFIG_DIR = Path.home() / ".pickhero"
CONFIG_FILE = CONFIG_DIR / "settings.json"


LOG_LIMIT_BYTES = 1_000_000


def log_error(message: str) -> None:
    """Record a failure somewhere it can actually be read.

    The shipped .exe is built with console=False, so sys.stdout is None and
    every print() in the app vanishes. Several failures the user needs to know
    about were reported that way, including the audio device falling back and
    the settings failing to save.
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}"
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        log = CONFIG_DIR / "pickhero.log"
        try:
            if log.stat().st_size > LOG_LIMIT_BYTES:
                # One generation is kept, enough to read back what happened.
                # One reached 7 MB, most of it one line repeated by test runs
                # that wrote here before they were isolated.
                log.replace(log.with_name(log.name + ".1"))
        except OSError:
            pass
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    try:
        if sys.stdout is not None:
            print(line)
    except Exception:
        pass


@dataclass
class StringCalibration:
    """Calibration data for a single guitar string."""
    midi_note: int        # detected MIDI note (e.g. 40 for E2)
    frequency: float      # median detected frequency (Hz)
    noise_floor_db: float  # noise floor measured before playing


@dataclass
class AudioConfig:
    """Audio capture and detection settings.

    The detection fields default to None, meaning "use whatever the active
    instrument profile says". They only hold a value once the user has
    deliberately overridden it. This matters because the right buffer size for
    a bass ukulele (4096) differs from the right one for a guitar (2048), and
    a stored guitar value must not silently cripple the bass profile.
    """
    device_index: int | None = None  # None = system default
    sample_rate: int = 44100
    buf_size: int | None = None
    hop_size: int | None = None
    confidence_threshold: float | None = None
    onset_threshold: float | None = None
    noise_gate_db: float | None = None  # ignore signals below this dB level


# The AudioConfig fields that belong to the instrument rather than to the
# person. None in any of them means "whatever the profile says".
PROFILE_AUDIO_FIELDS = (
    "buf_size", "hop_size", "confidence_threshold",
    "onset_threshold", "noise_gate_db",
)


@dataclass
class DisplayConfig:
    """Display and rendering settings."""
    width: int = 1280
    height: int = 720
    visible_beats: int = 16
    hit_zone_fraction: float = 0.20


@dataclass
class Config:
    """Application settings."""
    audio: AudioConfig = field(default_factory=AudioConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    instrument: str = "guitar"   # key into pickhero.instruments.PROFILES
    # Detection values the player set or measured, kept per instrument so
    # switching back and forth does not lose a measurement. See
    # switch_instrument.
    audio_by_instrument: dict = field(default_factory=dict)
    songs_dir: str = "songs"
    tempo_factor: float = 1.0
    timing_window_ms: float = 100.0
    audio_latency_offset_ms: float = 0.0
    chord_threshold_ms: float = 50.0
    backing_track_enabled: bool = True
    # The click, kept separate from the accompaniment: muting a backing track
    # is a taste, losing the beat is losing the exercise.
    metronome: bool = True
    count_in_beats: int = 4
    # Which of the three looks is active. The field used to be
    # called "theme" and held "dark" or "light"; load() translates
    # those, and does it there rather than at the point of use so a
    # file on disk cannot go on disagreeing with the running app
    # for a whole session.
    look: str = "studio"
    # Interface language. German is the reference language and the default;
    # an unknown code falls back to it rather than showing raw keys.
    language: str = "de"
    max_fret: int | None = None
    active_strings: list[bool] = field(default_factory=lambda: [True] * 6)
    chord_partial_credit: bool = True
    wait_mode: bool = False
    sort_mode: str = "name_asc"
    calibration: dict = field(default_factory=dict)
    # Whether the input guide has been through once. False means nobody has
    # been shown how to pick a microphone and set a level, so the app opens
    # the guide itself instead of waiting to be found. The guide existed long
    # before anything opened it, and a gate measured too high cost real notes
    # for days because the one screen that said so had to be discovered.
    setup_done: bool = False

    # Store default for HUD comparison (not serialized)
    _default_chord_partial_credit: bool = field(default=True, repr=False)

    def switch_instrument(self, key: str) -> None:
        """Change instrument, keeping each one's own detection settings.

        The detection values belong to the instrument: a noise gate measured
        against rubber strings says nothing about steel ones, and carrying it
        across is worse than having none. But simply clearing it would throw
        away a measurement the player had to sit still for, so the current
        values are kept under the instrument they were measured for and come
        back when that instrument does.
        """
        if key == self.instrument:
            return
        current = _validated_profile_audio({
            name: getattr(self.audio, name)
            for name in PROFILE_AUDIO_FIELDS
            if getattr(self.audio, name, None) is not None
        }, self.get_instrument(), self.audio.sample_rate)
        if current:
            self.audio_by_instrument[self.instrument] = current
        else:
            self.audio_by_instrument.pop(self.instrument, None)
        self.instrument = key
        stored = _validated_profile_audio(
            self.audio_by_instrument.get(key, {}), self.get_instrument(),
            self.audio.sample_rate)
        for name in PROFILE_AUDIO_FIELDS:
            setattr(self.audio, name, stored.get(name))

    def get_instrument(self):
        """The active instrument profile."""
        from pickhero import instruments
        return instruments.get(self.instrument)

    def effective_max_fret(self) -> int:
        """User override if set, otherwise the instrument's own fret count."""
        if self.max_fret is not None:
            return self.max_fret
        return self.get_instrument().max_fret

    def get_string_calibration(self, string: int) -> StringCalibration | None:
        """Return calibration for a string (1-6), or None if not calibrated."""
        strings = self.calibration.get("strings", {})
        data = strings.get(str(string))
        if data is None:
            return None
        return StringCalibration(**data)

    def set_string_calibration(self, string: int, cal: StringCalibration) -> None:
        """Store calibration for a string (1-6)."""
        if "strings" not in self.calibration:
            self.calibration["strings"] = {}
        self.calibration["strings"][str(string)] = asdict(cal)

    def is_calibrated(self) -> bool:
        """True if at least one string has been calibrated."""
        strings = self.calibration.get("strings", {})
        return len(strings) > 0

    def save(self) -> bool:
        """Save settings. Returns False instead of raising.

        Two things went wrong here and both were silent. Values measured from
        audio are numpy scalars, which json.dump cannot serialise, so the
        measured noise gate was never written and came back as null every
        time. And every caller assumed this could not fail, so a read-only
        home directory or a full disk would have taken the whole app down
        from a dozen places, one of them before the window even opens.
        """
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            data = asdict(self)
            tmp = CONFIG_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=_jsonable)
            tmp.replace(CONFIG_FILE)
            return True
        except (OSError, TypeError, ValueError) as exc:
            log_error(f"Einstellungen konnten nicht gespeichert werden: {exc}")
            return False

    @classmethod
    def load(cls) -> "Config":
        """Load settings from JSON file. Returns defaults if file doesn't exist."""
        if not CONFIG_FILE.exists():
            return cls()
        # Through jsonfile, because this file is not always UTF-8. One came
        # back one morning starting with 0xFF -- a UTF-16 byte order mark,
        # which PowerShell's redirect and Notepad's "Unicode" both write --
        # and the guard here caught OSError and JSONDecodeError, neither of
        # which is UnicodeDecodeError. The app died before the window opened.
        data = jsonfile.read(CONFIG_FILE, what="Die Einstellungsdatei")
        if not isinstance(data, dict):
            return cls()

        # Drop keys we do not know instead of discarding the whole file. The
        # original code passed everything straight into the constructor, so a
        # single unrecognised key raised TypeError and silently reset the
        # user's device choice, noise gate, tempo and calibration.
        def keep(cls_, raw):
            if not isinstance(raw, dict):
                return {}
            allowed = {f.name for f in fields(cls_)}
            return {k: v for k, v in raw.items() if k in allowed}

        # A settings file written before the looks existed says theme: dark
        # or theme: light. Neither was ever inverted, so each has exactly one
        # honest successor. Translating here rather than where the value is
        # used means the file and the running app cannot disagree for a whole
        # session, and the next save writes the new name.
        legacy = data.pop("theme", None)
        if legacy is not None and "look" not in data:
            data["look"] = {"dark": "studio", "light": "hifi"}.get(legacy if isinstance(legacy, str) else "",
                                                                   "studio")

        data.pop("_default_chord_partial_credit", None)
        audio_data = keep(AudioConfig, data.pop("audio", {}))

        # A settings file from before instruments existed carries detection
        # values chosen for a guitar and written unconditionally, not because
        # anyone picked them. Handing a 2048 buffer and a 0.8 confidence
        # threshold to the bass ukulele profile is exactly the configuration
        # that made detection feel random. Those files predate the instrument
        # key, so its absence is the signal: keep the device and the sample
        # rate, drop the rest back to "follow the profile".
        if "instrument" not in data:
            stale = [k for k in PROFILE_AUDIO_FIELDS if audio_data.get(k) is not None]
            for key in stale:
                audio_data.pop(key, None)
            if stale:
                log_error("Alte Einstellungen: Erkennungswerte "
                          f"({', '.join(stale)}) folgen wieder dem Instrument.")
        display_data = keep(DisplayConfig, data.pop("display", {}))
        audio_data = _validated_audio(audio_data)
        for name, low, high in (("width", 320, 7680), ("height", 240, 4320),
                                ("visible_beats", 1, 128)):
            if name in display_data and not integer(display_data[name], low, high):
                display_data.pop(name)
        if "hit_zone_fraction" in display_data and not number(display_data["hit_zone_fraction"], 0.05, 0.8):
            display_data.pop("hit_zone_fraction")
        data = _validated_settings(keep(cls, data))
        profile = cls(instrument=data.get("instrument", "guitar")).get_instrument()
        sample_rate = audio_data.get("sample_rate", AudioConfig.sample_rate)
        audio_data = _validated_profile_audio(audio_data, profile, sample_rate)
        from pickhero.instruments import get
        if "audio_by_instrument" in data:
            data["audio_by_instrument"] = {
                key: _validated_profile_audio(stored, get(key), sample_rate)
                for key, stored in data["audio_by_instrument"].items()}
        try:
            return cls(
                audio=AudioConfig(**audio_data),
                display=DisplayConfig(**display_data),
                **keep(cls, data),
            )
        except (TypeError, ValueError):
            return cls()


def _validated_audio(raw: dict) -> dict:
    valid = {}
    for key, value in raw.items():
        if value is None and key != "sample_rate":
            valid[key] = None
        elif key == "device_index" and integer(value, 0, 1_000_000):
            valid[key] = value
        elif key == "sample_rate" and integer(value, 8000, 384000):
            valid[key] = value
        elif key in ("buf_size", "hop_size") and integer(value, 64, 32768) and value & (value - 1) == 0:
            valid[key] = value
        elif key == "confidence_threshold" and number(value, 0, 1):
            valid[key] = value
        elif key == "onset_threshold" and number(value, 0, 100):
            valid[key] = value
        elif key == "noise_gate_db" and number(value, -120, 0):
            valid[key] = value
    return valid


def _validated_profile_audio(raw, profile, sample_rate: int) -> dict:
    """Restore overrides only when pitch and onset windows can accept them.

    Window defaults scale with the sample rate. Invalid buffer combinations
    revert to those defaults without discarding a measured gate or confidence.
    """
    valid = _validated_audio(raw) if isinstance(raw, dict) else {}
    scale = sample_rate / profile.sample_rate
    default_buf = max(256, int(round(profile.buf_size * scale / 64.0)) * 64)
    default_hop = min(profile.onset_buf_size,
                      max(64, int(round(profile.hop_size * scale))))
    hop = valid.get("hop_size") or default_hop
    buf = valid.get("buf_size") or default_buf
    if hop > min(buf, profile.onset_buf_size):
        valid.pop("hop_size", None)
        hop = default_hop
    if hop > buf:
        valid.pop("buf_size", None)
    return valid


def _validated_settings(data: dict) -> dict:
    from pickhero.instruments import PROFILES

    valid = {}
    choices = {"instrument": tuple(PROFILES), "look": ("studio", "stage", "hifi"),
               "sort_mode": ("name_asc", "difficulty", "accuracy", "last_played")}
    bounds = {"tempo_factor": (0.1, 2), "timing_window_ms": (1, 1000),
              "audio_latency_offset_ms": (-5000, 5000), "chord_threshold_ms": (0, 1000)}
    booleans = {"backing_track_enabled", "metronome", "chord_partial_credit", "wait_mode",
                "setup_done"}
    for name, value in data.items():
        if name in choices and isinstance(value, str) and value in choices[name]:
            valid[name] = value
        elif name in bounds and number(value, *bounds[name]):
            valid[name] = value
        elif name in booleans and type(value) is bool:
            valid[name] = value
        elif (name == "language" and isinstance(value, str)
              and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", value)):
            # User catalogues in ~/.pickhero/lang are supported too. Keep a
            # safe code here; i18n applies its normal fallback if unavailable.
            valid[name] = value
        elif name == "songs_dir" and text(value) and value.strip():
            valid[name] = value
        elif name == "count_in_beats" and integer(value, 0, 16):
            valid[name] = value
        elif name == "max_fret" and (value is None or integer(value, 0, 36)):
            valid[name] = value
        elif name == "active_strings" and isinstance(value, list) and 1 <= len(value) <= 12:
            if all(type(active) is bool for active in value) and any(value):
                valid[name] = value
        elif name == "audio_by_instrument" and isinstance(value, dict):
            valid[name] = {key: _validated_audio({k: v for k, v in stored.items()
                                                 if k in PROFILE_AUDIO_FIELDS})
                           for key, stored in value.items()
                           if key in PROFILES and isinstance(stored, dict)}
        elif name == "calibration" and isinstance(value, dict):
            calibration = {key: value[key] for key in ("instrument", "calibrated_at")
                           if text(value.get(key), 100)}
            calibration["strings"] = {}
            strings = value.get("strings")
            if isinstance(strings, dict):
                for string, item in strings.items():
                    if (string not in {str(i) for i in range(1, 13)} or not isinstance(item, dict)
                            or not integer(item.get("midi_note"), 0, 127)
                            or not number(item.get("frequency"), 10, 20000)
                            or not number(item.get("noise_floor_db"), -200, 0)):
                        continue
                    calibration["strings"][string] = {
                        k: item[k] for k in ("midi_note", "frequency", "noise_floor_db")}
            valid[name] = calibration
    return valid
