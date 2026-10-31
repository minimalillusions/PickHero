"""The lines shown the moment a song ends.

This module had no tests at all, and it runs at every completion: a crash
here lands exactly where the player is waiting to find out how it went.
"""

import pytest

from pickhero.i18n import t
from pickhero.progress import SongRecord
from pickhero.recommendations import generate_recommendations


def _stats(accuracy: float = 80.0) -> dict:
    return {"hits": int(accuracy), "close": 0, "misses": 100 - int(accuracy),
            "total": 100, "accuracy_percent": accuracy}


class TestRecommendations:
    def test_at_most_three_lines(self):
        record = SongRecord(attempts=5)
        record.tempo_history = [{"attempt": i, "tempo_factor": 0.5,
                                 "accuracy": 40.0} for i in range(1, 6)]
        record.section_history = [
            {"attempt": i, "sections": [{"measures": [2, 4], "accuracy": 30.0}]}
            for i in range(1, 6)
        ]
        recs = generate_recommendations(record, _stats(95.0),
                                        [(2, 4, 30.0)], 0.5)
        assert len(recs) <= 3

    def test_the_first_attempt_gets_its_own_line(self):
        recs = generate_recommendations(SongRecord(attempts=1), _stats(), [], 1.0)
        assert any("Erster Versuch" in r for r in recs)

    def test_everything_is_german(self):
        """The rest of the app speaks German. These strings go on the same
        screen as the score."""
        english = ("attempt", "Bars", "loop", "speed", "accuracy", "nice work",
                   "Plateau detected", "well done", "Try ")
        for attempts in (1, 2, 5, 10, 25, 40):
            record = SongRecord(attempts=attempts)
            record.tempo_history = [{"attempt": i, "tempo_factor": 0.6,
                                     "accuracy": 50.0} for i in range(1, attempts + 1)]
            record.section_history = [
                {"attempt": i, "sections": [{"measures": [1, 3], "accuracy": 20.0}]}
                for i in range(1, attempts + 1)
            ]
            for accuracy in (10.0, 55.0, 95.0):
                for tempo in (0.25, 0.5, 0.9, 1.0):
                    for rec in generate_recommendations(
                            record, _stats(accuracy), [(1, 3, 20.0)], tempo):
                        for word in english:
                            assert word not in rec, f"englisch: {rec!r}"

    def test_the_lines_stay_short_enough_to_fit(self):
        record = SongRecord(attempts=25)
        record.tempo_history = [{"attempt": i, "tempo_factor": 0.55,
                                 "accuracy": 95.0} for i in range(1, 26)]
        for accuracy in (5.0, 65.0, 99.0):
            for rec in generate_recommendations(record, _stats(accuracy),
                                                [(10, 14, 12.0)], 0.55):
                assert len(rec) <= 70, f"{len(rec)} Zeichen: {rec!r}"


class TestBadHistory:
    """The history is a JSON file. It can be anything."""

    def test_a_history_entry_without_an_accuracy_does_not_crash(self):
        record = SongRecord(attempts=4)
        record.tempo_history = [{"attempt": 1, "tempo_factor": 1.0},
                                {"attempt": 2, "tempo_factor": 1.0},
                                {"attempt": 3, "accuracy": 50.0},
                                {"attempt": 4, "accuracy": 60.0}]
        assert isinstance(
            generate_recommendations(record, _stats(70.0), [], 1.0), list)

    def test_a_section_without_measures_does_not_crash(self):
        record = SongRecord(attempts=6)
        record.section_history = [{"attempt": i, "sections": [{"accuracy": 10.0}]}
                                  for i in range(1, 7)]
        assert isinstance(
            generate_recommendations(record, _stats(40.0), [(1, 2, 10.0)], 1.0),
            list)

    def test_no_history_at_all_is_fine(self):
        assert isinstance(
            generate_recommendations(SongRecord(), _stats(), [], 1.0), list)

    def test_statistics_without_an_accuracy_key(self):
        assert isinstance(
            generate_recommendations(SongRecord(attempts=3), {}, [], 1.0), list)


class TestTempoNeedsMeasuredTiming:
    @pytest.mark.parametrize("tempo", [0.25, 0.5, 0.95, 1.0])
    @pytest.mark.parametrize("timing", [None, 0.0, 89.0, float("nan"), 101.0, "100", True])
    def test_pitch_alone_never_recommends_speed_or_clean_full_speed(self, tempo, timing):
        stats = _stats(100.0)
        if timing is not None:
            stats["timing_percent"] = timing
        recs = generate_recommendations(SongRecord(attempts=2), stats, [], tempo)
        assert recs == [t("rec.practice_timing")]

    @pytest.mark.parametrize("tempo", [0.5, 1.0])
    def test_wait_mode_retains_pitch_encouragement_without_rhythm_claim(self, tempo):
        stats = {**_stats(100.0), "timing_percent": 100.0, "timing_assessed": False}
        recs = generate_recommendations(SongRecord(attempts=2), stats, [], tempo)
        assert recs == [t("rec.practice_without_wait")]

    @pytest.mark.parametrize("tempo", [0.25, 0.5, 0.95, 1.0])
    def test_measured_pitch_and_timing_can_recommend_faster(self, tempo):
        stats = {**_stats(95.0), "timing_percent": 90.0}
        recs = generate_recommendations(SongRecord(attempts=2), stats, [], tempo)
        expected = (t("rec.full_speed_clean") if tempo == 1.0 else
                    t("rec.speed_up", n=round(tempo * 100), target=round(tempo * 100) + 5))
        assert recs == [expected]


class TestSlowingBelowHalfSpeed:
    @pytest.mark.parametrize("tempo, target", [(0.5, 45), (0.4, 35), (0.3, 25)])
    def test_low_accuracy_can_slow_to_quarter_speed(self, tempo, target):
        recs = generate_recommendations(SongRecord(attempts=2), _stats(20), [], tempo)
        assert recs == [t("rec.slow_down", n=target)]

    def test_no_unavailable_speed_below_quarter_speed(self):
        recs = generate_recommendations(SongRecord(attempts=2), _stats(20), [], 0.25)
        assert recs == []

    @pytest.mark.parametrize("tempo, target", [(0.5, 40), (0.35, 25), (0.3, 25)])
    def test_persistent_weak_section_can_slow_to_quarter_speed(self, tempo, target):
        record = SongRecord(attempts=3)
        record.section_history = [
            {"sections": [{"measures": [1, 2], "accuracy": 20.0}]} for _ in range(3)
        ]
        recs = generate_recommendations(record, _stats(70), [(1, 2, 20)], tempo)
        assert recs == [t("rec.section_stuck_slow", start=2, end=3, n=target)]

    def test_persistent_section_at_minimum_recommends_loop_without_lower_speed(self):
        record = SongRecord(attempts=3)
        record.section_history = [
            {"sections": [{"measures": [1, 2], "accuracy": 20.0}]} for _ in range(3)
        ]
        recs = generate_recommendations(record, _stats(70), [(1, 2, 20)], 0.25)
        assert recs == [t("rec.section_stuck", start=2, end=3)]
