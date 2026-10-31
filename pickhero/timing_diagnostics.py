"""Bounded local timing observations, separate from musical scoring.

The audio callback may record a value while the UI takes a snapshot. Only
copying the bounded numeric buffers happens under the lock; formatting and
percentiles run after releasing it. No audio, paths or free-form user/device
text is retained, and no observation changes an offset or a detector setting.

Software clocks and driver metadata cannot measure the physical input,
output or display delay. Those limits remain explicit in every snapshot.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from numbers import Real
from threading import Lock
from typing import Mapping


MAX_SAMPLES = 512
DEFAULT_SAMPLES = 256
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class _Metric:
    unit: str
    clock_domain: str
    evidence: str
    signed: bool = False


# Fixed names also put a ceiling on the number of retained series. Durations
# are elapsed real milliseconds even when the song plays at half speed.
_METRICS = {
    "callback_processing_ms": _Metric("ms", "wall", "software_measurement"),
    "callback_budget_ms": _Metric("ms", "wall", "sample_duration"),
    "capture_delivery_ms": _Metric("ms", "wall", "clock_observation", signed=True),
    "onset_correction_ms": _Metric("ms", "wall", "algorithm_correction"),
    "note_report_lag_ms": _Metric("ms", "wall", "clock_observation", signed=True),
    "queue_wait_ms": _Metric("ms", "wall", "software_measurement"),
    "frame_interval_ms": _Metric("ms", "wall", "software_measurement"),
    "frame_processing_ms": _Metric("ms", "wall", "software_measurement"),
    "timing_residual_song_ms": _Metric("ms", "song", "performance_residual", signed=True),
    "timing_residual_wall_ms": _Metric("ms", "wall", "performance_residual", signed=True),
    "pitch_error_cents": _Metric("cents", "pitch", "performance_residual", signed=True),
}
METRIC_NAMES = tuple(_METRICS)

_CLOCK_SOURCES = ("not_started", "adc", "fallback")
_NUMERIC_CONTEXT = frozenset({
    "sample_rate", "hop_size", "device_index", "noise_gate_db",
    "input_latency_ms", "driver_input_latency_ms", "driver_output_latency_ms",
    "callback_frames_min", "callback_frames_max", "callback_frames_last",
    "stream_start_duration_ms", "stream_activation_duration_ms", "dropped_blocks",
    "tempo_factor", "timing_window_song_ms", "timing_window_wall_ms",
    "manual_offset_ms", "onset_latency_ms", "analysis_window_ms", "target_fps",
})
_ENUM_CONTEXT = {
    "look": frozenset({"studio", "stage", "hifi"}),
    "clock_source": frozenset(_CLOCK_SOURCES),
    "instrument": frozenset({"guitar", "bass_guitar", "bass_ukulele"}),
    "health_issue": frozenset({"input_disconnected", "input_stalled", "input_overflow",
                               "input_clipping"}),
}


def _number(value: object) -> float | None:
    """Accept actual finite scalars, never strings, arrays or arbitrary objects."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    # Prevent overflow in aggregate arithmetic and reject corrupt timestamps.
    # A trillion milliseconds is over 31 years, far beyond a session sample.
    return numeric if math.isfinite(numeric) and abs(numeric) <= 1e12 else None


def _context(values: Mapping | None) -> dict:
    """Copy allowlisted scalars only, without traversing caller-owned data."""
    result = {}
    if not isinstance(values, Mapping):
        return result
    for key in _NUMERIC_CONTEXT:
        if key not in values:
            continue
        value = values[key]
        numeric = _number(value)
        if value is None or numeric is not None:
            result[key] = numeric
    for key, allowed in _ENUM_CONTEXT.items():
        if key not in values:
            continue
        value = values[key]
        if value is None or (isinstance(value, str) and value in allowed):
            result[key] = value
    return result


def _percentile(ordered: list[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _summary(values: tuple[float, ...], count: int, spec: _Metric) -> dict:
    result = {
        "count": count,
        "window_count": len(values),
        "unit": spec.unit,
        "clock_domain": spec.clock_domain,
        "evidence": spec.evidence,
        "last": values[-1] if values else None,
        "min": None, "max": None, "mean": None, "median": None, "p95": None,
    }
    if values:
        ordered = sorted(values)
        result.update(min=ordered[0], max=ordered[-1],
                      mean=math.fsum(values) / len(values),
                      median=_percentile(ordered, 0.5), p95=_percentile(ordered, 0.95))
    return result


class TimingDiagnostics:
    """Small thread-safe numeric recorder with explicit measurement limits.

    ``record`` is constant work and accepts only the fixed ``METRIC_NAMES``.
    ``snapshot`` is intended for diagnostics screens/exports, not every frame.
    Count is lifetime since ``reset``; statistics cover the most recent
    ``sample_capacity`` accepted values for each metric. Signed residuals
    describe a player's observations; they are not latency calibration.
    """

    def __init__(self, capacity: int = DEFAULT_SAMPLES):
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise TypeError("capacity must be an integer")
        if not 1 <= capacity <= MAX_SAMPLES:
            raise ValueError(f"capacity must be between 1 and {MAX_SAMPLES}")
        self._capacity = capacity
        self._lock = Lock()
        self._values = {name: deque(maxlen=capacity) for name in METRIC_NAMES}
        self._counts = {name: 0 for name in METRIC_NAMES}
        self._clock_counts = {name: 0 for name in _CLOCK_SOURCES}
        self._clock_last = "not_started"
        self._rejected = 0

    def record(self, metric: str, value: float) -> bool:
        """Record one finite scalar, returning False for invalid observations."""
        spec = _METRICS.get(metric) if isinstance(metric, str) else None
        numeric = _number(value)
        if spec is None or numeric is None or (not spec.signed and numeric < 0):
            with self._lock:
                self._rejected += 1
            return False
        with self._lock:
            self._values[metric].append(numeric)
            self._counts[metric] += 1
        return True

    def record_clock(self, source: str) -> bool:
        """Count the source used for a block, retaining mixed ADC/fallback runs."""
        if not isinstance(source, str) or source not in _CLOCK_SOURCES:
            return False
        with self._lock:
            self._clock_counts[source] += 1
            self._clock_last = source
        return True

    def reset(self) -> None:
        """Start a fresh observation window without changing any configuration."""
        with self._lock:
            for name in METRIC_NAMES:
                self._values[name].clear()
                self._counts[name] = 0
            for name in _CLOCK_SOURCES:
                self._clock_counts[name] = 0
            self._clock_last = "not_started"
            self._rejected = 0

    def snapshot(self, context: Mapping | None = None) -> dict:
        """Return independent JSON-safe aggregates, with no raw sample arrays."""
        with self._lock:
            copies = {name: (tuple(values), self._counts[name])
                      for name, values in self._values.items()}
            clock = {"last": self._clock_last, "counts": dict(self._clock_counts)}
            rejected = self._rejected
        return {
            "schema_version": SCHEMA_VERSION,
            "sample_capacity": self._capacity,
            "statistics_scope": "most_recent_samples",
            "metrics": {name: _summary(values, count, _METRICS[name])
                        for name, (values, count) in copies.items()},
            "clock": clock,
            "rejected_samples": rejected,
            "context": _context(context),
            "unmeasured": ["physical_input_latency", "physical_output_latency",
                           "display_latency", "roundtrip_latency"],
        }
