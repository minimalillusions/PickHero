"""Piecewise constant musical tempo, with indexed tick/time conversion."""

import bisect

from pickhero.tabs.import_safety import MAX_DURATION_MS, MAX_WORK, finite_range, tempo


class TempoMap:
    def __init__(self, initial_tempo: float, ticks_per_quarter: int = 960):
        self.ticks_per_quarter = finite_range(ticks_per_quarter, 1, 32767, "tick resolution")
        self._changes = {0.0: tempo(initial_tempo)}
        self._dirty = True

    def add_change(self, tick: float, bpm: float) -> None:
        tick = finite_range(tick, 0, MAX_DURATION_MS * self.ticks_per_quarter, "tempo tick")
        self._changes[tick] = tempo(bpm)
        if len(self._changes) > MAX_WORK:
            raise ValueError("Too many tempo changes")
        self._dirty = True

    def _index(self):
        if not self._dirty:
            return
        self._ticks = sorted(self._changes)
        self._bpms = [self._changes[t] for t in self._ticks]
        self._times = [0.0]
        for index in range(1, len(self._ticks)):
            self._times.append(self._times[-1] +
                               (self._ticks[index] - self._ticks[index - 1]) /
                               self.ticks_per_quarter * 60_000 / self._bpms[index - 1])
        self._dirty = False

    def tick_to_ms(self, tick: float) -> float:
        self._index()
        finite_range(tick, 0, MAX_DURATION_MS * self.ticks_per_quarter, "tick")
        index = max(0, bisect.bisect_right(self._ticks, tick) - 1)
        value = self._times[index] + (tick - self._ticks[index]) / self.ticks_per_quarter * 60_000 / self._bpms[index]
        return finite_range(value, 0, MAX_DURATION_MS, "song time")

    def tempo_at_tick(self, tick: float) -> float:
        self._index()
        return self._bpms[max(0, bisect.bisect_right(self._ticks, tick) - 1)]

    def duration_ticks_to_ms(self, ticks: float, at_tick: float) -> float:
        finite_range(ticks, 0, MAX_DURATION_MS * self.ticks_per_quarter, "duration ticks")
        return self.tick_to_ms(at_tick + ticks) - self.tick_to_ms(at_tick)

    @property
    def changes_ms(self) -> list[tuple[float, float]]:
        self._index()
        return list(zip(self._times, self._bpms))
