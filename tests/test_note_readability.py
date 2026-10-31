"""Exercise the actual drawing of dense passages, including narrow windows.

These fixtures are synthetic: no copyrighted song or physical audio needed.
"""
import pygame
import pytest

from pickhero.config import Config
from pickhero.matcher import MatchType
from pickhero.tabs.timeline import BeatInfo, MeasureInfo, NoteEvent, SongMetadata, Timeline
from pickhero.ui import design, paint_hifi, paint_stage, paint_studio
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.scrolling import PlayingScreen


@pytest.fixture(autouse=True)
def drawing(monkeypatch):
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((1280, 720))
    previous = get_look().key
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    design.clear_caches()
    yield
    set_look(previous)
    design.clear_caches()
    pygame.display.quit()


def dense_timeline(strings=4):
    tuning = ({1: 43, 2: 38, 3: 33, 4: 26} if strings == 4 else
              {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40})
    gap = 60000 / 81 / 4
    frets = [0, 2, 12, 24, 7, 9, 10, 11, 14, 16, 19, 22]
    notes = [NoteEvent(1000 + i * gap, gap * (1 if i % 2 else 0.5),
                       tuning[strings] + fret, strings, fret)
             for i, fret in enumerate(frets)]
    return Timeline(notes, SongMetadata(title="Dense passage", tempo=81, tuning=tuning))


def playing(look, timeline):
    set_look(look)
    screen = PlayingScreen(timeline, config=Config(instrument="bass_ukulele", max_fret=24))
    screen._playing = False
    return screen


@pytest.mark.parametrize("look", ["studio", "hifi", "stage"])
@pytest.mark.parametrize("size", [(1280, 720), (876, 706), (640, 480)])
@pytest.mark.parametrize("strings", [4, 6])
def test_dense_notes_remain_separate_and_every_fret_is_drawn(monkeypatch, look, size, strings):
    timeline = dense_timeline(strings)
    screen = playing(look, timeline)
    canvas = pygame.Surface(size)
    boxes = []
    seen = set()
    original = screen._draw_fret

    def record(surface, rect, fret, label_size, ink, **kwargs):
        boxes.append((rect.copy(), fret))
        # Check the real glyph rendering too: a narrow head must not push
        # its two-digit fret above the string or into the adjacent note.
        glyphs = pygame.Surface(size)
        glyphs.fill((0, 0, 0))
        original(glyphs, rect, fret, label_size, (255, 255, 255), **kwargs)
        mask = pygame.mask.from_threshold(glyphs, (0, 0, 0), (1, 1, 1, 255))
        mask.invert()
        if canvas.get_rect().contains(rect):
            assert mask.count(), (look, size, fret, rect)
            assert all(rect.contains(r) for r in mask.get_bounding_rects()), (
                look, size, fret, rect, label_size, kwargs, mask.get_bounding_rects())
        return original(surface, rect, fret, label_size, ink, **kwargs)

    monkeypatch.setattr(screen, "_draw_fret", record)
    for elapsed in (0, 700, 1300, 1900, 2500, 3100):
        screen._playback_ms = elapsed
        boxes.clear()
        screen.render(canvas)
        visible = [(r, f) for r, f in boxes if canvas.get_rect().contains(r)]
        seen.update(f for _, f in visible)
        ordered = sorted(visible, key=lambda item: item[0].centery if look == "stage"
                         else item[0].centerx)
        for (left, _), (right, _) in zip(ordered, ordered[1:]):
            if look == "stage":
                assert right.top - left.bottom >= 2, (left, right)
            else:
                assert right.left - left.right >= 6, (left, right)
                # Six Hi-fi strings in a 480px window also limit disc height.
                minimum = 24 if look == "hifi" and strings == 6 and size[1] == 480 else 32
                assert min(left.width, right.width) >= minimum
    assert seen == {note.fret for note in timeline.notes}


@pytest.mark.parametrize("look", ["studio", "hifi", "stage"])
def test_visual_scale_is_stable_across_seek_and_playback_speed(look):
    screen = playing(look, dense_timeline())
    canvas = pygame.Surface((876, 706))
    windows = []
    for elapsed, tempo in [(0, 1), (500, 1), (2100, 0.5), (5000, 1.5), (0, 1)]:
        screen._playback_ms = elapsed
        screen._tempo_factor = tempo
        screen.render(canvas)
        windows.append(screen._visible_window_ms)
        assert screen._playback_ms == elapsed
    assert 0 < windows[0] < 8000
    assert windows == [windows[0]] * len(windows)


@pytest.mark.parametrize("look", ["studio", "hifi", "stage"])
def test_sparse_exercises_keep_their_long_preview(look):
    tuning = {1: 43, 2: 38, 3: 33, 4: 28}
    timeline = Timeline([NoteEvent(i * 2000, 1000, 28, 4, 0) for i in range(8)],
                        SongMetadata(tempo=60, tuning=tuning))
    screen = playing(look, timeline)
    screen.render(pygame.Surface((1280, 720)))
    assert screen._visible_window_ms == 8000


@pytest.mark.parametrize("look,paint", [("studio", paint_studio), ("hifi", paint_hifi)])
@pytest.mark.parametrize("tempo", [0.5, 1, 1.5])
def test_imported_beat_lines_align_with_notes_at_every_practice_tempo(monkeypatch, look, paint, tempo):
    # Three quarter notes in bar 1; a tempo change makes bar 2 faster.
    beats = [BeatInfo(t, 0 if i < 3 else 1, i % 3)
             for i, t in enumerate([0, 500, 1000, 1500, 1900, 2300])]
    timeline = Timeline([NoteEvent(b.timestamp_ms, 100, 28, 4, i)
                         for i, b in enumerate(beats)],
                        SongMetadata(tempo=120, tuning={1: 43, 2: 38, 3: 33, 4: 28}),
                        measures=[MeasureInfo(0, 0, 1500), MeasureInfo(1, 1500, 2700)],
                        beats=beats, tempo_changes=[(1500, 150)])
    screen = playing(look, timeline)
    screen._tempo_factor = tempo
    screen._playback_ms = 600
    canvas = pygame.Surface((876, 706))
    notes = []
    real_fret = screen._draw_fret

    def fret(surface, rect, value, *args, **kwargs):
        notes.append((rect.left if look == "studio" else rect.centerx, value))
        return real_fret(surface, rect, value, *args, **kwargs)

    monkeypatch.setattr(screen, "_draw_fret", fret)
    screen.render(canvas)
    board = paint.board(screen._last_page)
    # Sample the real grid pixels without note heads covering them. Hi-fi
    # deliberately rules bars only; Studio also draws the interior beats.
    grid = pygame.Surface(canvas.get_size())
    paint.play(grid, screen._last_page)
    visible = [(x, beats[value]) for x, value in notes
               if board.left + 5 < x < board.right - 5
               and (look == "studio" or beats[value].accent)]
    assert len(visible) >= (4 if look == "studio" else 2)
    for x, beat in visible:
        colour = (get_look().hairline if look == "hifi" else
                  get_look().hairline_loud if beat.accent else get_look().hairline_quiet)
        assert any(grid.get_at((x + delta, board.top + 4))[:3] == colour
                   for delta in (-1, 0, 1)), (look, tempo, x, beat)


@pytest.mark.parametrize("look", ["studio", "hifi", "stage"])
def test_note_names_and_timing_labels_leave_other_heads_clear(monkeypatch, look):
    tuning = {1: 43, 2: 38, 3: 33, 4: 26}
    notes = [NoteEvent(500 + i * 185, 185, tuning[string] + i, string, i)
             for string in tuning for i in range(8)]
    notes.append(NoteEvent(2600, 800, 43, 1, 0))
    screen = playing(look, Timeline(notes, SongMetadata(tempo=81, tuning=tuning)))
    screen._audio_enabled = True
    monkeypatch.setattr(screen._feedback, "match_state",
                        lambda note: MatchType.EARLY if note.fret % 3 == 0 else None)
    monkeypatch.setattr(screen._feedback, "effect_age_ms", lambda *args: 100)
    monkeypatch.setattr(screen._feedback, "timing_error_ms", lambda note: -160)
    canvas = pygame.Surface((876, 706))
    heads, labels = [], []
    active = []
    real_text, real_rect, real_fret = design.text, pygame.draw.rect, screen._draw_fret

    def text(surface, *args, **kwargs):
        rect = real_text(surface, *args, **kwargs)
        if active and look == "hifi":
            labels.append((rect, active[-1]))
        return rect

    def rectangle(surface, colour, box, width=0, **kwargs):
        if active and width == 0:
            labels.append((pygame.Rect(box), active[-1]))
        return real_rect(surface, colour, box, width, **kwargs)

    def fret(surface, rect, *args, **kwargs):
        heads.append(rect.copy())
        return real_fret(surface, rect, *args, **kwargs)

    def label_wrapper(original):
        def call(surface, rect, *args, **kwargs):
            active.append(rect.copy())
            try:
                return original(surface, rect, *args, **kwargs)
            finally:
                active.pop()
        return call

    monkeypatch.setattr(design, "text", text)
    monkeypatch.setattr(pygame.draw, "rect", rectangle)
    monkeypatch.setattr(screen, "_draw_fret", fret)
    monkeypatch.setattr(screen, "_draw_note_name", label_wrapper(screen._draw_note_name))
    monkeypatch.setattr(screen, "_draw_timing_note", label_wrapper(screen._draw_timing_note))
    label_count = 0
    for elapsed in (0, 500, 1100, 1800):
        screen._playback_ms = elapsed
        heads.clear()
        labels.clear()
        screen.render(canvas)
        # A Hi-fi verdict has both a background and text; de-duplicate
        # contained text before comparing separate labels.
        boxes = [(r, owner) for r, owner in labels
                 if not any(other != r and other.contains(r) for other, _ in labels)]
        label_count += len(boxes)
        for rect, owner in boxes:
            assert all(not rect.colliderect(head) for head in heads if head != owner)
        for index, (rect, _) in enumerate(boxes):
            assert all(not rect.colliderect(other) for other, _ in boxes[index + 1:])
    if look != "stage":
        assert label_count, "Names and feedback remain available wherever space permits."


@pytest.mark.parametrize("look", ["studio", "hifi"])
def test_duration_is_drawn_to_its_real_end_before_later_heads(monkeypatch, look):
    timeline = Timeline([NoteEvent(1000, 900, 28, 4, 0),
                         NoteEvent(1200, 300, 30, 4, 2)],
                        SongMetadata(tuning={1: 43, 2: 38, 3: 33, 4: 28}))
    screen = playing(look, timeline)
    canvas = pygame.Surface((876, 706))
    calls, heads, tails = [], {}, []
    real_tail, real_fret = screen._draw_tail, screen._draw_fret

    def tail(surface, rect, width, *args):
        calls.append("tail")
        tails.append((rect.copy(), rect.right + width))
        return real_tail(surface, rect, width, *args)

    def fret(surface, rect, value, *args, **kwargs):
        calls.append("head")
        heads[value] = rect.copy()
        return real_fret(surface, rect, value, *args, **kwargs)

    monkeypatch.setattr(screen, "_draw_tail", tail)
    monkeypatch.setattr(screen, "_draw_fret", fret)
    screen.render(canvas)
    assert len(tails) == 2
    assert calls == sorted(calls, key=lambda kind: kind == "head")
    onset0, onset2 = ((heads[0].left, heads[2].left) if look == "studio" else
                      (heads[0].centerx, heads[2].centerx))
    # Independent scale from two rendered onset anchors, 200 ms apart.
    expected_end = onset0 + (onset2 - onset0) * (900 / 200)
    assert tails[0][1] == pytest.approx(expected_end, abs=3)


def test_an_extremely_long_hold_never_allocates_a_song_sized_image(monkeypatch):
    set_look("studio")
    canvas = pygame.Surface((876, 706))
    canvas.set_clip(pygame.Rect(100, 100, 600, 400))
    real_sprite = design.sprite
    allocated = []

    def sprite(key, size, painter):
        allocated.append(size)
        assert size[0] <= 600 and size[1] <= 400
        return real_sprite(key, size, painter)

    monkeypatch.setattr(design, "sprite", sprite)
    PlayingScreen._draw_tail(canvas, pygame.Rect(200, 300, 40, 34),
                             22_000_000, (180, 140, 255), 85)
    assert allocated


def test_song_header_reports_the_actual_alternate_tuning():
    screen = playing("studio", dense_timeline())
    screen.render(pygame.Surface((876, 706)))
    assert "D A D G" in screen._last_page.subtitle


@pytest.mark.parametrize("tempo", [0.5, 1, 1.5])
def test_stage_grid_passes_through_the_same_onsets_as_the_capsules(monkeypatch, tempo):
    from dataclasses import replace

    beats = [BeatInfo(stamp, 0, index) for index, stamp in enumerate((0, 500, 1000, 1400))]
    timeline = Timeline([NoteEvent(beat.timestamp_ms, 100, 28, 4, index)
                         for index, beat in enumerate(beats)],
                        SongMetadata(tuning={1: 43, 2: 38, 3: 33, 4: 28}), beats=beats)
    screen = playing("stage", timeline)
    screen._tempo_factor = tempo
    screen._playback_ms = 200
    canvas = pygame.Surface((876, 706))
    boxes = []
    real_fret = screen._draw_fret

    def fret(surface, rect, *args, **kwargs):
        boxes.append(rect.copy())
        return real_fret(surface, rect, *args, **kwargs)

    monkeypatch.setattr(screen, "_draw_fret", fret)
    screen.render(canvas)
    page = screen._last_page
    with_grid, without_grid = pygame.Surface(canvas.get_size()), pygame.Surface(canvas.get_size())
    paint_stage.play(with_grid, page)
    paint_stage.play(without_grid, replace(page, timeline_beats=[]))
    assert len(boxes) >= 3
    for box in boxes:
        # The lane centre is clear of rail seams and text. Removing only
        # score pulses must remove the light at the note's exact centre y.
        point = box.center
        assert with_grid.get_at(point) != without_grid.get_at(point), box
