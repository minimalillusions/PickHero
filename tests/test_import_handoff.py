"""Large result preparation stays cancellable and outside the frame loop."""
import json
import threading
import time
from unittest.mock import Mock

import pygame
import pytest

from pickhero import import_jobs, library
from pickhero.config import Config
from pickhero.import_worker import (
    MAX_PACKET_BYTES,
    PACKET_ROWS,
    DecodeCancelled,
    encode_song,
    read_result,
    write_result,
)
from pickhero.progress import ProgressTracker, song_key
from pickhero.score_preparation import prepare_score
from pickhero.tabs.timeline import (
    BeatInfo,
    MeasureInfo,
    NoteEvent,
    SongMetadata,
    Timeline,
)
from pickhero.ui.colors import set_look
from pickhero.ui.menu import MenuScreen


def score(count=300):
    return Timeline([NoteEvent(index * 125., 100., 28, 4, 0) for index in range(count)],
                    SongMetadata(title="Low bass", tuning={1: 43, 2: 38, 3: 33, 4: 28}),
                    [MeasureInfo(0, 0, count * 125.)], [BeatInfo(0)])


def test_packet_roundtrip_preserves_score_and_respects_parse_boundaries(tmp_path):
    original = score()
    path = tmp_path / "result.jsonl"
    write_result(path, {"song": encode_song(original), "content_id": "a" * 64})
    packets = path.read_bytes().splitlines(keepends=True)
    assert len(packets) > 4
    assert all(len(line) <= MAX_PACKET_BYTES for line in packets)
    value, (timeline, backing) = read_result(path)
    assert value == {"content_id": "a" * 64}
    assert timeline.notes == original.notes
    assert timeline.metadata == original.metadata
    assert timeline.measures == original.measures
    assert timeline._beats == original._beats
    assert len(backing) == 0


def test_empty_arrays_and_long_library_rows_have_bounded_packets(tmp_path):
    path = tmp_path / "result.jsonl"
    rows = [{"title": str(index) + "x" * 12000} for index in range(128)]
    write_result(path, {"songs": rows})
    assert all(len(line) <= MAX_PACKET_BYTES for line in path.read_bytes().splitlines(keepends=True))
    assert read_result(path) == ({"songs": rows}, None)
    write_result(path, {"songs": []})
    assert read_result(path) == ({"songs": []}, None)


@pytest.mark.parametrize("change", ["truncated", "trailing", "count", "unknown", "too_many", "oversized", "invalid_note"])
def test_packet_protocol_rejects_partial_invalid_and_oversized_results(tmp_path, change):
    path = tmp_path / "result.jsonl"
    write_result(path, {"song": encode_song(score())})
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if change == "truncated":
        lines.pop()
    elif change == "trailing":
        lines.append({})
    elif change == "count":
        lines[-1]["counts"]["notes"] += 1
    elif change == "unknown":
        lines[1]["field"] = "__dict__"
    elif change == "too_many":
        lines[1]["rows"] *= PACKET_ROWS
    elif change == "oversized":
        lines[0]["value"]["excess"] = "a" * MAX_PACKET_BYTES
    else:
        lines[1]["rows"][0]["timestamp_ms"] = -1
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    with pytest.raises((ValueError, TypeError)):
        read_result(path)


def test_decode_can_be_cancelled_between_packets(tmp_path):
    path = tmp_path / "result.jsonl"
    write_result(path, {"song": encode_song(score(1000))})
    cancelled = threading.Event()
    with pytest.raises(DecodeCancelled):
        read_result(path, cancelled=cancelled.is_set, report=lambda _: cancelled.set())


def test_prepared_layout_matches_distinct_onsets_for_chords_and_duplicates():
    from pickhero.ui.scrolling import PlayingScreen
    timeline = Timeline([NoteEvent(at, 100, 40, string, 0) for at, string in
                         [(500, 1), (0, 1), (0, 1), (0, 2), (125, 2), (500, 2), (1000, 1)]])
    neighbours, minimum = prepare_score(timeline)
    assert neighbours == PlayingScreen._neighbour_times(timeline.notes)
    assert minimum == 125


def test_layout_preparation_cancellation_never_publishes_partial_cache():
    timeline = score(10000)
    with pytest.raises(InterruptedError):
        prepare_score(timeline, cancelled=lambda: True)
    assert not hasattr(timeline, "_playback_preparation")


@pytest.fixture
def job_factory(monkeypatch, tmp_path):
    class Process:
        pid = 123
        running = True
        def poll(self):
            return None if self.running else 0
        def kill(self):
            self.running = False
        def wait(self, timeout):
            self.running = False
    process = Process()
    monkeypatch.setattr(import_jobs.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(import_jobs, "_WindowsJob", lambda *_a: Mock())
    monkeypatch.setattr(import_jobs, "_resume_windows_process", lambda *_a: None)
    job = import_jobs.ImportJob("load", tmp_path / "score.gp5", "bass_ukulele")
    yield job, process
    job.cancel()


def test_poll_does_not_read_or_decode_result_on_main_thread(job_factory, monkeypatch):
    job, process = job_factory
    write_result(job.directory / "result.jsonl", {"song": encode_song(score())})
    entered, release = threading.Event(), threading.Event()
    original = import_jobs.read_result
    def blocked_reader(*args, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(import_jobs, "read_result", blocked_reader)
    process.running = False
    assert not job.poll()
    assert entered.wait(1)
    for _ in range(10):
        assert not job.poll() and not job.done
    release.set()
    assert job._decode_ready.wait(2)
    assert job.poll() and not job.error
    assert len(job.prepared_song[0]) == 300
    assert not job.directory.exists()


def test_cancelling_decoder_discards_result_and_reclaims_directory(job_factory, monkeypatch):
    job, process = job_factory
    write_result(job.directory / "result.jsonl", {"song": encode_song(score())})
    entered = threading.Event()
    def until_cancelled(*_args, cancelled, **_kwargs):
        entered.set()
        deadline = time.monotonic() + 2
        while not cancelled():
            assert time.monotonic() < deadline
            time.sleep(.001)
        raise DecodeCancelled()
    monkeypatch.setattr(import_jobs, "read_result", until_cancelled)
    process.running = False
    assert not job.poll()
    assert entered.wait(1)
    job.cancel()
    assert job._decode_ready.wait(1)
    assert job.cancelled and job.done and job.result is None and job.prepared_song is None
    assert not job.directory.exists()


def test_decoder_deadline_discards_result_without_blocking_poll(job_factory, monkeypatch):
    job, process = job_factory
    write_result(job.directory / "result.jsonl", {"song": encode_song(score())})
    entered = threading.Event()
    def until_cancelled(*_args, cancelled, **_kwargs):
        entered.set()
        deadline = time.monotonic() + 2
        while not cancelled():
            assert time.monotonic() < deadline
            time.sleep(.001)
        raise DecodeCancelled()
    monkeypatch.setattr(import_jobs, "read_result", until_cancelled)
    process.running = False
    assert not job.poll()
    assert entered.wait(1)
    job._deadline = time.monotonic() - 1
    job.poll()
    assert job._decode_ready.wait(1)
    assert job.poll() and job.error == "timeout"
    assert job.result is None and job.prepared_song is None
    assert not job.directory.exists()


def saved_session(path):
    return {"song_path": str(path.resolve()), "instrument": "bass_ukulele", "track_index": 2,
            "position_ms": 12500, "measure": 6, "tempo_factor": .7, "wait_mode": True,
            "loop_enabled": True, "loop_start_ms": 12000, "loop_end_ms": 16000,
            "active_strings": [True, False, True, True], "max_fret": 5}


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
def test_resume_details_and_setup_guide_are_visible_and_keyboard_reachable(tmp_path, monkeypatch, look, size):
    pygame.font.init()
    set_look(look)
    song = library.SongInfo(str(tmp_path / "saved.gp5"), title="Saved practice", content_id="a" * 64)
    monkeypatch.setattr(library, "scan", lambda *_a, **_k: [song])
    tracker = ProgressTracker()
    key = song_key(song.file, "bass_ukulele", 2, content_id=song.content_id)
    tracker.save_session(key, saved_session(song.file))
    menu = MenuScreen(tmp_path, Config(instrument="bass_ukulele"), tracker)
    surface = pygame.Surface(size)
    try:
        menu.render(surface)
        assert menu._row_rects, "The workflow controls must not consume the whole song viewport"
        for name in ("resume", "resume_details"):
            assert surface.get_rect().contains(menu._button_rects[name][0])
        assert menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F2)) == "wizard"
        menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_r, mod=pygame.KMOD_SHIFT))
        assert menu._resume_panel is not None
        menu.render(surface)
        text = " ".join(line for _, lines in menu._resume_panel.sections for line in lines)
        assert "70" in text and "3" in text and "7" in text and "0:12" in text
        for action in menu._resume_panel.actions:
            assert surface.get_rect().contains(menu._resume_panel.hit[action.name])
        if size == (320, 240):
            menu.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-100))
            assert menu._resume_panel.scroll == menu._resume_panel.max_scroll > 0
            menu.render(surface)
        assert menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)) == "resume"
        menu._setup_open = True
        menu.render(surface)
        # The guided setup is in the common properties panel, including
        # when the compact library has no space for a duplicate footer button.
        menu._setup_scroll = 6 * 52
        menu.render(surface)
        assert menu.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1,
                                 pos=menu._button_rects["wizard"][-1].center)) == "wizard"
    finally:
        set_look("studio")


def test_library_derivatives_refresh_on_progress_revision_without_per_frame_scan(tmp_path, monkeypatch):
    pygame.font.init()
    song = library.SongInfo(str(tmp_path / "saved.gp5"), title="Saved practice", content_id="a" * 64)
    monkeypatch.setattr(library, "scan", lambda *_a, **_k: [song])
    tracker = ProgressTracker()
    menu = MenuScreen(tmp_path, Config(instrument="bass_ukulele"), tracker)
    surface = pygame.Surface((1280, 720))
    menu.render(surface)
    latest = Mock(wraps=tracker.latest_session)
    monkeypatch.setattr(tracker, "latest_session", latest)
    menu.render(surface)
    latest.assert_not_called()
    key = song_key(song.file, "bass_ukulele", 0, content_id=song.content_id)
    tracker.record_result(key, {"accuracy_percent": 95})
    tracker.save_session(key, saved_session(song.file))
    menu.render(surface)
    assert menu._last_page.scores[0] == 95
    assert "resume" in menu._button_rects
    latest.assert_called_once()
    menu.render(surface)
    latest.assert_called_once()


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_compact_library_keeps_last_song_reachable_with_resume_shelf(tmp_path, monkeypatch, look):
    pygame.font.init()
    set_look(look)
    songs = [library.SongInfo(str(tmp_path / f"piece-{i:03}.gp5"), title=f"Song {i:03}",
                             content_id=f"{i:064x}") for i in range(30)]
    monkeypatch.setattr(library, "scan", lambda *_a, **_k: songs)
    tracker = ProgressTracker()
    tracker.save_session(song_key(songs[0].file, "bass_ukulele", 2, content_id=songs[0].content_id),
                         saved_session(songs[0].file))
    menu = MenuScreen(tmp_path, Config(instrument="bass_ukulele"), tracker)
    surface = pygame.Surface((320, 240))
    try:
        menu.render(surface)
        assert len(menu._row_rects) >= 2
        menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_END))
        menu.render(surface)
        assert menu._selected == 29
        assert any(index == 29 for _, index in menu._row_rects)
        assert menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)) == songs[-1].file
        menu.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1,
                          pos=menu._button_rects["resume_details"][0].center))
        assert menu._resume_panel is not None
    finally:
        set_look("studio")


@pytest.mark.parametrize("size", [(640, 480), (900, 520), (1280, 720)])
def test_stage_library_rows_leave_room_for_filter_chips(tmp_path, monkeypatch, size):
    pygame.font.init()
    set_look("stage")
    songs = [library.SongInfo(str(tmp_path / f"piece-{i:03}.gp5"), title=f"Song {i:03}",
                             content_id=f"{i:064x}") for i in range(30)]
    monkeypatch.setattr(library, "scan", lambda *_a, **_k: songs)
    tracker = ProgressTracker()
    tracker.save_session(song_key(songs[0].file, "bass_ukulele", 2, content_id=songs[0].content_id),
                         saved_session(songs[0].file))
    menu = MenuScreen(tmp_path, Config(instrument="bass_ukulele"), tracker)
    try:
        menu.render(pygame.Surface(size))
        chips = [rect for name, rects in menu._button_rects.items()
                 if name.startswith("filter.") for rect in rects]
        assert chips and menu._row_rects
        assert min(rect.top for rect, _ in menu._row_rects) >= max(rect.bottom for rect in chips) + 12
    finally:
        set_look("studio")
