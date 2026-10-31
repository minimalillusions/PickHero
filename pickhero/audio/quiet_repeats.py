"""Bounded evidence for quiet repetitions with too few primary pitch votes."""
from collections import deque
from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Callable

import numpy as np

from pickhero.audio.note_utils import freq_to_midi, midi_to_freq

if TYPE_CHECKING:
    from pickhero.audio.attack import PendingAttack


@dataclass(frozen=True, slots=True)
class PitchAgreement:
    observed_hop: int
    midi_note: int
    primary_frequency: float
    periodicity_frequency: float
    periodicity_confidence: float


@dataclass(frozen=True, slots=True)
class QuietRepeatEvidence:
    native_sample: float
    complex_sample: float
    observed_sample: int
    pitch: tuple[PitchAgreement, ...]
    spectrum: "RepeatSpectrum"

    @property
    def confidence(self) -> float:
        return min(item.periodicity_confidence for item in self.pitch)


@dataclass(slots=True)
class ComplexPulse:
    sample: float
    observed_sample: int
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class CompetingPitch:
    midi_note: int
    harmonics: tuple[int, ...]
    before_power: tuple[float, ...]
    after_power: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class RepeatSpectrum:
    reference_end_sample: int
    observed_end_sample: int
    competing: tuple[CompetingPitch, ...]


def inspect_repeat_spectrum(history, bins: np.ndarray, native: float,
                            midi: int, observed_end: int, window_size: int,
                            sample_rate: int, midi_min: int, midi_max: int,
                            harmonics: int, rise_db: float, gate_db: float) -> RepeatSpectrum | None:
    """Veto a relaxed pitch decision if other audible partials were excited.

    Inspect the window of the first consecutive agreement pair, not the
    successor's current frame. Missing complete pre/post windows mean that
    the fallback has no sufficient evidence. Disjoint partial neighborhoods
    cannot borrow the reported note's own harmonics to assert a conflict.
    """
    references = [(end, power) for end, power in history if window_size <= end <= native]
    posts = [(end, power) for end, power in history if end == observed_end]
    if not references or not posts:
        return None
    reference_end, reference = references[-1]
    end, current = posts[-1]
    if end - window_size < native:
        return None
    resolution = sample_rate / window_size
    named = np.arange(1, harmonics + 1) * midi_to_freq(midi)
    competing = []
    for alternate in range(midi_min, midi_max + 1):
        if abs(alternate - midi) <= 1:
            continue
        growth = []
        for harmonic in range(1, harmonics + 1):
            centre = midi_to_freq(alternate) * harmonic
            if np.min(abs(named - centre)) <= resolution:
                continue
            mask = abs(bins - centre) <= resolution / 2
            old, new = float(reference[mask].sum()), float(current[mask].sum())
            if (new >= old * 10 ** (rise_db / 10)
                    and 10 * np.log10(max(new, 1e-20)) >= gate_db - 12):
                growth.append((harmonic, old, new))
        if len(growth) >= 2:
            competing.append(CompetingPitch(alternate, tuple(row[0] for row in growth),
                tuple(row[1] for row in growth), tuple(row[2] for row in growth)))
    return RepeatSpectrum(reference_end, end, tuple(competing))


class QuietRepeatCorroborator:
    """Supplement one existing vote with audible, agreeing pitch observations.

    This path requires a confirmed primary attack, the previously reported
    pitch and its own unused complex-domain onset within one pitch period.
    Two consecutive harmonic/YIN agreements must follow a full post-native
    analysis window. Later uncertain frames do not erase earlier evidence.
    No below-gate observation may provide a vote, even if YIN is confident.
    """

    def __init__(self, onset, sample_rate: int, hop_size: int):
        self.onset = onset
        self.sample_rate = sample_rate
        self.hop_size = hop_size
        self.pulses: deque[ComplexPulse] = deque(maxlen=16)
        self.agreements: deque[PitchAgreement] = deque(maxlen=8)
        self.owner_sequence: int | None = None

    def process(self, block, end_sample: int) -> None:
        if self.onset(block):
            native = float(self.onset.get_last())
            if math.isfinite(native) and 0 <= native <= end_sample:
                self.pulses.append(ComplexPulse(native, end_sample))
        while self.pulses and self.pulses[0].sample < end_sample - self.sample_rate * .5:
            self.pulses.popleft()

    def observe(self, attack: "PendingAttack", hop: int, primary: float,
                periodicity: float, confidence: float, window_size: int,
                vote_delay_hops: int, audible: bool,
                fundamental_present: Callable[[float], bool]) -> None:
        if attack.origin.sequence != self.owner_sequence:
            self.agreements.clear()
            self.owner_sequence = attack.origin.sequence
        native = attack.origin.onset_sample
        if (not audible or not attack.pending or attack.age_hops < vote_delay_hops
                or window_size <= 0
                or native is None or not math.isfinite(native)
                or hop * self.hop_size - native < window_size
                or not all(math.isfinite(value) for value in (primary, periodicity, confidence))
                or primary <= 0 or periodicity <= 0 or confidence < .85
                or abs(1200 * math.log2(primary / periodicity)) > 35
                or not fundamental_present(primary)):
            return
        self.agreements.append(PitchAgreement(hop, freq_to_midi(primary),
            primary, periodicity, confidence))

    def _near(self, native: float | None, frequency: float) -> list[ComplexPulse]:
        if (native is None or not math.isfinite(native)
                or not math.isfinite(frequency) or frequency <= 0):
            return []
        return [pulse for pulse in self.pulses if not pulse.consumed
                and abs(pulse.sample - native) <= self.sample_rate / frequency]

    def corroborate_pitch_tie(self, attack: "PendingAttack", tied: list[int],
                              observed_hop: int) -> tuple[PitchAgreement, ...]:
        """Resolve an adjacent-pitch tie only when both estimators agree.

        observe() admits audible, full post-onset windows owned by this
        candidate. Require two consecutive high-confidence YINfast frames,
        agreement on the named note in every such frame, and recent evidence.
        An estimate across a semitone boundary cannot overrule the primary
        estimate merely because their frequencies are less than 35 cents apart.
        """
        if (len(tied) != 2 or tied[1] - tied[0] != 1 or not attack.pending
                or attack.close_reason is not None
                or self.owner_sequence != attack.origin.sequence):
            return ()
        evidence = tuple(row for row in self.agreements if row.periodicity_confidence >= .9)
        if (len(evidence) < 2 or not 0 <= observed_hop - evidence[-1].observed_hop <= 2
                or not any(b.observed_hop == a.observed_hop + 1
                           for a, b in zip(evidence, evidence[1:]))):
            return ()
        pitches = {freq_to_midi(row.periodicity_frequency) for row in evidence}
        if (len(pitches) != 1 or not pitches.issubset(tied)
                or any(row.midi_note not in pitches for row in evidence)):
            return ()
        return evidence

    def consume(self, native: float | None, frequency: float) -> None:
        for pulse in self._near(native, frequency):
            pulse.consumed = True

    def corroborate(self, attack: "PendingAttack", last_midi: int,
                    inspect_spectrum: Callable[[float, int, int], RepeatSpectrum | None]) -> QuietRepeatEvidence | None:
        if (attack.close_reason is not None or not attack.pending or not attack.confirmed
                or len(attack.votes) != 1 or len(self.agreements) < 2
                or self.owner_sequence != attack.origin.sequence
                or attack.votes[0] != last_midi):
            return None
        midi = attack.votes[0]
        evidence = tuple(self.agreements)
        if (any(item.midi_note != midi for item in evidence)
                or not any(b.observed_hop == a.observed_hop + 1
                           for a, b in zip(evidence, evidence[1:]))):
            return None
        frequency = midi_to_freq(midi)
        near = self._near(attack.origin.onset_sample, frequency)
        if not near:
            return None
        pair_end = next(b.observed_hop for a, b in zip(evidence, evidence[1:])
                        if b.observed_hop == a.observed_hop + 1)
        spectrum = inspect_spectrum(attack.origin.onset_sample, midi, pair_end)
        if spectrum is None or spectrum.competing:
            return None
        pulse = near[0]
        self.consume(attack.origin.onset_sample, frequency)
        return QuietRepeatEvidence(attack.origin.onset_sample, pulse.sample,
                                   pulse.observed_sample, evidence, spectrum)
