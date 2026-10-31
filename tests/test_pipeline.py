"""End-to-end: real audio in, scored hits out.

Every part of this chain was tested in isolation and every part worked, yet
the app scored zero. The unit tests could not see it because the failure only
exists where the parts meet: aubio's onset detector is a streaming detector,
and the noise gate returned before feeding it, so it lost its sense of time
and missed half the strikes. Separately, the pitch was read on the same hop
the strike fired, when the analysis window still held the silence before it,
so the uncertain pitch was discarded and took the strike with it.

Both are invisible unless you run real audio through the whole pipeline and
count what the matcher actually scores. Hence this file.
"""

import wave
from pathlib import Path

import numpy as np
import pytest

from pickhero import instruments
from pickhero.audio.detector import PitchDetector
from pickhero.audio.input import TimestampedNote
from pickhero.matcher import NoteMatcher
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from tests._recordings import open_strings


# The fixture is three plucks each of E1, A1, D2, G2, 0.4 s apart on a 0.5 s grid.
EXPECTED_MIDI = [28] * 3 + [33] * 3 + [38] * 3 + [43] * 3
STRING_OF = {28: 4, 33: 3, 38: 2, 43: 1}


def _audio_ubass():
    """A full 70-second real take, if one is named in PICKHERO_UBASS.

    Not a fixture in the repo: it is 6 MB of someone's playing. The tests that
    need it skip without it rather than shipping it.
    """
    import os
    path = Path(os.environ.get("PICKHERO_UBASS", "ubass.wav"))
    if not path.exists():
        pytest.skip("ubass.wav liegt nicht auf dieser Maschine")
    with wave.open(str(path), "rb") as handle:
        sr = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0, sr


def _audio():
    with wave.open(str(open_strings()), "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0, sr


def _timeline():
    notes = [
        NoteEvent(timestamp_ms=i * 500.0, duration_ms=400.0, midi_note=midi,
                  string=STRING_OF[midi], fret=0, measure=i // 4)
        for i, midi in enumerate(EXPECTED_MIDI)
    ]
    return Timeline(notes, SongMetadata(
        title="fixture", tempo=120, tuning=instruments.BASS_UKULELE.tuning))


def _play(detector, audio, sr, timeline, timing_window_ms=150.0):
    """Feed audio through the detector into the matcher, as the app does."""
    matcher = NoteMatcher(
        timeline,
        timing_window_ms=timing_window_ms,
        # Without this the matcher retires each note as a miss before its own
        # detection can arrive, which no timestamp correction can undo.
        retire_delay_ms=detector.expected_delay_ms,
    )
    hop = detector.hop_size
    for i in range(0, len(audio) - hop + 1, hop):
        result = detector.process(audio[i:i + hop])
        now_ms = (i + hop) / sr * 1000.0
        detected = []
        if result is not None:
            # Date the event to the strike, not to when the pitch settled.
            ts = (now_ms
                  - result.onset_age_hops * (hop / sr) * 1000.0
                  - detector.onset_latency_ms)
            detected.append(TimestampedNote(note=result, timestamp_ms=ts))
        matcher.process_detected_notes(detected, now_ms)
    # Let anything still pending retire.
    matcher.process_detected_notes([], len(audio) / sr * 1000.0 + 5000.0)
    return matcher.get_statistics()


class TestRealAudioScores:
    def test_every_note_of_the_real_recording_is_hit(self):
        audio, sr = _audio()
        detector = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        stats = _play(detector, audio, sr, _timeline())
        assert stats["hits"] == len(EXPECTED_MIDI), (
            f"expected all {len(EXPECTED_MIDI)} notes to score as hits, got "
            f"{stats['hits']} hits / {stats['close']} close / {stats['misses']} misses"
        )
        assert stats["accuracy_percent"] == pytest.approx(100.0)

    def test_the_onset_detector_is_fed_on_silent_hops_too(self):
        """Structural guard for the noise-gate ordering.

        aubio's onset detector is a streaming detector with its own history
        and peak picker. Returning early on a quiet hop starves it. With the
        current specflux settings the end result happens to survive that, so
        an outcome test would not catch a regression here; this checks the
        behaviour directly instead.
        """
        audio, sr = _audio()
        detector = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        seen = []
        real_onset = detector._onset

        class Counting:
            def __call__(self, buf):
                seen.append(float(np.sqrt(np.mean(buf ** 2))))
                return real_onset(buf)

            def __getattr__(self, name):
                return getattr(real_onset, name)

        detector._onset = Counting()
        hop = detector.hop_size
        gate = detector.noise_gate_db
        hops = 0
        quiet = 0
        for i in range(0, len(audio) - hop + 1, hop):
            chunk = audio[i:i + hop]
            hops += 1
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            db = 20 * np.log10(rms) if rms > 0 else -120.0
            if db < gate:
                quiet += 1
            detector.process(chunk)
        assert quiet > 0, "the fixture should contain hops below the noise gate"
        assert len(seen) == hops, (
            f"onset detector saw {len(seen)} of {hops} hops; it must see all of "
            f"them, including the {quiet} below the noise gate"
        )

    def test_pitch_is_read_after_the_strike_not_on_it(self):
        """A strike must survive a few hops of not-yet-certain pitch."""
        audio, sr = _audio()
        decoupled = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        assert decoupled.onset_hold_hops > 0
        coupled = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        coupled.onset_hold_hops = 0          # the old behaviour
        assert _play(coupled, audio, sr, _timeline())["hits"] <=             _play(decoupled, audio, sr, _timeline())["hits"]

    def test_each_played_note_produces_a_strike_with_the_right_pitch(self):
        """One strike per pluck, with the right pitch, and nothing else.

        This used to check coverage rather than counting, because the onset
        detector fired 18 times for 12 plucks and the surplus was written off
        as harmless: a note already matched cannot be matched again. It is not
        harmless. An unmatched detection still reaches the UI, so it breaks
        any streak or combo counter and paints feedback for a note nobody
        played. The level-rise strike gate removes them, so this now counts.
        """
        audio, sr = _audio()
        detector = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        hop = detector.hop_size
        strikes = []
        for i in range(0, len(audio) - hop + 1, hop):
            r = detector.process(audio[i:i + hop])
            if r is not None and r.is_onset:
                ts = ((i + hop) / sr * 1000.0
                      - r.onset_age_hops * (hop / sr) * 1000.0
                      - detector.onset_latency_ms)
                strikes.append((ts, r.midi_note))

        covered = 0
        for index, midi in enumerate(EXPECTED_MIDI):
            want_ms = index * 500.0
            if any(abs(ts - want_ms) <= 250.0 and note == midi for ts, note in strikes):
                covered += 1
        # All 12, not 11. The comment here used to say the last G was read an
        # octave and a fourth low; raising the confidence threshold to 0.25
        # fixed that and the assertion was simply never tightened.
        assert covered == len(EXPECTED_MIDI), (
            f"{covered} of {len(EXPECTED_MIDI)} notes had a correctly pitched "
            f"strike near their time; strikes were {strikes}"
        )
        assert len(strikes) == len(EXPECTED_MIDI), (
            f"{len(strikes)} strikes for {len(EXPECTED_MIDI)} notes; spurious "
            "strikes would break any streak or combo counter"
        )


class TestOpenGOctave:
    """The open G was read an octave low in a quarter of its frames.

    98 Hz on a rubber string carries strong partials that are not whole
    multiples of the fundamental, which makes the half-frequency hypothesis
    look plausible to a harmonic-sum detector: its series has more chances to
    land on energy. Those frames score badly on confidence -- around 0.12
    against 0.43 for the correct ones -- so the threshold separates them. It
    could not be raised before, because a strike that lost frames to it threw
    away the ones it had and reported nothing at all.
    """

    def test_no_strike_of_the_recording_is_reported_an_octave_low(self):
        audio, sr = _audio()
        detector = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        hop = detector.hop_size
        reported = []
        for i in range(0, len(audio) - hop + 1, hop):
            result = detector.process(audio[i:i + hop])
            if result is not None and result.is_onset:
                reported.append(result.midi_note)
        assert reported, "no strike reported at all"
        low = [m for m in reported if m in (16, 21, 26, 31)]
        assert low == [], f"octave-low reports: {low}"

    def test_every_string_of_the_recording_is_struck(self):
        """Three plucks each of E1, A1, D2, G2. Each has to appear."""
        audio, sr = _audio()
        detector = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        hop = detector.hop_size
        reported = set()
        for i in range(0, len(audio) - hop + 1, hop):
            result = detector.process(audio[i:i + hop])
            if result is not None and result.is_onset:
                reported.add(result.midi_note)
        assert {28, 33, 38, 43} <= reported, f"missing strings: {reported}"

    def test_one_pluck_is_not_reported_as_two(self):
        """The onset detector fires twice per pluck. Twelve notes were played."""
        audio, sr = _audio()
        detector = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        hop = detector.hop_size
        count = 0
        for i in range(0, len(audio) - hop + 1, hop):
            result = detector.process(audio[i:i + hop])
            if result is not None and result.is_onset:
                count += 1
        assert count == 12, f"{count} strikes reported for 12 plucks"


class TestStrikeNeedsALevelRise:
    """A strike must be backed by the signal actually getting louder.

    aubio's onset detector measures spectral change, and a decaying rubber
    string keeps changing spectrally for seconds, so it fires again and again
    inside one note. Those re-fires are not filtered by min_note_gap_ms,
    because a re-fire usually names a DIFFERENT note than the strike before it
    and that check only suppresses repeats. What separates a pluck from a
    re-fire is not the spectrum but the level: a pluck puts energy into the
    string, a re-fire is that energy running out.

    The signal below is built to isolate exactly that. One 55 Hz tone decays
    for 1.2 s, then its partial balance changes abruptly -- a large spectral
    change, which fires the onset detector -- at a level the caller chooses.
    """

    SR = 44100

    def _signal(self, second_gain_db):
        sr = self.SR

        def tone(f, n, phase, partials):
            t = np.arange(n) / sr
            x = np.zeros(n)
            for k, w in enumerate(partials, start=1):
                x += w * np.sin(2 * np.pi * f * k * t + phase * k)
            return x * np.exp(-1.1 * t)

        n = int(1.2 * sr)
        a = tone(55.0, n, 0.0, (1.0, 0.5, 0.3, 0.2)) * 0.30
        b = tone(55.0, n, 1.7, (0.15, 1.0, 0.9, 0.7, 0.5))
        ref = float(np.sqrt(np.mean(a[-512:] ** 2)))
        cur = float(np.sqrt(np.mean(b[:512] ** 2)))
        b *= (ref * 10 ** (second_gain_db / 20.0)) / cur
        return np.concatenate(
            [np.zeros(int(0.3 * sr)), a, b]).astype(np.float32)

    def _strikes(self, signal, rise_db):
        det = PitchDetector(sample_rate=self.SR,
                            instrument=instruments.BASS_UKULELE)
        det.strike_rise_db = rise_db
        hop = det.hop_size
        out = []
        for i in range(0, len(signal) - hop + 1, hop):
            r = det.process(signal[i:i + hop])
            if r is not None and r.is_onset:
                out.append((i + hop) / self.SR)
        return out

    def test_without_the_gate_one_sustained_note_fires_repeatedly(self):
        """The problem, stated as a test. Two events, six strikes."""
        signal = self._signal(0.0)
        assert len(self._strikes(signal, 0.0)) >= 5

    def test_a_spectral_change_at_the_same_level_is_not_a_strike(self):
        """Nothing got louder, so nothing was played."""
        signal = self._signal(0.0)
        late = [t for t in self._strikes(signal, 18.0) if t > 1.0]
        assert late == [], f"strikes reported inside a decay: {late}"

    def test_a_real_level_jump_still_produces_a_strike(self):
        """The gate must not simply refuse everything after the first note."""
        signal = self._signal(30.0)
        late = [t for t in self._strikes(signal, 18.0) if t > 1.0]
        assert late, "a 30 dB jump was not reported as a new note"

    def test_the_guitar_profile_leaves_the_mechanism_off(self):
        """The number was measured on a bass ukulele and nowhere else.

        Guitar detection goes through aubio YIN, which was never part of any
        of this, and it must be bit-for-bit unaffected.
        """
        assert instruments.GUITAR.strike_rise_db == 0.0
        det = PitchDetector(sample_rate=44100, instrument=instruments.GUITAR)
        assert det.strike_rise_db == 0.0


class TestHowFastSheMayPlay:
    """The level-rise gate's blind spot, measured rather than assumed.

    The gate accepts an onset only when the level has risen above the quietest
    hop of the preceding window, which requires that window to contain
    something quiet. Neither reference recording has two plucks closer than
    680 ms, so the entire parameter choice was fitted in a region where this
    question was never asked. It is asked here.

    Built from one of their own recorded plucks rather than a synthesised tone,
    so the attack and the decay belong to the instrument. The damping between
    repeats is a model, not a recording, and that is the honest limit of this
    test: it says where the mechanism stops working on a plausible signal, not
    where it stops working on their hands.
    """

    GRAIN_AT_S = 9.42
    GRAIN_LEN_S = 1.30

    def _grain(self):
        audio, sr = _audio_ubass()
        start = int(self.GRAIN_AT_S * sr)
        return audio[start:start + int(self.GRAIN_LEN_S * sr)].copy(), sr

    def _passage(self, gap_ms: float, count: int = 8):
        import numpy as np

        grain, sr = self._grain()
        peak = float(np.abs(grain).max())
        step = int(gap_ms / 1000.0 * sr)
        out = np.zeros(step * (count - 1) + len(grain) + sr // 2, dtype=np.float32)
        fade = int(0.008 * sr)
        for i in range(count):
            at = i * step
            if i and step < len(grain):
                out[at:at + fade] *= np.linspace(1.0, 0.25, fade, dtype=np.float32)
                out[at + fade:] *= 0.25
            end = min(at + len(grain), len(out))
            out[at:end] += grain[:end - at]
        loudest = float(np.abs(out).max())
        return (out / loudest * peak).astype(np.float32) if loudest else out, sr

    def _strikes(self, signal, sr) -> int:
        detector = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        hop = detector.hop_size
        count = 0
        for i in range(0, len(signal) - hop, hop):
            note = detector.process(signal[i:i + hop])
            if note is not None and note.is_onset:
                count += 1
        return count

    @pytest.mark.parametrize("gap_ms,music", [
        (1000.0, "Viertel bei 60"),
        (500.0, "Achtel bei 60, Viertel bei 120"),
        (375.0, "Achtel bei 80"),
        (300.0, "Achtel bei 100"),
        (250.0, "Sechzehntel bei 60, Achtel bei 120"),
    ])
    def test_repeated_notes_survive_down_to_a_quarter_second(self, gap_ms, music):
        """Their exercises run at 60 to 72 BPM, where an eighth note is 417 to
        500 ms. The gate has to cost nothing there, with room to spare."""
        signal, sr = self._passage(gap_ms)
        assert self._strikes(signal, sr) >= 8, music

    def test_the_gate_is_not_what_limits_the_tempo(self):
        """Below about 250 ms the detector stops resolving repeats -- but it
        stops with the gate off as well, so this is a limit that was already
        there and not one the gate introduced. Worth a test so that if the
        limit ever moves, it is visible which half moved it."""
        signal, sr = self._passage(200.0)
        with_gate = self._strikes(signal, sr)

        detector = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        detector.strike_rise_db = 0.0
        hop = detector.hop_size
        without = 0
        for i in range(0, len(signal) - hop, hop):
            note = detector.process(signal[i:i + hop])
            if note is not None and note.is_onset:
                without += 1
        assert with_gate >= without - 1, (
            f"mit Flanke {with_gate}, ohne {without}: die Flanke waere der Engpass")


class TestANoteStruckWhileAnotherRings:
    """Their actual complaint, and the case no recording covered.

    The player plays open strings on a bass ukulele. Nothing damps them, so when the player
    plucks the E the A the player played two seconds ago is still sounding at -16 dB.
    Every reference recording is single plucks, or runs on one string, so the
    whole detector was tuned in a region where this never happened.

    What went wrong was not the pitch. The analysis window is 93 ms wide, and
    for that long after a new note is struck it still holds mostly the note
    before it. The vote was decided from those frames, so it reported the A
    the player had let ring instead of the E the player had just played, and the correct
    reading arrived 130 ms later and counted as a second, spurious strike.

    Built from their own plucks: two real notes laid over each other, not
    synthesised tones, because the decay of a rubber string is the whole
    point.
    """

    RING_AT_S = 19.35      # the open A, from the single-pluck section
    RING_MIDI = 33
    PLUCK_AT_S = 9.42      # the open E, the quietest string on the instrument
    PLUCK_MIDI = 28

    def _over(self, gap_s: float = 2.0):
        import numpy as np

        audio, sr = _audio_ubass()
        ring = audio[int(self.RING_AT_S * sr):int((self.RING_AT_S + 4.5) * sr)]
        pluck = audio[int(self.PLUCK_AT_S * sr):int((self.PLUCK_AT_S + 2.5) * sr)]
        gap = int(gap_s * sr)
        out = np.zeros(max(len(ring), gap + len(pluck)) + sr, dtype=np.float32)
        out[:len(ring)] += ring
        out[gap:gap + len(pluck)] += pluck
        return out, sr, gap / sr * 1000.0

    def _strikes(self, signal, sr):
        detector = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        hop = detector.hop_size
        out = []
        for i in range(0, len(signal) - hop, hop):
            note = detector.process(signal[i:i + hop])
            if note is not None and note.is_onset:
                out.append((i / sr * 1000.0, note.midi_note))
        return out

    def test_the_new_note_is_the_one_reported(self):
        signal, sr, at = self._over()
        near = [m for ms, m in self._strikes(signal, sr) if abs(ms - at) <= 450]
        assert self.PLUCK_MIDI in near, \
            f"bei {at:.0f} ms gemeldet: {near}, erwartet {self.PLUCK_MIDI}"

    def test_the_ringing_note_is_not_reported_again(self):
        """It is already sounding; it was not struck. Reporting it is a
        phantom, and it consumes the written note that the real strike was
        meant for."""
        signal, sr, at = self._over()
        near = [m for ms, m in self._strikes(signal, sr) if abs(ms - at) <= 450]
        assert self.RING_MIDI not in near, f"Geisteranschlag: {near}"

    def test_one_pluck_gives_one_strike(self):
        signal, sr, at = self._over()
        near = [m for ms, m in self._strikes(signal, sr) if abs(ms - at) <= 450]
        assert len(near) == 1, f"{len(near)} Anschlaege fuer einen: {near}"

    @pytest.mark.parametrize("gap_s", [2.0, 2.5, 3.0])
    def test_it_holds_while_the_neighbour_is_still_clearly_audible(self, gap_s):
        signal, sr, at = self._over(gap_s=gap_s)
        near = [m for ms, m in self._strikes(signal, sr) if abs(ms - at) <= 450]
        assert self.PLUCK_MIDI in near, f"{gap_s} s: gemeldet {near}"

    @pytest.mark.parametrize("gap_s,expected", [(0.8, [28]), (1.0, [28]), (1.5, [])])
    def test_covered_e_recovery_and_remaining_overlap_case(self, gap_s, expected):
        """Growing pitch evidence recovers the formerly missed 0.8/1s E.

        The separately phased 1.5s mixture still supplies no eligible note.
        These are three recording transforms, not a universal tempo boundary.
        Keep the exact outputs so a returning A or duplicate cannot pass.
        """
        signal, sr, at = self._over(gap_s=gap_s)
        near = [m for ms, m in self._strikes(signal, sr) if abs(ms - at) <= 450]
        assert near == expected


class TestItDoesNotInventNotes:
    """Two strings together must not produce a third that nobody played.

    B3 at 247 Hz and E4 at 330 are the third and fourth harmonics of 82 Hz,
    so a pitch tracker legitimately reports an E2 -- a note two octaves below
    anything that was struck. Measured, that E2 sat 105 dB below the notes
    actually sounding: the frequency it named had essentially no energy. A
    harmonic cannot exist without its own fundamental being at least faintly
    present, so the app now asks whether there is anything there at all.

    Saying nothing is the honest answer for a monophonic detector faced with
    two notes. Saying a third note is not.
    """

    SR = 44100

    def _pluck(self, midi: int, seconds: float = 2.0, seed: int = 0):
        """Karplus-Strong. Enough of a plucked string for this question."""
        import numpy as np
        from pickhero.audio.note_utils import midi_to_freq

        rng = np.random.default_rng(seed)
        period = int(self.SR / midi_to_freq(midi))
        buffer = rng.uniform(-1, 1, period).astype("float32")
        out = np.empty(int(seconds * self.SR), dtype="float32")
        for i in range(len(out)):
            out[i] = buffer[i % period]
            buffer[i % period] = 0.996 * 0.5 * (
                buffer[i % period] + buffer[(i + 1) % period])
        return out / max(abs(out).max(), 1e-9) * 0.5

    def _reported(self, signal):
        detector = PitchDetector(sample_rate=self.SR, instrument=instruments.GUITAR)
        hop = detector.hop_size
        out = []
        for i in range(0, len(signal) - hop, hop):
            note = detector.process(signal[i:i + hop])
            if note is not None and note.is_onset:
                out.append(note.midi_note)
        return out

    @pytest.mark.parametrize("midi", [40, 45, 50, 55, 59, 64])
    def test_one_string_is_still_heard(self, midi):
        assert midi in self._reported(self._pluck(midi, seed=1))

    @pytest.mark.parametrize("a,b", [(59, 64), (40, 45), (50, 55), (45, 50),
                                     (55, 59), (40, 64)])
    def test_two_strings_never_produce_a_third(self, a, b):
        mixed = (self._pluck(a, seed=1) + self._pluck(b, seed=2)) * 0.5
        for midi in self._reported(mixed):
            assert midi in (a, b), (
                f"{a}+{b} gemeldet als {midi} -- ein Ton, den niemand spielte")

    def test_a_real_low_note_survives_the_check(self):
        """Their instrument's fundamental is genuinely weak -- the body cannot
        radiate 41 Hz -- so the check must not throw the low E away with the
        invented ones. Measured, real fundamentals run 6 to 30 dB below the
        loudest partial and the invented one sat at 105."""
        audio, sr = _audio_ubass()
        detector = PitchDetector(sample_rate=sr, instrument=instruments.BASS_UKULELE)
        hop = detector.hop_size
        segment = audio[int(9.3 * sr):int(11.5 * sr)]
        found = set()
        for i in range(0, len(segment) - hop, hop):
            note = detector.process(segment[i:i + hop])
            if note is not None and note.is_onset:
                found.add(note.midi_note)
        assert 28 in found, f"die tiefe E-Saite ging verloren: {found}"
