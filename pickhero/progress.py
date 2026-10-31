"""Per-song progress tracking.

Stores best accuracy, attempt count, last played date, and detailed
section/tempo history per song. JSON file in the user's home directory
alongside settings.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pickhero import jsonfile
from pickhero.config import CONFIG_DIR, _jsonable, log_error
from pickhero.validation import integer, number, text

PROGRESS_FILE = CONFIG_DIR / "progress.json"
MAX_SAVED_PASSAGES = 32
MAX_PROGRESS_BYTES = jsonfile.MAX_JSON_BYTES


class ProgressCapacityError(ValueError):
    pass


def song_key(path: str | Path, instrument: str, track_index: int = 0, *, content_id: str | None = None) -> str:
    """A file, selected track and instrument form one practice history."""
    if (isinstance(content_id, str) and len(content_id) == 64
            and all(c in "0123456789abcdef" for c in content_id)):
        return f"song-v3|{instrument}|{track_index}|{content_id}"
    identity = os.path.normcase(str(Path(path).resolve()))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"song-v2|{instrument}|{track_index}|{digest}"


@dataclass
class SongRecord:
    best_accuracy: float = 0.0
    best_hits: int = 0
    best_total: int = 0
    attempts: int = 0
    last_played: str = ""

    # Per-section accuracy history across attempts.
    # Each entry: {"attempt": N, "sections": [{"measures": [start, end], "accuracy": float}, ...]}
    section_history: list[dict] = field(default_factory=list)

    # Per-attempt tempo/accuracy tracking.
    # Each entry: {"attempt": N, "tempo_factor": float, "accuracy": float}
    tempo_history: list[dict] = field(default_factory=list)
    # Sections never contribute to the full-song personal best.
    section_attempts: list[dict] = field(default_factory=list)
    practice_state: dict = field(default_factory=dict)
    migrated_content_id: str = ""
    goal_pitch_met: bool = False
    goal_rhythm_met: bool = False
    saved_passages: list[dict] = field(default_factory=list)
    passages_changed_at: str = ""
    section_result_count: int = 0
    advice_offers: list[dict] = field(default_factory=list)


class ProgressTracker:
    """Loads and saves per-song progress data."""

    def __init__(self) -> None:
        self._data: dict[str, SongRecord] = {}
        self._revision = 0
        self._last_save_error = None
        self._load()

    @property
    def revision(self) -> int:
        """In-process mutation version for derived library views."""
        return self._revision

    @property
    def last_save_error(self) -> str | None:
        return self._last_save_error

    def record_result(self, song_key: str, stats: dict) -> bool:
        """Record a completed attempt. Returns True if new personal best."""
        if stats.get("reliable", True) is not True:
            return False
        record = self._data.get(song_key, SongRecord())
        record.attempts += 1
        record.last_played = datetime.now(timezone.utc).isoformat()

        accuracy = stats.get("accuracy_percent", 0.0)

        is_new_best = accuracy > record.best_accuracy
        if is_new_best:
            record.best_accuracy = accuracy
            record.best_hits = stats.get("hits", 0)
            record.best_total = stats.get("total", 0)

        self._data[song_key] = record
        self._save()
        return is_new_best

    def record_detailed_result(
        self,
        song_key: str,
        stats: dict,
        weakest_sections: list[tuple[int, int, float]],
        tempo_factor: float,
    ) -> tuple[bool, list[str]]:
        """Record a completed attempt with section-level detail.

        Calls record_result internally for backward compatibility, then
        stores section and tempo history and generates recommendations.

        Args:
            song_key: Unique identifier for the song/track.
            stats: Dict from matcher.get_statistics().
            weakest_sections: List of (start_measure, end_measure, accuracy_pct).
            tempo_factor: Tempo scaling factor used for this attempt (0.5-1.0).

        Returns:
            Tuple of (is_new_best, recommendations) where recommendations
            is a list of short practice suggestion strings.
        """
        if stats.get("reliable", True) is not True:
            return False, []
        if stats.get("timing_unknown", 0):
            stats = dict(stats, timing_assessed=False)
        is_new_best = self.record_result(song_key, stats)
        record = self._data[song_key]
        accuracy = stats.get("accuracy_percent", 0.0)
        if stats.get("reliable") is True and tempo_factor >= 1 and accuracy >= 80:
            record.goal_pitch_met = True
            if stats.get("timing_assessed") is True and number(stats.get("timing_percent"), 60, 100):
                record.goal_rhythm_met = True

        # Store section history for this attempt
        section_entry = {
            "attempt": record.attempts,
            "sections": [
                {"measures": [s, e], "accuracy": acc}
                for s, e, acc in weakest_sections
            ],
        }
        record.section_history.append(section_entry)

        # Store tempo history for this attempt
        tempo_entry = {
            "attempt": record.attempts,
            "tempo_factor": tempo_factor,
            "accuracy": accuracy,
            "timing_percent": stats.get("timing_percent", 0.0),
            "timing_assessed": stats.get("timing_assessed", True),
            "reliable": True,
        }
        tempo_entry.update(_timing_counts(stats))
        record.tempo_history.append(tempo_entry)

        # Cap history length to prevent unbounded growth (keep last 50)
        if len(record.section_history) > 50:
            record.section_history = record.section_history[-50:]
        if len(record.tempo_history) > 50:
            record.tempo_history = record.tempo_history[-50:]

        self._save()

        # Generate recommendations
        from pickhero.recommendations import generate_recommendations

        recommendations = generate_recommendations(
            record, stats, weakest_sections, tempo_factor,
        )

        return is_new_best, recommendations

    def save_session(self, key: str, state: dict) -> None:
        """Persist an explicit resume point; never treat it as an attempt."""
        valid = validate_session(state)
        if not key or not valid:
            return
        valid["saved_at"] = datetime.now(timezone.utc).isoformat()
        valid["saved_order"] = max((r.practice_state.get("saved_order", 0)
                                    for r in self._data.values()), default=0) + 1
        record = self._data.setdefault(key, SongRecord())
        record.practice_state = valid
        self._save()

    def get_session(self, key: str) -> dict | None:
        record = self._data.get(key)
        if record is None or not record.practice_state:
            return None
        return dict(copy.deepcopy(record.practice_state), song_key=key)

    def latest_session(self, instrument: str | None = None) -> dict | None:
        states = [self.get_session(key) for key in self._data]
        states = [s for s in states if s and (instrument is None or s["instrument"] == instrument)]
        return max(states, key=lambda s: (s.get("saved_order", 0), s.get("saved_at", ""),
                                         s["song_key"].startswith("song-v3|")), default=None)

    def record_section_result(self, key: str, stats: dict, context: dict) -> float | None:
        """Save one completed passage, returning an optional tempo suggestion."""
        entry = _validated_section(dict(context, **{
            "accuracy": stats.get("accuracy_percent", 0),
            "timing_percent": stats.get("timing_percent", 0),
            "total": stats.get("total", 0),
            "timing_assessed": stats.get("timing_assessed", False),
            "reliable": stats.get("reliable", True),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            **_timing_counts(stats),
        }))
        if not key or entry is None:
            return None
        record = self._data.setdefault(key, SongRecord())
        record.section_attempts.append(entry)
        record.section_attempts = record.section_attempts[-100:]
        record.section_result_count += 1
        self._save()
        from pickhero.recommendations import section_tempo_suggestion
        return section_tempo_suggestion(record.section_attempts)

    def claim_section_advice(self, key: str) -> dict | None:
        """Remember an offered choice so reloading cannot repeatedly nag."""
        from pickhero.practice_advice import section_advice
        record = self._data.get(key)
        if record is None:
            return None
        advice = section_advice(record.section_attempts, record.advice_offers,
                                record.section_result_count)
        if advice is not None:
            record.advice_offers.append(dict(advice, at_result=record.section_result_count))
            record.advice_offers = record.advice_offers[-32:]
            self._save()
        return advice

    def saved_passages(self, key: str) -> list[dict]:
        record = self._data.get(key)
        return copy.deepcopy(record.saved_passages) if record else []

    def save_passage(self, key: str, name: str, state: dict) -> str:
        """Save a named loop under this score, instrument and track identity."""
        name = passage_name(name)
        valid = _passage_state(state)
        if not key or not valid or not _state_matches_key(key, valid):
            raise ValueError("state")
        before = copy.deepcopy(self._data.get(key))
        record = self._data.setdefault(key, SongRecord())
        if len(record.saved_passages) >= MAX_SAVED_PASSAGES:
            raise ValueError("limit")
        if any(item["name"].casefold() == name.casefold() for item in record.saved_passages):
            raise ValueError("duplicate")
        identifier = uuid4().hex
        stamp = datetime.now(timezone.utc).isoformat()
        record.saved_passages.append({"id": identifier, "name": name, "state": valid,
                                      "created_at": stamp, "updated_at": stamp})
        record.passages_changed_at = stamp
        if not self._save():
            if before is None:
                self._data.pop(key, None)
            else:
                self._data[key] = before
            raise ValueError(self._last_save_error or "write")
        return identifier

    def rename_passage(self, key: str, identifier: str, name: str) -> bool:
        name = passage_name(name)
        record = self._data.get(key)
        if record is None:
            return False
        item = next((item for item in record.saved_passages if item["id"] == identifier), None)
        if item is None:
            return False
        if any(other["id"] != identifier and other["name"].casefold() == name.casefold()
               for other in record.saved_passages):
            raise ValueError("duplicate")
        before = copy.deepcopy(record)
        item["name"], item["updated_at"] = name, datetime.now(timezone.utc).isoformat()
        record.passages_changed_at = item["updated_at"]
        if not self._save():
            self._data[key] = before
            raise ValueError(self._last_save_error or "write")
        return True

    def delete_passage(self, key: str, identifier: str) -> bool:
        record = self._data.get(key)
        if record is None:
            return False
        remaining = [item for item in record.saved_passages if item["id"] != identifier]
        if len(remaining) == len(record.saved_passages):
            return False
        before = copy.deepcopy(record)
        record.saved_passages = remaining
        record.passages_changed_at = datetime.now(timezone.utc).isoformat()
        if not self._save():
            self._data[key] = before
            raise ValueError(self._last_save_error or "write")
        return True

    def passage_comparison(self, key: str, identifier: str, *, input_key=None) -> list[dict]:
        from pickhero.practice_advice import comparable_attempts
        record = self._data.get(key)
        if record is None:
            return []
        item = next((item for item in record.saved_passages if item["id"] == identifier), None)
        return comparable_attempts(record.section_attempts, item["state"], input_key) if item else []

    def migrate_content_identity(self, song_file, instrument: str, track_index: int, content_id: str) -> None:
        """Migrate a single imported score and persist only an actual change."""
        if self._migrate_content_identity(song_file, instrument, track_index, content_id):
            self._save()

    def migrate_content_identities(self, songs, instrument: str) -> None:
        """Migrate a library scan with one atomic write, regardless of size."""
        changed = False
        check_legacy = any(key.startswith("song-v2|") for key in self._data)
        for song in songs:
            changed = self._migrate_content_identity(
                song.file, instrument, song.track_index,
                getattr(song, "content_id", None), check_legacy=check_legacy) or changed
        if changed:
            self._save()

    def _migrate_content_identity(self, song_file, instrument: str, track_index: int,
                                  content_id: str | None, *, check_legacy=True) -> bool:
        if not self._data:
            return False
        new = song_key(song_file, instrument, track_index, content_id=content_id)
        old = song_key(song_file, instrument, track_index) if check_legacy else None
        changed = False
        source = self._data.get(old)
        if old != new and source is not None and not source.migrated_content_id:
            # A path may later contain a different song. Bind its legacy
            # history exactly once; retain the original as a readable backup.
            source.migrated_content_id = content_id
            if new not in self._data:
                self._data[new] = copy.deepcopy(source)
            changed = True
        record = self._data.get(new)
        if record is None or (not record.practice_state and not record.saved_passages):
            return changed
        requested_path = str(song_file)
        stored_paths = ([record.practice_state["song_path"]] if record.practice_state else [])
        stored_paths.extend(item["state"]["song_path"] for item in record.saved_passages)
        if Path(song_file).is_absolute() and all(path == requested_path for path in stored_paths):
            return changed
        actual_path = str(Path(song_file).resolve())
        if record and record.practice_state and record.practice_state.get("song_path") != actual_path:
            record.practice_state["song_path"] = actual_path
            changed = True
        if record:
            for passage in record.saved_passages:
                if passage["state"]["song_path"] != actual_path:
                    passage["state"]["song_path"] = actual_path
                    changed = True
        return changed

    def migrate_content_replacement(self, old_id: str, new_id: str, instrument: str) -> None:
        """Keep history across an explicitly verified generated lesson rewrite."""
        if (old_id == new_id or len(old_id) != 64 or len(new_id) != 64
                or any(c not in "0123456789abcdef" for c in old_id + new_id)):
            return
        changed = False
        for key, record in list(self._data.items()):
            parts = key.split("|")
            if len(parts) == 4 and parts[:2] == ["song-v3", instrument] and parts[3] == old_id:
                new = "|".join(parts[:3] + [new_id])
                target = self._data.get(new)
                if target is None:
                    self._data[new] = copy.deepcopy(record)
                else:
                    # This API is restricted to equivalent generated lessons.
                    # Returning to an earlier language must retain the newer
                    # attempts and independently preserve any earned goals.
                    latest = max((record, target), key=lambda r: (
                        r.last_played, r.attempts,
                        max((e.get("completed_at", "") for e in r.section_attempts), default="")))
                    merged = copy.deepcopy(latest)
                    best = max((record, target), key=lambda r: r.best_accuracy)
                    merged.best_accuracy, merged.best_hits, merged.best_total = (
                        best.best_accuracy, best.best_hits, best.best_total)
                    merged.attempts = max(record.attempts, target.attempts)
                    merged.goal_pitch_met = record.goal_pitch_met or target.goal_pitch_met
                    merged.goal_rhythm_met = record.goal_rhythm_met or target.goal_rhythm_met
                    merged.practice_state = copy.deepcopy(max((record.practice_state, target.practice_state),
                        key=lambda state: (state.get("saved_order", 0), state.get("saved_at", ""))))
                    # The latest explicit list edit includes deletions. A
                    # union would resurrect a deleted loop on language return.
                    passage_source = max((record, target), key=lambda r: r.passages_changed_at)
                    merged.saved_passages = copy.deepcopy(passage_source.saved_passages)
                    merged.passages_changed_at = passage_source.passages_changed_at
                    self._data[new] = merged
                changed = True
        if changed:
            self._save()

    def get_best(self, song_key: str) -> SongRecord | None:
        """Get best record for a song, or None if never played."""
        return self._data.get(song_key)

    def migrate_legacy(self, songs, instrument: str) -> None:
        """Assign unambiguous old stems once; preserve a copy of the original.

        Older files contain neither path nor instrument. Only a unique stem
        in the current library can be associated with the currently selected
        instrument. Ambiguous records stay intact for later manual recovery.
        """
        by_stem = {}
        for song in songs:
            by_stem.setdefault(os.path.normcase(song.file.stem), []).append(song)
        changed = False
        for old in list(self._data):
            if old.startswith(("song-v2|", "song-v3|", "legacy-v1|")):
                continue
            candidates = by_stem.get(os.path.normcase(old), [])
            if len(candidates) != 1 or not candidates[0].playable:
                continue
            song = candidates[0]
            key = song_key(song.file, instrument, song.track_index,
                           content_id=getattr(song, "content_id", None))
            original = self._data.pop(old)
            self._data.setdefault(f"legacy-v1|{old}", copy.deepcopy(original))
            self._data.setdefault(key, original)
            changed = True
        if changed:
            self._save()

    def _load(self) -> None:
        """Read the history, tolerating anything the file might contain.

        One malformed record used to abort the whole load, so a single bad
        entry lost every song's history. Worse, the failure happened during
        App construction, so a file that was not a dict at all took the app
        down before the window opened.
        """
        if not PROGRESS_FILE.exists():
            return
        # Their practice history. Worth more than the settings, so it goes
        # through the same reader, which tries the other encodings before it
        # gives up and moves an unreadable file aside rather than letting the
        # next save write over it.
        raw = jsonfile.read(PROGRESS_FILE, what="Der Fortschritt")
        if raw is None:
            return
        if not isinstance(raw, dict):
            log_error("Fortschrittsdatei hat ein unerwartetes Format, wird ignoriert.")
            return
        for key, val in raw.items():
            if not isinstance(val, dict):
                continue
            try:
                # Backward compat: ignore unknown fields, supply defaults
                # for new fields that may be missing from old data.
                if text(key):
                    record = _validated_record(val)
                    record.saved_passages = [item for item in record.saved_passages
                                             if _state_matches_key(key, item["state"])]
                    self._data[key] = record
            except (TypeError, ValueError):
                # Skip this one record, keep the rest.
                continue

    def _save(self) -> bool:
        """Write the history atomically, and never take the app down for it.

        It used to write straight over the live file, so a crash or a full
        disk mid-write destroyed every record rather than the newest one.
        """
        self._revision += 1
        tmp = PROGRESS_FILE.with_suffix(".tmp")
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            encoder = json.JSONEncoder(indent=2, default=_jsonable, ensure_ascii=False)
            count = 0
            with open(tmp, "wb") as f:
                for chunk in encoder.iterencode({k: asdict(v) for k, v in self._data.items()}):
                    data = chunk.encode("utf-8")
                    count += len(data)
                    if count > min(MAX_PROGRESS_BYTES, jsonfile.MAX_JSON_BYTES):
                        raise ProgressCapacityError("Progress storage capacity reached")
                    f.write(data)
            tmp.replace(PROGRESS_FILE)
            self._last_save_error = None
            return True
        except (OSError, TypeError, ValueError) as exc:
            self._last_save_error = "capacity" if isinstance(exc, ProgressCapacityError) else "write"
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            log_error(f"Fortschritt konnte nicht gespeichert werden: {exc}")
            return False


def _validated_record(raw: dict) -> SongRecord:
    record = SongRecord()
    if number(raw.get("best_accuracy"), 0, 100):
        record.best_accuracy = raw["best_accuracy"]
    for name in ("best_hits", "best_total", "attempts"):
        if integer(raw.get(name), 0, 1_000_000_000):
            setattr(record, name, raw[name])
    if text(raw.get("last_played"), 100):
        record.last_played = raw["last_played"]
    for name in ("section_history", "tempo_history"):
        entries = raw.get(name)
        if not isinstance(entries, list):
            continue
        for entry in entries[-50:]:
            if not isinstance(entry, dict) or not integer(entry.get("attempt"), 0, 1_000_000_000):
                continue
            if name == "tempo_history":
                if number(entry.get("tempo_factor"), 0.1, 2) and number(entry.get("accuracy"), 0, 100):
                    item = {k: entry[k] for k in ("attempt", "tempo_factor", "accuracy")}
                    if number(entry.get("timing_percent"), 0, 100):
                        item["timing_percent"] = entry["timing_percent"]
                    for flag in ("timing_assessed", "reliable"):
                        if type(entry.get(flag)) is bool:
                            item[flag] = entry[flag]
                    item.update(_timing_counts(entry))
                    if item.get("timing_unknown", 0):
                        item["timing_assessed"] = False
                    record.tempo_history.append(item)
            elif isinstance(entry.get("sections"), list):
                sections = []
                for section in entry["sections"][:10000]:
                    if not isinstance(section, dict):
                        continue
                    measures = section.get("measures")
                    if (isinstance(measures, list) and len(measures) == 2
                            and all(integer(m, 0, 1_000_000) for m in measures)
                            and measures[0] <= measures[1]
                            and number(section.get("accuracy"), 0, 100)):
                        sections.append({"measures": measures, "accuracy": section["accuracy"]})
                record.section_history.append({"attempt": entry["attempt"], "sections": sections})
    record.practice_state = validate_session(raw.get("practice_state"))
    binding = raw.get("migrated_content_id")
    if isinstance(binding, str) and len(binding) == 64 and all(c in "0123456789abcdef" for c in binding):
        record.migrated_content_id = binding
    for flag in ("goal_pitch_met", "goal_rhythm_met"):
        if type(raw.get(flag)) is bool:
            setattr(record, flag, raw[flag])
    entries = raw.get("section_attempts")
    if isinstance(entries, list):
        record.section_attempts = [valid for entry in entries[-100:]
                                   if (valid := _validated_section(entry)) is not None]
    count = raw.get("section_result_count")
    record.section_result_count = max(len(record.section_attempts),
                                     count if integer(count, 0, 1_000_000_000) else 0)
    entries = raw.get("saved_passages")
    if isinstance(entries, list):
        seen = set()
        for item in entries[:MAX_SAVED_PASSAGES]:
            valid = _validated_passage(item)
            if valid and valid["id"] not in seen:
                record.saved_passages.append(valid)
                seen.add(valid["id"])
    stamp = raw.get("passages_changed_at")
    if text(stamp, 100):
        try:
            datetime.fromisoformat(stamp)
            record.passages_changed_at = stamp
        except ValueError:
            pass
    from pickhero.practice_advice import validated_offer
    entries = raw.get("advice_offers")
    if isinstance(entries, list):
        record.advice_offers = [item for raw_offer in entries[-32:]
                               if (item := validated_offer(raw_offer)) is not None]
    return record


def passage_name(value) -> str:
    if (not isinstance(value, str) or not value.isprintable()
            or not 1 <= len(value.strip()) <= 60):
        raise ValueError("name")
    return value.strip()


def _passage_state(raw) -> dict:
    state = validate_session(raw)
    required = ("tempo_factor", "loop_start_ms", "loop_end_ms", "active_strings",
                "max_fret", "wait_mode", "chord_partial_credit")
    if (not state or any(name not in state for name in required)
            or state.get("loop_enabled") is not True
            or state["loop_end_ms"] <= state["loop_start_ms"]):
        return {}
    state["position_ms"] = state["loop_start_ms"]
    state["completed"] = False
    for name in ("saved_at", "saved_order", "measure"):
        state.pop(name, None)
    return state


def _state_matches_key(key, state) -> bool:
    parts = key.split("|") if isinstance(key, str) else []
    return (len(parts) == 4 and parts[0] in ("song-v2", "song-v3")
            and parts[1] == state["instrument"] and parts[2] == str(state["track_index"])
            and len(parts[3]) == 64 and all(c in "0123456789abcdef" for c in parts[3]))


def _validated_passage(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    identifier = raw.get("id")
    if not (isinstance(identifier, str) and len(identifier) == 32
            and all(c in "0123456789abcdef" for c in identifier)):
        return None
    try:
        name = passage_name(raw.get("name"))
    except ValueError:
        return None
    state = _passage_state(raw.get("state"))
    if not state:
        return None
    dates = {}
    for key in ("created_at", "updated_at"):
        stamp = raw.get(key)
        if not text(stamp, 100):
            return None
        try:
            datetime.fromisoformat(stamp)
        except ValueError:
            return None
        dates[key] = stamp
    return dict(id=identifier, name=name, state=state, **dates)


def validate_session(raw) -> dict:
    """Untrusted settings must not create out-of-range seeks or broken filters."""
    if not isinstance(raw, dict):
        return {}
    from pickhero.instruments import PROFILES
    if (not text(raw.get("song_path"), 32768) or not isinstance(raw.get("instrument"), str)
            or raw["instrument"] not in PROFILES
            or not integer(raw.get("track_index"), 0, 10000)
            or not number(raw.get("position_ms"), 0, 86_400_000)):
        return {}
    valid = {k: raw[k] for k in ("song_path", "instrument", "track_index", "position_ms")}
    for key, low, high in (("tempo_factor", .25, 1), ("loop_start_ms", 0, 86_400_000),
                           ("loop_end_ms", 0, 86_400_000)):
        if number(raw.get(key), low, high):
            valid[key] = raw[key]
    for key in ("loop_enabled", "wait_mode", "chord_partial_credit", "completed"):
        if type(raw.get(key)) is bool:
            valid[key] = raw[key]
    for key, high in (("measure", 1_000_000), ("max_fret", 36)):
        if integer(raw.get(key), 0, high):
            valid[key] = raw[key]
    strings = raw.get("active_strings")
    if (isinstance(strings, list) and 1 <= len(strings) <= 12
            and all(type(s) is bool for s in strings) and any(strings)):
        valid["active_strings"] = list(strings)
    if text(raw.get("saved_at"), 100):
        valid["saved_at"] = raw["saved_at"]
    if integer(raw.get("saved_order"), 0, 1_000_000_000_000_000):
        valid["saved_order"] = raw["saved_order"]
    return valid


def _timing_counts(raw: dict) -> dict:
    return {key: raw[key] for key in ("timing_unknown", "timing_measured")
            if integer(raw.get(key), 0, 1_000_000)}


def _validated_section(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    numeric = {"start_ms": (0, 86_400_000), "end_ms": (0, 86_400_000),
               "tempo_factor": (.25, 1), "accuracy": (0, 100), "timing_percent": (0, 100)}
    if (not all(number(raw.get(k), *bounds) for k, bounds in numeric.items())
            or raw["end_ms"] <= raw["start_ms"]
            or not integer(raw.get("total"), 0, 1_000_000)
            or not text(raw.get("filter_key"), 2048)):
        return None
    valid = {k: raw[k] for k in (*numeric, "total", "filter_key")}
    for key in ("timing_assessed", "reliable", "uniform_tempo", "complete_loop"):
        valid[key] = raw.get(key) is True
    valid.update(_timing_counts(raw))
    if valid.get("timing_unknown", 0):
        valid["timing_assessed"] = False
    for key in ("wait_mode", "uniform_wait", "uniform_input"):
        if type(raw.get(key)) is bool:
            valid[key] = raw[key]
    if (isinstance(raw.get("input_key"), str) and len(raw["input_key"]) == 64
            and all(c in "0123456789abcdef" for c in raw["input_key"])):
        valid["input_key"] = raw["input_key"]
    if text(raw.get("completed_at"), 100):
        valid["completed_at"] = raw["completed_at"]
    return valid
