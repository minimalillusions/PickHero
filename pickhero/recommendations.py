"""What to practise next, from the history of past attempts.

Shown on the completion screen. Four things are looked at -- how many
attempts there have been, whether this one was better than the last few,
whether the tempo should move, and which bars keep failing -- and at most
three lines come back. They are short on purpose: this is the moment the
player finds out how it went, not a place for a report.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from pickhero.i18n import t

if TYPE_CHECKING:
    from pickhero.progress import SongRecord

# Maximum number of recommendations to return
_MAX_RECOMMENDATIONS = 3

# Thresholds
_WEAK_SECTION_ACCURACY = 70.0   # below this = weak
_TEMPO_UP_ACCURACY = 90.0       # above this at current tempo = suggest faster
_TEMPO_UP_TIMING = 90.0         # finding the notes alone does not prove fluency
_TEMPO_DOWN_ACCURACY = 60.0     # below this = suggest slower
_TEMPO_MIN = 0.25              # the player's supported minimum speed
_PLATEAU_THRESHOLD = 2.0        # accuracy change < this across 3 attempts = plateau


def section_tempo_suggestion(history: list[dict]) -> float | None:
    """Three consecutive comparable, measured loops unlock a voluntary +5%."""
    recent = history[-3:]
    if len(recent) != 3:
        return None
    first = recent[0]
    comparable = ("start_ms", "end_ms", "tempo_factor", "filter_key", "total")
    if any(any(entry.get(key) != first.get(key) for key in comparable) for entry in recent):
        return None
    for entry in recent:
        if entry.get("timing_unknown", 0):
            return None
        if not all(entry.get(flag) is True for flag in
                   ("reliable", "timing_assessed", "uniform_tempo", "complete_loop")):
            return None
        if entry.get("total", 0) < 4:
            return None
        for metric in ("accuracy", "timing_percent"):
            value = entry.get(metric)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 90 <= value <= 100):
                return None
    tempo = first.get("tempo_factor", 1)
    return round(min(1.0, tempo + .05), 2) if .25 <= tempo < 1 else None


def generate_recommendations(
    record: SongRecord,
    current_stats: dict,
    weakest_sections: list[tuple[int, int, float]],
    tempo_factor: float,
) -> list[str]:
    """Generate practice recommendations based on progress history.

    Args:
        record: The SongRecord with full history (already updated with
                this attempt's data).
        current_stats: Dict from matcher.get_statistics() for this attempt.
        weakest_sections: List of (start_measure, end_measure, accuracy_pct)
                          from matcher.get_weakest_sections().
        tempo_factor: Current tempo scaling factor (0.25 to 1.0).

    Returns:
        List of short recommendation strings (max 3 items, ~60 chars each).
    """
    recs: list[str] = []
    accuracy = current_stats.get("accuracy_percent", 0.0)
    attempts = record.attempts

    # 1. Attempt milestones
    milestone = _attempt_milestone(attempts)
    if milestone:
        recs.append(milestone)

    # 2. Improvement tracking (compare to recent attempts)
    improvement = _improvement_recommendation(record, accuracy)
    if improvement:
        recs.append(improvement)

    # 3. Tempo progression
    tempo_rec = _tempo_recommendation(
        record, accuracy, tempo_factor,
        timing_percent=current_stats.get("timing_percent"),
        timing_assessed=current_stats.get("timing_assessed", True),
        timing_unknown=current_stats.get("timing_unknown", 0),
    )
    if tempo_rec:
        recs.append(tempo_rec)

    # 4. Weakest section focus
    section_rec = _section_recommendation(record, weakest_sections, tempo_factor)
    if section_rec:
        recs.append(section_rec)

    # Trim to max and return
    return recs[:_MAX_RECOMMENDATIONS]


def _attempt_milestone(attempts: int) -> str | None:
    """Return a milestone message for notable attempt counts."""
    if attempts == 1:
        return t("rec.first_attempt")
    if attempts == 5:
        return t("rec.fifth_attempt")
    if attempts == 10:
        return t("rec.tenth_attempt")
    if attempts == 25:
        return t("rec.attempt_25")
    if attempts > 0 and attempts % 50 == 0:
        return t("rec.attempt_milestone", n=attempts)
    return None


def _improvement_recommendation(
    record: SongRecord, current_accuracy: float,
) -> str | None:
    """Compare current accuracy to recent history."""
    history = record.tempo_history
    if len(history) < 2:
        return None

    # Get accuracy from recent attempts (exclude the current one which is
    # already appended to history by record_detailed_result)
    recent = history[-4:-1] if len(history) > 3 else history[:-1]
    if not recent:
        return None

    recent_avg = sum(float(h.get("accuracy", 0.0)) for h in recent) / len(recent)
    diff = current_accuracy - recent_avg

    if diff > 5.0:
        return t("rec.much_better", n=f"{diff:.0f}")
    if diff > 0:
        return t("rec.better", n=f"{diff:.1f}")

    # Check for plateau (last 3+ attempts within threshold)
    if len(recent) >= 2:
        accuracies = [float(h.get("accuracy", 0.0)) for h in recent] + [current_accuracy]
        spread = max(accuracies) - min(accuracies)
        if spread < _PLATEAU_THRESHOLD and current_accuracy < 95.0:
            return t("rec.plateau")

    if diff < -5.0:
        return t("rec.worse")

    return None


def _tempo_recommendation(
    record: SongRecord, accuracy: float, tempo_factor: float,
    timing_percent: float | None = None, timing_assessed: bool = True,
    timing_unknown: int = 0,
) -> str | None:
    """Increase speed only when both pitch and measured timing are secure."""
    if timing_unknown:
        return None
    pct = int(round(tempo_factor * 100))
    timing_ready = (timing_assessed and isinstance(timing_percent, (int, float))
                    and not isinstance(timing_percent, bool)
                    and math.isfinite(timing_percent)
                    and _TEMPO_UP_TIMING <= timing_percent <= 100.0)

    if accuracy >= _TEMPO_UP_ACCURACY and not timing_ready:
        return t("rec.practice_timing" if timing_assessed else "rec.practice_without_wait")

    if tempo_factor >= 1.0:
        # Already at full speed
        if accuracy >= _TEMPO_UP_ACCURACY:
            return t("rec.full_speed_clean")
        if accuracy < _TEMPO_DOWN_ACCURACY:
            return t("rec.slow_down_from_full", n=pct - 10)
        return None

    if accuracy >= _TEMPO_UP_ACCURACY:
        # Doing great at reduced tempo -- suggest increase
        new_pct = min(100, pct + 5)
        return t("rec.speed_up", n=pct, target=new_pct)

    if accuracy < _TEMPO_DOWN_ACCURACY and tempo_factor > _TEMPO_MIN:
        new_pct = max(int(_TEMPO_MIN * 100), pct - 5)
        return t("rec.slow_down", n=new_pct)

    return None


def _section_recommendation(
    record: SongRecord,
    weakest_sections: list[tuple[int, int, float]],
    tempo_factor: float,
) -> str | None:
    """Suggest looping weak sections, especially persistent ones."""
    if not weakest_sections:
        return None

    worst = weakest_sections[0]
    start, end, acc = worst[0], worst[1], worst[2]

    # Check if this section has been consistently weak
    persistent = _is_persistent_weakness(record, start, end)
    pct = int(round(tempo_factor * 100))

    if persistent and acc < _WEAK_SECTION_ACCURACY:
        # Two whole sentences rather than one with a clause glued on: the
        # tail moves to a different place in other languages.
        if tempo_factor > _TEMPO_MIN:
            return t("rec.section_stuck_slow", start=start + 1, end=end + 1,
                     n=max(int(_TEMPO_MIN * 100), pct - 10))
        return t("rec.section_stuck", start=start + 1, end=end + 1)

    if acc < _WEAK_SECTION_ACCURACY:
        return t("rec.section_loop", start=start + 1, end=end + 1,
                 n=f"{acc:.0f}")

    return None


def _is_persistent_weakness(
    record: SongRecord, start_measure: int, end_measure: int,
) -> bool:
    """Check if a section has been weak across the last 3 attempts."""
    history = record.section_history
    if len(history) < 3:
        return False

    recent = history[-3:]
    weak_count = 0
    for entry in recent:
        for section in entry.get("sections", []):
            measures = section.get("measures", [])
            if len(measures) == 2:
                s, e = measures[0], measures[1]
                # Overlapping range check
                if s <= end_measure and e >= start_measure:
                    if section.get("accuracy", 100.0) < _WEAK_SECTION_ACCURACY:
                        weak_count += 1
                        break
    return weak_count >= 3
