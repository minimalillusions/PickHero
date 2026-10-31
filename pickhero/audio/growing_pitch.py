"""Conservative pitch evidence for an onset covered by a ringing string.

No score, expected note, string number or exercise enters this measurement.
Spectral peak agreement supplies pitch; a separate complex onset and two
short waveform windows establish that new energy belongs near this onset.
"""
from collections import deque
from dataclasses import dataclass
import math

import numpy as np

from pickhero.audio.note_utils import freq_to_midi, midi_to_freq


@dataclass(frozen=True, slots=True)
class GrowingPitchFrame:
    observed_hop: int
    reference_end_sample: int
    end_sample: int
    frequency: float
    midi_note: int
    score: float
    margin: float
    harmonics: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class GrowingPitchEvidence:
    native_sample: float
    complex_sample: float
    complex_observed_sample: int
    frames: tuple[GrowingPitchFrame, GrowingPitchFrame]
    short_start_sample: int
    short_split_sample: int
    short_end_sample: int
    short_growth: tuple[tuple[int, float], ...]

    @property
    def midi_note(self) -> int:
        return self.frames[0].midi_note

    @property
    def confidence(self) -> float:
        # A relative score contrast, not a probability of being correct.
        return 1 - math.exp(-min(frame.margin for frame in self.frames))


class GrowingPitchCorroborator:
    def __init__(self, sample_rate, hop_size, buf_size, freq_min, freq_max):
        self.sample_rate, self.hop_size, self.buf_size = sample_rate, hop_size, buf_size
        self.frequencies = np.arange(freq_min, freq_max, .25, dtype=np.float64)
        self.reset()

    def reset(self):
        self.owner = None
        self.frames = deque(maxlen=32)
        self.reported = None

    def observe(self, attack, hop, history, bins, nfft, gate_db, signal_db):
        if self.owner != attack.origin.sequence:
            self.owner = attack.origin.sequence
            self.frames.clear()
        if not attack.pending or signal_db < gate_db:
            return
        native = attack.origin.onset_sample
        if native is None or hop*self.hop_size - self.buf_size < native:
            return
        references = [(end, power) for end, power in history if self.buf_size <= end <= native]
        if not references:
            return
        ref_end, before = references[-1]
        end, now = history[-1]
        peaks = np.flatnonzero((now[1:-1] > now[:-2]) & (now[1:-1] > now[2:])) + 1
        if not len(peaks):
            return
        db = 10*np.log10(np.maximum(now[peaks], 1e-24))
        peaks = peaks[db >= max(float(db.max()) - 35, gate_db - 12)]
        if not len(peaks):
            return
        log = np.log(np.maximum(now, 1e-24))
        delta = .5*(log[peaks-1] - log[peaks+1])/(log[peaks-1] - 2*log[peaks] + log[peaks+1])
        frequencies = (peaks + np.clip(delta, -.5, .5))*self.sample_rate/nfft
        growth = 10*np.log10(np.maximum(now[peaks], 1e-24)/np.maximum(before[peaks], 1e-24))
        numbers = np.rint(frequencies[None, :]/self.frequencies[:, None])
        cents = 1200*np.log2(np.maximum(frequencies[None, :]/np.maximum(numbers*self.frequencies[:, None], 1e-20), 1e-20))
        valid = (numbers >= 1) & (numbers <= 8) & (abs(cents) < 35) & (growth >= 6)[None, :]
        scores = np.sum(np.where(valid, (1-abs(cents)/35)*(.9**np.maximum(0, numbers-1)), 0), axis=1)
        best = int(scores.argmax())
        frequency = float(self.frequencies[best])
        near = abs(1200*np.log2(self.frequencies/frequency)) < 100
        if np.all(near):
            return
        self.frames.append(GrowingPitchFrame(hop, ref_end, end, frequency, freq_to_midi(frequency),
            float(scores[best]), float(scores[best] - scores[~near].max()),
            tuple(int(value) for value in numbers[best, valid[best]])))

    def corroborate(self, attack, last_midi, complex_onsets, waveform, fundamental_present):
        if (not attack.pending or attack.close_reason is not None or self.owner != attack.origin.sequence
                or last_midi < 0 or complex_onsets is None or waveform is None):
            return None
        pair = None
        for first, second in zip(self.frames, list(self.frames)[1:]):
            if (second.observed_hop != first.observed_hop+1 or first.midi_note != second.midi_note
                    or first.midi_note == last_midi):
                continue
            if any(len(set(frame.harmonics)) < 4 or math.gcd(*frame.harmonics) != 1
                   or frame.score < 1.6 or frame.margin < .3 for frame in (first, second)):
                continue
            if abs(1200*math.log2(first.frequency/second.frequency)) > 35:
                continue
            pair = first, second
            break
        if pair is None:
            return None
        frequency = midi_to_freq(pair[0].midi_note)
        native = attack.origin.onset_sample
        pulses = complex_onsets._near(native, frequency)
        if not pulses or not fundamental_present(frequency):
            return None
        # Near a semitone boundary the evidence cannot name one fret safely.
        cents = sum(1200*math.log2(frame.frequency/frequency) for frame in pair)/2
        if abs(cents) > 35 or any(min(frame.harmonics) > 2 for frame in pair):
            return None
        measured_frequency = sum(frame.frequency for frame in pair)/2
        width = round(2*self.sample_rate/measured_frequency)
        centre = round(native)
        before = waveform._read(centre-width, centre)
        after = waveform._read(centre, centre+width)
        if before is None or after is None:
            return None
        # This window checks energy ownership, not the pitch search above.
        # Padding fourfold only oversamples the same two-period spectrum
        # and needlessly enlarges both transforms at high device rates.
        nfft = 1 << int(np.ceil(np.log2(width)))
        window = np.hanning(width)
        bins = np.fft.rfftfreq(nfft, 1/self.sample_rate)
        old = np.abs(np.fft.rfft(before*window, n=nfft))**2
        new = np.abs(np.fft.rfft(after*window, n=nfft))**2
        rows = []
        for harmonic in sorted(set(pair[0].harmonics) & set(pair[1].harmonics)):
            mask = abs(bins-harmonic*measured_frequency) <= self.sample_rate/width/2
            p, q = float(old[mask].sum()), float(new[mask].sum())
            rows.append((harmonic, float(10*np.log10(max(q, 1e-20)/max(p, 1e-20)))))
        if sum(growth >= 6 for harmonic, growth in rows) < 2:
            return None
        pulse = pulses[0]
        return GrowingPitchEvidence(native, pulse.sample, pulse.observed_sample, pair,
            centre-width, centre, centre+width, tuple(rows))
