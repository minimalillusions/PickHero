"""Real painted controls and real multi-track files, not direct filter calls."""

from dataclasses import replace

import guitarpro
import pygame
import pytest

from pickhero import instruments, library
from pickhero.config import Config
from pickhero.i18n import t
from pickhero.progress import ProgressTracker, song_key
from pickhero.tabs.loader import load_gp_file
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.menu import MenuScreen


@pytest.fixture(autouse=True)
def font_display():
    previous = get_look().key
    pygame.display.init()
    pygame.font.init()
    yield
    set_look(previous)


def _write_tracks(path, parts):
    model = guitarpro.models
    header = model.MeasureHeader(number=1)
    header.start = 960
    song = model.Song(tracks=[], measureHeaders=[header])
    song.title = path.stem
    for index, (name, tuning, program) in enumerate(parts):
        track = model.Track(song, index + 1)
        track.name = name
        track.channel.instrument = program
        track.channel.channel = index + 1
        track.strings = [model.GuitarString(number=string, value=pitch)
                         for string, pitch in tuning.items()]
        measure = model.Measure(track, header)
        voice = measure.voices[0]
        beat = model.Beat(voice)
        beat.start = 960
        beat.status = model.BeatStatus.normal
        note = model.Note(beat)
        note.type = model.NoteType.normal
        note.string = max(tuning)
        note.value = 0
        beat.notes.append(note)
        voice.beats.append(beat)
        track.measures = [measure]
        song.tracks.append(track)
    guitarpro.write(song, str(path), version=(5, 1, 0))


@pytest.fixture
def instrument_library(tmp_path):
    four = dict(instruments.BASS_GUITAR.tuning)
    five = {**four, 5: 23}
    guitar = dict(instruments.GUITAR.tuning)
    _write_tracks(tmp_path / "four.gp5", [("Four bass", four, 33)])
    _write_tracks(tmp_path / "five.gp5", [("Five bass", five, 33)])
    _write_tracks(tmp_path / "guitar.gp5", [("Guitar", guitar, 24)])
    _write_tracks(tmp_path / "band.gp5", [("Five bass", five, 33),
                                        ("Guitar", guitar, 24),
                                        ("Four bass", four, 33)])
    return tmp_path


def test_four_string_library_excludes_five_string_parts_but_keeps_compatible_band(instrument_library):
    before = {path.name: path.read_bytes() for path in instrument_library.glob("*.gp5")}
    for use_cache in (False, True, True):
        songs = library.scan(instrument_library, instruments.BASS_GUITAR, use_cache=use_cache)
        assert {song.file.stem for song in songs} == {"four", "band"}
        assert all(song.strings == 4 for song in songs)
        band = next(song for song in songs if song.file.stem == "band")
        assert band.track_index == 2
        loaded = load_gp_file(band.file, instrument=instruments.BASS_GUITAR)
        assert loaded.metadata.track_index == band.track_index
        assert loaded.metadata.tuning == instruments.BASS_GUITAR.tuning
    assert before == {path.name: path.read_bytes() for path in instrument_library.glob("*.gp5")}


def test_guitar_library_selects_guitar_part_of_same_band(instrument_library):
    songs = library.scan(instrument_library, instruments.GUITAR)
    assert {song.file.stem for song in songs} == {"guitar", "band"}
    band = next(song for song in songs if song.file.stem == "band")
    assert band.track_index == 1 and band.strings == 6


def test_five_string_bass_can_use_four_string_parts(instrument_library):
    bass5 = replace(instruments.BASS_GUITAR, key="five_bass",
                    tuning={**instruments.BASS_GUITAR.tuning, 5: 23})
    songs = library.scan(instrument_library, bass5)
    assert {song.file.stem for song in songs} == {"four", "five", "band"}
    assert next(song for song in songs if song.file.stem == "band").track_index == 0


def _browser(tmp_path, monkeypatch, look):
    songs = [library.SongInfo(str(tmp_path / f"{title}.gp5"), title=title,
                             strings=4, difficulty=difficulty, track_index=track)
             for title, difficulty, track in [("Delta", 2, 2), ("Beta", 3, 1),
                                               ("Gamma", 1, 0), ("Alpha", 4, 3)]]
    tracker = ProgressTracker()
    for title, accuracy, date in [("Beta", 60, "2026-10-01"),
                                   ("Gamma", 0, "2026-10-02"),
                                   ("Alpha", 95, "2026-09-30")]:
        song = next(song for song in songs if song.title == title)
        key = song_key(song.file, "bass_guitar", song.track_index)
        tracker.record_result(key, {"accuracy_percent": accuracy})
        tracker.get_best(key).last_played = date
    monkeypatch.setattr(library, "scan", lambda *args, **kwargs: songs)
    screen = MenuScreen(tmp_path, Config(instrument="bass_guitar"), tracker)
    set_look(look)
    return screen, pygame.Surface((1280, 720))


def _click(screen, surface, name, *, panel=False):
    screen.render(surface)
    assert name in screen._button_rects, f"{get_look().key}: {name} was not drawn"
    rects = screen._button_rects[name]
    if panel:
        card = screen._button_rects["setup.card"][0]
        rects = [rect for rect in rects if card.contains(rect)]
    rect = rects[0]
    screen.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=rect.center))


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_real_progress_filter_clicks_change_rows_in_every_look(tmp_path, monkeypatch, look):
    screen, surface = _browser(tmp_path, monkeypatch, look)
    for bucket, expected in [("new", ["Delta"]), ("wip", ["Beta", "Gamma"]),
                             ("done", ["Alpha"])]:
        _click(screen, surface, f"filter.progress.{bucket}")
        assert screen._filter == ("progress", bucket)
        assert [song.title for song in screen._rows] == expected
    _click(screen, surface, "filter.all.None")
    assert len(screen._rows) == 4


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_real_sort_clicks_change_row_order_and_persist(tmp_path, monkeypatch, look):
    screen, surface = _browser(tmp_path, monkeypatch, look)
    _click(screen, surface, "setup")
    for mode, expected in [("difficulty", ["Gamma", "Delta", "Beta", "Alpha"]),
                           ("accuracy", ["Alpha", "Beta", "Gamma", "Delta"]),
                           ("last_played", ["Gamma", "Beta", "Alpha", "Delta"])]:
        _click(screen, surface, "sort", panel=True)
        assert screen._sort_mode == mode
        assert [song.title for song in screen._rows] == expected
        assert Config.load().sort_mode == mode


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_real_search_click_accepts_text_and_filters(tmp_path, monkeypatch, look):
    screen, surface = _browser(tmp_path, monkeypatch, look)
    _click(screen, surface, "search")
    assert screen.is_searching
    for character in "beta":
        screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=ord(character), unicode=character))
    assert [song.title for song in screen._rows] == ["Beta"]


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_instrument_control_rescans_and_removes_wrong_parts(instrument_library, monkeypatch, look):
    import pickhero.exercises as exercises
    monkeypatch.setattr(exercises, "ensure_exercises", lambda *args, **kwargs: False)
    before = {path.name: path.read_bytes() for path in instrument_library.glob("*.gp5")}
    screen = MenuScreen(instrument_library, Config(instrument="guitar"))
    set_look(look)
    surface = pygame.Surface((1280, 720))
    _click(screen, surface, "setup")
    _click(screen, surface, "instrument")
    assert screen._config.instrument == "bass_ukulele"
    assert {song.file.stem for song in screen._rows} == {"four", "band"}
    assert all(song.strings == 4 for song in screen._rows)
    assert before == {path.name: path.read_bytes() for path in instrument_library.glob("*.gp5")}


@pytest.mark.parametrize("legacy", [False, True])
def test_hidden_same_stem_file_never_donates_progress_to_visible_file(tmp_path, legacy):
    four_path, five_path = tmp_path / "four" / "same.gp5", tmp_path / "five" / "same.gp5"
    four_path.parent.mkdir()
    five_path.parent.mkdir()
    four = dict(instruments.BASS_GUITAR.tuning)
    _write_tracks(four_path, [("Four", four, 33)])
    _write_tracks(five_path, [("Five", {**four, 5: 23}, 33)])
    before = {path: path.read_bytes() for path in (four_path, five_path)}
    tracker = ProgressTracker()
    source = "same" if legacy else song_key(five_path, "bass_guitar", 0)
    tracker.record_result(source, {"accuracy_percent": 96})
    for _ in range(2):  # Fresh parse and cached library must preserve ambiguity.
        menu = MenuScreen(tmp_path, Config(instrument="bass_guitar"), tracker)
        assert [song.file for song in menu._rows] == [four_path]
        assert menu._accuracy(menu._rows[0]) is None
        menu._set_filter(("progress", "done"))
        assert not menu._rows
        assert tracker.get_best(source).best_accuracy == 96
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_empty_status_and_instrument_explain_why_without_file_import_prompt(tmp_path, monkeypatch, look):
    import importlib
    import pickhero.exercises as exercises

    monkeypatch.setattr(exercises, "ensure_exercises", lambda *a, **kw: False)
    _write_tracks(tmp_path / "guitar.gp5", [("Guitar", instruments.GUITAR.tuning, 24)])
    screen = MenuScreen(tmp_path, Config(instrument="guitar"), ProgressTracker())
    set_look(look)
    surface = pygame.Surface((1280, 720))
    # Observe real paint calls as well as the page state, so a painter cannot
    # silently replace the message with its old generic file-import hint.
    module = importlib.import_module("pickhero.ui.library_browser")
    function_name = "paragraph"
    original = getattr(module, function_name)
    drawn = []

    def capture(surface, value, *args, **kwargs):
        drawn.append(value)
        return original(surface, value, *args, **kwargs)

    monkeypatch.setattr(module, function_name, capture)
    _click(screen, surface, "filter.progress.done")
    drawn.clear()
    screen.render(surface)
    assert t("menu.empty_filter", name=t("menu.progress_done")) in drawn
    assert t("menu.empty_filter_hint") in drawn
    assert t("menu.empty_hint") not in drawn
    _click(screen, surface, "filter.all.None")
    _click(screen, surface, "setup")
    _click(screen, surface, "instrument")
    assert screen._config.instrument == "bass_ukulele" and not screen._rows
    screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
    drawn.clear()
    screen.render(surface)
    expected = t("menu.empty_instrument", instrument=screen._instrument_short_name())
    assert expected in drawn
    assert t("menu.empty_instrument_hint") in drawn
    assert t("menu.empty_hint") not in drawn
