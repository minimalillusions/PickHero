"""Visual feedback for note matching.

Keeps the per-note hit/close/miss effects and the streak count. The drawing
happens in scrolling.py, which lays the feedback out with the rest of the HUD;
this module only decides what a note currently looks like.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic as _display_time

from pickhero.matcher import MatchResult, MatchType, ON_PITCH
from pickhero.tabs.timeline import NoteEvent
from pickhero.ui.colors import dimmed, get_look

EFFECT_DURATION_MS = 500.0
LANE_VERDICT_MS = 800.0


def _match_color(match_type: MatchType) -> tuple[int, int, int]:
    """Get the feedback color for a match type from the active theme."""
    t = get_look()
    return {
        MatchType.HIT: t.feedback_hit,
        MatchType.EARLY: t.feedback_hit,
        MatchType.LATE: t.feedback_hit,
        MatchType.CLOSE: t.feedback_close,
        MatchType.MISS: t.feedback_miss,
    }.get(match_type, t.hud_text)


@dataclass
class _FeedbackEffect:
    """A timed visual effect on a note."""
    note_key: tuple[float, int]  # (timestamp_ms, string)
    match_type: MatchType
    start_ms: float
    # How far from the written time it was played. Negative is early, None
    # when nothing was heard.
    timing_error_ms: float | None = None


class FeedbackRenderer:
    """Manages visual feedback effects for matched notes."""

    def __init__(self):
        # Strikes that belonged to no written note, newest last.
        self._strays: list[tuple[float, int | None]] = []
        self._effects: dict[tuple[float, int], _FeedbackEffect] = {}
        self._lane_verdicts: dict[int, tuple[NoteEvent, MatchType, float]] = {}
        self._streak = 0

    @property
    def streak(self) -> int:
        return self._streak

    def add_results(self, results: list[MatchResult], playback_ms: float) -> None:
        """Create visual effects from match results and update streak."""
        for result in results:
            if result.match_type is MatchType.STRAY:
                # Nothing to attach it to, so it is held on its own and drawn
                # at the strike line for a moment.
                self._strays.append((playback_ms, result.heard_midi))
                del self._strays[:-6]
                continue
            for event in result.matched_events:
                key = (event.timestamp_ms, event.string)
                self._effects[key] = _FeedbackEffect(
                    note_key=key,
                    match_type=result.match_type,
                    start_ms=playback_ms,
                    timing_error_ms=result.timing_error_ms,
                )
                # A detector may report after the moving note has already
                # left the road. Keep the newest judgement on each string
                # on a separate real-time clock, independent of song tempo.
                if result.match_type not in (MatchType.PENDING, MatchType.STRAY):
                    now = _display_time()
                    previous = self._lane_verdicts.get(event.string)
                    if (previous is None or (now - previous[2]) * 1000 >= LANE_VERDICT_MS
                            or event.timestamp_ms >= previous[0].timestamp_ms):
                        self._lane_verdicts[event.string] = (event, result.match_type, now)

            # Update streak
            if result.match_type in ON_PITCH:
                self._streak += 1
            elif result.match_type in (MatchType.MISS, MatchType.CLOSE):
                self._streak = 0

    def match_state(self, event: NoteEvent) -> MatchType | None:
        """What happened to this note, or None if it is still pending.

        Callers that draw feedback as a ring rather than as a fill colour need
        the state, not a colour: recolouring the note throws away the string
        identity exactly when the player is looking at it.
        """
        effect = self._effects.get((event.timestamp_ms, event.string))
        return effect.match_type if effect is not None else None

    def recent_lane_verdicts(self) -> list[tuple[NoteEvent, MatchType, float]]:
        """Latest verdict per string and real age, including after note culling."""
        now = _display_time()
        return [(note, state, (now - at) * 1000)
                for note, state, at in self._lane_verdicts.values()
                if 0 <= (now - at) * 1000 < LANE_VERDICT_MS]

    STRAY_MS = 700.0

    def recent_strays(self, playback_ms: float) -> list[tuple[float, int]]:
        """(age in ms, midi) for strikes that matched no written note."""
        return [(playback_ms - at, midi) for at, midi in self._strays
                if midi is not None and 0.0 <= playback_ms - at < self.STRAY_MS]

    def timing_error_ms(self, event: NoteEvent) -> float | None:
        """How far from the written time the player played it. Negative is early."""
        effect = self._effects.get((event.timestamp_ms, event.string))
        return effect.timing_error_ms if effect is not None else None

    def effect_age_ms(self, event: NoteEvent, playback_ms: float) -> float | None:
        """Milliseconds since this note was judged, or None."""
        effect = self._effects.get((event.timestamp_ms, event.string))
        if effect is None:
            return None
        return playback_ms - effect.start_ms

    def get_note_color(
        self,
        event: NoteEvent,
        base_color: tuple[int, int, int],
        playback_ms: float,
        is_past: bool,
    ) -> tuple[int, int, int]:
        """Get the display color for a note event."""
        key = (event.timestamp_ms, event.string)
        effect = self._effects.get(key)
        if effect is not None:
            elapsed = playback_ms - effect.start_ms
            color = _match_color(effect.match_type)
            if elapsed <= EFFECT_DURATION_MS:
                return color
            # Effect expired but state is permanent — still show color (dimmed)
            return dimmed(color, 0.6)

        return dimmed(base_color) if is_past else base_color

    def cleanup(self, playback_ms: float) -> None:
        """Remove expired effects."""
        expired = [
            key for key, eff in self._effects.items()
            if playback_ms - eff.start_ms > EFFECT_DURATION_MS * 2
        ]
        for key in expired:
            del self._effects[key]

    def reset(self) -> None:
        """Clear all effects and streak."""
        self._effects.clear()
        self._lane_verdicts.clear()
        self._strays.clear()
        self._streak = 0
