"""Offline pitch/onset regression on explicitly annotated original WAV files.

Usage: python scripts/benchmark-audio.py manifest.json --output result.json
Never opens an audio device. Audio remains in place, unchanged. The manifest
pins every recording by SHA256; expected notes are used only after detection.
Scores measure agreement with those labels; this tool does not establish
their acoustic accuracy. Use check-audio-references.py for a separate audit.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import io
import json
import math
from pathlib import Path
import platform
import sys
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
PROFILES = {"guitar", "bass_guitar", "bass_ukulele"}


def read_bounded(path, limit):
    # Bound the read itself, including if a file grows after an earlier stat.
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"File exceeds {limit} byte size limit: {path.name}")
    return raw


def digest(path, limit=None):
    return hashlib.sha256(read_bounded(path, MAX_FILE_BYTES if limit is None else limit)).hexdigest()


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def load_manifest(path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Manifest exceeds 4 MiB")
    document = json.loads(read_bounded(path, 4 * 1024 * 1024).decode("utf-8-sig"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("Unsupported manifest schema")
    entries = document.get("files")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 4096:
        raise ValueError("Manifest needs 1 to 4096 recordings")
    seen, total = set(), 0
    root = path.resolve().parent
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid recording")
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative:
            raise ValueError("Missing recording path")
        audio = (root / relative).resolve()
        if Path(relative).is_absolute() or not audio.is_relative_to(root):
            raise ValueError("Recording must stay within the manifest directory")
        if audio in seen:
            raise ValueError("Duplicate recording path")
        seen.add(audio)
        if audio.suffix.lower() != ".wav" or not audio.is_file():
            raise ValueError("Recording must be a WAV file")
        size = audio.stat().st_size
        total += size
        if not 0 < size <= MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Audio exceeds evaluation size limit")
        if entry.get("sha256") != digest(audio):
            raise ValueError(f"Recording checksum mismatch: {relative}")
        if entry.get("profile") not in PROFILES or entry.get("kind") not in ("single", "sequence"):
            raise ValueError("Invalid profile or recording kind")
        notes = entry.get("expected")
        if not isinstance(notes, list) or not 1 <= len(notes) <= 10000:
            raise ValueError("Missing expected notes")
        if entry["kind"] == "single" and len(notes) != 1:
            raise ValueError("Single-note recording must have exactly one expected pitch")
        previous = -1.0
        for note in notes:
            if not isinstance(note, dict) or type(note.get("midi")) is not int or not 0 <= note["midi"] <= 127:
                raise ValueError("Invalid expected MIDI pitch")
            if entry["kind"] == "sequence":
                onset = note.get("onset_s")
                if not finite_number(onset) or onset < 0 or onset < previous:
                    raise ValueError("Sequence needs ordered finite onset annotations")
                previous = onset
        groups = entry.get("groups", {})
        if (not isinstance(groups, dict) or len(groups) > 12
                or any(not isinstance(k, str) or not isinstance(v, (str, int))
                       for k, v in groups.items())):
            raise ValueError("Invalid recording groups")
    return document


def load_pcm(path, expected_sha256):
    raw = read_bounded(path, MAX_FILE_BYTES)
    if len(raw) > MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Recording changed after manifest validation")
    with wave.open(io.BytesIO(raw), "rb") as source:
        rate, channels, width, frames = (source.getframerate(), source.getnchannels(),
                                         source.getsampwidth(), source.getnframes())
        if (source.getcomptype() != "NONE" or not 8000 <= rate <= 192000
                or channels not in (1, 2) or width not in (1, 2, 3, 4)
                or not 0 < frames <= rate * 600):
            raise ValueError("Unsupported or oversized PCM recording")
        pcm = source.readframes(frames)
    if len(pcm) != frames * channels * width:
        raise ValueError("Truncated PCM recording")
    if width == 1:
        signal = (np.frombuffer(pcm, np.uint8).astype(np.float32) - 128) / 128
    elif width == 3:
        data = np.frombuffer(pcm, np.uint8).reshape(-1, 3).astype(np.int32)
        values = data[:, 0] | data[:, 1] << 8 | data[:, 2] << 16
        signal = ((values ^ 0x800000) - 0x800000).astype(np.float32) / 8388608
    else:
        signal = np.frombuffer(pcm, f"<i{width}").astype(np.float32) / (2 ** (8 * width - 1))
    return signal.reshape(-1, channels).mean(axis=1).astype(np.float32), rate


def match_events(expected, detected, tolerance):
    """Maximum one-to-one exact-pitch matching, closest edges considered first."""
    edges = [[j for j, event in sorted(enumerate(detected),
                                      key=lambda pair: abs(pair[1]["onset_s"] - note["onset_s"]))
              if event["midi"] == note["midi"]
              and abs(event["onset_s"] - note["onset_s"]) <= tolerance + 1e-12]
             for note in expected]
    # Iterative augmenting paths avoid recursion limits on long passages.
    assigned = {}
    for start in range(len(expected)):
        queue, parents, visited = [start], {}, {start}
        free = None
        for reference in queue:
            for prediction in edges[reference]:
                if prediction in parents:
                    continue
                parents[prediction] = reference
                if prediction not in assigned:
                    free = prediction
                    break
                owner = assigned[prediction]
                if owner not in visited:
                    visited.add(owner)
                    queue.append(owner)
            if free is not None:
                break
        if free is not None:
            reverse = {reference: prediction for prediction, reference in assigned.items()}
            while free is not None:
                reference = parents[free]
                previous = reverse.get(reference)
                assigned[free] = reference
                free = previous
    return {reference: prediction for prediction, reference in assigned.items()}


def score(entry, found):
    expected = entry["expected"]
    if entry["kind"] == "single":
        correct = int(any(event["midi"] == expected[0]["midi"] for event in found))
        return {"notes": 1, "correct": correct, "missed": 1 - correct,
                "extra": len(found) - correct, "exactly_one_correct": int(correct and len(found) == 1)}
    result = {"notes": len(expected)}
    for window in (50, 150):
        matched = match_events(expected, found, window / 1000)
        errors = [(found[j]["onset_s"] - expected[i]["onset_s"]) * 1000 for i, j in matched.items()]
        result[str(window)] = {"correct": len(matched), "missed": len(expected) - len(matched),
                               "extra": len(found) - len(matched), "onset_errors_ms": errors}
    return result


def detect(signal, rate, profile):
    from pickhero.audio.detector import PitchDetector
    from pickhero.instruments import PROFILES as instruments
    detector = PitchDetector(instrument=instruments[profile], sample_rate=rate)
    padded = np.concatenate([np.zeros(rate // 2, np.float32), signal, np.zeros(rate, np.float32)])
    found, cpu = [], []
    for offset in range(0, len(padded) - detector.hop_size + 1, detector.hop_size):
        started = time.perf_counter_ns()
        note = detector.process(padded[offset:offset + detector.hop_size])
        cpu.append((time.perf_counter_ns() - started) / 1e6)
        if note is not None and note.is_onset:
            arrival = (offset + detector.hop_size) / rate - .5
            found.append({"midi": note.midi_note, "arrival_s": arrival,
                          "onset_s": arrival - note.onset_age_hops * detector.hop_size / rate
                          - detector.onset_latency_ms / 1000})
    return found, {"median_ms": float(np.median(cpu)), "p95_ms": float(np.percentile(cpu, 95)),
                   "max_ms": max(cpu), "hop_budget_ms": detector.hop_size / rate * 1000,
                   "over_budget": sum(value > detector.hop_size / rate * 1000 for value in cpu)}


def summarize(records):
    groups = defaultdict(list)
    for record in records:
        prefix = (record["profile"], record["kind"])
        groups[(*prefix, "all", "all")].append(record)
        for key, value in record.get("groups", {}).items():
            groups[(*prefix, key, str(value))].append(record)
    summaries = []
    for (profile, kind, group, value), members in sorted(groups.items()):
        for window in (None,) if kind == "single" else ("50", "150"):
            metrics = [entry["metrics"] if window is None else entry["metrics"][window] for entry in members]
            summary = {"profile": profile, "kind": kind, "group": group, "value": value,
                       "window_ms": int(window) if window else None, "files": len(members),
                       "notes": sum(entry["metrics"]["notes"] for entry in members)}
            for metric in ("correct", "missed", "extra") + (("exactly_one_correct",) if window is None else ()):
                summary[metric] = sum(entry[metric] for entry in metrics)
            summaries.append(summary)
    return summaries


def run(manifest_path, output_path):
    if output_path.exists():
        raise ValueError("Output already exists; choose a new report path")
    manifest_hash = digest(manifest_path, 4 * 1024 * 1024)
    manifest = load_manifest(manifest_path)
    source_paths = [Path(__file__).resolve(), *(ROOT / name for name in (
        "pickhero/audio/detector.py", "pickhero/audio/harmonic.py", "pickhero/audio/note_utils.py", "pickhero/instruments.py"))]
    hashes = {path.relative_to(ROOT).as_posix(): digest(path) for path in source_paths}
    records = []
    for index, entry in enumerate(manifest["files"]):
        signal, rate = load_pcm(manifest_path.parent / entry["path"], entry["sha256"])
        if entry["kind"] == "sequence" and entry["expected"][-1]["onset_s"] >= len(signal) / rate:
            raise ValueError("Annotated onset falls outside the recording")
        found, cpu = detect(signal, rate, entry["profile"])
        records.append({**entry, "sample_rate": rate, "duration_s": len(signal) / rate,
                        "detected": found, "metrics": score(entry, found), "cpu": cpu})
        print(f'{index + 1}/{len(manifest["files"])} {entry["path"]}', flush=True)
    if (digest(manifest_path, 4 * 1024 * 1024) != manifest_hash
            or any(digest(ROOT / name) != value for name, value in hashes.items())):
        raise ValueError("Manifest or source changed during evaluation")
    result = {"utc": datetime.now(timezone.utc).isoformat(), "manifest_sha256": manifest_hash,
              "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                          "numpy": version("numpy"), "aubio": version("aubio")},
              "source_sha256": hashes, "provenance": manifest.get("provenance", {}),
              "method": "Original PCM, mono average, no normalization; 500 ms lead and 1000 ms tail, fresh default detector per file. Exact MIDI, one-to-one matching. Single-note files have no onset-accuracy claim.",
              "limitations": "Scores measure agreement with the unchanged manifest labels, whose acoustic accuracy is not verified here. Offline CPU, no hardware latency; corpus coverage is not population accuracy. Dataset provenance and prior usage are declared in the manifest.",
              "summary": summarize(records), "records": records}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, ensure_ascii=False)
    print(json.dumps([entry for entry in result["summary"] if entry["group"] == "all"]), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.manifest.resolve(), args.output.resolve())
