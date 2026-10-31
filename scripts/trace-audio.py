"""Trace one original recording without changing the production detector.

Usage: python scripts/trace-audio.py manifest.json --recording audio/note.wav
       --output trace.json
The recording must exactly match one path in a benchmark-audio.py manifest.
Only that recording is processed; the shared loader validates the manifest.
Logs contain software decisions, not inferred acoustic ground truth. No audio
devices, settings changes, resampling, normalization or per-frame trace arrays.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("trace_benchmark", ROOT / "scripts/benchmark-audio.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)
from pickhero.audio.detector import PitchDetector
from pickhero.instruments import PROFILES

MAX_TRACE_EVENTS, MAX_NOTE_EVENTS = 20000, 10000


def event(detector, note):
    arrival = detector._hops_seen * detector.hop_size / detector.sample_rate - .5
    return {**asdict(note), "arrival_s": arrival, "onset_s": arrival
            - note.onset_age_hops * detector.hop_size / detector.sample_rate
            - detector.onset_latency_ms / 1000}


def evidence_snapshot(evidence):
    details = asdict(evidence)
    if evidence.kind in ("partials", "decaying_partials"):
        # The internal field is shared with octave-band evidence. For this
        # kind it contains one-based harmonic numbers, not band indexes.
        details["harmonics"] = list(details.pop("bands"))
    return details


def attack_snapshot(attack):
    """Bounded candidate history; identity does not prove a physical attack."""
    return {"origin": asdict(attack.origin),
            "close_reason": attack.close_reason, "decision": attack.decision,
            "report_outcome": attack.report_outcome,
            "closed_age_hops": attack.closed_age_hops,
            "decided_votes": list(attack.decided_votes),
            "pitch_tie_evidence": [asdict(row) for row in attack.pitch_tie_evidence],
            "late_quiet_start_sample": getattr(attack, "late_quiet_start_sample", None),
            "reference_span": list(attack.reference_span),
            "phase_ready": bool(getattr(attack, "phase_ready", False)),
            "phase_reference": list(getattr(attack, "phase_reference", ())),
            "timing_ambiguity": asdict(attack.timing_ambiguity) if attack.timing_ambiguity is not None else None,
            "waveform_prediction": asdict(attack.waveform_prediction) if attack.waveform_prediction is not None else None,
            "repeat_evidence": asdict(attack.repeat_evidence) if attack.repeat_evidence is not None else None,
            "quiet_repeat_evidence": asdict(attack.quiet_repeat_evidence) if attack.quiet_repeat_evidence is not None else None,
            "growing_pitch_evidence": asdict(attack.growing_pitch_evidence) if attack.growing_pitch_evidence is not None else None,
            "leading_quiet": bool(getattr(attack, "leading_quiet", False)),
            "preceded_by_quiet": bool(getattr(attack, "preceded_by_quiet", False)),
            "complex_confirmation": (asdict(attack.complex_confirmation)
                                     if getattr(attack, "complex_confirmation", None) is not None else None),
            "quiet_start_evidence": (asdict(attack.quiet_start_evidence)
                                     if getattr(attack, "quiet_start_evidence", None) is not None else None),
            "sparse_pitch_evidence": (asdict(attack.sparse_pitch_evidence)
                                      if getattr(attack, "sparse_pitch_evidence", None) is not None else None),
            "bass_continuation_sample": getattr(attack, "bass_continuation_sample", None),
            "bass_continuation_evidence": (asdict(attack.bass_continuation_evidence)
                if getattr(attack, "bass_continuation_evidence", None) is not None else None),
            "bass_duplicate_evidence": (asdict(attack.bass_duplicate_evidence)
                if getattr(attack, "bass_duplicate_evidence", None) is not None else None),
            "evidence": [evidence_snapshot(item) for item in attack.evidence],
            "continuations": [asdict(item) for item in getattr(attack, "continuations", ())]}


class ObservedOnset:
    def __init__(self, native, owner):
        self.native, self.owner = native, owner

    def __call__(self, block):
        result = self.native(block)
        if bool(result):
            admitted = bool(self.owner.last_signal_db >= self.owner.noise_gate_db
                            or self.owner.instrument.transient_rise_db > 0
                            or (self.owner.instrument.key == "bass_ukulele" and not self.owner._attack.pending))
            self.owner._admitted_onset_this_hop = admitted
            self.owner.log("native_onset", native_sample=float(self.native.get_last()),
                           native_onset_s=float(self.native.get_last()) / self.owner.sample_rate - .5,
                           admitted=admitted, before=self.owner.snapshot())
        return result

    def __getattr__(self, name):
        return getattr(self.native, name)


class TracedDetector(PitchDetector):
    def __init__(self, *args, **kwargs):
        self.trace, self.counts = [], Counter()
        super().__init__(*args, **kwargs)

    def _build(self):
        super()._build()
        if self._onset is not None:
            self._onset = ObservedOnset(self._onset, self)

    def snapshot(self):
        return {"attack": attack_snapshot(self._attack),
                "pending": self._onset_pending, "age_hops": self._onset_age,
                "backdate_hops": self._onset_backdate_hops,
                "votes": list(self._onset_votes), "yin_votes": list(self._bass_pitch_votes),
                "periodicity_votes": list(self._periodicity_votes),
                "attack_confirmed": bool(self._strike_confirmed),
                "transient_candidate": bool(self._transient_candidate),
                "quiet_hops": self._quiet_hops,
                "signal_db": float(self.last_signal_db), "reference_db": float(self._strike_ref_db)}

    def log(self, kind, **details):
        if len(self.trace) >= MAX_TRACE_EVENTS:
            raise ValueError("Trace event limit exceeded; choose a shorter recording")
        self.counts[kind] += 1
        self.trace.append({"kind": kind, "hop": self._hops_seen,
                           "arrival_s": self._hops_seen * self.hop_size / self.sample_rate - .5,
                           **details})

    def _flush_votes(self):
        before = self.snapshot()
        note = super()._flush_votes()
        self._flushed = True
        reason = ("already_closed" if before["attack"]["close_reason"] is not None else
                  "no_candidate" if not before["pending"] and not before["votes"] else
                  "eligible" if note is not None else "insufficient_votes"
                  if len(before["votes"]) < self.onset_votes_minimum else "attack_unconfirmed")
        trigger = ("native_onset" if getattr(self, "_admitted_onset_this_hop", False) else
                   "timeout" if before["age_hops"] > self.onset_hold_hops else "sound_end")
        self.log("flush", before=before, trigger=trigger, outcome=reason,
                 attack=attack_snapshot(self._attack),
                 note=event(self, note) if note is not None else None)
        return note

    def _corroborated_transient(self, midi):
        result = super()._corroborated_transient(midi)
        self.log("transient_check", midi=midi, accepted=bool(result), before=self.snapshot())
        return result

    def _retain_pending_attack(self, native, frequency, confidence):
        result = super()._retain_pending_attack(native, frequency, confidence)
        if result:
            # The native onset was observed, but it did not open a new owner.
            self._admitted_onset_this_hop = False
            self.log("onset_continuation", native_sample=native,
                     pitch_frequency=frequency, pitch_confidence=confidence,
                     attack=attack_snapshot(self._attack))
        return result

    def _raw_phase_rise_supported(self):
        supported = super()._raw_phase_rise_supported()
        if self.instrument.key in {"bass_guitar", "bass_ukulele"} and self._attack.phase_ready:
            self.log("phase_check", origin=asdict(self._attack.origin), supported=bool(supported),
                     powers=list(self._phase_powers), reference=list(self._attack.phase_reference),
                     bounds=list(self._phase_bounds), window_hops=list(self._phase_windows))
        return supported

    def _report(self, note):
        owner = (self._report_candidate[1] if self._report_candidate is not None
                 and self._report_candidate[0] is note else None)
        result = super()._report(note)
        self.log("report", admitted=result is not None,
                 attack=attack_snapshot(owner) if owner is not None else None,
                 outcome=("accepted_quiet_repeat" if owner is not None and owner.report_outcome == "reported_quiet_repeat"
                          else "accepted_repeat" if owner is not None and owner.report_outcome == "reported_repeat"
                          else "accepted" if result is not None else "prediction_filter"
                          if owner is not None and owner.report_outcome == "prediction_filtered"
                          else "sustain_filter"
                          if owner is not None and owner.report_outcome == "sustain_filtered"
                          else owner.report_outcome
                          if owner is not None and owner.report_outcome in {"quiet_start_unconfirmed", "quiet_start_duplicate", "continuation_unconfirmed", "continuation_duplicate"}
                          else "repeat_filter"), note=event(self, note))
        return result

    def process(self, block):
        before = self.snapshot() if self._onset_pending else None
        self._flushed = False
        self._admitted_onset_this_hop = False
        note = super().process(block)
        self.counts["hops"] += 1
        self.counts["below_gate_hops"] += int(self.last_signal_db < self.noise_gate_db)
        if before is not None and not self._onset_pending and not self._flushed:
            self.log("vote_completion", before_hop=before,
                     attack=attack_snapshot(self._last_closed_attack),
                     returned_onset=bool(note is not None and note.is_onset))
        return note


def stream(detector, padded):
    found = []
    for pos in range(0, len(padded) - detector.hop_size + 1, detector.hop_size):
        note = detector.process(padded[pos:pos + detector.hop_size])
        if note is not None and note.is_onset:
            if len(found) >= MAX_NOTE_EVENTS:
                raise ValueError("Note event limit exceeded")
            found.append(event(detector, note))
    return found


def trace_recording(signal, rate, profile):
    traced = TracedDetector(instrument=PROFILES[profile], sample_rate=rate)
    plain = PitchDetector(instrument=PROFILES[profile], sample_rate=rate)
    padded = np.concatenate((np.zeros(rate // 2, np.float32), signal, np.zeros(rate, np.float32)))
    expected, actual = stream(plain, padded), stream(traced, padded)
    if expected != actual:
        raise ValueError("Instrumentation changed detector events; no trace report written")
    return {"events_equal": True, "detected": actual, "trace": traced.trace,
            "counts": dict(traced.counts), "profile": asdict(traced.instrument),
            "sample_rate": rate, "pitch_window": traced.buf_size, "hop_size": traced.hop_size,
            "secondary_window": getattr(traced._pitch_check, "buf_size", None),
            "attack_probe_window": traced._PROBE_NFFT}


def run(manifest_path, recording, output_path):
    manifest_path, output_path = Path(manifest_path).resolve(), Path(output_path).resolve()
    if output_path.exists():
        raise ValueError("Output already exists; choose a new report path")
    manifest_hash = benchmark.digest(manifest_path, 4 * 1024 * 1024)
    manifest = benchmark.load_manifest(manifest_path)
    matches = [entry for entry in manifest["files"] if entry["path"] == recording]
    if len(matches) != 1:
        raise ValueError("Recording must exactly match one manifest path")
    entry = matches[0]
    audio = manifest_path.parent / recording
    paths = [Path(__file__).resolve(), ROOT / "scripts/benchmark-audio.py", *(ROOT / name for name in (
        "pickhero/audio/detector.py", "pickhero/audio/attack.py", "pickhero/audio/harmonic.py",
        "pickhero/audio/repeats.py", "pickhero/audio/quiet_repeats.py", "pickhero/audio/waveform_prediction.py",
        "pickhero/audio/note_utils.py", "pickhero/instruments.py"))]
    hashes = {path.relative_to(ROOT).as_posix(): benchmark.digest(path) for path in paths}
    import aubio._aubio as native
    import sounddevice
    native_hash = benchmark.digest(Path(native.__file__))
    def forbidden(*args, **kwargs):
        raise AssertionError("Audio hardware is forbidden in offline traces")
    with ExitStack() as guards:
        for name in ("InputStream", "OutputStream", "Stream", "RawInputStream", "RawOutputStream",
                     "RawStream", "query_devices", "play", "rec", "playrec"):
            guards.enter_context(patch.object(sounddevice, name, forbidden))
        signal, rate = benchmark.load_pcm(audio, entry["sha256"])
        result = trace_recording(signal, rate, entry["profile"])
    if (benchmark.digest(manifest_path, 4 * 1024 * 1024) != manifest_hash
            or benchmark.digest(audio) != entry["sha256"]
            or benchmark.digest(Path(native.__file__)) != native_hash
            or any(benchmark.digest(ROOT / name) != value for name, value in hashes.items())):
        raise ValueError("Manifest, audio or source changed during tracing")
    report = {"utc": datetime.now(timezone.utc).isoformat(), "recording": recording,
              "audio_sha256": entry["sha256"], "manifest_sha256": manifest_hash,
              "source_sha256": hashes, "aubio_extension_sha256": native_hash,
              "duration_s": len(signal) / rate, **result,
              "method": "Original PCM; mono average; 500ms lead/1000ms tail. Fresh default profile, "
                        "all original gates and fractional onset times. Exact complete-event parity "
                        "against a fresh unobserved detector on the same input is mandatory.",
              "limitations": "Software gate observations, not acoustic ground truth or hardware latency. "
                             "No annotation proximity is used to assign a cause. Vote-completion snapshots "
                             "precede the final hop; flush snapshots precede vote clearing. "
                             "Attack origin, closure and evidence describe software candidates, not proven "
                             "physical plucks. Evidence hops are one-based block endpoints; an empty "
                             "reference interval means the startup floor. No per-hop logs."}
    encoded = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as output:
        output.write(encoded)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--recording", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.manifest, args.recording, args.output)
