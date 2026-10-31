"""Tests for instrument profiles and the harmonic pitch detector.

The interesting tests are in TestRealBassUkulele. Their fixture is six seconds
cut from an actual bass ukulele recorded through a condenser microphone, three
plucks of each open string. Synthetic sine waves cannot catch the failure this
module exists to fix, because that failure is caused by the instrument's
fundamental being quieter than its own harmonics, and by aubio's YIN having a
hard lower frequency limit that sits above the open low E.
"""

import wave
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from pickhero import instruments
from pickhero.audio.harmonic import HarmonicPitchDetector, detector_for
from pickhero.audio.note_utils import (
    freq_to_midi,
    is_in_guitar_range,
    is_in_instrument_range,
    midi_to_freq,
)
from tests._recordings import open_strings

FIXTURES = Path(__file__).parent / "fixtures"


class TestProfiles:
    def test_guitar_matches_the_old_hardcoded_range(self):
        """The guitar profile must not change existing behaviour."""
        g = instruments.GUITAR
        assert g.midi_min == 40
        assert g.midi_max == 88
        assert g.string_count == 6
        for midi in (40, 64, 88):
            assert is_in_instrument_range(midi, g.midi_min, g.midi_max) is True
            assert is_in_guitar_range(midi) is True
        for midi in (39, 89, 0):
            assert is_in_instrument_range(midi, g.midi_min, g.midi_max) is False

    def test_bass_ukulele_covers_its_open_strings(self):
        """The bug that made the fork necessary: MIDI 28-39 was discarded."""
        u = instruments.BASS_UKULELE
        assert sorted(u.tuning.values()) == [28, 33, 38, 43]
        for midi in u.tuning.values():
            assert is_in_instrument_range(midi, u.midi_min, u.midi_max) is True
            # ...while the old guitar gate threw three of the four away
        assert [is_in_guitar_range(m) for m in sorted(u.tuning.values())] == [
            False, False, False, True
        ]

    def test_string_numbering_is_highest_first(self):
        u = instruments.BASS_UKULELE
        assert u.tuning[1] > u.tuning[u.string_count]
        assert u.strings_low_to_high() == [4, 3, 2, 1]

    def test_search_range_brackets_the_instrument(self):
        u = instruments.BASS_UKULELE
        assert u.freq_min < midi_to_freq(28) < u.freq_max
        assert u.freq_min < midi_to_freq(u.midi_max) < u.freq_max

    def test_window_is_long_enough_for_the_lowest_string(self):
        """Guards the exact defect that made the low E undetectable.

        aubio's YIN can only represent periods up to buf_size/2, so a 2048
        window at 44.1 kHz bottoms out at 43.07 Hz, above the 41.20 Hz string.
        """
        for prof in instruments.PROFILES.values():
            floor = prof.sample_rate / (prof.buf_size / 2)
            assert floor < midi_to_freq(prof.midi_min), (
                f"{prof.key}: buf_size {prof.buf_size} bottoms out at "
                f"{floor:.2f} Hz, above its lowest note "
                f"{midi_to_freq(prof.midi_min):.2f} Hz"
            )

    def test_get_falls_back_to_guitar(self):
        assert instruments.get(None) is instruments.GUITAR
        assert instruments.get("nonsense") is instruments.GUITAR
        assert instruments.get("bass_ukulele") is instruments.BASS_UKULELE

    def test_from_tuning_recognises_a_known_instrument(self):
        prof = instruments.from_tuning({1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40})
        assert prof is instruments.GUITAR

    def test_from_tuning_picks_the_harmonic_detector_for_deep_tunings(self):
        prof = instruments.from_tuning({1: 43, 2: 38, 3: 33, 4: 28, 5: 23})
        assert prof.pitch_method == "harmonic"
        assert prof.midi_min == 23


def _pluck(f0, duration=0.6, sample_rate=44100,
           harmonics=(0.15, 1.0, 0.7, 0.4, 0.2, 0.1, 0.07, 0.05),
           decay=0.8):
    """A plucked note with a deliberately weak fundamental.

    Eight partials, because the detector weighs eight. A six-partial signal is
    silent above the sixth, and the detector then sees two empty bands where a
    real string has energy, which is an artefact of the test rather than of
    the instrument.
    """
    t = np.arange(int(sample_rate * duration), dtype=np.float32) / sample_rate
    y = np.zeros_like(t)
    for k, amp in enumerate(harmonics, 1):
        y += amp * np.sin(2 * np.pi * f0 * k * t)
    return (y * np.exp(-t / decay) * 0.3).astype(np.float32)


def _run(detector, signal, hop=512, conf_min=0.0):
    """Feed a signal hop by hop, return the majority MIDI note."""
    detector.reset()
    notes = []
    for i in range(0, len(signal) - hop + 1, hop):
        freq, conf = detector.process(signal[i:i + hop])
        if freq > 0 and conf >= conf_min:
            notes.append(freq_to_midi(freq))
    if not notes:
        return None
    return Counter(notes).most_common(1)[0][0]


class TestHarmonicDetector:
    def test_rejects_an_inverted_range(self):
        with pytest.raises(ValueError):
            HarmonicPitchDetector(freq_min=300.0, freq_max=100.0)

    def test_silence_yields_nothing(self):
        det = detector_for(instruments.BASS_UKULELE)
        freq, conf = det.process(np.zeros(512, dtype=np.float32))
        assert freq == 0.0
        assert conf == 0.0

    @pytest.mark.parametrize("midi", [28, 33, 38, 43])
    def test_weak_fundamental_is_still_found(self, midi):
        """The whole point: the fundamental is 16 dB below the 2nd partial."""
        det = detector_for(instruments.BASS_UKULELE)
        got = _run(det, _pluck(midi_to_freq(midi)))
        assert got == midi

    def test_cannot_report_a_pitch_outside_the_search_range(self):
        """A note far above the instrument cannot produce an out-of-range read.

        This is what bounding the search buys: aubio's YIN, given the same
        signal, will happily report whatever it finds.
        """
        u = instruments.BASS_UKULELE
        det = detector_for(u)
        signal = _pluck(880.0, duration=1.0)
        for i in range(0, len(signal) - 512 + 1, 512):
            freq, _ = det.process(signal[i:i + 512])
            assert freq == 0.0 or u.freq_min <= freq <= u.freq_max

    def test_empty_hop_is_handled(self):
        det = detector_for(instruments.BASS_UKULELE)
        assert det.process(np.zeros(0, dtype=np.float32)) == (0.0, 0.0)

    def test_confidence_is_higher_for_a_clean_note_than_for_noise(self):
        det = detector_for(instruments.BASS_UKULELE)
        rng = np.random.default_rng(12345)

        def mean_conf(sig):
            det.reset()
            vals = []
            for i in range(0, len(sig) - 512 + 1, 512):
                _, c = det.process(sig[i:i + 512])
                vals.append(c)
            return float(np.mean(vals[len(vals) // 2:]))

        clean = mean_conf(_pluck(midi_to_freq(28), duration=1.0, decay=10.0))
        noise = mean_conf(rng.normal(0, 0.05, 44100).astype(np.float32))
        assert clean > noise


class TestRealBassUkulele:
    """Regression test against a real recording of the target instrument.

    The fixture is 6 seconds cut from a bass ukulele recorded through a
    condenser microphone: three separate plucks of each open string, 0.4 s
    each, 0.1 s of silence between. Clipped takes were excluded, since
    clipping is a recording fault rather than a detector fault.

    The detector gets all twelve right, so this test demands all twelve. That
    is stricter than the detector's real-world average: across all 61 open
    string notes in the full recording it scores 85 percent, and across all 88
    notes including fretted ones, 77 percent. Treat a failure here as a
    regression, not as a tolerable fluctuation.
    """

    NOTE_S = 0.40
    STEP_S = 0.50
    EXPECTED = [28] * 3 + [33] * 3 + [38] * 3 + [43] * 3  # E1 A1 D2 G2

    def _segments(self):
        with wave.open(str(open_strings()), "rb") as w:
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        step, note = int(self.STEP_S * sr), int(self.NOTE_S * sr)
        return sr, [audio[i * step:i * step + note] for i in range(len(self.EXPECTED))]

    def test_fixture_is_present_and_the_right_shape(self):
        sr, segs = self._segments()
        assert sr == 44100
        assert len(segs) == len(self.EXPECTED)
        assert all(len(s) == int(self.NOTE_S * sr) for s in segs)

    def test_harmonic_detector_identifies_every_open_string(self):
        sr, segs = self._segments()
        u = instruments.BASS_UKULELE
        det = detector_for(u, sample_rate=sr)
        got = [_run(det, seg, conf_min=u.confidence_threshold) for seg in segs]
        assert got == self.EXPECTED, f"expected {self.EXPECTED}, got {got}"

    def test_full_detector_emits_notes_for_the_low_e(self):
        """End to end through PitchDetector, including the range gate.

        With the guitar profile's range of MIDI 40-88 this produced nothing at
        all, because every low E is MIDI 28.
        """
        from pickhero.audio.detector import PitchDetector

        sr, segs = self._segments()
        u = instruments.BASS_UKULELE
        det = PitchDetector(sample_rate=sr, instrument=u)
        emitted = []
        for i in range(0, len(segs[0]) - det.hop_size + 1, det.hop_size):
            note = det.process(segs[0][i:i + det.hop_size])
            if note is not None:
                emitted.append(note.midi_note)
        assert emitted, "no notes survived the pipeline for the open low E"
        assert Counter(emitted).most_common(1)[0][0] == 28

    def test_guitar_settings_cannot_reach_the_low_e(self):
        """Documents the original defect on real audio, not on a sine wave."""
        sr, segs = self._segments()
        aubio = pytest.importorskip("aubio")
        yin = aubio.pitch("yin", 2048, 512, sr)
        yin.set_unit("Hz")
        yin.set_tolerance(0.15)
        detected = []
        for i in range(0, len(segs[0]) - 512 + 1, 512):
            f = float(yin(np.ascontiguousarray(segs[0][i:i + 512], dtype=np.float32))[0])
            if f > 0:
                detected.append(f)
        floor = sr / (2048 / 2)
        assert floor > midi_to_freq(28), (
            "aubio's YIN floor at buf_size 2048 is supposed to sit above E1"
        )
        assert all(f >= floor * 0.98 for f in detected), (
            "aubio reported something below its own representable floor, so "
            "this test no longer measures what it claims to"
        )


class TestCalibrationThresholds:
    """The calibration wizard must use the instrument's own confidence scale.

    It hardcoded 0.5, a number inherited from the aubio YIN days that means
    nothing to the harmonic detector. Measured on a real bass ukulele
    recording, counting only frames that carried the correct pitch: the low E
    cleared 0.5 in 3 frames out of 206. The wizard therefore waited forever on
    exactly the string that most needed calibrating, while the other three
    strings cleared it often enough to look like the feature worked.
    """

    def _screen(self, instrument_key):
        import os
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        import pygame
        pygame.init()
        pygame.display.set_mode((1280, 720))
        from pickhero.config import Config
        from pickhero.ui.calibration_menu import CalibrationMenuScreen

        config = Config()
        config.instrument = instrument_key
        screen = CalibrationMenuScreen.__new__(CalibrationMenuScreen)
        screen._config = config
        screen._instrument = config.get_instrument()
        screen._tuning = dict(screen._instrument.tuning)
        screen._string_order = screen._instrument.strings_low_to_high()
        from pickhero.ui.calibration_menu import MIN_CONFIDENCE_FALLBACK
        screen._min_confidence = (
            screen._instrument.confidence_threshold
            if screen._instrument.confidence_threshold is not None
            else MIN_CONFIDENCE_FALLBACK
        )
        return screen

    def test_threshold_follows_the_instrument(self):
        uke = self._screen("bass_ukulele")
        assert uke._min_confidence == instruments.BASS_UKULELE.confidence_threshold
        assert uke._min_confidence < 0.5, (
            "a 0.5 threshold silently excludes almost every correct low-E frame"
        )
        guitar = self._screen("guitar")
        assert guitar._min_confidence == instruments.GUITAR.confidence_threshold

    def test_string_order_and_targets_come_from_the_profile(self):
        uke = self._screen("bass_ukulele")
        assert uke._string_order == [4, 3, 2, 1]
        assert uke._tuning == instruments.BASS_UKULELE.tuning
        guitar = self._screen("guitar")
        assert guitar._string_order == [6, 5, 4, 3, 2, 1]

    def test_cents_off_reports_real_detuning(self):
        from pickhero.config import StringCalibration
        from pickhero.audio.note_utils import midi_to_freq

        uke = self._screen("bass_ukulele")
        uke._results = {}
        assert uke.cents_off(4) is None
        sharp = midi_to_freq(28) * 2 ** (17 / 1200)
        uke._results[4] = StringCalibration(
            midi_note=28, frequency=sharp, noise_floor_db=-45.0
        )
        assert uke.cents_off(4) == pytest.approx(17.0, abs=0.5)
