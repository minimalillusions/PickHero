"""What the audit of 2026-10-01 found that was still true on 2026-10-29.

Sixty-eight findings, re-checked one by one against the code as it now
stands. Most had been fixed in the meantime, many by Codex's import worker
and validation. These are the ones that had not, in the parts of the app
that are not detection: text that ended the app, a score line that called a
semitone-off note found, timing counted against notes that were never
found, MIDI songs without bars, a log without a ceiling, a language switch
that closed the panel it was made in, and a few smaller ones.
"""

import struct

import pygame
import pytest

from pickhero import i18n
from pickhero.config import Config
from pickhero.instruments import BASS_UKULELE
from pickhero.ui import design


@pytest.fixture(scope="module", autouse=True)
def _pygame():
    pygame.init()
    yield


# ── text that ended the app (#4, #13, #14) ───────────────────────────────

POISON = {
    "NUL": "Lied\x00Rest",
    "lone surrogate": "a\ud800b",
    "zero-width space": "a​b",
    "soft hyphen": "Bass­lauf",
    "control codes": "Zeile1\nZeile2\x1b[31m\x07",
}


@pytest.mark.parametrize("name", POISON)
@pytest.mark.parametrize("tracking", [0.0, 0.08])
def test_no_character_can_take_the_frame_down(name, tracking):
    surface = pygame.Surface((400, 60))
    rect = design.text(surface, POISON[name], (0, 0), (255, 255, 255), 16, tracking=tracking)
    assert rect.width >= 0
    assert design.text_width(POISON[name], 16, tracking=tracking) >= 0


def test_printable_keeps_ordinary_text_untouched():
    for value in ("Pachelbels Kanon", "Größe · 9/26", "In der Halle des Bergkönigs"):
        assert design.printable(value) is value or design.printable(value) == value


def test_printable_turns_breaks_into_spaces_and_drops_the_rest():
    assert design.printable("a\nb\tc\x00d\x07e") == "a b cde"
    assert design.printable("x\ud800y") == "x�y"


# ── caches and a broken font file (#59, #15) ─────────────────────────────

def test_the_font_cache_has_a_ceiling():
    design.clear_font_cache()
    for size in range(8, 8 + 3 * design._FONT_CACHE_LIMIT):
        design.font(size)
    assert len(design._font_cache) <= design._FONT_CACHE_LIMIT


def test_a_truncated_bundled_font_falls_through_instead_of_failing_later(tmp_path, monkeypatch):
    broken = tmp_path / "Broken.ttf"
    broken.write_bytes(b"\x00\x01\x00\x00" + b"garbage" * 10)
    monkeypatch.setattr(design, "FONT_DIR", tmp_path)
    monkeypatch.setitem(design.BUNDLED, "broken", {design.DEFAULT_REGULAR: "Broken.ttf"})
    assert design._bundled_face("broken", 16, design.DEFAULT_REGULAR) is None


# ── eliding by bisection (#18) ───────────────────────────────────────────

def _naive(value, fits):
    if fits(value):
        return value
    cut = value
    while cut and not fits(cut + "…"):
        cut = cut[:-1]
    return (cut.rstrip() + "…") if cut else ""


@pytest.mark.parametrize("limit", [0, 3, 10, 25, 200])
def test_elide_gives_what_the_old_loop_gave(limit):
    value = "In der Halle des Bergkönigs, zweite Fassung"
    fits = lambda s: len(s) <= limit
    assert design.elide(value, fits) == _naive(value, fits)


def test_elide_measures_logarithmically_not_quadratically():
    calls = []
    def fits(s):
        calls.append(s)
        return len(s) <= 40
    design.elide("x" * 4000, fits)
    assert len(calls) <= 16, f"{len(calls)} measurements for a 4000-character title"


# ── the score line (#12, #50) ────────────────────────────────────────────

@pytest.fixture
def play_screen(monkeypatch):
    from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
    from pickhero.ui.scrolling import PlayingScreen
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *a: None)
    config = Config(instrument="bass_ukulele")
    config.save = lambda: None
    notes = [NoteEvent(i * 500.0, 400.0, 28, 4, 0) for i in range(26)]
    return PlayingScreen(Timeline(notes, SongMetadata(tempo=120)), config=config)


def test_a_semitone_off_is_not_found(play_screen, monkeypatch):
    stats = {"hits": 3, "close": 5, "misses": 18, "total": 26, "accuracy_percent": 11.5,
             "in_time": 3, "timing_measured": 3, "timing_unknown": 0}
    from types import SimpleNamespace
    play_screen._matcher = SimpleNamespace(get_statistics=lambda: stats)
    assert play_screen._page(pygame.Surface((1280, 720))).found == 3


@pytest.mark.parametrize("language", ["de", "en"])
def test_in_time_is_counted_over_the_notes_found(language, monkeypatch):
    """Ten found, all ten in time, must not read as ten of twenty-six."""
    from pickhero.ui import paint_hifi
    from pickhero.ui.page import Page
    monkeypatch.setattr(i18n, "_current", language)
    page = Page(width=1280, height=720, found=10, expected=26, in_time=10,
                timing_measured=10, timing_unknown=0, accuracy=38.0)
    line = paint_hifi._score(page)
    assert i18n.t("play.in_time", in_time=10, total=10) in line
    assert i18n.t("play.in_time", in_time=10, total=26) not in line


# ── MIDI songs have bars (#28) ───────────────────────────────────────────

def _vlq(value):
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def test_a_midi_song_puts_its_notes_in_their_bars(tmp_path):
    from pickhero.tabs.midi_import import load_midi_file
    events = _vlq(0) + b"\xff\x51\x03" + (500_000).to_bytes(3, "big")   # 120 BPM
    for pitch in (28, 33, 38, 43, 28, 33, 38, 43, 28, 33, 38, 43):        # three bars of 4/4
        events += _vlq(0) + bytes([0x90, pitch, 90]) + _vlq(480) + bytes([0x80, pitch, 0])
    body = events + _vlq(0) + b"\xff\x2f\x00"
    data = (b"MThd" + struct.pack(">IHHH", 6, 1, 1, 480)
            + b"MTrk" + struct.pack(">I", len(body)) + body)
    path = tmp_path / "drei_takte.mid"
    path.write_bytes(data)
    timeline = load_midi_file(path, instrument=BASS_UKULELE)
    assert [n.measure for n in timeline.notes] == [0] * 4 + [1] * 4 + [2] * 4
    assert len(timeline.measures) == 3


# ── a folder that is not a folder (#33) ──────────────────────────────────

def test_only_a_real_folder_is_handed_to_the_file_manager(tmp_path):
    from pickhero.ui.menu import MenuScreen
    not_a_folder = tmp_path / "evil.exe"
    not_a_folder.write_bytes(b"MZ")
    screen = MenuScreen.__new__(MenuScreen)
    screen._songs_dir = not_a_folder
    # The conftest seal fails this test if os.startfile is reached.
    screen._open_folder()


# ── the log has a ceiling (#37) ──────────────────────────────────────────

def test_the_log_rolls_over_instead_of_growing_forever():
    from pickhero import config as config_module
    log = config_module.CONFIG_DIR / "pickhero.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"x" * (config_module.LOG_LIMIT_BYTES + 10))
    config_module.log_error("one more")
    older = log.with_name("pickhero.log.1")
    assert older.exists() and older.stat().st_size > config_module.LOG_LIMIT_BYTES
    assert log.stat().st_size < 200
    assert "one more" in log.read_text(encoding="utf-8")


# ── a language switch keeps the library as it was (#52) ──────────────────

def test_switching_language_keeps_filter_search_selection_and_panel(tmp_path, monkeypatch):
    from pickhero.exercises import write_exercises
    from pickhero.ui.app import App
    write_exercises(BASS_UKULELE, tmp_path)
    config = Config(instrument="bass_ukulele", songs_dir=str(tmp_path))
    config.save = lambda: None
    app = App(config)
    monkeypatch.setattr("pickhero.music.ensure_music", lambda *a, **k: False)
    app._build_menu()
    menu = app._menu
    menu._search_text = "Saite"
    menu._setup_open, menu._setup_scroll = True, 52
    menu._apply_filter()
    chosen = menu._selected_path()
    app._cycle_language()
    assert app._menu is not menu, "the screen is rebuilt"
    assert app._menu._search_text == "Saite"
    assert app._menu._setup_open and app._menu._setup_scroll == 52
    assert app._menu._selected_path() == chosen


# ── a hand-made language pack (#54) ──────────────────────────────────────

def test_a_language_pack_cannot_format_its_way_into_trouble(monkeypatch):
    import json
    from pickhero import config as config_module
    folder = config_module.CONFIG_DIR / "lang"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "xx.json").write_text(json.dumps({"name": "Probe", "strings": {
        "menu.language": "Sprak · {n}",
        "hud.percent": "{percent:99999999}",
        "play.pause": "{n.__class__}",
        "lesson.review_heading": "Les {number:02d}",
    }}), encoding="utf-8")
    monkeypatch.setattr(i18n, "_catalogues", {})
    strings = i18n._user_catalogues()["xx"][0]
    assert "menu.language" in strings and "lesson.review_heading" in strings
    assert "hud.percent" not in strings and "play.pause" not in strings


def test_the_built_in_catalogues_pass_the_same_rule():
    from pickhero.lang import de, en
    for module in (de, en):
        assert all(i18n._safe_entry(value) for value in module.STRINGS.values())
