"""Offline tracing must observe actual decisions without changing any event."""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pickhero.audio.detector import DetectedNote
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE
from tests._recordings import open_strings

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audio_trace", ROOT / "scripts/trace-audio.py")
tracing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracing)


@pytest.fixture
def corpus(tmp_path):
    audio = tmp_path / "recording.wav"
    audio.write_bytes(open_strings().read_bytes())
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "files": [{
        "path": audio.name, "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
        "profile": "bass_ukulele", "kind": "single", "expected": [{"midi": 28}]}]}), encoding="utf-8")
    return manifest, audio, tmp_path / "trace.json"


def test_real_fixture_events_are_exact_and_source_audio_stays_unchanged(corpus):
    pytest.importorskip("aubio")
    manifest, audio, output = corpus
    originals = manifest.read_bytes(), audio.read_bytes()
    result = tracing.run(manifest, audio.name, output)
    assert result["events_equal"] and len(result["detected"]) == 12
    assert all(event["is_onset"] for event in result["detected"])
    assert all(event["arrival_s"] > event["onset_s"] for event in result["detected"])
    assert result["counts"]["native_onset"] > 0 and result["counts"]["flush"] > 0
    assert result["counts"]["report"] >= len(result["detected"])
    assert result["counts"]["phase_check"] > 0
    assert "scripts/trace-audio.py" in result["source_sha256"]
    assert "pickhero/audio/attack.py" in result["source_sha256"]
    reports = [row for row in result["trace"] if row["kind"] == "report"]
    assert all(row["attack"]["origin"]["sequence"] > 0 for row in reports)
    assert all(row["attack"]["decision"] == "accepted" for row in reports)
    assert all(row["attack"]["report_outcome"] ==
               ("reported" if row["admitted"] else "repeat_filtered") for row in reports)
    assert len(result["aubio_extension_sha256"]) == 64
    assert "frames" not in result and len(result["trace"]) < result["counts"]["hops"]
    assert json.loads(output.read_text(encoding="utf-8"))["detected"] == result["detected"]
    assert (manifest.read_bytes(), audio.read_bytes()) == originals
    saved = output.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        tracing.run(manifest, audio.name, output)
    assert output.read_bytes() == saved


def armed(instrument=BASS_GUITAR):
    result = tracing.TracedDetector(instrument=instrument)
    result._pitch = SimpleNamespace(process=lambda block: (55., .95))
    result._pitch_check = None
    result._onset = lambda block: False
    result._onset_pending = True
    result._onset_age = 7
    result._onset_backdate_hops = .375
    result._onset_votes = [33, 33]
    result._strike_confirmed = True
    result._hops_seen = 30
    return result


@pytest.mark.parametrize("trigger", ["sound_end", "timeout"])
def test_pending_flush_retains_votes_and_fractional_onset_before_clearing(trigger):
    detector = armed()
    if trigger == "timeout":
        detector._onset_age = detector.onset_hold_hops
    previous_age = detector._onset_age
    note = detector.process(np.full(detector.hop_size, 0 if trigger == "sound_end" else .1, np.float32))
    flush = next(row for row in detector.trace if row["kind"] == "flush")
    assert flush["trigger"] == trigger and flush["outcome"] == "eligible"
    assert flush["before"]["votes"] == [33, 33]
    assert note.onset_age_hops == previous_age + 1.375
    assert flush["note"] == tracing.event(detector, note)
    assert detector._onset_votes == []


@pytest.mark.parametrize("cause", ["insufficient_votes", "attack_unconfirmed"])
def test_flush_rejection_records_the_actual_gate(cause):
    detector = armed()
    if cause == "insufficient_votes":
        detector._onset_votes = [33]
    else:
        detector._strike_confirmed = False
    assert detector._flush_votes() is None
    row = next(row for row in detector.trace if row["kind"] == "flush")
    assert row["outcome"] == cause and row["note"] is None


def test_repeat_rejection_keeps_original_pitch_and_strike_time_visible():
    detector = armed()
    detector._last_report_hop, detector._last_report_note = 20, 40
    note = DetectedNote(28, 41.2, .98, "E1", True, 5.25, original_midi_note=40)
    assert detector._report(note) is None
    row = detector.trace[-1]
    assert row["outcome"] == "repeat_filter" and not row["admitted"]
    assert row["note"]["original_midi_note"] == 40
    assert row["note"]["onset_s"] == pytest.approx((30 - 5.25) * 512 / 44100 - .5 - .038)


def test_idle_flush_does_not_resurrect_stale_secondary_votes():
    detector = armed()
    detector._onset_pending = False
    detector._onset_votes = []
    detector._bass_pitch_votes = [(33, .99), (33, .99)]
    assert detector._flush_votes() is None
    assert detector.trace[-1]["before"]["pending"] is False
    assert detector.trace[-1]["before"]["yin_votes"] == [(33, .99), (33, .99)]
    assert detector._bass_pitch_votes == [(33, .99), (33, .99)]


def test_new_onset_and_old_flush_have_separate_snapshots():
    detector = armed()
    class Native:
        def __call__(self, block):
            return [1.]
        def get_last(self):
            return detector._hops_seen * detector.hop_size - .25 * detector.hop_size
    detector._onset = tracing.ObservedOnset(Native(), detector)
    note = detector.process(np.full(detector.hop_size, .1, np.float32))
    kinds = [row["kind"] for row in detector.trace if row["kind"] != "phase_check"]
    assert kinds == ["native_onset", "flush", "report"]
    assert detector.trace[1]["trigger"] == "native_onset"
    assert detector.trace[1]["before"]["age_hops"] == 8
    assert note.onset_age_hops == 8.375 and detector._onset_age == 0
    old = next(row["attack"] for row in detector.trace if row["kind"] == "report")
    assert old["close_reason"] == "replaced" and old["report_outcome"] == "reported"
    assert old["closed_age_hops"] == 8.375 and old["decided_votes"] == [33, 33]
    assert old["origin"]["sequence"] != detector._attack.origin.sequence
    assert detector._attack.report_outcome is None


def test_second_flush_is_identified_as_closed_instead_of_missing_pitch():
    detector = armed()
    assert detector._flush_votes().is_onset
    assert detector._flush_votes() is None
    assert detector.trace[-1]["outcome"] == "already_closed"
    assert detector.trace[-1]["attack"]["decided_votes"] == [33, 33]


@pytest.mark.parametrize("instrument", [BASS_GUITAR, BASS_UKULELE], ids=lambda item: item.key)
def test_phase_veto_trace_identifies_the_candidate_and_both_energy_scales(monkeypatch, instrument):
    # The existing causal waveform supplies a false spectral onset in a
    # steady bass tone. Observe the actual guard without changing its result.
    spec = importlib.util.spec_from_file_location(
        "trace_phase_fixture", ROOT / "tests/test_detector_phase_attack.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    monkeypatch.setattr(fixture, "PitchDetector", tracing.TracedDetector)
    detector, found, _ = fixture.scenario(44100, np.pi / 2, instrument=instrument)
    assert found == []
    checks = [row for row in detector.trace if row["kind"] == "phase_check"]
    assert checks and any(not row["supported"] for row in checks)
    assert all(row["origin"]["sequence"] == 1 for row in checks)
    assert all(len(row["powers"]) == len(row["reference"]) == len(row["bounds"]) == 2
               for row in checks)
    assert not any(row["kind"] == "report" and row["admitted"] for row in detector.trace)


def test_normal_vote_rejection_is_logged_without_storing_frames():
    detector = armed()
    detector.strike_rise_db = 0
    detector._strike_confirmed = False
    detector._onset_votes = [33] * 7
    detector._fundamental_present = lambda freq: True
    note = detector.process(np.full(detector.hop_size, .1, np.float32))
    assert not note.is_onset
    assert detector.trace[-1]["kind"] == "vote_completion"
    assert detector.trace[-1]["before_hop"]["votes"] == [33] * 7
    assert not detector.trace[-1]["returned_onset"]
    assert not next(row for row in detector.trace if row["kind"] == "transient_check")["accepted"]


@pytest.mark.parametrize("recording", ["../recording.wav", "/recording.wav", "./recording.wav", "missing.wav"])
def test_selection_must_match_manifest_exactly_before_processing(corpus, monkeypatch, recording):
    manifest, _, output = corpus
    monkeypatch.setattr(tracing, "trace_recording", lambda *a: pytest.fail("No selection, no detector"))
    with pytest.raises(ValueError, match="exactly match"):
        tracing.run(manifest, recording, output)
    assert not output.exists()


def test_manifest_path_safety_is_inherited_before_tracing(corpus, monkeypatch):
    manifest, _, output = corpus
    doc = json.loads(manifest.read_text())
    doc["files"][0]["path"] = "../outside.wav"
    manifest.write_text(json.dumps(doc))
    monkeypatch.setattr(tracing, "trace_recording", lambda *a: pytest.fail("Unsafe manifest"))
    with pytest.raises(ValueError, match="within"):
        tracing.run(manifest, "../outside.wav", output)


def test_trace_limits_fail_instead_of_silently_dropping_events(monkeypatch):
    detector = armed()
    monkeypatch.setattr(tracing, "MAX_TRACE_EVENTS", 1)
    detector.log("first")
    with pytest.raises(ValueError, match="limit"):
        detector.log("second")
    assert len(detector.trace) == 1


def test_instrumentation_mismatch_is_rejected(monkeypatch):
    original = tracing.stream
    def altered(detector, signal):
        return [{}] if isinstance(detector, tracing.TracedDetector) else original(detector, signal)
    monkeypatch.setattr(tracing, "stream", altered)
    with pytest.raises(ValueError, match="changed detector events"):
        tracing.trace_recording(np.zeros(512, np.float32), 44100, "bass_ukulele")


def test_nonfinite_report_is_rejected_before_output_creation(corpus, monkeypatch):
    manifest, audio, output = corpus
    monkeypatch.setattr(tracing, "trace_recording", lambda *a: {"invalid": float("nan")})
    with pytest.raises(ValueError, match="JSON"):
        tracing.run(manifest, audio.name, output)
    assert not output.exists()


def test_hardware_guard_restores_callables_even_after_failure(corpus, monkeypatch):
    import sounddevice
    manifest, audio, output = corpus
    previous = sounddevice.InputStream
    monkeypatch.setattr(tracing, "trace_recording", lambda *a: sounddevice.InputStream())
    with pytest.raises(AssertionError, match="hardware is forbidden"):
        tracing.run(manifest, audio.name, output)
    assert sounddevice.InputStream is previous and not output.exists()


def test_audio_changed_during_trace_is_rejected_without_report(corpus, monkeypatch):
    manifest, audio, output = corpus
    def mutate(*args):
        audio.write_bytes(audio.read_bytes() + b"changed")
        return {}
    monkeypatch.setattr(tracing, "trace_recording", mutate)
    with pytest.raises(ValueError, match="changed during tracing"):
        tracing.run(manifest, audio.name, output)
    assert not output.exists()


@pytest.mark.parametrize("trigger", ["sound_end", "timeout"])
def test_rejected_quiet_native_onset_does_not_relabel_an_old_flush(trigger):
    detector = armed(BASS_UKULELE)
    class Native:
        def __call__(self, block):
            return [1.]
        def get_last(self):
            return detector._hops_seen * detector.hop_size
    detector._onset = tracing.ObservedOnset(Native(), detector)
    if trigger == "timeout":
        detector._onset_age = detector.onset_hold_hops
    note = detector.process(np.full(detector.hop_size, 0 if trigger == "sound_end" else .0001, np.float32))
    assert note.is_onset  # the previous strike survives; no new candidate was opened
    native = next(row for row in detector.trace if row["kind"] == "native_onset")
    flush = next(row for row in detector.trace if row["kind"] == "flush")
    assert not native["admitted"] and native["before"]["signal_db"] < detector.noise_gate_db
    assert flush["trigger"] == trigger and not detector._onset_pending


@pytest.mark.parametrize("profile,midi", [("guitar", 40), ("bass_guitar", 28)])
def test_real_backends_have_exact_event_parity_for_other_profiles(profile, midi):
    pytest.importorskip("aubio")
    rate = 44100
    time = np.arange(rate // 2) / rate
    frequency = 440 * 2 ** ((midi - 69) / 12)
    signal = sum(a * np.sin(2 * np.pi * frequency * (i + 1) * time)
                 for i, a in enumerate((1., .25, .125, .06)))
    signal *= .15 * np.exp(-3 * time)
    signal[:221] *= np.linspace(0, 1, 221)
    signal[-221:] *= np.linspace(1, 0, 221)
    result = tracing.trace_recording(signal.astype(np.float32), rate, profile)
    assert result["events_equal"] and len(result["detected"]) == 1
    assert result["detected"][0]["midi_note"] == midi


def test_partial_evidence_names_harmonics_instead_of_octave_band_indexes():
    from pickhero.audio.attack import AttackEvidence
    detector = armed(BASS_UKULELE)
    detector._attack.evidence.append(AttackEvidence(
        "partials", 30, 10, 17, (-74., -79.), (-58., -51.), (2, 6)))
    evidence, = tracing.attack_snapshot(detector._attack)["evidence"]
    assert evidence["kind"] == "partials"
    assert evidence["harmonics"] == [2, 6] and "bands" not in evidence
    assert evidence["reference"] == (-74., -79.)
    assert evidence["observed"] == (-58., -51.)


@pytest.mark.parametrize("cents", [-25, 0, 25])
def test_retained_native_onset_is_visible_without_changing_any_event(cents):
    from pickhero.audio.detector import PitchDetector
    from tests.test_detector_refinement import passage
    signal, rate, _ = passage(375, cents, True)
    traced = tracing.TracedDetector(instrument=BASS_UKULELE, sample_rate=rate)
    plain = PitchDetector(instrument=BASS_UKULELE, sample_rate=rate)
    actual, expected = tracing.stream(traced, signal), tracing.stream(plain, signal)
    assert actual == expected and len(actual) == 8
    continuations = [row for row in traced.trace if row["kind"] == "onset_continuation"]
    assert continuations
    for row in continuations:
        owner = row["attack"]["origin"]
        decision = row["attack"]["continuations"][-1]
        assert owner["onset_sample"] < decision["native_sample"] == row["native_sample"]
        assert decision["reference_end_sample"] <= owner["onset_sample"]
        assert decision["before_retrigger_end_sample"] <= decision["native_sample"]
        reports = [entry for entry in traced.trace if entry["kind"] == "report"
                   and entry["attack"] is not None
                   and entry["attack"]["origin"]["sequence"] == owner["sequence"]]
        assert len(reports) == 1 and reports[0]["admitted"]
        assert reports[0]["attack"]["origin"] == owner
        assert reports[0]["attack"]["continuations"] == row["attack"]["continuations"]


@pytest.mark.parametrize("phase", [0., np.pi / 2])
def test_sustain_veto_has_its_own_outcome_and_owned_partial_evidence(phase):
    from pickhero.audio.detector import PitchDetector
    from tests.test_detector_unmasking import unmasking_signal
    signal = unmasking_signal(96000, phase)
    traced = tracing.TracedDetector(instrument=BASS_UKULELE, sample_rate=96000)
    plain = PitchDetector(instrument=BASS_UKULELE, sample_rate=96000)
    assert tracing.stream(traced, signal) == tracing.stream(plain, signal)
    rejected = [row for row in traced.trace if row["kind"] == "report" and row["outcome"] == "sustain_filter"]
    assert len(rejected) == (2 if phase == 0. else 1)
    for row in rejected:
        assert not row["admitted"] and row["attack"]["report_outcome"] == "sustain_filtered"
        evidence = row["attack"]["evidence"][-1]
        assert evidence["kind"] == "decaying_partials" and evidence["harmonics"] == [2, 6]
        assert "bands" not in evidence
        assert all(now < before for now, before in zip(evidence["observed"], evidence["reference"]))
        assert evidence["reference_last_hop"] * traced.hop_size <= row["attack"]["origin"]["onset_sample"]
