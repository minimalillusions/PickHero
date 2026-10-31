"""Freeze what today's detector hears in their own recorded takes.

Usage: python scripts/freeze-real-takes.py [--takes DIR] [--output FILE]

The practice recorder stores each take outside the repository (see .gitignore:
"Personal test takes"). Those WAV files are 25 MB of real playing and they
stay out of git. What goes in is this: a small baseline naming, per take, the
onsets the current detector reports. tests/test_real_takes.py replays the audio
and compares, so a change that alters what the app hears in real playing fails
a test instead of being noticed weeks later.

This never opens an audio device and never writes into the takes directory.
Running it is a deliberate act: it overwrites the baseline, which is the same
as declaring "the new behaviour is the intended behaviour". Read the diff it
prints before committing the result.

A take is identified by the PCM hash the recorder itself stored in
session.json, not by its directory name, so a baseline can never be matched
against different audio.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import sys
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "tests" / "fixtures" / "real_takes_baseline.json"
SCHEMA_VERSION = 1


def _benchmark():
    """Reuse the benchmark's PCM reader rather than writing a second one.

    It bounds the read, rejects compressed, truncated and oversized files and
    averages stereo without normalising. A private copy here would drift.
    """
    spec = importlib.util.spec_from_file_location(
        "freeze_benchmark", ROOT / "scripts" / "benchmark-audio.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def takes(folder: pathlib.Path) -> list[pathlib.Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if (p / "session.json").is_file() and (p / "input.wav").is_file())


def describe(take: pathlib.Path) -> dict | None:
    """The session's own record of how it was captured, or None if unusable."""
    try:
        session = json.loads((take / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if session.get("status") != "saved" or not session.get("pcm_sha256"):
        return None
    context = session.get("context") or {}
    state = (session.get("finish") or {}).get("state") or {}
    return {
        # Named by its audio, not by its folder: the folder name is the
        # moment the take was recorded, and a baseline that goes into a
        # public repository has no business saying when anyone practised.
        "take": f"take-{str(session['pcm_sha256'])[:8]}",
        "pcm_sha256": str(session["pcm_sha256"]),
        "sample_rate": int(session.get("sample_rate") or 0),
        "profile": str(context.get("instrument") or ""),
        "label": str(context.get("label") or ""),
        "noise_gate_db": state.get("noise_gate_db"),
        "duration_seconds": session.get("duration_seconds"),
    }


WINDOW_SECONDS = 0.05


def hop_ms(profile: str, rate: int) -> float:
    """One analysis hop in milliseconds, asked of the detector itself.

    Not 512: the profile scales its hop to the sample rate, so a bass ukulele
    at 48 kHz steps 557 samples, or 11.6 ms. Hard-coding the 44.1 kHz figure
    here would quietly make a timing tolerance 8 % too tight.
    """
    from pickhero.audio.detector import PitchDetector
    from pickhero.instruments import PROFILES

    detector = PitchDetector(instrument=PROFILES[profile], sample_rate=rate)
    return detector.hop_size / rate * 1000.0


def loud_windows(signal: np.ndarray, rate: int,
                 noise_gate_db: float | None) -> tuple[int, int]:
    """How many 50 ms windows carry more than the gate, and how many there are.

    This is what tells a silent take from a played one, and it is why a take
    with no onsets can still be a useful baseline. A plucked string decays
    over many windows; a knock against the microphone or the record button is
    one window loud and the rest room noise. Stored alongside the onsets so
    the fixture says for itself why zero is the right answer.
    """
    gate = -45.0 if noise_gate_db is None else float(noise_gate_db)
    width = max(1, round(WINDOW_SECONDS * rate))
    count = total = 0
    for start in range(0, len(signal) - width + 1, width):
        block = signal[start:start + width]
        power = float(np.mean(block.astype(np.float64) ** 2))
        total += 1
        if 10 * np.log10(max(power, 1e-24)) > gate:
            count += 1
    return count, total


def onsets(signal: np.ndarray, rate: int, profile: str,
           noise_gate_db: float | None) -> list[list[float]]:
    """The onsets the detector reports, dated to the strike.

    Fed exactly as audio/input.py feeds it: hop-sized chunks in order, the
    report backdated by its own onset age and the onset detector's lag. The
    half second of leading silence lets the analysis window fill before the
    first note, as scripts/benchmark-audio.py does; it is subtracted again so
    the times are relative to the recording.
    """
    from pickhero.audio.detector import PitchDetector
    from pickhero.instruments import PROFILES

    detector = PitchDetector(instrument=PROFILES[profile], sample_rate=rate)
    if noise_gate_db is not None:
        # The take was captured behind this gate. Replaying it behind the
        # profile default would hear the room instead of the instrument.
        detector.set_noise_gate_db(float(noise_gate_db))
    hop = detector.hop_size
    lead = rate // 2
    padded = np.concatenate([np.zeros(lead, np.float32), signal,
                             np.zeros(rate, np.float32)])
    found: list[list[float]] = []
    for offset in range(0, len(padded) - hop + 1, hop):
        note = detector.process(padded[offset:offset + hop])
        if note is None or not note.is_onset:
            continue
        arrival = (offset + hop) / rate - lead / rate
        struck = (arrival - note.onset_age_hops * hop / rate
                  - detector.onset_latency_ms / 1000)
        found.append([round(struck * 1000, 2), int(note.midi_note)])
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--takes", type=pathlib.Path,
                        default=pathlib.Path.home() / ".pickhero" / "recordings")
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    found = takes(args.takes)
    if not found:
        print(f"Keine Aufnahmen in {args.takes}", file=sys.stderr)
        return 1

    benchmark = _benchmark()
    previous = {}
    if args.output.is_file():
        try:
            stored = json.loads(args.output.read_text(encoding="utf-8"))
            previous = {entry["pcm_sha256"]: entry for entry in stored.get("takes", [])}
        except (OSError, ValueError, KeyError, TypeError):
            previous = {}

    entries = []
    for take in found:
        meta = describe(take)
        if meta is None:
            print(f"  {take.name}: übersprungen (nicht gespeichert)")
            continue
        if not meta["profile"]:
            print(f"  {take.name}: übersprungen (kein Instrument notiert)")
            continue
        raw = (take / "input.wav").read_bytes()
        signal, rate = benchmark.load_pcm(take / "input.wav",
                                          hashlib.sha256(raw).hexdigest())
        if rate != meta["sample_rate"]:
            print(f"  {take.name}: übersprungen (Rate {rate} != {meta['sample_rate']})")
            continue
        started = time.perf_counter()
        marks = onsets(signal, rate, meta["profile"], meta["noise_gate_db"])
        seconds = time.perf_counter() - started
        loud, windows = loud_windows(signal, rate, meta["noise_gate_db"])
        meta["windows_above_gate"] = loud
        meta["windows"] = windows

        was = previous.get(meta["pcm_sha256"])
        if was is None:
            change = "NEU"
        elif was.get("onsets") == marks:
            change = "unverändert"
        else:
            change = (f"GEÄNDERT: {len(was.get('onsets', []))} -> {len(marks)} Onsets")
        print(f"  {take.name}: {len(marks):4} Onsets  "
              f"{meta['duration_seconds']:6.1f}s Audio in {seconds:5.1f}s  "
              f"{loud:4}/{windows:4} Fenster laut  {change}")
        entries.append({**meta, "onsets": marks})

    if not entries:
        print("Nichts einzufrieren.", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(
        {"schema_version": SCHEMA_VERSION,
         "note": "Generated by scripts/freeze-real-takes.py. The audio itself "
                 "stays outside the repository; see the script's docstring.",
         "takes": entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\ngeschrieben: {args.output}  ({len(entries)} Aufnahmen)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
