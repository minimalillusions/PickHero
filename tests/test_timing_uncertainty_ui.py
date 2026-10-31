"""Carry ambiguous attacks through pitch feedback, results and saved practice."""
from copy import deepcopy

import pygame
import pytest

from pickhero import i18n, practice_advice
from pickhero.audio.detector import DetectedNote
from pickhero.audio.input import TimestampedNote
from pickhero.audio.note_utils import midi_to_freq
from pickhero.config import Config
from pickhero.progress import ProgressTracker, SongRecord
from pickhero.recommendations import generate_recommendations, section_tempo_suggestion
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.scrolling import PlayingScreen
from tests.test_practice_persistence import context
from tests.test_timing_feedback import language, simulated_capture  # noqa: F401
from tests.test_ukulele_pitch_progress import attempt, measurement, passage, BEFORE_UKULELE_GUARD


def played(known=7, unknown=1):
    notes = [NoteEvent(1000 + index * 1500, 100, 28, 4, 0)
             for index in range(known + unknown)]
    screen = PlayingScreen(Timeline(notes, SongMetadata(title="Timing", tempo=80)),
                           config=Config(instrument="bass_ukulele", count_in_beats=0))
    screen._start_audio()
    for index, note in enumerate(notes):
        uncertain = index >= known
        attack = TimestampedNote(DetectedNote(28, midi_to_freq(28), 1, "E1", True,
                                               timing_uncertain=uncertain),
                                 note.timestamp_ms - (300 if uncertain else 0))
        screen._matcher.process_detected_notes([attack], note.timestamp_ms)
    screen._matcher.finish()
    return screen, screen._matcher.get_statistics()


@pytest.fixture(autouse=True)
def fonts():
    pygame.font.init()


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("known", [0, 7])
def test_results_keep_pitch_success_and_show_measured_denominator(language, look, known):
    set_look(look)
    screen, stats = played(known=known)
    assert stats["accuracy_percent"] == 100
    assert screen._timing_advice(stats) == ""
    panel = screen._result_panel()
    assert panel.metrics[0][1] == "100%"
    assert panel.metrics[1][1] == (f"{known} / {known}" if known else "—")
    assert panel.metrics[1][2] == i18n.t("play.timing_unknown_count", count=1)
    assert (i18n.t("play.timing_unknown"), [i18n.t("play.timing_unknown_help")]) in panel.sections
    if not known:
        assert panel.metrics[1][0] == i18n.t("play.timing_unknown")
    for size in ((1280, 720), (640, 480), (360, 280)):
        surface = pygame.Surface(size)
        panel.render(surface)
        assert panel.body.height > 0
        assert all(surface.get_rect().contains(rect) for rect in panel.hit.values())
    screen._timing_assessed = False
    assert screen._result_panel().metrics[1][0] == i18n.t("practice.result.timing_unassessed")


@pytest.mark.parametrize("known", [0, 7])
def test_live_stage_is_neutral_and_hifi_does_not_claim_zero_of_zero(language, known, monkeypatch):
    from pickhero.ui import paint_hifi, paint_stage
    screen, stats = played(known=known)
    surface = pygame.Surface((1280, 720))
    page = screen._page(surface)
    assert page.timing_unknown == 1 and page.timing_measured == known
    assert page.in_time == known
    summary = paint_hifi._score(page)
    assert i18n.t("play.timing_unknown_count", count=1) in summary
    if known:
        assert i18n.t("play.in_time", in_time=known, total=known) in summary
    else:
        assert i18n.t("play.in_time", in_time=0, total=0) not in summary
    set_look("stage")
    page = screen._page(surface)
    page.paused = False
    labels = []
    original = paint_stage._fitted
    def capture(surface, text, position, colour, *args, **kwargs):
        labels.append((text, colour))
        if text == i18n.t("play.timing_unknown"):
            from pickhero.ui import design
            assert design.text_width(text.upper(), args[0], weight="bold", italic=True) <= args[1]
        return original(surface, text, position, colour, *args, **kwargs)
    monkeypatch.setattr(paint_stage, "_fitted", capture)
    paint_stage._gauge(surface, page, get_look())
    assert (i18n.t("play.timing_unknown"), get_look().ink_2) in labels


def test_uncertainty_survives_save_reload_without_rhythm_goal_or_tempo_advice(language):
    screen, stats = played()
    stats.update(reliable=True, timing_assessed=True)
    before = deepcopy(stats)
    tracker = ProgressTracker()
    for _ in range(6):
        tracker.record_detailed_result("song", stats, [], 1)
        assert tracker.record_section_result("song", stats, context()) is None
        tracker = ProgressTracker()
    record = tracker.get_best("song")
    assert record.best_accuracy == 100 and record.attempts == 6
    assert record.goal_pitch_met and not record.goal_rhythm_met
    for entry in record.section_attempts + record.tempo_history:
        assert entry["timing_unknown"] == 1 and entry["timing_measured"] == 7
        assert entry["timing_assessed"] is False
    assert stats == before
    screen._progress_tracker, screen._song_key = tracker, "song"
    lines = screen._history_panel().sections[0][1]
    assert len(lines) == 6
    assert all(i18n.t("play.timing_unknown_count", count=1) in line for line in lines)
    assert all("Timing — ·" in line for line in lines)


@pytest.mark.parametrize("known", [0, 7])
def test_diagnostic_log_keeps_uncertain_attacks_separate(monkeypatch, known):
    screen, stats = played(known=known)
    lines = []
    monkeypatch.setattr("pickhero.ui.scrolling.log_error", lines.append)
    screen._log_result(stats)
    assert len(lines) == 1
    assert f"timing_measured={known} timing_unknown=1" in lines[0]
    assert "0 zu frueh, 0 zu spaet" in lines[0]


@pytest.mark.parametrize("accuracy", [40, 100])
def test_no_practice_tempo_advice_can_be_inferred_from_uncertain_timing(accuracy):
    history = [dict(attempt("a" * 64, accuracy), timing_unknown=1, timing_measured=7)
               for _ in range(6)]
    assert practice_advice.section_advice(history) is None
    assert section_tempo_suggestion(history) is None
    stats = dict(accuracy_percent=accuracy, timing_percent=100, timing_assessed=True,
                 timing_unknown=1, timing_measured=7)
    # Attempt milestones and weak spots are independent of tempo advice.
    record = SongRecord(attempts=4)
    assert generate_recommendations(record, stats, [], .5) == []


BEFORE_TIMING_UKE = {
    ("bass_ukulele", 0, None): "8246d489e893d612bcd06b0ebec02b090ca9ceaa3f9725017b77fc1984099340",
    ("bass_ukulele", 0, 48000): "9c8ce1008386eeafb0b37d6cceb6d1970e66cae7919977eaa3de4085f071cffa",
    ("bass_ukulele", 0, 192000): "3c56c79c504eec45fd63f588f9d7d9345f8ba5eb3e736713d9221f03cc882854",
    ("bass_ukulele", -2, None): "44a08521ccdb496c752cb4dd0ec9af190a10a2623624b6d8a7dd240e0dbb8e84",
    ("bass_ukulele", -2, 48000): "f245deca980537d3bef791728d2551c50a7b7f80a7e49d623078c8072c082c5f",
    ("bass_ukulele", -2, 192000): "636dad9d49fd747e8bc2dac6562d808620a5c5a1b7ca014c26d5b203aca0cb7c",
}


@pytest.mark.parametrize("case", list(BEFORE_UKULELE_GUARD))
def test_historical_timing_comparisons_keep_old_attempts_across_bass_policies(case):
    old = BEFORE_TIMING_UKE.get(case, BEFORE_UKULELE_GUARD[case])
    current = practice_advice.input_fingerprint(*measurement(case))
    assert (old != current) is (case[0] in {"bass_guitar", "bass_ukulele"})
    if case[0] in {"bass_guitar", "bass_ukulele"}:
        history = [attempt(old, 40), attempt(old, 60), attempt(current)]
        before = deepcopy(history)
        assert practice_advice.comparable_attempts(history, passage(), input_key=current) == history[-1:]
        assert practice_advice.comparable_attempts(history, passage(), input_key=old) == history[:2]
        assert practice_advice.section_advice(history) is None
        assert history == before
