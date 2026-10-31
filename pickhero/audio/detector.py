"""Pitch and onset detection.

Wraps two interchangeable pitch backends and aubio's onset detector.
Processes audio buffers and returns detected notes.

Which backend is used comes from the active Instrument profile:

- "yin"      aubio's YIN (FFT-YIN above 48 kHz). Its analysis window reaches
             lags of buf_size/2, so its lowest representable frequency is a
             hard floor at sample_rate/(buf_size/2), and it tends to lock onto
             whichever partial is loudest.
- "harmonic" The in-repo harmonic-sum detector. Searches the instrument's
             range and can handle weak fundamentals. Harmonic and octave
             ambiguities remain possible, especially with ringing strings.
"""

from collections import deque
from dataclasses import dataclass, field, replace

import numpy as np

from pickhero import instruments
from pickhero.audio.attack import (AttackEvidence, AttackOrigin, PendingAttack, TimingAmbiguity, BassContinuation,
                                   SupportingOnset, QuietStartEvidence, SparsePitchEvidence)
from pickhero.audio.harmonic import HarmonicPitchDetector
from pickhero.audio.growing_pitch import GrowingPitchCorroborator
from pickhero.audio.waveform_prediction import WaveformHistory
from pickhero.audio.repeats import RepeatCorroborator
from pickhero.audio.quiet_repeats import QuietRepeatCorroborator, inspect_repeat_spectrum
from pickhero.audio.note_utils import (
    freq_to_midi,
    midi_to_freq,
    is_in_instrument_range,
    midi_to_name,
)

try:  # aubio is only needed for the "yin" backend and for onset detection
    import aubio
except ImportError:  # pragma: no cover - exercised only on machines without aubio
    aubio = None


# How far above a measured room floor the gate should sit.
#
# Six, not nine. The gate only decides whether a hop is worth analysing for
# pitch; onsets are fed regardless, and a false note still has to clear the
# confidence threshold twice over. Nine decibels of headroom therefore bought
# very little and cost real notes: measured against a room floor around -42,
# it put the gate at -33 and swallowed the quietest pluck of a take outright.
GATE_MARGIN_DB = 6.0
# A gate is never useful outside this band.
GATE_MIN_DB = -70.0
GATE_MAX_DB = -25.0
# Higher rates exceed the callback budget on supported CPU-only backends.
# Capture negotiates a lower device rate; no samples are silently discarded.
MAX_ANALYSIS_RATE = 192000


@dataclass
class DetectedNote:
    """A single detected pitch event."""
    midi_note: int
    frequency: float
    confidence: float
    name: str
    is_onset: bool  # True if this pitch belongs to a new note strike
    onset_age_hops: float = 0  # fractional hops; profile latency is applied separately
    original_midi_note: int | None = None  # preserve repeat eligibility after a pitch correction
    timing_uncertain: bool = False  # pitch remains usable; onset ownership is ambiguous


def _round_pow2_friendly(value: float) -> int:
    """Round an analysis window to a multiple of 64 samples.

    aubio is happiest with tidy sizes and the numpy FFT does not care, so the
    window is kept on a 64-sample grid rather than left at whatever a rate
    ratio produces.
    """
    return max(256, int(round(value / 64.0)) * 64)


def _attack_view(field: str) -> property:
    """Keep diagnostic/test hooks as views, never as a second state store."""
    return property(lambda self: getattr(self._attack, field),
                    lambda self, value: setattr(self._attack, field, value))


@dataclass(frozen=True, slots=True)
class _OnsetContinuation:
    native_sample: float
    observed_hop: int
    midi_note: int
    reference_end_sample: int
    before_retrigger_end_sample: int
    current_end_sample: int
    harmonics: tuple[int, ...]
    reference_db: tuple[float, ...]
    before_retrigger_db: tuple[float, ...]
    current_db: tuple[float, ...]


@dataclass(slots=True)
class _PhaseCheckedAttack(PendingAttack):
    continuations: list[_OnsetContinuation] = field(default_factory=list)
    phase_ready: bool = False
    bass_continuation_sample: float | None = None
    bass_continuation_evidence: BassContinuation | None = None
    bass_duplicate_evidence: BassContinuation | None = None
    leading_quiet: bool = False
    preceded_by_quiet: bool = False
    quiet_reference_span: tuple[int, int] = (0, 0)
    quiet_reference_peak_db: float = -120.0
    complex_confirmation: SupportingOnset | None = None
    quiet_start_evidence: QuietStartEvidence | None = None
    late_quiet_start_sample: float | None = None
    sparse_pitch_evidence: SparsePitchEvidence | None = None
    phase_reference: tuple[float, float] = (0.0, 0.0)


class PitchDetector:
    """Real-time pitch and onset detection.

    Processes audio buffers (hop_size samples each) and returns detected notes
    with confidence values. The accepted note range and the pitch backend both
    come from the Instrument profile, so a bass ukulele is not silently
    filtered out by guitar-shaped assumptions.
    """

    _onset_pending = _attack_view("pending")
    _onset_age = _attack_view("age_hops")
    _onset_votes = _attack_view("votes")
    _strike_ref_db = _attack_view("reference_db")
    _strike_band_ref = _attack_view("band_reference")
    _strike_confirmed = _attack_view("confirmed")
    _transient_candidate = _attack_view("transient_candidate")
    _periodicity_votes = _attack_view("periodicity_votes")
    _bass_pitch_votes = _attack_view("bass_pitch_votes")

    @property
    def _onset_backdate_hops(self) -> float:
        return self._attack.origin.backdate_hops

    @_onset_backdate_hops.setter
    def _onset_backdate_hops(self, value: float) -> None:
        # Compatibility for controlled test inputs. Production constructs a
        # fixed origin once, when the candidate is opened.
        self._attack.origin = replace(self._attack.origin, backdate_hops=value)

    def __init__(
        self,
        buf_size: int | None = None,
        hop_size: int | None = None,
        sample_rate: int = 44100,
        confidence_threshold: float | None = None,
        onset_threshold: float | None = None,
        noise_gate_db: float | None = None,
        calibration: dict | None = None,
        instrument: "instruments.Instrument | None" = None,
        onset_method: str | None = None,
    ):
        inst = instrument or instruments.GUITAR
        if sample_rate > MAX_ANALYSIS_RATE:
            raise ValueError(f"Audio analysis supports sample rates up to {MAX_ANALYSIS_RATE} Hz")
        self.instrument = inst

        # Every window in this file is a length of TIME, not a number of
        # samples. The profile states it in samples at the rate it was
        # measured at, and the device decides the rate it will actually give.
        #
        # This was not always so, and the difference is not academic. WASAPI
        # shared mode refuses 44100 on their microphone and hands the app 48000,
        # while every constant here was measured at 44100. The 4096-sample
        # window is 92.9 ms at 44100 and 85.3 ms at 48000 -- 3.83 periods of
        # the open low E against 3.52 -- and that is enough to put the E's
        # pitch confidence exactly ON the threshold instead of above it.
        # Measured across 81 hop alignments: 100 percent of low-E plucks
        # detected at 44100, 67.9 percent at 48000, all-or-nothing depending
        # on where the hop grid happened to fall relative to the attack. The
        # player cannot perceive or control that alignment, so from where the player
        # sits a third of their low notes vanish at random. Holding the window
        # constant in time removes the whole effect.
        self.sample_rate = sample_rate
        scale = sample_rate / float(inst.sample_rate or sample_rate)
        self.buf_size = (buf_size if buf_size is not None
                         else _round_pow2_friendly(inst.buf_size * scale))
        self.hop_size = (hop_size if hop_size is not None
                         else min(inst.onset_buf_size,
                                  max(64, int(round(inst.hop_size * scale)))))
        # The onset analysis window remains fixed in samples. A device may
        # negotiate 192 kHz even when the saved preference was 44.1 kHz;
        # scaling the hop beyond that window makes aubio refuse to start.
        self.confidence_threshold = (
            confidence_threshold if confidence_threshold is not None
            else inst.confidence_threshold
        )
        self.onset_threshold = (
            onset_threshold if onset_threshold is not None else inst.onset_threshold
        )
        self.noise_gate_db = (
            noise_gate_db if noise_gate_db is not None else inst.noise_gate_db
        )
        self.onset_method = onset_method or inst.onset_method
        self.onset_buf_size = inst.onset_buf_size
        self.onset_latency_ms = inst.onset_latency_ms
        self.yin_tolerance = inst.yin_tolerance
        self.pitch_method = inst.pitch_method

        self.midi_min = inst.midi_min
        self.midi_max = inst.midi_max

        self.last_signal_db: float = -120.0
        self.last_freq: float = 0.0
        self.last_confidence: float = 0.0

        # Octave jump protection (only meaningful for the yin backend)
        self._prev_freq: float = 0.0
        self._calibration = calibration

        # An onset fires the moment the string is struck, but at that instant
        # the pitch analysis window is still full of whatever came before, so
        # the pitch is not yet confident. Reporting both on the same hop meant
        # the onset was thrown away together with the uncertain pitch, and the
        # matcher only scores detections that carry an onset. Measured on a
        # real recording: 13 onsets fired across 14 played notes, but only 2
        # survived to the matcher, so almost nothing could ever score.
        #
        # So an onset is remembered and attached to the next confident pitch
        # instead. The window needs buf_size/hop_size hops to fill with the
        # new note, which is exactly how long the strike stays pending.
        self._attack_sequence = 0
        self._attack = _PhaseCheckedAttack(AttackOrigin(0, 0, None, 0.0))
        self._last_closed_attack: PendingAttack | None = None
        self._report_candidate: tuple[DetectedNote, PendingAttack] | None = None
        # A low string's partials can concentrate energy in one half-cycle.
        # A single short quiet hop therefore does not establish a note end.
        # Require one period of the lowest searched frequency below the gate;
        # exact digital silence can still close a pending strike immediately.
        self._gate_release_hops = max(1, int(np.ceil(
            self.sample_rate / (self.instrument.freq_min * self.hop_size))))
        self._quiet_hops = 0
        # Pitches seen since the strike, so the reported note is a vote over
        # several frames instead of whatever a single frame happened to say.
        # One frame is where the fourth-confusions live: a low E read as an A.
        self.onset_hold_hops: int = max(1, self.buf_size // max(self.hop_size, 1)) * 2
        # Measured on a real recording: one frame per strike gets 79 percent of
        # notes right and fires 18 strikes for 12 played notes; eight frames
        # gets 92 percent and fires exactly 12. The fourth-confusions a player
        # notices ("my low E is read as an A") live in that single frame.
        self.onset_votes_wanted: int = 8
        # Below this a strike is not reported at all. One frame is a single
        # spectrum and was what produced the fourth-confusions the vote exists
        # to prevent; two agreeing frames is the least that means anything.
        self.onset_votes_minimum: int = 2
        # How long after a strike the vote has to wait before it counts.
        #
        # The analysis window is 93 ms wide. For that long after a new note
        # is struck, the window still holds mostly the note before it, and on
        # this instrument the note before is still ringing -- nothing damps an
        # open string. So the vote was decided from frames that predate the
        # attack, and on the alternating-strings exercise it reported the A
        # the player had just let ring instead of the E the player had just played. The
        # correct reading arrived about 130 ms later and was counted as a
        # second, spurious strike.
        #
        # Waiting costs nothing in accuracy: the report is back-dated by
        # onset_age_hops, so the note still lands at the moment it was
        # struck.
        #
        # Measured as a fraction of the window, against two cases that pull
        # opposite ways. Alternating strings, built from their own plucks: 5 of
        # 8 with no wait, 8 of 8 from 0.35 on. The twelve-note fixture, where
        # notes are separated by digital silence and the window fills with
        # nothing: 12 of 12 up to 0.55, then 11 because a short quiet note
        # runs out of usable frames before the vote opens. 0.45 is the middle
        # of that plateau, one step from either edge.
        self.vote_delay_hops: int = max(
            1, int(round(self.buf_size / max(self.hop_size, 1) * 0.45)))
        # A pluck makes the onset detector fire twice, roughly 120 ms apart:
        # once on the attack and once as the string settles. Reporting the
        # same note twice for one pluck is a phantom strike. The first report
        # is kept because it sits closest to the actual attack.
        self.min_note_gap_ms: float = float(
            getattr(inst, "min_note_gap_ms", 0.0) or 0.0
        )
        self._hops_seen: int = 0
        self._probe_ring = np.zeros(self._PROBE_NFFT, dtype=np.float32)
        self._probe_window = np.hanning(self._PROBE_NFFT).astype(np.float32)
        _probe_bins = np.fft.rfftfreq(self._PROBE_NFFT, 1.0 / sample_rate)
        self._probe_band = (_probe_bins > 30.0) & (_probe_bins < 2500.0)
        self._attack_bands = [
            (_probe_bins >= low) & (_probe_bins < low * 2)
            for low in (40, 80, 160, 320, 640, 1280, 2560)
            if low * 2 <= sample_rate / 2
        ]
        self._last_report_hop: int = -10 ** 9
        self._last_report_note: int = -1
        self._quiet_start_anchor: int | None = None
        self._bass_recovery: tuple[BassContinuation, int] | None = None
        self._reported_uke_pitches: set[int] | None = set() if inst.key == "bass_ukulele" else None

        # ── level-rise strike gate ──────────────────────────────────────
        #
        # aubio's onset detector measures spectral change, and a decaying
        # rubber string keeps changing spectrally for seconds. So it fires
        # again and again inside one note: on the reference recording it fired
        # 108 times for 35 plucks, and 27 of the 62 strikes that survived to
        # the matcher belonged to no pluck at all. min_note_gap_ms cannot
        # touch those, because a phantom usually carries a DIFFERENT midi note
        # than the strike before it (a rising chromatic run produced 32, 31,
        # 33 in the middle of a climb) and that check only suppresses repeats.
        #
        # What separates the two cases is not spectrum but level. A pluck puts
        # energy into the string; a phantom is that energy running out. So a
        # strike is only reported if the signal actually got louder: at least
        # `strike_rise_db` above the quietest hop in the `strike_rise_window_ms`
        # before the onset fired.
        #
        # The rise is not required at the onset hop itself. The detector
        # sometimes fires on the leading edge of a transient, before the level
        # has caught up (measured: one pluck read +6 dB at the fire hop and
        # +25 dB two hops later). So the strike is armed unconfirmed and any
        # hop of its pending window may confirm it. This costs nothing: the
        # strike was already waiting for votes.
        self.strike_rise_db: float = float(
            getattr(inst, "strike_rise_db", 0.0) or 0.0
        )
        self.strike_rise_window_ms: float = float(
            getattr(inst, "strike_rise_window_ms", 160.0) or 160.0
        )
        self._rise_lookback_hops: int = max(1, int(round(
            self.strike_rise_window_ms * self.sample_rate
            / (1000.0 * max(self.hop_size, 1))
        )))
        self._phase_windows = (self._gate_release_hops,
                               max(self._gate_release_hops, self._rise_lookback_hops))
        self._phase_bounds = tuple(
            1.0 + 1.0 / max(1, int(np.floor(hops * self.hop_size
                                           * self.instrument.freq_min / self.sample_rate)))
            for hops in self._phase_windows
        )
        self._phase_power_ring = deque(maxlen=self._phase_windows[-1])
        self._phase_power_history = deque(maxlen=self._rise_lookback_hops)
        self._phase_powers = (0.0, 0.0)
        self._db_ring: deque[float] = deque(maxlen=self._rise_lookback_hops)
        self._band_db_ring: deque[np.ndarray] = deque(maxlen=self._rise_lookback_hops)
        self._attack.band_reference = np.full(len(self._attack_bands), -120.0)
        # True when the mechanism is off, so every strike passes unchanged.
        self._transient_hops = max(1, round(0.035 * self.sample_rate / self.hop_size))
        self._transient_hop = -10 ** 9
        self._transient_evidence: AttackEvidence | None = None
        # Both bass profiles use bounded waveform evidence. The rubber-string
        # profile also keeps its independent partial-energy confirmation.
        self._waveform_history = (WaveformHistory(self.sample_rate, self.hop_size)
                                  if self.instrument.key in ("bass_ukulele", "bass_guitar") else None)
        self._partial_ring = None
        if self.instrument.key == "bass_ukulele":
            self._partial_ring = np.zeros(self.buf_size, np.float32)
            self._partial_window = np.hanning(self.buf_size).astype(np.float32)
            self._partial_nfft = 1 << int(np.ceil(np.log2(self.buf_size * 4)))
            partial_bins = np.fft.rfftfreq(self._partial_nfft, 1. / self.sample_rate)
            # The three note-energy checks only inspect MIDI pitches inside
            # this instrument's range. Keep their highest partial's complete
            # neighborhood; the remaining FFT bins are never read. Copy the
            # prefix so the retained view cannot keep the full array alive.
            upper = (midi_to_freq(self.midi_max) * self.instrument.harmonics
                     + self.sample_rate / self.buf_size / 2)
            count = int(np.searchsorted(partial_bins, upper, side="right"))
            self._partial_bins = partial_bins[:count].copy()
            self._partial_history = deque(maxlen=self._rise_lookback_hops + self.onset_hold_hops
                                          + int(np.ceil(self.buf_size / self.hop_size)) + 4)

        # ── decide-once-and-hold ────────────────────────────────────────
        #
        # Once a strike has named a note, refuse to report ANY further strike
        # for this long. min_note_gap_ms above is the same idea restricted to
        # a repeat of the same note; this is the unrestricted version, and it
        # is the only thing that catches a phantom that re-decides the note
        # wrongly. It is also blunt: it puts a hard floor under how fast notes
        # may be played, so it is off unless a profile asks for it.

        # A fixed noise gate is a guess about someone else's room. Measured in
        # the room this was written for, the floor sits around -46 dB while the
        # shipped gate was -45: under a decibel of headroom. The gate then
        # flickers open and shut on room noise, noise transients look like note
        # strikes, and detection feels random although the pitch detector is
        # fine.
        #
        # Adapting automatically does not work: a quiet room and the decaying
        # tail of a note look identical frame by frame, so a tracker either
        # climbs into the music and gates it out, or never rises at all. The
        # room is therefore measured once, deliberately, while nothing is
        # played (see ui/check_menu.py) and the result is stored in the config.
        self.effective_gate_db: float = self.noise_gate_db

        self._repeat_corroborator: RepeatCorroborator | None = None
        self._quiet_repeat_corroborator: QuietRepeatCorroborator | None = None
        self._growing_pitch: GrowingPitchCorroborator | None = None
        self._build()

    # ── backend construction ────────────────────────────────────────────

    def _build(self) -> None:
        self._pitch = self._make_pitch()
        self._pitch_check = None
        if self.pitch_method == "harmonic" and aubio is not None:
            # FFT-YIN provides independent periodicity evidence without YIN's
            # quadratic cost. aubio requires a power-of-two FFT, whereas the
            # main harmonic window is scaled in time for negotiated rates.
            check_size = max(1 << (self.buf_size.bit_length() - 1),
                             1 << (self.hop_size - 1).bit_length())
            try:
                self._pitch_check = aubio.pitch(
                    "yinfast", check_size, self.hop_size, self.sample_rate)
                self._pitch_check.set_unit("Hz")
                self._pitch_check.set_tolerance(self.yin_tolerance)
            except RuntimeError:
                # An optional corroborator must not disable a usable primary
                # backend on aubio builds that lack this FFT implementation.
                self._pitch_check = None
        self._onset = self._make_onset()
        self._repeat_corroborator = None
        self._quiet_repeat_corroborator = None
        self._growing_pitch = None
        if self.instrument.key == "bass_ukulele" and aubio is not None:
            try:
                energy = aubio.onset("energy", self.onset_buf_size, self.hop_size, self.sample_rate)
                energy.set_threshold(self.onset_threshold)
                self._repeat_corroborator = RepeatCorroborator(energy, self.sample_rate)
            except RuntimeError:
                # Optional evidence must not disable the primary detector.
                pass
            if self.pitch_method == "harmonic" and self._pitch_check is not None:
                try:
                    complex_onset = aubio.onset("complex", self.onset_buf_size,
                                                self.hop_size, self.sample_rate)
                    complex_onset.set_threshold(self.onset_threshold)
                    self._quiet_repeat_corroborator = QuietRepeatCorroborator(
                        complex_onset, self.sample_rate, self.hop_size)
                except RuntimeError:
                    pass
        if self._quiet_repeat_corroborator is not None:
            self._growing_pitch = GrowingPitchCorroborator(
                self.sample_rate, self.hop_size, self.buf_size,
                self.instrument.freq_min, self.instrument.freq_max)

    def _make_pitch(self):
        if self.pitch_method == "harmonic":
            return HarmonicPitchDetector(
                buf_size=self.buf_size,
                hop_size=self.hop_size,
                sample_rate=self.sample_rate,
                freq_min=self.instrument.freq_min,
                freq_max=self.instrument.freq_max,
                harmonics=self.instrument.harmonics,
            )
        if aubio is None:
            raise RuntimeError(
                "The 'yin' pitch backend needs aubio, which is not installed. "
                "Install it, or use an instrument profile with "
                "pitch_method='harmonic' (numpy only)."
            )
        method = "yin"
        window = self.buf_size
        if self.sample_rate > 48000:
            # Direct YIN is quadratic in its window size. At 192 kHz it
            # exceeds the hop budget even in silence; its FFT equivalent
            # keeps the stream continuous without that quadratic cost.
            method = "yinfast"
            window = max(1 << (window.bit_length() - 1),
                         1 << (self.hop_size - 1).bit_length())
        p = aubio.pitch(method, window, self.hop_size, self.sample_rate)
        p.set_unit("Hz")
        # aubio's tolerance is the YIN dip threshold, NOT a confidence: a
        # candidate is accepted when its normalised difference is BELOW it, and
        # aubio's own default is 0.15. This app used to pass the confidence
        # threshold (0.8) here, which makes the search 5x looser than aubio's
        # default and biases it toward the octave above. They are separate
        # concepts, so they are separate settings.
        p.set_tolerance(self.yin_tolerance)
        return p

    def _make_onset(self):
        if aubio is None:
            return None
        o = aubio.onset(
            self.onset_method, self.onset_buf_size, self.hop_size, self.sample_rate
        )
        o.set_threshold(self.onset_threshold)
        return o

    # ── detection ───────────────────────────────────────────────────────

    def process(self, audio_buffer: np.ndarray) -> DetectedNote | None:
        note = self._process(audio_buffer)
        if self._growing_pitch is not None:
            self._growing_pitch.observe(self._attack, self._hops_seen,
                self._partial_history, self._partial_bins, self._partial_nfft,
                self.noise_gate_db, self.last_signal_db)
        return note

    def _process(self, audio_buffer: np.ndarray) -> DetectedNote | None:
        """Process a single audio buffer (hop_size float32 samples).

        Returns a DetectedNote if a confident, in-range pitch was detected,
        None otherwise.
        """
        if audio_buffer.dtype != np.float32:
            audio_buffer = audio_buffer.astype(np.float32)
        if not np.isfinite(audio_buffer).all():
            # Invalid capture is a discontinuity, not a measured pause. Do not
            # poison the DSP windows or confirm an attack from NaN/Inf values.
            # The input layer timestamps later reports against their own chunks.
            self.reset()
            return None
        self._hops_seen += 1
        if self._waveform_history is not None:
            if audio_buffer.ndim == 1 and len(audio_buffer) == self.hop_size:
                self._waveform_history.append(audio_buffer, self._hops_seen * self.hop_size)
                if self._repeat_corroborator is not None:
                    self._repeat_corroborator.process(audio_buffer, self._hops_seen * self.hop_size)
                if self._quiet_repeat_corroborator is not None:
                    self._quiet_repeat_corroborator.process(audio_buffer, self._hops_seen * self.hop_size)
            else:
                self._waveform_history.reset()
                self._repeat_corroborator = None
                self._quiet_repeat_corroborator = None
        n = audio_buffer.size
        if self._partial_ring is not None:
            if n >= self.buf_size:
                self._partial_ring[:] = audio_buffer[-self.buf_size:]
            else:
                self._partial_ring[:-n] = self._partial_ring[n:]
                self._partial_ring[-n:] = audio_buffer
            spectrum = np.fft.rfft(
                (self._partial_ring - self._partial_ring.mean()) * self._partial_window,
                n=self._partial_nfft)
            # Integrated power must not grow merely because of zero padding.
            # Slice before forming power: stored arrays own only the used
            # bins, with exactly the same values and normalization as before.
            power = np.abs(spectrum[:len(self._partial_bins)]) ** 2 / (self._partial_nfft * self.buf_size)
            self._partial_history.append((self._hops_seen * self.hop_size, power))
        if n >= self._PROBE_NFFT:
            self._probe_ring[:] = audio_buffer[-self._PROBE_NFFT:]
        else:
            self._probe_ring[:-n] = self._probe_ring[n:]
            self._probe_ring[-n:] = audio_buffer

        rms = np.sqrt(np.mean(audio_buffer ** 2))
        db = 20 * np.log10(rms) if rms > 0 else -120.0
        self.last_signal_db = db
        loud_enough = db >= self.noise_gate_db
        self._quiet_hops = 0 if loud_enough else self._quiet_hops + 1
        sound_ended = (rms == 0 or self._quiet_hops >= self._gate_release_hops)

        # Both pitch backends retain a rolling analysis window. Skipping quiet
        # hops, or a hop that flushes an earlier strike, leaves old samples in
        # that window and can assign the previous pitch to the next pluck.
        if self.pitch_method == "harmonic":
            freq, confidence = self._pitch.process(audio_buffer)
            primary_confidence = confidence
            if self._pitch_check is not None:
                check_freq = float(self._pitch_check(audio_buffer)[0])
                check_confidence = float(self._pitch_check.get_confidence())
                # Never replace a harmonic estimate with an unrelated YIN
                # pitch. Only agreement within 35 cents can strengthen it.
                if (freq > 0 and check_freq > 0 and check_confidence >= 0.9
                        and abs(1200 * np.log2(check_freq / freq)) <= 35):
                    confidence = max(confidence, check_confidence)
        else:
            freq = float(self._pitch(audio_buffer)[0])
            confidence = float(self._pitch.get_confidence())
            if loud_enough and freq > 0:
                freq = self._correct_octave_jump(freq, confidence)


        # The quiet reference for the level-rise gate: the quietest hop in the
        # window BEFORE this one, so the current hop cannot lower its own bar.
        phase_reference = (0.0, 0.0)
        phase_ready = False
        if self.instrument.key in ("bass_guitar", "bass_ukulele"):
            self._phase_power_ring.append(float(rms) ** 2)
            values = list(self._phase_power_ring)
            self._phase_powers = tuple(sum(values[-hops:]) / len(values[-hops:])
                                       for hops in self._phase_windows)
            if self._phase_power_history:
                phase_reference = tuple(min(row[index] for row in self._phase_power_history)
                                        for index in (0, 1))
            # Only veto once every historical reference covers a full window.
            phase_ready = self._hops_seen >= self._phase_windows[-1] + self._rise_lookback_hops
            self._phase_power_history.append(self._phase_powers)
        rise_ref_db = -120.0
        rise_reference_span = (max(1, self._hops_seen - len(self._db_ring)), self._hops_seen - 1)
        if self.strike_rise_db > 0.0:
            if self._db_ring:
                rise_ref_db = min(self._db_ring)
            self._db_ring.append(db)
            # A ringing low string can mask the broadband level rise of a
            # new, quieter pluck. Its upper partials decay faster: require a
            # rise in two audible octave bands as independent attack evidence.
            mag = np.abs(np.fft.rfft(self._probe_ring * self._probe_window))
            band_db = np.array([
                10 * np.log10(max(float(np.sum(mag[band] ** 2))
                                 / self._PROBE_NFFT ** 2, 1e-12))
                for band in self._attack_bands
            ])
            band_ref = (np.min(self._band_db_ring, axis=0)
                        if self._band_db_ring else np.full_like(band_db, -120.0))
            # A quieter repeated steel-string attack may not clear the long
            # rise gate. An abrupt rise in two audible bands is additional
            # evidence, but it only qualifies with independent pitch votes.
            if (self.instrument.transient_rise_db > 0
                    and len(self._band_db_ring) >= self._transient_hops):
                sharp = ((band_db - self._band_db_ring[-self._transient_hops]
                          >= self.instrument.transient_rise_db)
                         & (band_db >= max(self.noise_gate_db - 12.0, db - 35.0)))
                if np.count_nonzero(sharp) >= 2:
                    self._transient_hop = self._hops_seen
                    indices = tuple(int(index) for index in np.flatnonzero(sharp))
                    reference = self._band_db_ring[-self._transient_hops]
                    reference_hop = self._hops_seen - self._transient_hops
                    self._transient_evidence = AttackEvidence(
                        "transient", self._hops_seen, reference_hop, reference_hop,
                        tuple(float(reference[index]) for index in indices),
                        tuple(float(band_db[index]) for index in indices), indices,
                    )
            self._band_db_ring.append(band_db)

        # Feed the onset detector EVERY hop, including quiet ones. It is a
        # streaming detector with its own history and its own peak picker; if
        # it only sees the loud hops its sense of time is wrong and it misses
        # strikes. Measured on a real recording: aubio found all 12 strikes
        # when fed continuously, but only 6 survived when the noise gate
        # returned early and starved it.
        struck = bool(self._onset(audio_buffer)) if self._onset is not None else False
        if (struck and self.instrument.key == "bass_guitar" and self.pitch_method == "harmonic"
                and self._pitch_check is not None):
            get_last = getattr(self._onset, "get_last", None)
            if get_last is not None and self._retain_sparse_bass_attack(
                    float(get_last()), check_freq, check_confidence):
                struck = False
        if (struck and self.instrument.key == "bass_ukulele" and self.pitch_method == "harmonic"
                and loud_enough and self._attack.pending):
            get_last = getattr(self._onset, "get_last", None)
            if get_last is not None and self._retain_pending_attack(float(get_last()), freq, primary_confidence):
                struck = False
        flushed: DetectedNote | None = None
        # A steel-string onset can arrive in a quiet leading block before
        # the pluck crosses the gate. Remember its time instead of losing
        # the attack. Votes still require audible, confident pitch and the
        # existing rise confirmation; a quiet candidate alone scores nothing.
        # Keep the independently measured guitar/rubber-string paths intact.
        if struck and (loud_enough or self.instrument.transient_rise_db > 0
                       or (self.instrument.key == "bass_ukulele" and not self._attack.pending)):
            # Report what the previous strike had gathered before starting a
            # new one. Two notes played quickly used to come out as one.
            if self._attack.pending:
                self._attack.age_hops += 1
            self._attack.requested_close_reason = "replaced"
            flushed = self._flush_votes()
            # aubio's peak picker fires after the actual attack. Its sample
            # timestamp also includes the fractional-hop timing estimate;
            # using the callback that fired instead introduces a guitar
            # timing bias of several hops. Keep legacy profile compensation
            # separate because AudioCapture already subtracts it.
            backdate_hops = 0.0
            native_sample = None
            get_last = getattr(self._onset, "get_last", None)
            if get_last is not None:
                onset_sample = float(get_last())
                sample_now = self._hops_seen * self.hop_size
                if np.isfinite(onset_sample) and 0 <= onset_sample <= sample_now:
                    native_sample = onset_sample
                    backdate_hops = (
                        (sample_now - onset_sample) / self.hop_size
                        - self.onset_latency_ms * self.sample_rate / (1000 * self.hop_size)
                    )
            # A sparse fundamental may use YIN and the spectral peak only
            # after a measured quiet interval. An overlapping ringing string
            # cannot borrow this independent-pitch path.
            quiet_before = False
            quiet_span = (0, 0)
            quiet_peak = -120.
            if self.instrument.key == "bass_guitar" and native_sample is not None:
                quiet_rows = [(self._hops_seen-len(self._db_ring)+i+1, value) for i,value in enumerate(self._db_ring)
                              if (self._hops_seen-len(self._db_ring)+i+1)*self.hop_size <= native_sample]
                needed = int(np.ceil(.05*self.sample_rate/self.hop_size))
                quiet_before = len(quiet_rows)>=needed and all(value < self.noise_gate_db for _,value in quiet_rows[-needed:])
                if quiet_before:
                    quiet_span = (quiet_rows[-needed][0], quiet_rows[-1][0])
                    quiet_peak = float(max(value for _, value in quiet_rows[-needed:]))
            self._attack_sequence += 1
            self._attack = _PhaseCheckedAttack(
                AttackOrigin(self._attack_sequence, self._hops_seen, native_sample, backdate_hops),
                pending=True, band_reference=np.full(len(self._attack_bands), -120.0),
                leading_quiet=(self.instrument.key == "bass_ukulele" and not loud_enough),
                preceded_by_quiet=quiet_before, quiet_reference_span=quiet_span,
                quiet_reference_peak_db=quiet_peak,
            )
            if self.strike_rise_db > 0.0:
                self._attack.reference_db = rise_ref_db
                self._attack.band_reference = band_ref.copy()
                self._attack.reference_span = rise_reference_span
                self._attack.phase_ready = phase_ready
                self._attack.phase_reference = phase_reference
                self._attack.confirmed = (db >= rise_ref_db + self.strike_rise_db
                                          and self._raw_phase_rise_supported())
                if self._attack.confirmed:
                    self._record_level_evidence(db)
        elif self._attack.pending:
            if self.strike_rise_db > 0.0 and not self._attack.confirmed:
                # The onset can fire on the leading edge of a transient, a hop
                # or two before the level has actually risen. Any hop of the
                # pending window may confirm the strike.
                self._attack.confirmed = (
                    db >= self._attack.reference_db + self.strike_rise_db
                    and self._raw_phase_rise_supported()
                )
                if self._attack.confirmed:
                    self._record_level_evidence(db)
            self._attack.age_hops += 1
            if sound_ended or self._attack.age_hops > self.onset_hold_hops:
                # A strike cannot outlive its own sound. Once the signal is
                # under the gate for a full low period the note has ended;
                # periodic energy troughs alone must not cancel its attack.
                # Holding the
                # strike open across the silence let it swallow the next one:
                # the following pluck merely flushed it, so that pluck was
                # reported at the time of the one before it.
                #
                # Whatever votes it did gather are reported rather than
                # discarded. Throwing them away meant a note the detector had
                # already named correctly was never reported at all, and it
                # only takes one frame under the confidence threshold to end
                # up here.
                self._attack.requested_close_reason = "sound_ended" if sound_ended else "timeout"
                flushed = self._flush_votes()
                self._attack.pending = False
                self._attack.age_hops = 0
        if self.strike_rise_db > 0.0 and self._attack.pending and not self._attack.confirmed:
            rising = ((band_db >= self._attack.band_reference + self.strike_rise_db)
                      & (band_db >= max(self.noise_gate_db - 12.0, db - 35.0)))
            self._attack.confirmed = np.count_nonzero(rising) >= 2
            if self._attack.confirmed:
                indices = tuple(int(index) for index in np.flatnonzero(rising))
                self._attack.evidence.append(AttackEvidence(
                    "bands", self._hops_seen, *self._attack.reference_span,
                    tuple(float(self._attack.band_reference[index]) for index in indices),
                    tuple(float(band_db[index]) for index in indices), indices,
                ))
            if self._hops_seen - self._transient_hop <= self._transient_hops:
                if not self._attack.transient_candidate and self._transient_evidence is not None:
                    self._attack.evidence.append(self._transient_evidence)
                self._attack.transient_candidate = True
        if self.pitch_method == "harmonic" and self._pitch_check is not None:
            self._collect_bass_pitch(check_freq, check_confidence)
            if self._quiet_repeat_corroborator is not None:
                self._quiet_repeat_corroborator.observe(
                    self._attack, self._hops_seen, freq, check_freq, check_confidence,
                    getattr(self._pitch_check, "buf_size", 0), self.vote_delay_hops, loud_enough,
                    self._fundamental_present)
        # A retained sparse tone can finish as soon as the existing independent
        # pitch requirements are met. Waiting for unrelated primary votes can
        # let a real following pluck invalidate the first tone's evidence.
        if (flushed is None and self._attack.pending
                and self._attack.bass_continuation_sample is not None):
            sparse = self._sparse_bass_pitch()
            if sparse is not None and self._bass_continuation_supported(self._attack, midi_to_freq(sparse[0])):
                self._attack.requested_close_reason = "votes_complete"
                flushed = self._flush_votes()
        if flushed is not None:
            return self._report(flushed)

        if not loud_enough:
            return None

        self.last_freq = freq
        self.last_confidence = confidence

        if confidence < self.confidence_threshold or freq <= 0:
            return None

        midi_note = freq_to_midi(freq)
        if not is_in_instrument_range(midi_note, self.midi_min, self.midi_max):
            return None
        if not self._fundamental_present(freq):
            return None

        # A pending strike collects votes before it is reported. Deciding the
        # note from the single frame where the pitch first became confident is
        # what produced the fourth-confusions; a majority over a handful of
        # frames is markedly steadier, and costs only the few milliseconds the
        # timestamp is back-dated by anyway.
        is_onset = False
        age = 0
        if self._attack.pending:
            if self._attack.age_hops < self.vote_delay_hops:
                # Still looking at the note before this one.
                return None
            self._attack.votes.append(midi_note)
            if (self.pitch_method == "harmonic" and self._pitch_check is not None
                    and check_confidence >= 0.9 and check_freq > 0
                    and abs(1200 * np.log2(check_freq / freq)) <= 35):
                self._attack.periodicity_votes.append(midi_note)
            if (len(self._attack.votes) >= self.onset_votes_wanted
                    or self._attack.age_hops >= self.onset_hold_hops):
                confirmed = self._attack.confirmed
                enough_votes = len(self._attack.votes) >= self.onset_votes_minimum
                age = self._attack.age_hops + self._attack.origin.backdate_hops
                voted = self._choose_pitch(self._attack.votes)
                confirmed = ((confirmed or self._corroborated_transient(voted))
                             and self._continuation_matches(voted))
                decision = ("insufficient_votes" if not enough_votes else
                            "accepted" if confirmed else "unconfirmed")
                self._close_attack("votes_complete", decision)
                self._attack.age_hops = 0
                if confirmed and enough_votes:
                    is_onset = True
                    midi_note = voted
                    freq = midi_to_freq(midi_note)
                else:
                    # Nothing got louder, so this was not a new note. The
                    # pitch is still real and still worth showing; it just
                    # does not go to the matcher as a strike.
                    age = 0
            else:
                return None

        note = DetectedNote(
            midi_note=midi_note,
            frequency=freq,
            confidence=confidence,
            name=midi_to_name(midi_note),
            is_onset=is_onset,
            onset_age_hops=age,
        )
        return self._report(self._prepare_report(note)) if is_onset else note

    # How far below the loudest partial a real fundamental may sit. Measured:
    # on their bass ukulele, whose body is too small to radiate 41 Hz, real
    # fundamentals run 6 to 30 dB down; on a guitar, 0 to 11. The note the
    # app invented for a B3 and E4 played together sat 105 dB down. The
    # threshold is set in the empty space between.
    FUNDAMENTAL_FLOOR_DB = -45.0
    _PROBE_NFFT = 2048

    def _fundamental_present(self, freq: float) -> bool:
        """Reject a pitch whose own fundamental is not actually sounding.

        Two notes a fourth apart share a plausible fundamental well below
        either of them: B3 at 247 Hz and E4 at 330 are the third and fourth
        harmonics of 82 Hz, so a pitch tracker legitimately reports an E2
        that nobody played. Measured, that E2 sat 105 dB below the notes that
        were actually struck -- the frequency it named had essentially no
        energy at all.

        This is a rejection heuristic for the measured instruments, not a
        physical guarantee: a real signal can have a missing fundamental,
        and nearby notes can leak energy into this finite analysis window.
        """
        if freq <= 0:
            return False
        ring = self._probe_ring
        if ring is None:
            return True
        block = ring - ring.mean()
        mag = np.abs(np.fft.rfft(block * self._probe_window))
        band = self._probe_band
        peak = float(mag[band].max()) if band.any() else 0.0
        if peak <= 0.0:
            return True
        bin_hz = self.sample_rate / self._PROBE_NFFT
        index = int(round(freq / bin_hz))
        lo = max(0, index - 2)
        here = float(mag[lo:index + 3].max()) if index < len(mag) else 0.0
        if here <= 0.0:
            return False
        return 20.0 * np.log10(here / peak) >= self.FUNDAMENTAL_FLOOR_DB

    def _report(self, note: "DetectedNote") -> "DetectedNote | None":
        helper = self._growing_pitch
        attack = (self._report_candidate[1] if self._report_candidate is not None
                  and self._report_candidate[0] is note else None)
        if attack is not None and attack.growing_pitch_evidence is not None:
            self._report_candidate = None
            if self._decaying_uke_return(note, attack):
                attack.report_outcome = "sustain_filtered"
                return None
            helper.reported = (self._hops_seen-note.onset_age_hops, note.midi_note)
            self._quiet_repeat_corroborator.consume(attack.origin.onset_sample, note.frequency)
            attack.report_outcome = "reported_growing_pitch"
            return note
        near = False
        repeat = None
        if helper is not None and helper.reported is not None and attack is not None:
            hop, midi = helper.reported
            gap = self._hops_seen-note.onset_age_hops-hop
            near = (midi == note.midi_note
                    and 0 <= gap < self.min_note_gap_ms*self.sample_rate/(1000*self.hop_size))
            if near and self._repeat_corroborator is not None and self._waveform_history is not None:
                # Inspect independent repetition evidence without consuming
                # it ahead of the ordinary report path's own decision.
                pulses = [(pulse, pulse.consumed) for pulse in self._repeat_corroborator.pulses]
                try:
                    repeat = self._repeat_corroborator.corroborate(
                        attack.origin.onset_sample, note.frequency,
                        self._waveform_history, self.noise_gate_db)
                finally:
                    for pulse, consumed in pulses:
                        pulse.consumed = consumed
        result = self._report_primary(note)
        if (result is not None and attack is not None
                and attack.late_quiet_start_sample is not None
                and self._quiet_repeat_corroborator is not None):
            # The delayed confirmation supplies this report's own timestamp.
            # Consume it only once the ordinary report filters have accepted.
            for pulse in self._quiet_repeat_corroborator.pulses:
                if pulse.sample == attack.late_quiet_start_sample:
                    pulse.consumed = True
        if result is not None and near:
            # The ordinary state must still advance: the next string change
            # relies on its reference pitch even when this duplicate output
            # has already been supplied by the earlier recovered onset.
            if repeat is None:
                attack.report_outcome = "growing_pitch_duplicate"
                return None
            attack.repeat_evidence = repeat
            attack.report_outcome = "growing_pitch_repeat"
        return result

    def _report_primary(self, note: "DetectedNote") -> "DetectedNote | None":
        """Drop a strike that repeats the one just reported.

        A single pluck fires the onset detector twice, so without this the
        same note is announced twice for one note played.

        A blunter version of this was measured and rejected: holding the floor
        against strikes of ANY note for a while removes phantoms, but it does
        it by putting a hard ceiling on how fast notes may be played, and the
        level-rise gate removes the same phantoms without that cost. At the
        best setting it still left 22 of 27 phantoms while starting to merge
        real notes.
        """
        attack = None
        if self._report_candidate is not None and self._report_candidate[0] is note:
            attack = self._report_candidate[1]
        self._report_candidate = None
        if attack is not None and attack.bass_continuation_sample is not None:
            if (attack.sparse_pitch_evidence is None
                    or not self._bass_continuation_supported(attack, note.frequency)):
                attack.report_outcome = "continuation_unconfirmed"
                return None
        if (attack is not None and self._bass_recovery is not None
                and attack.bass_continuation_sample is None and attack.origin.onset_sample is not None):
            recovered, recovered_midi = self._bass_recovery
            gap = attack.origin.onset_sample - recovered.origin_sample
            if note.midi_note == recovered_midi and 0 <= gap < self.min_note_gap_ms * self.sample_rate / 1000:
                proof = self._predict_bass_continuation(
                    recovered.origin_sample, recovered.native_sample, note.frequency)
                if proof is not None:
                    attack.bass_duplicate_evidence = proof
                    attack.report_outcome = "continuation_duplicate"
                    return None
        # An onset arriving in a waveform trough is only provisional. It
        # needs its own complex-domain onset and a phase-integrated rise.
        if attack is not None and attack.leading_quiet:
            proof, later = self._confirm_quiet_start(attack, note.frequency)
            if proof is None:
                attack.report_outcome = "quiet_start_unconfirmed"
                return None
            attack.quiet_start_evidence = proof
            if later is not None:
                attack.late_quiet_start_sample = later
                note = replace(note, onset_age_hops=note.onset_age_hops
                               - (later - attack.origin.onset_sample) / self.hop_size)
        if (attack is not None and attack.waveform_prediction is not None
                and attack.waveform_prediction.is_continuation
                and attack.waveform_prediction.supports_pitch(note.frequency)):
            # An old waveform can predict this interval without a new pluck.
            # Keep the rejected candidate out of the repeat gate: the actual
            # successor still owns its native onset and may be reported.
            # The fitted waveform must contain this pitch: predicting a
            # different tone cannot certify the reported tone's continuity.
            attack.report_outcome = "prediction_filtered"
            return None
        if attack is not None and attack.quiet_repeat_evidence is not None:
            # This candidate already owns its distinct complex onset and pitch
            # evidence. Like an energy-backed repeat, it must not extend the
            # ordinary time-only suppression and steal the following note.
            attack.report_outcome = "reported_quiet_repeat"
            return note
        hops_per_ms = self.sample_rate / (1000.0 * self.hop_size)
        strike_hop = self._hops_seen - note.onset_age_hops
        since = strike_hop - self._last_report_hop
        original_midi = (note.original_midi_note if note.original_midi_note is not None
                         else note.midi_note)
        if (original_midi == self._last_report_note
                and since < self.min_note_gap_ms * hops_per_ms):
            if (attack is not None and attack.decision == "accepted"
                    and note.confidence >= self.confidence_threshold
                    and self._repeat_corroborator is not None
                    and self._waveform_history is not None):
                evidence = self._repeat_corroborator.corroborate(
                    attack.origin.onset_sample, note.frequency,
                    self._waveform_history, self.noise_gate_db)
                if evidence is not None:
                    attack.repeat_evidence = evidence
                    attack.report_outcome = "reported_repeat"
                    if self._quiet_repeat_corroborator is not None:
                        self._quiet_repeat_corroborator.consume(attack.origin.onset_sample, note.frequency)
                    # Do not extend the ordinary time-only suppression. A
                    # separately evidenced repetition must not steal the next
                    # report that the unchanged primary path would admit.
                    return note
            # One later ordinary duplicate would have anchored the original
            # repeat gate. Preserve that suppression once, even though the
            # earlier recovered onset already supplied the visible note.
            # An independently corroborated repeat above still takes priority.
            if (self._quiet_start_anchor == original_midi
                    and attack is not None and not attack.leading_quiet):
                self._last_report_hop = strike_hop
                self._quiet_start_anchor = None
                attack.report_outcome = "quiet_start_duplicate"
                return None
            if attack is not None:
                attack.report_outcome = "repeat_filtered"
            return None
        if self._decaying_uke_return(note, attack):
            attack.report_outcome = "sustain_filtered"
            return None
        if attack is not None and attack.bass_continuation_evidence is not None:
            self._bass_recovery = (attack.bass_continuation_evidence, note.midi_note)
            attack.report_outcome = "reported_continuation"
            # This recovered first tone must not consume the ordinary repeat
            # allowance of the genuine next pluck. Only a fresh continuation
            # proof can suppress its possible duplicate above.
            return note
        self._bass_recovery = None
        self._quiet_start_anchor = original_midi if attack is not None and attack.leading_quiet else None
        self._last_report_hop = strike_hop
        self._last_report_note = original_midi
        if self._reported_uke_pitches is not None and note.is_onset and 0 <= note.midi_note <= 127:
            self._reported_uke_pitches.add(note.midi_note)
        if attack is not None:
            attack.report_outcome = "reported"
            if self._repeat_corroborator is not None:
                self._repeat_corroborator.consume(attack.origin.onset_sample, note.frequency)
            if self._quiet_repeat_corroborator is not None:
                self._quiet_repeat_corroborator.consume(attack.origin.onset_sample, note.frequency)
        return note

    def _decaying_uke_return(self, note: DetectedNote, attack: PendingAttack | None) -> bool:
        """Veto a broad-band onset when the returning tone only decays.

        This never creates a note or overrides native timing. Only a return
        to a previously reported uke pitch is eligible, with complete native
        pre/post windows and two audible partials separated from the preceding
        pitch. Missing evidence leaves the original decision intact.
        """
        if (self._reported_uke_pitches is None or attack is None
                or note.midi_note not in self._reported_uke_pitches
                or note.midi_note == self._last_report_note
                or not any(item.kind == "bands" for item in attack.evidence)
                or any(item.kind == "level" for item in attack.evidence)):
            return False
        native = attack.origin.onset_sample
        if native is None or not self._partial_history:
            return False
        references = [(end, power) for end, power in self._partial_history
                      if self.buf_size <= end <= native]
        current_end, current = self._partial_history[-1]
        if not references or current_end - self.buf_size < native:
            return False
        reference_end, reference = references[-1]
        frequency = midi_to_freq(note.midi_note)
        previous = midi_to_freq(self._last_report_note)
        resolution = self.sample_rate / self.buf_size
        visible = []
        for harmonic in range(1, self.instrument.harmonics + 1):
            centre = frequency * harmonic
            if centre + resolution / 2 >= self.sample_rate / 2:
                continue
            if min(abs(centre - previous * n)
                   for n in range(1, self.instrument.harmonics + 1)) < 2 * resolution:
                continue
            mask = abs(self._partial_bins - centre) <= resolution / 2
            before = 10 * np.log10(max(float(reference[mask].sum()), 1e-12))
            now = 10 * np.log10(max(float(current[mask].sum()), 1e-12))
            # A newly audible target partial is positive evidence even if
            # it was too quiet to join the declining-partial comparison.
            if now > before and now >= self.last_signal_db - 35.:
                return False
            if min(before, now) >= self.last_signal_db - 35.:
                visible.append((harmonic, before, now))
        if len(visible) < 2 or any(now >= before for _, before, now in visible):
            return False
        # The same summed-power floor as positive partial confirmation. A
        # single loud partial cannot justify a veto without a second audible
        # declining partial; neighboring/shared harmonics never finance it.
        total = min(10 * np.log10(max(sum(10 ** (row[index] / 10) for row in visible), 1e-12))
                    for index in (1, 2))
        if total < max(self.noise_gate_db - 12., self.last_signal_db - 35.):
            return False
        attack.evidence.append(AttackEvidence(
            "decaying_partials", self._hops_seen,
            max(1, (reference_end - self.buf_size) // self.hop_size + 1),
            reference_end // self.hop_size,
            tuple(float(before) for _, before, _ in visible),
            tuple(float(now) for _, _, now in visible),
            tuple(harmonic for harmonic, _, _ in visible),
        ))
        return True


    def _raw_phase_rise_supported(self) -> bool:
        if self.instrument.key not in ("bass_guitar", "bass_ukulele") or not self._attack.phase_ready:
            return True
        # This can only veto an existing raw14dB approval. Neither this
        # evidence nor its absence authorizes an onset or bypasses pitch votes.
        return any(power > bound * reference for power, bound, reference in zip(
            self._phase_powers, self._phase_bounds, self._attack.phase_reference))

    def _record_level_evidence(self, db: float) -> None:
        self._attack.evidence.append(AttackEvidence(
            "level", self._hops_seen, *self._attack.reference_span,
            (float(self._attack.reference_db),), (float(db),),
        ))

    def _close_attack(self, reason, decision) -> None:
        if self._attack.close(reason, decision):
            self._last_closed_attack = self._attack

    def _prepare_report(self, note: DetectedNote) -> DetectedNote:
        # Keep the owner alongside the corrected note until _report, even if
        # a new onset replaces self._attack on the same processing hop.
        note = self._correct_bass_pitch(note)
        if self._attack.timing_ambiguity is not None:
            note = replace(note, timing_uncertain=True)
        self._report_candidate = (note, self._attack)
        return note

    def _flush_votes(self) -> "DetectedNote | None":
        """Report the note a strike collected, however few frames it got.

        A closed candidate cannot be emitted again. Too few votes or missing
        attack confirmation also produce no onset event.
        """
        if self._attack.close_reason is not None:
            return None
        votes = self._attack.votes
        if not self._attack.pending and not votes:
            return None
        reason = self._attack.requested_close_reason or "flush"
        if len(votes) < self.onset_votes_minimum:
            sparse = self._sparse_bass_pitch()
            if sparse is not None:
                midi, confidence = sparse
                note = DetectedNote(midi,midi_to_freq(midi),confidence,midi_to_name(midi),True,
                                    self._attack.age_hops+self._attack.origin.backdate_hops)
                self._close_attack(reason,"accepted")
                return self._prepare_report(note)
            if (len(votes) == 1 and self._quiet_repeat_corroborator is not None
                    and self._continuation_matches(votes[0])):
                evidence = self._quiet_repeat_corroborator.corroborate(
                    self._attack, self._last_report_note, self._quiet_repeat_spectrum)
                if evidence is not None:
                    midi = votes[0]
                    self._attack.quiet_repeat_evidence = evidence
                    note = DetectedNote(midi, midi_to_freq(midi), evidence.confidence,
                        midi_to_name(midi), True,
                        self._attack.age_hops + self._attack.origin.backdate_hops)
                    self._close_attack(reason, "accepted")
                    return self._prepare_report(note)
            if self._growing_pitch is not None:
                proof = self._growing_pitch.corroborate(self._attack, self._last_report_note,
                    self._quiet_repeat_corroborator, self._waveform_history, self._fundamental_present)
                if (proof is not None and self._continuation_matches(proof.midi_note)
                        and is_in_instrument_range(proof.midi_note, self.midi_min, self.midi_max)):
                    midi = proof.midi_note
                    self._attack.growing_pitch_evidence = proof
                    note = DetectedNote(midi, midi_to_freq(midi), proof.confidence,
                        midi_to_name(midi), True,
                        self._attack.age_hops + self._attack.origin.backdate_hops)
                    self._close_attack(reason, "accepted")
                    return self._prepare_report(note)
            self._close_attack(reason, "insufficient_votes")
            return None
        midi_note = self._choose_pitch(votes)
        if (not self._continuation_matches(midi_note)
                or not (self._attack.confirmed or self._corroborated_transient(midi_note))):
            # No level rise ever backed this strike: it was the onset detector
            # re-firing inside a note that was already sounding.
            self._close_attack(reason, "unconfirmed")
            return None
        note = DetectedNote(
            midi_note=midi_note,
            frequency=midi_to_freq(midi_note),
            confidence=self.last_confidence,
            name=midi_to_name(midi_note),
            is_onset=True,
            onset_age_hops=self._attack.age_hops + self._attack.origin.backdate_hops,
        )
        if reason == "replaced":
            self._mark_ambiguous_timing(midi_note)
        self._close_attack(reason, "accepted")
        return self._prepare_report(note)

    def _choose_pitch(self, votes: list[int]) -> int:
        original = max(set(votes), key=votes.count)
        if self.instrument.key != "bass_ukulele" or self._quiet_repeat_corroborator is None:
            return original
        tied = sorted(pitch for pitch in set(votes) if votes.count(pitch) == votes.count(original))
        if votes.count(original) < 2:
            return original
        proof = self._quiet_repeat_corroborator.corroborate_pitch_tie(self._attack, tied, self._hops_seen)
        if not proof:
            return original
        self._attack.pitch_tie_evidence = proof
        return freq_to_midi(proof[0].periodicity_frequency)

    def _confirm_quiet_start(self, attack, frequency):
        """Keep ordinary confirmation; allow a later independently timed rise.

        A spectral-flux onset can precede the audible rise of a soft pluck.
        One unused complex onset in the following pitch period may confirm
        it, with the same two-period energy windows and unchanged gate/rise
        thresholds. Such a report uses the complex onset's time, never the
        earlier provisional time. Missing or ambiguous evidence rejects it.
        """
        native = attack.origin.onset_sample
        helper, history = self._quiet_repeat_corroborator, self._waveform_history
        if native is None or helper is None or history is None:
            return None, None
        period = self.sample_rate / frequency
        width = round(2 * period)

        def inspect(pulse, centre):
            before = history._read(centre-width, centre)
            after = history._read(centre, centre+width)
            if before is None or after is None:
                return None
            old = float(np.mean(before.astype(np.float64)**2))
            new = float(np.mean(after.astype(np.float64)**2))
            if (new < old * 10**(self.strike_rise_db/10)
                    or 10*np.log10(max(new, 1e-20)) < self.noise_gate_db):
                return None
            return QuietStartEvidence(
                SupportingOnset(native, pulse.sample, pulse.observed_sample, self.strike_rise_db),
                centre-width, centre, centre+width, old, new)

        near = helper._near(native, frequency)
        if near:
            proof = inspect(near[0], round(native))
            if proof is not None:
                return proof, None
        later = [pulse for pulse in helper.pulses if not pulse.consumed
                 and period < pulse.sample-native <= 2*period]
        if len(later) != 1:
            return None, None
        pulse = later[0]
        proof = inspect(pulse, round(pulse.sample))
        return (proof, pulse.sample) if proof is not None else (None, None)

    def _quiet_repeat_spectrum(self, native: float, midi: int, observed_hop: int):
        return inspect_repeat_spectrum(self._partial_history, self._partial_bins,
            native, midi, observed_hop * self.hop_size, self.buf_size, self.sample_rate,
            self.midi_min, self.midi_max, self.instrument.harmonics,
            self.strike_rise_db, self.noise_gate_db)

    def _mark_ambiguous_timing(self, midi_note: int) -> None:
        """Keep the tone, but do not treat borrowed confirmation as a clock.

        A real quiet repetition may qualify in exactly this situation, so
        dropping every such candidate loses valid notes. Only an exceptionally
        accurate causal forecast can veto a predictable continuation. Otherwise
        keep the uncertainty annotation. Neither path changes native timing.
        """
        attack = self._attack
        if (self.instrument.key != "bass_ukulele" or not attack.confirmed
                or midi_note != self._last_report_note or not attack.evidence
                or attack.origin.onset_sample is None or attack.continuations):
            return
        get_last = getattr(self._onset, "get_last", None)
        if get_last is None:
            return
        native = float(get_last())
        if (not np.isfinite(native)
                or not attack.origin.onset_sample <= attack.origin.opened_hop * self.hop_size < native
                <= self._hops_seen * self.hop_size
                or any(item.kind not in ("level", "bands") for item in attack.evidence)):
            return
        starts = [item.observed_hop * self.hop_size
                  - (self.hop_size if item.kind == "level" else self._PROBE_NFFT)
                  for item in attack.evidence]
        if min(starts) >= native:
            attack.timing_ambiguity = TimingAmbiguity(native, min(starts), self._hops_seen)
            if self._waveform_history is not None:
                attack.waveform_prediction = self._waveform_history.predict(
                    attack.origin.onset_sample, native)

    def _collect_bass_pitch(self, frequency: float, confidence: float) -> None:
        # Keep this evidence out of attack confirmation and harmonic votes.
        if (self.instrument.key != "bass_guitar" or not self._attack.pending
                or self.last_signal_db < self.noise_gate_db
                or not np.isfinite(frequency) or not np.isfinite(confidence)
                or frequency <= 0 or confidence < 0.9):
            return
        window = getattr(self._pitch_check, "buf_size", 0)
        since_onset = ((self._attack.age_hops + self._attack.origin.backdate_hops) * self.hop_size
                       + self.onset_latency_ms * self.sample_rate / 1000.0)
        # A window still containing the previous note cannot correct this strike.
        if window <= 0 or since_onset < window:
            return
        midi = freq_to_midi(frequency)
        if is_in_instrument_range(midi, self.midi_min, self.midi_max):
            self._attack.bass_pitch_votes.append((midi, confidence))

    def _retain_sparse_bass_attack(self, native: float, frequency: float, confidence: float) -> bool:
        attack = self._attack
        origin = attack.origin.onset_sample
        votes = attack.bass_pitch_votes
        if (not attack.pending or not attack.preceded_by_quiet or not attack.confirmed
                or len(attack.votes) >= self.onset_votes_minimum or not votes or origin is None
                or self._sparse_bass_pitch() is not None):
            return False
        midi = votes[-1][0]
        expected = midi_to_freq(midi)
        peak = getattr(self._pitch, "dominant_frequency", 0.)
        if (not all(np.isfinite(value) for value in (native, frequency, confidence, peak))
                or not 0 < native - origin < self.buf_size
                or native > self._hops_seen * self.hop_size
                or confidence < .98 or frequency <= 0 or peak <= 0
                or any(note != midi for note, _ in votes)
                or abs(1200 * np.log2(peak / expected)) > 35
                or abs(1200 * np.log2(frequency / expected)) > 35):
            return False
        previous = attack.bass_continuation_sample
        attack.bass_continuation_sample = native if previous is None else previous
        if self._bass_continuation_supported(attack, expected):
            return True
        attack.bass_continuation_sample = previous
        return False

    def _predict_bass_continuation(self, origin: float, native: float,
                                   frequency: float) -> BassContinuation | None:
        if self._waveform_history is None:
            return None
        end = self._hops_seen * self.hop_size
        prediction = self._waveform_history.predict_continuation(origin, native, end)
        if prediction is None or not prediction.is_continuation or not prediction.supports_pitch(frequency):
            return None
        return BassContinuation(origin, native, end, prediction)

    def _bass_continuation_supported(self, attack: _PhaseCheckedAttack, frequency: float) -> bool:
        origin, native = attack.origin.onset_sample, attack.bass_continuation_sample
        if origin is None or native is None:
            return False
        attack.bass_continuation_evidence = self._predict_bass_continuation(origin, native, frequency)
        return attack.bass_continuation_evidence is not None

    def _sparse_bass_pitch(self) -> tuple[int, float] | None:
        """Corroborate a sparse isolated bass tone; never extend its candidate.

        Full post-native YIN windows must name one pitch consistently, with
        three strong final observations. The long-window spectral maximum
        must independently agree. Missing quiet history or a competing pitch
        leaves the normal primary path untouched.
        """
        votes = self._attack.bass_pitch_votes
        if (self.instrument.key != "bass_guitar" or not self._attack.preceded_by_quiet
                or not self._attack.confirmed or len(votes) < 3):
            return None
        midi = votes[0][0]
        if any(m != midi for m, _ in votes) or any(c < .98 for _, c in votes[-3:]):
            return None
        peak = getattr(self._pitch, "dominant_frequency", 0.)
        if not np.isfinite(peak) or peak <= 0 or abs(1200*np.log2(peak/midi_to_freq(midi))) > 35:
            return None
        confidence = min(c for _, c in votes[-3:])
        self._attack.sparse_pitch_evidence = SparsePitchEvidence(
            midi, float(peak), float(confidence), self._hops_seen,
            *self._attack.quiet_reference_span, self._attack.quiet_reference_peak_db,
            tuple(votes))
        return midi, confidence

    def _correct_bass_pitch(self, note: "DetectedNote") -> "DetectedNote":
        sparse = self._sparse_bass_pitch()
        if sparse is not None:
            midi, confidence = sparse
            if midi != note.midi_note:
                return replace(note, original_midi_note=note.midi_note, midi_note=midi,
                               frequency=midi_to_freq(midi), name=midi_to_name(midi), confidence=confidence)
        votes = self._attack.bass_pitch_votes
        if (self.instrument.key != "bass_guitar" or not self._attack.confirmed
                or len(votes) < self.onset_votes_minimum):
            return note
        candidate = votes[0][0]
        if (abs(candidate - note.midi_note) != 12
                or any(midi != candidate for midi, _confidence in votes)
                or not self._fundamental_present(midi_to_freq(candidate))):
            return note
        # The snapshot travels with a flushed old strike. _report still applies
        # the original pitch's repeat filter, so this cannot unlock an onset.
        return replace(note, original_midi_note=note.midi_note, midi_note=candidate,
                       frequency=midi_to_freq(candidate), name=midi_to_name(candidate),
                       confidence=min(confidence for _midi, confidence in votes))

    def _retain_pending_attack(self, native: float, frequency: float, confidence: float) -> bool:
        """Reject a spectral retrigger only with earlier tone-specific energy.

        The original candidate keeps its time, references and expiry. No vote
        or rise threshold is relaxed: final pitch/periodicity and attack
        confirmation are still required. A short native gap alone is not
        evidence that two onsets belong to the same physical pluck.
        """
        attack = self._attack
        origin = attack.origin.onset_sample
        if (self.instrument.key != "bass_ukulele" or not attack.pending
                or attack.confirmed or attack.close_reason is not None
                or origin is None or attack.age_hops >= self.onset_hold_hops
                or len(attack.votes) >= self.onset_votes_minimum
                or not np.isfinite(native) or not np.isfinite(frequency)
                or not np.isfinite(confidence) or frequency <= 0
                or confidence < self.confidence_threshold
                or native > self._hops_seen * self.hop_size
                or not 0 < native - origin < self.buf_size):
            return False
        midi = freq_to_midi(frequency)
        if (midi == self._last_report_note
                or not is_in_instrument_range(midi, self.midi_min, self.midi_max)
                or not self._continuation_matches(midi)):
            return False
        references = [(end, power) for end, power in self._partial_history
                      if self.buf_size <= end <= origin]
        earlier = [(end, power) for end, power in self._partial_history if end <= native]
        if not references or not earlier:
            return False
        reference_end, reference = references[-1]
        earlier_end, earlier_power = earlier[-1]
        current_end, current = self._partial_history[-1]
        if current_end - self.buf_size < origin:
            return False
        resolution = self.sample_rate / self.buf_size
        previous = midi_to_freq(self._last_report_note) if self._last_report_note >= 0 else None
        partials = []
        for harmonic in range(1, self.instrument.harmonics + 1):
            centre = midi_to_freq(midi) * harmonic
            if centre + resolution / 2 >= self.sample_rate / 2:
                continue
            if previous is not None and min(abs(centre - previous * n)
                    for n in range(1, self.instrument.harmonics + 1)) < 2 * resolution:
                continue
            mask = abs(self._partial_bins - centre) <= resolution / 2
            before = 10 * np.log10(max(float(reference[mask].sum()), 1e-12))
            middle = 10 * np.log10(max(float(earlier_power[mask].sum()), 1e-12))
            power = float(current[mask].sum())
            now = 10 * np.log10(max(power, 1e-12))
            if now - before >= self.strike_rise_db and now >= self.last_signal_db - 35.:
                partials.append((harmonic, before, middle, now, power))
        total = 10 * np.log10(max(sum(row[4] for row in partials), 1e-12))
        if (len(partials) < 2 or total < max(self.noise_gate_db - 12., self.last_signal_db - 35.)
                or not any(row[2] - row[1] >= self.strike_rise_db
                           and row[2] >= self.last_signal_db - 35. for row in partials)):
            return False
        attack.continuations.append(_OnsetContinuation(
            native, self._hops_seen, midi, reference_end, earlier_end, current_end,
            tuple(row[0] for row in partials), tuple(float(row[1]) for row in partials),
            tuple(float(row[2]) for row in partials), tuple(float(row[3]) for row in partials),
        ))
        return True

    def _continuation_matches(self, midi: int) -> bool:
        return all(item.midi_note == midi for item in self._attack.continuations)


    def _corroborated_transient(self, midi_note: int) -> bool:
        return ((self._attack.transient_candidate
                 and self._attack.periodicity_votes.count(midi_note) >= self.onset_votes_minimum)
                or self._corroborated_note_energy(midi_note))

    def _corroborated_note_energy(self, midi_note: int) -> bool:
        """Confirm a new uke pitch using only its newly growing partials.

        Native onset time, primary votes, and two matching periodicity votes
        remain mandatory. Complete pre/post-onset windows exclude the previous
        pitch's partial neighborhoods. Each selected partial must grow by the
        existing rise threshold; only their summed energy can satisfy the
        existing audibility floor. A pitch change alone is never sufficient.
        """
        if (self.instrument.key != "bass_ukulele" or self._attack.confirmed
                or self._attack.close_reason is not None
                or self._attack.periodicity_votes.count(midi_note) < self.onset_votes_minimum
                or midi_note == self._last_report_note):
            return False
        native = self._attack.origin.onset_sample
        if native is None or not self._partial_history:
            return False
        references = [(end, power) for end, power in self._partial_history if self.buf_size <= end <= native]
        if not references:
            return False
        reference_end, reference = references[-1]
        current_end, current = self._partial_history[-1]
        if current_end - self.buf_size < native:
            return False
        frequency = midi_to_freq(midi_note)
        resolution = self.sample_rate / self.buf_size
        previous = midi_to_freq(self._last_report_note) if self._last_report_note >= 0 else None
        complex_pulses = (self._quiet_repeat_corroborator._near(native, frequency)
                          if self._quiet_repeat_corroborator is not None else [])
        # Only a separate, unused complex onset can support the smaller
        # 9dB rise already used by the independent repetition evidence.
        required_rise = min(self.strike_rise_db, 9.) if complex_pulses else self.strike_rise_db
        details, growing = [], []
        for harmonic in range(1, self.instrument.harmonics + 1):
            centre = frequency * harmonic
            if centre + resolution / 2 >= self.sample_rate / 2:
                continue
            # Each measured band extends half a resolution to either side.
            # Reject overlapping bands, but keep disjoint growing A partials
            # beside a ringing E. Requiring twice this separation discarded
            # useful attack evidence even with two independent pitch votes.
            # Finite-window leakage remains possible; the rise, audibility
            # and complete pre/post-window checks still apply below.
            if previous is not None and min(abs(centre - previous * n)
                    for n in range(1, self.instrument.harmonics + 1)) < resolution:
                continue
            mask = abs(self._partial_bins - centre) <= resolution / 2
            power_before = float(reference[mask].sum())
            power_now = float(current[mask].sum())
            before = 10 * np.log10(max(power_before, 1e-12))
            now = 10 * np.log10(max(power_now, 1e-12))
            eligible = now - before >= required_rise and now >= self.last_signal_db - 35.
            details.append({"harmonic": harmonic, "before_db": before, "current_db": now,
                            "qualifies": bool(eligible)})
            if eligible:
                growing.append(power_now)
        total_db = 10 * np.log10(max(sum(growing), 1e-12))
        audible = max(self.noise_gate_db - 12., self.last_signal_db - 35.)
        accepted = len(growing) >= 2 and total_db >= audible
        if accepted:
            if complex_pulses:
                pulse = complex_pulses[0]
                self._attack.complex_confirmation = SupportingOnset(
                    native, pulse.sample, pulse.observed_sample, required_rise)
            # Own the spectral evidence on the candidate that it confirms.
            selected = [item for item in details if item["qualifies"]]
            self._attack.evidence.append(AttackEvidence(
                "partials", self._hops_seen,
                max(1, (reference_end - self.buf_size) // self.hop_size + 1),
                reference_end // self.hop_size,
                tuple(float(item["before_db"]) for item in selected),
                tuple(float(item["current_db"]) for item in selected),
                tuple(item["harmonic"] for item in selected),
            ))
        return bool(accepted)

    def _correct_octave_jump(self, freq: float, confidence: float) -> float:
        """Suppress octave jumps caused by harmonic detection.

        Only used by the yin backend. The harmonic backend handles ambiguity
        through its own candidate scores and optional periodicity check.
        """
        corrected = freq

        # Calibration-based correction. Guarded by confidence: without the
        # guard this halves unconditionally, so every note at the 12th fret
        # collapses onto its own open string.
        if self._calibration and freq > 0 and confidence < 0.95:
            cal_strings = self._calibration.get("strings", {})
            half_freq = freq / 2.0
            for cal in cal_strings.values():
                cal_freq = cal.get("frequency", 0)
                if cal_freq > 0:
                    ratio = half_freq / cal_freq
                    if 0.944 < ratio < 1.06:
                        corrected = half_freq
                        self._prev_freq = corrected
                        return corrected

        if self._prev_freq > 0 and confidence < 0.95:
            ratio = freq / self._prev_freq
            if 1.95 <= ratio <= 2.05:
                corrected = self._prev_freq
            elif 0.48 <= ratio <= 0.52:
                corrected = self._prev_freq

        self._prev_freq = corrected
        return corrected

    @property
    def expected_delay_ms(self) -> float:
        """How late a detection can arrive after the string was struck.

        The analysis window must fill with the new note, the strike waits for
        enough votes, and the onset detector has its own lag on top.
        """
        per_hop = self.hop_size / self.sample_rate * 1000.0
        return (self.onset_hold_hops * per_hop
                + self.onset_votes_wanted * per_hop
                + self.onset_latency_ms)

    def set_noise_gate_db(self, db: float) -> None:
        """Update the noise gate threshold (dB). Takes effect on next process()."""
        if db != self.noise_gate_db:
            self._quiet_hops = 0
        self.noise_gate_db = db
        self.effective_gate_db = db

    def reset(self):
        """Reset detector state. Call when starting a new song/session."""
        self._prev_freq = 0.0
        self._quiet_start_anchor = None
        self._bass_recovery = None
        self._last_report_hop = -10 ** 9
        self._last_report_note = -1
        if self._reported_uke_pitches is not None:
            self._reported_uke_pitches.clear()
        self._hops_seen = 0
        if self._waveform_history is not None:
            self._waveform_history.reset()
        if self._attack.pending:
            self._close_attack("reset", None)
        self._attack = _PhaseCheckedAttack(
            AttackOrigin(self._attack_sequence, 0, None, 0.0),
            band_reference=np.full(len(self._attack_bands), -120.0),
        )
        self._report_candidate = None
        self._quiet_hops = 0
        if self._partial_ring is not None:
            self._partial_ring.fill(0.)
            self._partial_history.clear()
        self._phase_power_ring.clear()
        self._phase_power_history.clear()
        self._phase_powers = (0.0, 0.0)
        self._db_ring.clear()
        self._band_db_ring.clear()
        self._transient_hop = -10 ** 9
        self._transient_evidence = None
        self._probe_ring.fill(0)
        self.last_signal_db = -120.0
        self.last_freq = 0.0
        self.last_confidence = 0.0
        self.effective_gate_db = self.noise_gate_db
        self._repeat_corroborator = None
        self._quiet_repeat_corroborator = None
        self._build()
