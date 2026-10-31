"""Comparable passage measurements and deliberately infrequent choices."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict

from pickhero.validation import integer, number, text

CONDITIONS = ("start_ms", "end_ms", "tempo_factor", "filter_key", "total", "input_key")


def input_fingerprint(config, instrument, capture=None, *, timing_window_ms=None):
    """Describe measurement conditions without restoring device settings."""
    from pickhero.config import _jsonable
    payload = {"scoring_policy": 2,
               "effective_timing_window_ms": (max(config.timing_window_ms, instrument.timing_window_ms)
                                               if timing_window_ms is None else timing_window_ms),
               "audio": asdict(config.audio), "instrument": asdict(instrument),
               "offset": config.audio_latency_offset_ms, "window": config.timing_window_ms,
               "chord_window": config.chord_threshold_ms, "calibration": config.calibration,
               "actual_rate": getattr(capture, "sample_rate", config.audio.sample_rate),
               "actual_device": getattr(capture, "device_index",
                                        getattr(capture, "_device_index", config.audio.device_index))}
    if instrument.key == "bass_guitar":
        # Changed pitch or attack decisions must not appear as player progress.
        payload["bass_pitch_policy"] = 4
    if instrument.key == "bass_ukulele":
        # Detector changes must not masquerade as the player's improvement.
        payload["bass_ukulele_attack_policy"] = 11
        payload["bass_ukulele_timing_policy"] = 1
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                    default=_jsonable).encode("utf-8")).hexdigest()


def _input_key(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def filter_key_for_state(state):
    return json.dumps([state["active_strings"], state["max_fret"],
                       state["chord_partial_credit"]], separators=(",", ":"))


def _eligible(entry):
    return (isinstance(entry, dict)
            and not entry.get("timing_unknown", 0)
            and all(entry.get(flag) is True for flag in
                    ("reliable", "uniform_tempo", "complete_loop", "timing_assessed"))
            and entry.get("uniform_wait", True) is True
            and entry.get("uniform_input") is True and _input_key(entry.get("input_key"))
            and integer(entry.get("total"), 4, 1_000_000)
            and number(entry.get("accuracy"), 0, 100)
            and number(entry.get("timing_percent"), 0, 100)
            and number(entry.get("tempo_factor"), .25, 1)
            and number(entry.get("start_ms"), 0, 86_400_000)
            and number(entry.get("end_ms"), 0, 86_400_000)
            and entry["end_ms"] > entry["start_ms"]
            and text(entry.get("filter_key"), 2048))


def section_advice(history, offers=(), result_count=None):
    """Offer after three comparable loops; never alter playback settings.

    A repeated choice waits twelve new results. Reversing the previous
    direction requires six further results, avoiding a three-loop seesaw.
    The caller persists each displayed offer, including a declined one.
    """
    if not isinstance(history, list) or len(history) < 3:
        return None
    last = history[-3:]
    if not all(_eligible(entry) for entry in last):
        return None
    current = last[-1]
    if any(any(entry[key] != current[key] for key in CONDITIONS) for entry in last):
        return None
    if all(entry["accuracy"] >= 90 and entry["timing_percent"] >= 90 for entry in last):
        if current["tempo_factor"] >= 1:
            return None
        kind, target = "raise", min(1, round(current["tempo_factor"] + .05, 2))
    elif (all(entry["accuracy"] < 70 for entry in last)
          or all(entry["timing_percent"] < 50 for entry in last)):
        kind, target = "ease", max(.25, round(current["tempo_factor"] - .05, 2))
    else:
        return None
    count = result_count if integer(result_count, 0, 1_000_000_000) else len(history)
    for raw in reversed(offers):
        offer = validated_offer(raw)
        if offer is None or any(offer[k] != current[k]
                                for k in ("start_ms", "end_ms", "filter_key", "total", "input_key")):
            continue
        gap = count - offer["at_result"]
        if offer["kind"] != kind and gap < 6:
            return None
        if offer["kind"] == kind and offer["tempo_factor"] == current["tempo_factor"] and gap < 12:
            return None
        break
    return dict({key: current[key] for key in CONDITIONS}, kind=kind, target_tempo=target)


def validated_offer(raw):
    if not isinstance(raw, dict) or raw.get("kind") not in ("raise", "ease"):
        return None
    for key, low, high in (("start_ms", 0, 86_400_000), ("end_ms", 0, 86_400_000),
                           ("tempo_factor", .25, 1), ("target_tempo", .25, 1)):
        if not number(raw.get(key), low, high):
            return None
    if (raw["end_ms"] <= raw["start_ms"] or not text(raw.get("filter_key"), 2048)
            or not integer(raw.get("total"), 4, 1_000_000)
            or not _input_key(raw.get("input_key"))
            or not integer(raw.get("at_result"), 0, 1_000_000_000)):
        return None
    return {key: raw[key] for key in (*CONDITIONS, "kind", "target_tempo", "at_result")}


def comparable_attempts(history, state, input_key=None):
    """Return previous/current results at the saved passage's exact settings."""
    fingerprint = filter_key_for_state(state)
    matches = [entry for entry in history if isinstance(entry, dict)
               and entry.get("reliable") is True and entry.get("uniform_tempo") is True
               and entry.get("complete_loop") is True and entry.get("uniform_wait") is True
               and entry.get("uniform_input") is True and _input_key(entry.get("input_key"))
               and entry.get("wait_mode") is state["wait_mode"]
               and entry.get("timing_assessed") is (not state["wait_mode"])
               and entry.get("start_ms") == state["loop_start_ms"]
               and entry.get("end_ms") == state["loop_end_ms"]
               and entry.get("tempo_factor") == state["tempo_factor"]
               and entry.get("filter_key") == fingerprint and entry.get("total", 0) > 0
               and text(entry.get("completed_at"), 100)]
    if not matches:
        return []
    wanted = input_key if input_key is not None else matches[-1]["input_key"]
    return copy.deepcopy([entry for entry in matches if entry["total"] == matches[-1]["total"]
                          and entry["input_key"] == wanted][-2:])
