"""Compare an existing aubio pitch method inside the unchanged bass pipeline.

Offline developer experiment, never a saved app setting. Only the primary
bass_guitar estimator changes. Attack gates, timing, range and the secondary
YINfast check remain unchanged. With YIN as primary, that secondary check is
no longer independent evidence: results do not certify a deployable detector.
YINFFT cannot satisfy this pipeline's confidence contract in aubio 0.4.9 and
is intentionally excluded; compare its raw pitches separately.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
from importlib.metadata import version
import json
from pathlib import Path
import platform
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("backend_benchmark_io", ROOT / "scripts/benchmark-audio.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)
BACKENDS = ("profile", "yin", "yinfast")


def make_detector(profile, rate, backend):
    """Create a private detector instance; never patch module globals/profiles."""
    from pickhero.audio.detector import PitchDetector
    from pickhero.instruments import PROFILES

    if backend not in BACKENDS:
        raise ValueError(f"Unsupported comparison backend: {backend}")

    class PrimaryPitch:
        def __init__(self, detector):
            import aubio

            self.buf_size = detector.buf_size
            if backend == "yinfast" and self.buf_size & (self.buf_size - 1):
                raise ValueError(
                    f"YINfast requires a power-of-two pitch window; this profile uses "
                    f"{self.buf_size} samples at {rate} Hz. This comparison does not "
                    "round the window or resample the recording.")
            self.pitch = aubio.pitch(backend, self.buf_size, detector.hop_size, rate)
            self.pitch.set_unit("Hz")
            self.pitch.set_tolerance(detector.yin_tolerance)

        def process(self, block):
            frequency = float(self.pitch(block)[0])
            return frequency, float(self.pitch.get_confidence())

    class ComparisonDetector(PitchDetector):
        def _make_pitch(self):
            if self.instrument.key == "bass_guitar" and backend != "profile":
                return PrimaryPitch(self)
            return super()._make_pitch()

    detector = ComparisonDetector(instrument=PROFILES[profile], sample_rate=rate)
    applied = profile == "bass_guitar" and backend != "profile"
    primary = detector._pitch
    standard = primary.pitch if applied else primary if detector.pitch_method == "yin" else None
    settings = {
        "requested_backend": backend, "primary_replaced": applied,
        "primary_method": backend if applied else getattr(primary, "method", detector.pitch_method),
        "sample_rate": rate, "configured_pitch_window": detector.buf_size,
        "actual_pitch_window": primary.buf_size, "hop_size": detector.hop_size,
        "onset_window": detector.onset_buf_size,
        "confidence_threshold": detector.confidence_threshold,
        "noise_gate_db": detector.noise_gate_db,
        "aubio_tolerance": float(standard.get_tolerance()) if standard is not None else None,
        "aubio_silence_db": float(standard.get_silence()) if standard is not None else None,
        "secondary_yinfast": detector._pitch_check is not None,
        "independent_primary_and_secondary_algorithms": (
            not applied if detector._pitch_check is not None else None),
    }
    return detector, settings


def detect(signal, rate, profile, backend):
    detector, settings = make_detector(profile, rate, backend)
    padded = np.concatenate([np.zeros(rate // 2, np.float32), signal, np.zeros(rate, np.float32)])
    found, cpu = [], []
    hop = detector.hop_size
    for offset in range(0, len(padded) - hop + 1, hop):
        started = time.perf_counter_ns()
        note = detector.process(padded[offset:offset + hop])
        cpu.append((time.perf_counter_ns() - started) / 1e6)
        if note is not None and note.is_onset:
            arrival = (offset + hop) / rate - .5
            found.append({"midi": note.midi_note, "arrival_s": arrival,
                          "onset_s": arrival - note.onset_age_hops * hop / rate
                          - detector.onset_latency_ms / 1000})
    budget = hop / rate * 1000
    return found, {"median_ms": float(np.median(cpu)), "p95_ms": float(np.percentile(cpu, 95)),
                   "max_ms": max(cpu), "hop_budget_ms": budget,
                   "over_budget": sum(value > budget for value in cpu)}, settings


def run(manifest_path, output_path, backend="profile"):
    if backend not in BACKENDS:
        raise ValueError(f"Unsupported comparison backend: {backend}")
    if output_path.exists():
        raise ValueError("Output already exists; choose a new report path")
    manifest_hash = benchmark.digest(manifest_path, 4 * 1024 * 1024)
    manifest = benchmark.load_manifest(manifest_path)
    import aubio._aubio as native_aubio
    native_path = Path(native_aubio.__file__)
    native_hash = benchmark.digest(native_path)
    sources = [Path(__file__).resolve(), ROOT / "scripts/benchmark-audio.py", *(ROOT / name for name in (
        "pickhero/audio/detector.py", "pickhero/audio/harmonic.py",
        "pickhero/audio/note_utils.py", "pickhero/instruments.py"))]
    hashes = {path.relative_to(ROOT).as_posix(): benchmark.digest(path) for path in sources}
    records = []
    for index, entry in enumerate(manifest["files"]):
        signal, rate = benchmark.load_pcm(manifest_path.parent / entry["path"], entry["sha256"])
        if entry["kind"] == "sequence" and entry["expected"][-1]["onset_s"] >= len(signal) / rate:
            raise ValueError("Annotated onset falls outside the recording")
        found, cpu, settings = detect(signal, rate, entry["profile"], backend)
        records.append({**entry, "sample_rate": rate, "duration_s": len(signal) / rate,
                        "settings": settings, "detected": found,
                        "metrics": benchmark.score(entry, found), "cpu": cpu})
        print(f'{backend}: {index + 1}/{len(manifest["files"])} {entry["path"]}', flush=True)
    if (benchmark.digest(manifest_path, 4 * 1024 * 1024) != manifest_hash
            or benchmark.digest(native_path) != native_hash
            or any(benchmark.digest(ROOT / name) != value for name, value in hashes.items())):
        raise ValueError("Manifest or source changed during comparison")
    result = {
        "utc": datetime.now(timezone.utc).isoformat(), "backend": backend,
        "manifest_sha256": manifest_hash, "source_sha256": hashes,
        "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                    "numpy": version("numpy"), "aubio": version("aubio"),
                    "aubio_extension_sha256": native_hash},
        "provenance": manifest.get("provenance", {}),
        "method": "Original PCM, mono average, no normalization/resampling. No additional "
                  "window rounding by this comparison; existing profile policy retained. "
                  "500ms lead,1000ms tail; fresh instance per recording. Primary steel-bass pitch "
                  "only is replaced; all other profile and pipeline rules retained. "
                  "Unchanged exact-MIDI one-to-one50/150ms scoring; no manual onset shift.",
        "limitations": "Exploratory substitution, not an app setting or deployment candidate. "
                       "Primary YIN/YINfast and secondary YINfast are not independent evidence. "
                       "Confidence scales differ. Scores measure agreement with unchanged nominal "
                       "labels, not proven acoustic truth or population accuracy. Offline CPU "
                       "does not measure hardware latency. Guitar and bass ukulele are unchanged.",
        "summary": benchmark.summarize(records), "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, ensure_ascii=False)
    print(json.dumps([entry for entry in result["summary"] if entry["group"] == "all"]), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--backend", choices=BACKENDS, default="profile")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.manifest.resolve(), args.output.resolve(), args.backend)
