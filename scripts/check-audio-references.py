"""Read-only pitch evidence for the single-note files in an audio manifest.

Usage: python scripts/check-audio-references.py manifest.json --output evidence.json
       --windows 0.6:0.8,0.8:1.0,1.0:1.2

This is a reference-quality aid, not a replacement ground truth or a detector
benchmark. Fixed windows may contain silence, hum, another note or a transition.
No labels or benchmark scores are changed, and no audio device is opened.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import platform

import numpy as np

LOADER_PATH = Path(__file__).resolve().with_name("benchmark-audio.py")
_spec = importlib.util.spec_from_file_location("reference_pcm_loader", LOADER_PATH)
benchmark = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(benchmark)

DEFAULT_WINDOWS = ((.6, .8), (.8, 1.), (1., 1.2))
SETTINGS = {
    "frequency_min_hz": 30.0,
    "frequency_max_hz": 2000.0,
    "spectral_max_hz": 8000.0,
    "candidate_periodicity": .90,
    "strong_periodicity": .98,
    "minimum_cycles": 6.0,
    "minimum_rms_dbfs": -70.0,
    "spectral_support_cents": 35.0,
    "spectral_support_relative_db": -20.0,
    "ambiguity_support_relative_db": -50.0,
    "maximum_window_spread_cents": 20.0,
    "consistent_within_cents": 35.0,
    "suspect_beyond_cents": 65.0,
    "minimum_usable_windows": 2,
    "fft_zero_padding_factor": 8,
    "reported_spectral_peaks": 8,
}


def canonical_hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_windows(windows):
    """Bound work and prevent duplicated/overlapping evidence counting twice."""
    if not isinstance(windows, (list, tuple)) or not 1 <= len(windows) <= 16:
        raise ValueError("Choose 1 to 16 fixed, non-overlapping windows")
    result, previous_end = [], 0.0
    for item in windows:
        if (not isinstance(item, (list, tuple)) or len(item) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in item)):
            raise ValueError("Window endpoints must be finite numbers")
        start, end = map(float, item)
        if not 0 <= start < end <= 600 or not .05 - 1e-12 <= end - start <= 1.0 + 1e-12:
            raise ValueError("Windows must be 50 to 1000 ms long, within 0 to 600 seconds")
        if start < previous_end:
            raise ValueError("Windows must be ordered and must not overlap")
        result.append([start, end])
        previous_end = end
    return result


def parse_windows(value):
    try:
        return validate_windows([tuple(float(part) for part in item.split(":"))
                                 for item in value.split(",")])
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def interpolate_peak(values, index):
    left, center, right = values[index - 1:index + 2]
    curvature = left - 2 * center + right
    offset = .5 * (left - right) / curvature if curvature else 0.0
    return float(index + np.clip(offset, -.5, .5))


def analyze_window(signal, rate, window, settings):
    """Measure without receiving a profile, expected MIDI, or detector output."""
    start, end = window
    first, last = round(start * rate), round(end * rate)
    result = {"window_s": list(window), "usable": False, "reasons": []}
    if last > len(signal) or last - first < 8:
        result["reasons"].append("window_outside_recording")
        return result
    block = np.asarray(signal[first:last], dtype=np.float64)
    if not np.all(np.isfinite(block)):
        result["reasons"].append("non_finite_samples")
        return result
    # DC is not evidence of a musical fundamental. The input file is unmodified.
    block = block - block.mean()
    rms = float(np.sqrt(np.mean(block ** 2)))
    result["rms_dbfs"] = 20 * math.log10(rms) if rms > 0 else None
    if rms == 0 or result["rms_dbfs"] < settings["minimum_rms_dbfs"]:
        result["reasons"].append("no_usable_signal")
        return result

    size = len(block)
    nfft = 1 << (2 * size - 1).bit_length()
    transform = np.fft.rfft(block, nfft)
    cross = np.fft.irfft(np.abs(transform) ** 2, nfft)[:size]
    energy = np.concatenate([[0.0], np.cumsum(block ** 2)])
    lags = np.arange(size)
    denominator = np.sqrt(np.maximum(energy[size - lags] * (energy[size] - energy[lags]), 0))
    correlation = np.clip(cross / np.maximum(denominator, 1e-30), -1, 1)
    low_lag = max(1, int(rate / settings["frequency_max_hz"]))
    high_lag = min(size - 2, int(math.ceil(rate / settings["frequency_min_hz"])))
    candidates = [index for index in range(low_lag + 1, high_lag)
                  if correlation[index] > correlation[index - 1]
                  and correlation[index] >= correlation[index + 1]
                  and correlation[index] >= settings["candidate_periodicity"]]

    # Spectral evidence is measured even when no convincing period is found.
    fft_size = settings["fft_zero_padding_factor"] * (1 << (size - 1).bit_length())
    magnitude = np.abs(np.fft.rfft(block * np.hanning(size), fft_size))
    bins = np.fft.rfftfreq(fft_size, 1.0 / rate)
    indices = np.flatnonzero((magnitude[1:-1] > magnitude[:-2])
                             & (magnitude[1:-1] >= magnitude[2:])) + 1
    indices = indices[(bins[indices] >= settings["frequency_min_hz"])
                      & (bins[indices] <= settings["spectral_max_hz"])]
    strongest = float(np.max(magnitude[indices])) if len(indices) else 0.0
    peaks = []
    if strongest > 0:
        log_magnitude = np.log(np.maximum(magnitude, 1e-30))
        for index in indices:
            relative = 20 * math.log10(max(float(magnitude[index]) / strongest, 1e-30))
            # Very low sidelobes cannot qualify as fundamental support.
            if relative >= -60:
                peaks.append({"frequency_hz": interpolate_peak(log_magnitude, index) * rate / fft_size,
                              "relative_db": relative})
        peaks.sort(key=lambda peak: peak["relative_db"], reverse=True)
    result["spectral_peaks"] = peaks[:settings["reported_spectral_peaks"]]
    result["spectral_window_resolution_hz"] = rate / size
    if not candidates:
        result["reasons"].append("no_strong_period_candidate")
        return result

    index = candidates[0]
    period = interpolate_peak(correlation, index)
    frequency = rate / period
    result["period_samples"] = period
    result["period_ms"] = period / rate * 1000
    result["periodicity"] = float(correlation[index])
    result["frequency_hz"] = frequency
    result["midi"] = 69 + 12 * math.log2(frequency / 440)
    result["cycles_in_window"] = size / period
    if not settings["frequency_min_hz"] <= frequency <= settings["frequency_max_hz"]:
        result["reasons"].append("period_outside_analysis_range")
    if result["periodicity"] < settings["strong_periodicity"]:
        result["reasons"].append("weak_or_changing_periodicity")
    if result["cycles_in_window"] < settings["minimum_cycles"]:
        result["reasons"].append("too_few_periods")

    supports = [peak for peak in peaks
                if peak["relative_db"] >= settings["spectral_support_relative_db"]
                and abs(1200 * math.log2(peak["frequency_hz"] / frequency)) <= settings["spectral_support_cents"]]
    result["fundamental_spectral_support"] = min(
        supports, key=lambda peak: abs(peak["frequency_hz"] - frequency), default=None)
    if not supports:
        result["reasons"].append("fundamental_weak_missing_or_ambiguous")

    # A dominant second, third or higher partial can have a very strong short
    # period. Its spectral peak is not independent evidence that it is the
    # fundamental. Look for longer strong periods with their own lower peak.
    # A weak visible alternative can make a result ambiguous without being
    # strong enough to establish a different fundamental or corrected label.
    alternatives = []
    for other in candidates[1:]:
        if correlation[other] < settings["strong_periodicity"]:
            continue
        other_period = interpolate_peak(correlation, other)
        other_frequency = rate / other_period
        if (not settings["frequency_min_hz"] <= other_frequency < frequency
                or size / other_period < settings["minimum_cycles"]):
            continue
        lower_supports = [peak for peak in peaks
                          if peak["relative_db"] >= settings["ambiguity_support_relative_db"]
                          and abs(1200 * math.log2(peak["frequency_hz"] / other_frequency))
                          <= settings["spectral_support_cents"]]
        if lower_supports:
            alternatives.append({"frequency_hz": other_frequency,
                                 "period_samples": other_period,
                                 "periodicity": float(correlation[other]),
                                 "spectral_support": min(lower_supports, key=lambda peak:
                                                         abs(peak["frequency_hz"] - other_frequency))})
    result["lower_period_alternatives"] = alternatives
    if alternatives:
        result["reasons"].append("lower_fundamental_or_harmonic_identity_ambiguous")
    result["usable"] = not result["reasons"]
    return result


def check_single(signal, rate, expected_midi, windows, settings):
    evidence = [analyze_window(signal, rate, window, settings) for window in windows]
    usable = [window for window in evidence if window["usable"]]
    result = {"status": "uncertain", "nominal_midi": expected_midi,
              "reasons": [], "windows": evidence,
              "usable_windows": len(usable), "requested_windows": len(windows)}
    if len(usable) != len(windows):
        result["reasons"].append("not_all_windows_have_clear_pitch_evidence")
    if len(usable) < settings["minimum_usable_windows"]:
        result["reasons"].append("not_enough_independent_windows")
    if not usable:
        return result
    midi_values = [window["midi"] for window in usable]
    median = float(np.median(midi_values))
    spread = (max(midi_values) - min(midi_values)) * 100
    difference = (median - expected_midi) * 100
    result.update(observed_midi=median, cents_from_nominal=difference,
                  window_spread_cents=spread)
    nominal_frequency = 440 * 2 ** ((expected_midi - 69) / 12)
    if not settings["frequency_min_hz"] <= nominal_frequency <= settings["frequency_max_hz"]:
        result["reasons"].append("nominal_pitch_outside_analysis_range")
    if spread > settings["maximum_window_spread_cents"]:
        result["reasons"].append("pitch_changes_between_windows")
    if result["reasons"]:
        return result
    if abs(difference) <= settings["consistent_within_cents"]:
        result["status"] = "consistent"
        result["reasons"].append("stable_evidence_near_nominal_pitch")
    elif abs(difference) >= settings["suspect_beyond_cents"]:
        result["status"] = "suspect"
        result["reasons"].append("stable_evidence_differs_from_nominal_pitch")
    else:
        result["reasons"].append("difference_in_conservative_boundary_region")
    return result


def run(manifest_path, output_path, windows=DEFAULT_WINDOWS):
    manifest_path, output_path = Path(manifest_path).resolve(), Path(output_path).resolve()
    if output_path.exists():
        raise ValueError("Output already exists; choose a new report path")
    windows = validate_windows(windows)
    settings = dict(SETTINGS)
    manifest_hash = benchmark.digest(manifest_path, 4 * 1024 * 1024)
    manifest = benchmark.load_manifest(manifest_path)
    source_paths = (Path(__file__).resolve(), LOADER_PATH)
    hashes = {path.name: benchmark.digest(path) for path in source_paths}
    inputs = [{"path": entry["path"], "sha256": entry["sha256"]} for entry in manifest["files"]]
    records = []
    for index, entry in enumerate(manifest["files"]):
        if entry["kind"] == "sequence":
            evidence = {"status": "skipped", "reasons": ["sequence_needs_individual_note_windows"]}
        else:
            signal, rate = benchmark.load_pcm(manifest_path.parent / entry["path"], entry["sha256"])
            evidence = check_single(signal, rate, entry["expected"][0]["midi"], windows, settings)
            evidence.update(sample_rate=rate, duration_s=len(signal) / rate)
        records.append({**entry, **evidence})
        print(f'{index + 1}/{len(manifest["files"])} {evidence["status"]}: {entry["path"]}', flush=True)
    if (benchmark.digest(manifest_path, 4 * 1024 * 1024) != manifest_hash
            or any(benchmark.digest(path) != hashes[path.name] for path in source_paths)
            or any(benchmark.digest(manifest_path.parent / entry["path"]) != entry["sha256"]
                   for entry in inputs)):
        raise ValueError("Manifest, input audio, or checker source changed during evaluation")
    counts = Counter(record["status"] for record in records)
    result = {
        "schema_version": 1, "utc": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": manifest_hash, "input_manifest_sha256": canonical_hash(inputs),
        "source_sha256": hashes, "windows_s": windows, "windows_sha256": canonical_hash(windows),
        "settings": settings, "settings_sha256": canonical_hash(settings),
        "runtime": {"python": platform.python_version(), "numpy": np.__version__},
        "provenance": manifest.get("provenance", {}),
        "method": "Original hash-verified PCM, mono average, no resampling or amplitude normalization. Fixed windows; DC removed only in analysis. Overlap-energy-normalized autocorrelation selects the first local peak >=0.90 in 30..2000 Hz without receiving the nominal pitch. Hann-windowed FFT peaks independently support the estimated fundamental. Zero padding interpolates peaks; it does not improve physical resolution.",
        "status_policy": "All requested windows must be usable, with at least two non-overlapping windows, >=6 periods, periodicity >=0.98, RMS >=-70 dBFS, and a spectral peak within 35 cents and 20 dB of the strongest peak. Longer strong periods with a lower spectral peak within 35 cents and 50 dB of the strongest peak make harmonic identity uncertain; no alternative label is selected. Across-window spread must be <=20 cents. Nominal differences <=35 cents are consistent; >=65 cents are suspect; intermediate or incomplete evidence is uncertain. These are conservative review heuristics, not calibrated error probabilities.",
        "limitations": [
            "Suspect means review the reference and recording; it does not prove that a dataset label is wrong.",
            "Consistent means agreement in the chosen windows only; it does not verify the entire recording, attack time, or recording identity.",
            "Windows may miss a short note or contain hum, a transition or another sound. Absent or weak fundamentals, strong partials, and changing pitch can be ambiguous.",
            "Periods and spectral peaks are finite-window estimates; the analysis assumes A4=440 Hz. No human annotation or instrument tuning intent is inferred.",
            "No detector is run, no audio device is opened, and no labels, files, benchmark scores or denominators are rewritten or excluded.",
            "These status counts are reference-review findings, not general pitch-recognition accuracy."],
        "summary": {"files": len(records), **{status: counts[status]
                    for status in ("consistent", "suspect", "uncertain", "skipped")}},
        "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, allow_nan=False, indent=2)
    print(json.dumps(result["summary"]), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--windows", type=parse_windows, default=DEFAULT_WINDOWS,
                        help="Ordered non-overlapping start:end seconds, separated by commas")
    args = parser.parse_args()
    run(args.manifest, args.output, args.windows)
