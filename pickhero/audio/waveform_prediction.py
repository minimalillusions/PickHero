"""Bounded causal waveform prediction for an already ambiguous onset.

This is deliberately a veto of an exceptionally predictable continuation,
not a pitch estimator or a detector of every physical attack. Failure to
fit or missing history means no decision. The evaluated interval is never
used to train the linear recurrence or to initialize its forecast.
"""
from dataclasses import dataclass
import math
import numpy as np


@dataclass(frozen=True, slots=True)
class WaveformPrediction:
    train_start_sample: int
    train_end_sample: int
    end_sample: int
    stride: int
    rank: int
    pole_radius: float
    fit_error: float
    prediction_error: float
    peak_error: float
    reference_power: float
    observed_power: float
    component_frequencies: tuple[float, ...] = ()

    def supports_pitch(self, frequency: float) -> bool:
        """A forecast of another pitch cannot certify this tone's continuity."""
        return (math.isfinite(frequency) and frequency > 0
                and any(abs(1200 * math.log2(component / frequency)) <= 35
                        for component in self.component_frequencies if component > 0))

    @property
    def is_continuation(self) -> bool:
        return (self.fit_error < 1e-5 and self.prediction_error < 1e-3
                and self.peak_error < 3e-3 and self.pole_radius <= 1.001
                and self.reference_power > 1e-8 and self.observed_power > 1e-8)


class WaveformHistory:
    """Half a second of PCM, regardless of the number of processed blocks."""
    def __init__(self, sample_rate: int, hop_size: int):
        self.sample_rate = sample_rate
        self.data = np.zeros(math.ceil(sample_rate * .5) + hop_size, np.float32)
        self.reset()

    def reset(self) -> None:
        self.end_sample = self.filled = self.cursor = 0
        self.data.fill(0)

    def append(self, block: np.ndarray, end_sample: int) -> None:
        if block.ndim != 1 or not np.isfinite(block).all():
            self.reset()
            return
        length = len(block)
        if length == 0:
            return
        if end_sample - length != self.end_sample:
            self.reset()
        if length >= len(self.data):
            self.data[:] = block[-len(self.data):]
            self.cursor = 0
            self.filled = len(self.data)
        else:
            first = min(length, len(self.data) - self.cursor)
            self.data[self.cursor:self.cursor + first] = block[:first]
            self.data[:length - first] = block[first:]
            self.cursor = (self.cursor + length) % len(self.data)
            self.filled = min(len(self.data), self.filled + length)
        self.end_sample = end_sample

    def _read(self, start: int, stop: int) -> np.ndarray | None:
        if start < self.end_sample - self.filled or stop > self.end_sample or stop <= start:
            return None
        first = (self.cursor - (self.end_sample - start)) % len(self.data)
        count = stop - start
        if first + count <= len(self.data):
            return self.data[first:first + count].copy()
        return np.concatenate((self.data[first:], self.data[:count - (len(self.data) - first)]))

    def predict(self, native_sample: float, successor_sample: float) -> WaveformPrediction | None:
        rate = self.sample_rate
        if not math.isfinite(native_sample) or not math.isfinite(successor_sample):
            return None
        start = native_sample / rate
        begin = round((start - .14) * rate)
        split = round((start - .02) * rate)
        stop = int(successor_sample)
        if begin < 0 or stop - split < rate * .02:
            return None
        return self._predict_interval(begin, split, stop)

    def predict_continuation(self, origin_sample: float, native_sample: float,
                             end_sample: int) -> WaveformPrediction | None:
        """Forecast a settling pluck across a later native onset candidate.

        Train after the first leading edge, leaving a guard before the later
        candidate. The forecast must explain all audio through the current
        block. It never trains on that later candidate or extends the first
        candidate's lifetime. Missing settled history means no decision.
        """
        if (not all(math.isfinite(value) for value in (origin_sample, native_sample, end_sample))
                or not 0 <= origin_sample < native_sample <= end_sample):
            return None
        begin = round(origin_sample + .02 * self.sample_rate)
        split = round(native_sample - .01 * self.sample_rate)
        if split <= begin:
            return None
        return self._predict_interval(begin, split, int(end_sample), minimum_target=16)

    def _predict_interval(self, begin: int, split: int, stop: int,
                          minimum_target: int = 1) -> WaveformPrediction | None:
        rate = self.sample_rate
        raw = self._read(begin, stop)
        if raw is None:
            return None
        stride = max(1, round(rate / 2000))
        count = len(raw) // stride
        x = raw[:count * stride].reshape(count, stride).mean(axis=1).astype(np.float64)
        cut = (split - begin) // stride
        train, target = x[:cut], x[cut:]
        order = 24
        if len(train) < order * 3 or len(target) < minimum_target:
            return None
        windows = np.lib.stride_tricks.sliding_window_view(train, order + 1)
        a, b = windows[:, :-1][:, ::-1], windows[:, -1]
        try:
            coef, _, rank, _ = np.linalg.lstsq(a, b, rcond=1e-6)
            roots = np.roots(np.r_[1., -coef])
        except np.linalg.LinAlgError:
            return None
        if not np.isfinite(coef).all() or not np.isfinite(roots).all():
            return None
        history = np.r_[train[-order:], np.zeros(len(target))]
        with np.errstate(over="ignore", invalid="ignore"):
            for index in range(len(target)):
                history[index + order] = np.dot(coef, history[index:index + order][::-1])
            residual = target - history[order:]
            energy, before = float(np.mean(target**2)), float(np.mean(b**2))
            fit = float(np.mean((a @ coef - b)**2) / max(before, 1e-20))
            error = float(np.mean(residual**2) / max(energy, 1e-20))
            peak = max(float(np.mean(residual[i:i + 16]**2))
                       for i in range(0, len(target), 16)) / max(before, energy, 1e-20)
        radius = float(max(abs(roots)))
        if not all(math.isfinite(v) for v in (energy, before, fit, error, peak, radius)):
            return None
        frequencies = np.angle(roots) * rate / (2 * np.pi * stride)
        components = tuple(float(f) for f in frequencies if f > 0)
        return WaveformPrediction(begin, split, stop, stride, int(rank), radius,
                                  fit, error, peak, before, energy, components)
