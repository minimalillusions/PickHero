"""The language is a visible choice, and a file of their own can be heard first.

Future players are expected to choose English, and the app starts in German.
So the language has to be findable by someone who cannot read the screen it
is on: the settings row names the active language in that language, and the
first page of the setup guide offers the other one by its own name.

A preview of one's own file has to decode a file from elsewhere. That is done
in the import worker, a separate process, and nowhere else -- the same road
playing takes. These tests replace the worker; they never start one.
"""

from types import SimpleNamespace

import pygame
import pytest

from pickhero import i18n
from pickhero.config import Config
from pickhero.curriculum import build_file_demo
from pickhero.instruments import BASS_UKULELE
from pickhero.library import SongInfo
from pickhero.tabs.timeline import MeasureInfo, NoteEvent, SongMetadata, Timeline
from pickhero.ui import library_setup
from pickhero.ui.library_catalogue import Catalogue
from pickhero.ui.setup_guide import SetupGuideScreen


@pytest.fixture(autouse=True)
def _german_first():
    before = i18n.current_language()
    i18n.set_language("de")
    yield
    i18n.set_language(before)


def key(code):
    return pygame.event.Event(pygame.KEYDOWN, key=code, mod=0, unicode="")


# ── the language is a visible choice ─────────────────────────────────────

@pytest.mark.parametrize("language,shown", [("de", "Deutsch"), ("en", "English")])
def test_the_settings_row_names_the_active_language_in_itself(language, shown):
    i18n.set_language(language)
    page = SimpleNamespace(instrument_short="", instrument_name="", tuning_label="",
                           device_name="", device_ready=False, sort_label="")
    row = next(r for r in library_setup.setup_rows(page) if r[0] == "language")
    assert row[2] == shown


@pytest.mark.parametrize("language", ["de", "en"])
def test_the_language_label_can_be_read_in_either_language(language):
    i18n.set_language(language)
    label = i18n.t("menu.language")
    assert "Sprache" in label and "Language" in label


@pytest.mark.parametrize("language,offered", [("de", "English"), ("en", "Deutsch")])
def test_the_first_guide_page_offers_the_other_language_by_its_own_name(language, offered):
    """Whoever is stuck finds their own language's name, not a foreign word."""
    i18n.set_language(language)
    screen = SetupGuideScreen(Config(), instrument=BASS_UKULELE)
    assert screen.STAGES[screen.stage] == "instrument"
    action = next(a for a in screen._panel.actions if a.name == "language")
    assert action.label.startswith(offered)


def test_the_guide_opened_from_a_song_does_not_offer_it():
    screen = SetupGuideScreen(Config(), instrument=BASS_UKULELE, can_change_instrument=False)
    assert all(a.name != "language" for a in screen._panel.actions)


def test_pressing_l_in_the_guide_switches_language_and_redraws_the_page(tmp_path, monkeypatch):
    from pickhero.ui.app import App
    monkeypatch.setattr(App, "_build_menu", lambda self: None)
    config = Config(songs_dir=str(tmp_path))
    saved = []
    config.save = lambda: saved.append(config.language)
    app = App(config)
    app._open_guide()
    german_title = app._setup_guide._panel.title
    app._handle_guide_event(key(pygame.K_l))
    assert i18n.current_language() == "en" and config.language == "en"
    assert saved == ["en"], "the choice has to reach the file"
    assert app._setup_guide._panel.title != german_title
    assert app._state == "wizard", "choosing a language is not leaving the guide"
    # And it is a key press, not a check: the guide still comes back.
    assert config.setup_done is False


# ── a preview of one's own file ──────────────────────────────────────────

def _timeline(*, intro_bars=0, bars=8, tempo=120):
    """Four quarter notes a bar, after `intro_bars` empty bars."""
    bar_ms = 4 * 60_000 / tempo
    measures = [MeasureInfo(i, i * bar_ms, (i + 1) * bar_ms) for i in range(intro_bars + bars)]
    notes = [NoteEvent((intro_bars + b) * bar_ms + q * bar_ms / 4, bar_ms / 4 * .9, 33, 3, 0,
                       measure=intro_bars + b)
             for b in range(bars) for q in range(4)]
    return Timeline(notes, SongMetadata(tempo=tempo), measures=measures)


def _note_ons(demo):
    from pickhero.audio.midi_playback import NOTE_ON
    return [e for e in demo.track._events if e.event_type == NOTE_ON]


def test_a_file_demo_is_four_clicks_then_four_bars_of_the_part():
    demo = build_file_demo(_timeline(), BASS_UKULELE)
    ons = _note_ons(demo)
    clicks = [e for e in ons if e.channel == 9]
    played = [e for e in ons if e.channel == 0]
    assert len(clicks) == 4 and demo.count_in_ms == pytest.approx(2000)
    assert len(played) == 16 and demo.bars == 4
    assert played[0].timestamp_ms == pytest.approx(demo.count_in_ms)


def test_a_long_silent_intro_is_skipped_not_previewed():
    """A bass that enters after eight bars must not preview as sixteen seconds of nothing."""
    demo = build_file_demo(_timeline(intro_bars=8), BASS_UKULELE)
    first = next(e for e in _note_ons(demo) if e.channel == 0)
    assert first.timestamp_ms == pytest.approx(demo.count_in_ms)


def test_a_slow_file_is_cut_at_twenty_seconds():
    demo = build_file_demo(_timeline(tempo=40), BASS_UKULELE)
    assert demo.duration_ms - demo.count_in_ms <= 20_000 + 1e-6


def test_a_part_without_notes_has_no_preview():
    empty = Timeline([], SongMetadata(tempo=120), measures=[MeasureInfo(0, 0, 2000)])
    with pytest.raises(ValueError):
        build_file_demo(empty, BASS_UKULELE)


class FakeJob:
    started = []

    def __init__(self, operation, path, instrument, **options):
        FakeJob.started.append((operation, str(path), instrument, options))
        self.operation, self.done, self.cancelled = operation, False, False
        self.error = self.result = None
        self.prepared_song = (_timeline(), None)

    def poll(self):
        return self.done

    def cancel(self):
        self.cancelled = True


class FakePlayer:
    def __init__(self, track):
        self.track, self.closed = track, False

    def open(self):
        return True

    def update(self, ms):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def own_file(tmp_path, monkeypatch):
    FakeJob.started = []
    monkeypatch.setattr("pickhero.import_jobs.ImportJob", FakeJob)
    monkeypatch.setattr("pickhero.ui.library_catalogue.MidiPlayer", FakePlayer)

    def parsed_here(*args, **kwargs):
        raise AssertionError("a file from elsewhere was parsed in the app process")
    monkeypatch.setattr("pickhero.tabs.loader.load_gp_file", parsed_here)
    path = tmp_path / "Band" / "riff.gp5"
    path.parent.mkdir()
    path.write_bytes(b"not read by the app process")
    song = SongInfo(str(path), title="Riff", track_index=1, content_id="abc")
    return Catalogue(tmp_path), song


def test_own_file_loads_through_the_worker_and_then_plays(own_file):
    catalogue, song = own_file
    catalogue.toggle_preview(song, BASS_UKULELE)
    assert catalogue.status == "loading"
    assert FakeJob.started == [("load", str(song.file), "bass_ukulele",
                                {"track_index": 1, "content_id": "abc"})]
    catalogue._job.done = True
    catalogue.update()
    assert catalogue.status == "playing" and catalogue._player is not None


def test_a_second_listen_does_not_load_again(own_file):
    catalogue, song = own_file
    catalogue.toggle_preview(song, BASS_UKULELE)
    catalogue._job.done = True
    catalogue.update()
    catalogue.toggle_preview(song, BASS_UKULELE)          # stop
    catalogue.toggle_preview(song, BASS_UKULELE)          # again
    assert len(FakeJob.started) == 1 and catalogue.status == "playing"


def test_leaving_the_file_while_it_loads_ends_the_worker(own_file):
    catalogue, song = own_file
    catalogue.toggle_preview(song, BASS_UKULELE)
    job = catalogue._job
    catalogue.stop()
    assert job.cancelled and catalogue._job is None and catalogue.status == "idle"


def test_a_file_the_worker_cannot_read_says_so_and_the_next_starts_fresh(own_file):
    catalogue, song = own_file
    catalogue.toggle_preview(song, BASS_UKULELE)
    catalogue._job.error, catalogue._job.done = "decode", True
    catalogue.update()
    assert catalogue.status == "failed" and catalogue._player is None
    catalogue.stop()                                       # moving to another file
    assert catalogue.status == "idle"


def _detail_of(catalogue, song):
    """Draw the library's detail panel for one song and return what it marked."""
    from pickhero.ui import library_browser
    from pickhero.ui.page import Page
    pygame.init()
    page = Page(width=900, height=600, rows=[song], selected=0)
    screen = SimpleNamespace(catalogue=catalogue, _instrument=lambda: BASS_UKULELE,
                             _record_for=lambda s: None)
    library_browser._detail(pygame.Surface((900, 600)), screen, page,
                            pygame.Rect(400, 0, 500, 600), narrow=False)
    return page


def test_the_preview_button_is_drawn_for_own_files(own_file):
    catalogue, song = own_file
    assert "preview" in _detail_of(catalogue, song).hit


@pytest.mark.parametrize("status,text", [("loading", "Lädt …"), ("failed", "Keine Hörprobe")])
def test_the_button_says_what_the_preview_is_doing(own_file, monkeypatch, status, text):
    from pickhero.ui import library_browser
    labels = []
    real = library_browser._button
    monkeypatch.setattr(library_browser, "_button",
                        lambda surface, page, name, label, rect, **kw:
                        (labels.append((name, label)), real(surface, page, name, label, rect, **kw))[1])
    catalogue, song = own_file
    catalogue.status = status
    _detail_of(catalogue, song)
    assert ("preview", text) in labels
