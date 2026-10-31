"""State owned by one onset candidate, independent of later audio candidates.

An aubio onset is a candidate, not proof that a string was plucked again.
Identity and evidence provenance are diagnostic; neither bypasses a gate.
"""

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from pickhero.audio.waveform_prediction import WaveformPrediction
from pickhero.audio.repeats import RepeatEvidence
from pickhero.audio.quiet_repeats import QuietRepeatEvidence, PitchAgreement
from pickhero.audio.growing_pitch import GrowingPitchEvidence


CloseReason = Literal["replaced", "sound_ended", "timeout", "votes_complete", "reset", "flush"]
Decision = Literal["accepted", "insufficient_votes", "unconfirmed"]


@dataclass(frozen=True, slots=True)
class AttackOrigin:
    sequence: int
    opened_hop: int
    onset_sample: float | None
    backdate_hops: float


@dataclass(frozen=True, slots=True)
class AttackEvidence:
    """Where an energy decision came from (hops are one-based)."""

    kind: Literal["level", "bands", "transient", "partials", "decaying_partials"]
    observed_hop: int
    reference_first_hop: int
    reference_last_hop: int
    reference: tuple[float, ...]
    observed: tuple[float, ...]
    bands: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class SupportingOnset:
    native_sample: float
    complex_sample: float
    observed_sample: int
    required_rise_db: float


@dataclass(frozen=True, slots=True)
class QuietStartEvidence:
    onset: SupportingOnset
    reference_start_sample: int
    split_sample: int
    end_sample: int
    reference_power: float
    observed_power: float


@dataclass(frozen=True, slots=True)
class SparsePitchEvidence:
    midi_note: int
    dominant_frequency: float
    confidence: float
    observed_hop: int
    quiet_first_hop: int
    quiet_last_hop: int
    quiet_peak_db: float
    periodicity_votes: tuple[tuple[int, float], ...]


@dataclass(frozen=True, slots=True)
class BassContinuation:
    origin_sample: float
    native_sample: float
    observed_sample: int
    prediction: WaveformPrediction


@dataclass(frozen=True, slots=True)
class TimingAmbiguity:
    """A later native onset precedes all of this candidate's rise evidence."""

    successor_sample: float
    confirmation_start_sample: int
    observed_hop: int


@dataclass(slots=True)
class PendingAttack:
    origin: AttackOrigin
    pending: bool = False
    age_hops: int = 0
    votes: list[int] = field(default_factory=list)
    reference_db: float = -120.0
    band_reference: np.ndarray = field(default_factory=lambda: np.empty(0))
    reference_span: tuple[int, int] = (0, 0)
    confirmed: bool = True
    transient_candidate: bool = False
    periodicity_votes: list[int] = field(default_factory=list)
    bass_pitch_votes: list[tuple[int, float]] = field(default_factory=list)
    evidence: list[AttackEvidence] = field(default_factory=list)
    requested_close_reason: CloseReason | None = None
    close_reason: CloseReason | None = None
    decision: Decision | None = None
    report_outcome: Literal["reported", "reported_repeat", "reported_quiet_repeat", "reported_continuation", "reported_growing_pitch", "growing_pitch_duplicate", "growing_pitch_repeat", "repeat_filtered", "sustain_filtered", "prediction_filtered", "quiet_start_unconfirmed", "quiet_start_duplicate", "continuation_unconfirmed", "continuation_duplicate"] | None = None
    closed_age_hops: float | None = None
    decided_votes: tuple[int, ...] = ()
    pitch_tie_evidence: tuple[PitchAgreement, ...] = ()
    timing_ambiguity: TimingAmbiguity | None = None
    waveform_prediction: WaveformPrediction | None = None
    repeat_evidence: RepeatEvidence | None = None
    quiet_repeat_evidence: QuietRepeatEvidence | None = None
    growing_pitch_evidence: GrowingPitchEvidence | None = None

    def close(self, reason: CloseReason, decision: Decision | None) -> bool:
        """Consume the candidate once, keeping its decision evidence bounded."""
        if self.close_reason is not None:
            return False
        self.close_reason = reason
        self.decision = decision
        self.closed_age_hops = self.age_hops + self.origin.backdate_hops
        self.decided_votes = tuple(self.votes)
        self.votes = []
        self.pending = False
        return True
