"""Independent, bounded evidence for an otherwise suppressed repetition."""
from collections import deque
from dataclasses import dataclass
import math

import numpy as np

from pickhero.audio.waveform_prediction import WaveformHistory


@dataclass(frozen=True, slots=True)
class RepeatEvidence:
    energy_sample: float
    observed_sample: int
    reference_start_sample: int
    split_sample: int
    end_sample: int
    reference_power: float
    observed_power: float
    rise_db: float


@dataclass(slots=True)
class EnergyPulse:
    sample: float
    observed_sample: int
    consumed: bool = False


class RepeatCorroborator:
    """One energy pulse can corroborate at most one native pitch candidate.

    The main onset, pitch votes and repeat interval stay intact. A separate
    local rise of 9dB across two complete pitch periods is required, using
    the same magnitude as the existing steel-bass transient threshold, but
    integrated over longer windows here. This is measured evidence, not a
    guarantee that every energy rise came from another physical pluck.
    """

    def __init__(self, onset, sample_rate: int):
        self.onset = onset
        self.sample_rate = sample_rate
        self.pulses: deque[EnergyPulse] = deque(maxlen=16)

    def process(self, block: np.ndarray, end_sample: int) -> None:
        if self.onset(block):
            native = float(self.onset.get_last())
            if math.isfinite(native) and 0 <= native <= end_sample:
                self.pulses.append(EnergyPulse(native, end_sample))
        while self.pulses and self.pulses[0].sample < end_sample - self.sample_rate * .5:
            self.pulses.popleft()

    def _near(self, native: float | None, frequency: float) -> list[EnergyPulse]:
        if (native is None or not math.isfinite(native)
                or not math.isfinite(frequency) or frequency <= 0):
            return []
        return [pulse for pulse in self.pulses if not pulse.consumed
                and abs(pulse.sample - native) <= self.sample_rate / frequency]

    def consume(self, native: float | None, frequency: float) -> None:
        for pulse in self._near(native, frequency):
            pulse.consumed = True

    def corroborate(self, native: float | None, frequency: float,
                    history: WaveformHistory, gate_db: float) -> RepeatEvidence | None:
        nearby = self._near(native, frequency)
        if not nearby:
            return None
        width = round(2 * self.sample_rate / frequency)
        for pulse in nearby:
            centre = round(pulse.sample)
            before = history._read(centre - width, centre)
            after = history._read(centre, centre + width)
            if before is None or after is None:
                continue
            old = float(np.mean(before.astype(np.float64) ** 2))
            new = float(np.mean(after.astype(np.float64) ** 2))
            if 10 * np.log10(max(new, 1e-20)) < gate_db or new < old * 10 ** .9:
                continue
            evidence = RepeatEvidence(pulse.sample, pulse.observed_sample,
                centre - width, centre, centre + width, old, new,
                float(10 * np.log10(max(new, 1e-20) / max(old, 1e-20))))
            self.consume(native, frequency)
            return evidence
        return None
