"""Pixel regressions for soft note light and the visible strike targets."""
import numpy as np
import pygame
import pytest

from pickhero.config import Config
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui import design, shapes, paint_stage, paint_studio
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.scrolling import PlayingScreen


@pytest.fixture(autouse=True)
def drawing(monkeypatch):
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((1280, 720))
    before = get_look().key
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    design.clear_caches()
    yield
    set_look(before)
    design.clear_caches()
    pygame.display.quit()


def test_round_corner_contains_light_instead_of_a_square_transparent_hole():
    background = (20, 24, 36)
    canvas = pygame.Surface((200, 150))
    canvas.fill(background)
    box = pygame.Rect(50, 40, 90, 60)
    shapes.glow(canvas, box, (90, 200, 240), radius=16, spread=20, alpha=180)
    assert canvas.get_at(box.topleft).b > background[2] + 20
    assert canvas.get_at(box.center)[:3] == background
    assert canvas.get_at((box.left - 23, box.centery))[:3] == background


@pytest.mark.parametrize("radius, lean, ellipse", [(16, 0.0, False), (30, 0.0, False),
                                                    (0, 0.25, False), (0, 0, True)])
def test_glow_never_introduces_black_fringe_on_nonblack_background(radius, lean, ellipse):
    background = (30, 35, 45)
    canvas = pygame.Surface((200, 150), pygame.SRCALPHA)
    canvas.fill(background)
    shapes.glow(canvas, pygame.Rect(50, 40, 90, 60), (100, 190, 240),
                radius=radius, lean=lean, ellipse=ellipse, spread=18, alpha=200)
    assert np.all(pygame.surfarray.array3d(canvas) >= np.array(background))
    assert np.all(pygame.surfarray.array_alpha(canvas) == 255)


def test_corner_radius_changes_the_halo_shape():
    square = pygame.Surface((200, 150))
    rounded = square.copy()
    box = pygame.Rect(50, 40, 90, 60)
    shapes.glow(square, box, (255, 255, 255), radius=0, spread=20, alpha=180)
    shapes.glow(rounded, box, (255, 255, 255), radius=20, spread=20, alpha=180)
    assert rounded.get_at(box.topleft).r > square.get_at(box.topleft).r + 20


def test_opacity_animation_reuses_the_same_mask(monkeypatch):
    canvas = pygame.Surface((200, 150))
    box = pygame.Rect(50, 40, 90, 60)
    shapes.glow(canvas, box, (90, 200, 240), alpha=20)
    # Rebuilding the expensive distance field during a fade would jank on
    # modest hardware. Changing only alpha needs no new distance evaluation.
    monkeypatch.setattr(np, "hypot", lambda *args: pytest.fail("rebuilt animated glow"))
    for alpha in (30, 70, 150, 255):
        shapes.glow(canvas, box, (90, 200, 240), alpha=alpha)


def _playing(look, note_time=1000, string=3):
    set_look(look)
    tuning = {1: 43, 2: 38, 3: 33, 4: 28}
    timeline = Timeline([NoteEvent(note_time, 100, tuning[string], string, 0)],
                        SongMetadata(tempo=60, tuning=tuning))
    screen = PlayingScreen(timeline, config=Config(instrument="bass_ukulele"))
    screen._playback_ms = 1000
    screen._playing = True
    canvas = pygame.Surface((1280, 720))
    screen.render(canvas)
    return screen, canvas


def test_stage_lights_the_target_bearing_the_played_strings_name():
    screen, canvas = _playing("stage")
    page = screen._last_page
    # String 3 is A1: the second pad from the left on E/A/D/G.
    x, y, _ = paint_stage.project(page, 1, 4, 0)
    pixel = canvas.get_at((x + 20, y - 8))[:3]
    assert pixel == design.string_colour(3)
    wrong_x, wrong_y, _ = paint_stage.project(page, 2, 4, 0)
    assert canvas.get_at((wrong_x + 20, wrong_y - 8))[:3] != design.string_colour(3)


def test_stage_future_note_has_neon_rim_and_glow_on_its_matching_lane():
    screen, canvas = _playing("stage", note_time=4000)
    page = screen._last_page
    box = paint_stage.note_box(page, 1, 4, 3000 / screen._visible_window_ms)
    assert canvas.get_at((box.centerx, box.top))[:3] == get_look().ink_1
    blank = pygame.Surface(canvas.get_size())
    paint_stage.play(blank, page)
    point = (box.centerx, box.top - 4)
    assert canvas.get_at(point).b > blank.get_at(point).b + 15


@pytest.mark.parametrize("offset", [-120, -60, -1, 0, 60, 61])
def test_stage_target_pulse_never_anticipates_the_beat(monkeypatch, offset):
    screen, canvas = _playing("stage")
    screen._playback_ms = 1000 + offset
    rings, heads = [], []
    original_ring, original_fret = paint_stage.strike_ring, screen._draw_fret

    def ring(surface, page, lane, lanes, **kwargs):
        if kwargs.get("lit"):
            rings.append(lane)
        return original_ring(surface, page, lane, lanes, **kwargs)

    def fret(surface, rect, *args, **kwargs):
        heads.append(rect.copy())
        return original_fret(surface, rect, *args, **kwargs)

    monkeypatch.setattr(paint_stage, "strike_ring", ring)
    monkeypatch.setattr(screen, "_draw_fret", fret)
    screen.render(canvas)
    if 0 <= offset <= 60:
        assert rings == [1] and not heads
    else:
        assert not rings and len(heads) == 1
    if offset < 0:
        # Rounding may place a centre on the same pixel shortly before zero;
        # the cue must still follow musical time, not the rounded geometry.
        assert heads[0].centery <= paint_stage.strike_y(screen._last_page)


def test_studio_leading_edge_reaches_the_actual_onset_line():
    screen, canvas = _playing("studio")
    page = screen._last_page
    box = paint_studio.note_box(page, 3, 4, paint_studio.playhead_x(page), 68)
    assert box.left == paint_studio.playhead_x(page)
    assert box.centery == paint_studio.lane_y(page, 3, 4)
    # The live drawing agrees with the geometry, away from the fret digit.
    assert canvas.get_at((box.left + 10, box.centery))[:3] == design.string_colour(3)


@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
@pytest.mark.parametrize("tempo", [0.5, 1.0, 1.5])
@pytest.mark.parametrize("gap", [200, 3000])
def test_studio_card_starts_at_the_musical_beat_regardless_of_head_width(
        monkeypatch, size, tempo, gap):
    set_look("studio")
    tuning = {1: 43, 2: 38, 3: 33, 4: 28}
    timeline = Timeline([NoteEvent(1000, 100, 28, 4, 0),
                         NoteEvent(1000 + gap, 100, 30, 4, 2)],
                        SongMetadata(tempo=60, tuning=tuning))
    screen = PlayingScreen(timeline, config=Config(instrument="bass_ukulele"))
    screen._tempo_factor = tempo
    canvas = pygame.Surface(size)
    heads = {}
    original = screen._draw_fret

    def record(surface, rect, fret, *args, **kwargs):
        heads[fret] = rect.copy()
        return original(surface, rect, fret, *args, **kwargs)

    monkeypatch.setattr(screen, "_draw_fret", record)
    for elapsed in (800, 1000, 1200):
        screen._playback_ms = elapsed
        screen.render(canvas)
        page = screen._last_page
        line = paint_studio.playhead_x(page)
        scale = (paint_studio.board(page).right - line) / screen._visible_window_ms
        expected = round(line + (1000 - elapsed) * scale)
        assert heads[0].left == expected
        assert (heads[0].left > line) == (elapsed < 1000)
        assert (heads[0].left < line) == (elapsed > 1000)
        # The visible onset spine is inside the coloured card, not its glow.
        edge = canvas.get_at((heads[0].left, heads[0].centery))[:3]
        assert edge != get_look().panel_right


@pytest.mark.parametrize("fret", [0, 12, 24])
def test_distant_stage_fret_stays_inside_its_projected_capsule(fret):
    set_look("stage")
    canvas = pygame.Surface((100, 100))
    canvas.fill((0, 0, 0))
    box = pygame.Rect(40, 40, 20, 8)
    PlayingScreen._draw_fret(canvas, box, fret, 20, (255, 255, 255),
                            outline=False, fit_inside=True)
    visible = pygame.mask.from_threshold(canvas, (0, 0, 0), (1, 1, 1, 255))
    visible.invert()
    ink = visible.get_bounding_rects()
    assert ink, "The fret remains visible at the back of the highway."
    assert all(box.contains(part) for part in ink)
