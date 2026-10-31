"""Press everything, in every look, at every size, and see what falls over.

The player clicked the settings and the app died. The cause was mine and it was two
days old: I changed the hit map to hold a LIST of rects per name, because one
action may be drawn in more than one place, and updated the two screens that
read it -- but not the painter that also reads it, to withdraw the controls a
popover covers. It only fired on Hi-fi, only with the panel open, only on the
one row that switches look, because that is the only path that opens Studio's
setup card with a hit map built by somebody else.

No test of mine was going to find that by aiming at it. This file does not
aim: it exercises every control the app reports, every key it binds, in every
look, at every window size, on both screens, and asserts only that nothing
raises. It is slow and it is shallow and it would have caught it on the first
run.
"""

import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

from pickhero.ui import design  # noqa: E402
from pickhero.ui.colors import _LOOKS, get_look, set_look  # noqa: E402

LOOKS = sorted(_LOOKS)
SIZES = [(1280, 720), (1024, 640), (640, 480)]

# Every key either screen binds, plus the ones that look like they might.
KEYS = [pygame.K_SPACE, pygame.K_ESCAPE, pygame.K_RETURN, pygame.K_HOME,
        pygame.K_END, pygame.K_PAGEUP, pygame.K_PAGEDOWN, pygame.K_UP,
        pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_TAB,
        pygame.K_BACKSPACE, pygame.K_DELETE, pygame.K_F1]
KEYS += [getattr(pygame, f"K_{c}") for c in "abcdefghijklmnopqrstuvwxyz"]
KEYS += [getattr(pygame, f"K_{c}") for c in "0123456789"]


@pytest.fixture(scope="module", autouse=True)
def _display():
    pygame.init()
    pygame.display.set_mode((1280, 720))
    yield
    pygame.quit()


@pytest.fixture(autouse=True)
def _restore():
    before = get_look().key
    yield
    set_look(before)
    design.clear_caches()


class _InertJob:
    """Stands in for the import worker and never finishes."""

    def __init__(self, *args, **kwargs):
        self.done = self.cancelled = False
        self.error = self.result = self.prepared_song = None

    def poll(self):
        return False

    def cancel(self):
        self.cancelled = True


@pytest.fixture(autouse=True)
def _no_import_worker(monkeypatch):
    # "Anhören" on a file of their own decodes it in the import worker, which
    # is a second process. Pressing every key and clicking every control
    # reaches that button, and the seal in conftest rightly fails the run if
    # anything starts. What is tested here is that the click arrives.
    monkeypatch.setattr("pickhero.import_jobs.ImportJob", _InertJob)


def _library(tmp_path, look):
    from pathlib import Path

    from pickhero import instruments
    from pickhero.config import Config
    from pickhero.exercises import write_exercises
    from pickhero.progress import ProgressTracker, SongRecord
    from pickhero.ui.menu import MenuScreen

    write_exercises(instruments.BASS_UKULELE, tmp_path)
    tracker = ProgressTracker()
    tracker._data = {"01 one_open_string": SongRecord(best_accuracy=72.0,
                                                      attempts=4,
                                                      last_played="2026-09-29")}
    config = Config()
    config.instrument = "bass_ukulele"
    set_look(look)
    design.clear_caches()
    screen = MenuScreen(Path(tmp_path), config=config, progress=tracker)
    # "Ordner öffnen" starts the real file manager. This file clicks every
    # control in three looks at three sizes with the panel open and shut, so
    # the first run of it opened about forty Explorer windows on the desktop.
    # A test may not launch anything outside the process; what is being
    # tested is that the click arrives and nothing raises.
    screen._open_folder = lambda *a, **k: None
    return screen


def _play(look):
    from pathlib import Path

    from pickhero.config import Config
    from pickhero.matcher import NoteMatcher
    from pickhero.tabs.loader import load_gp_file
    from pickhero.ui.scrolling import PlayingScreen

    config = Config()
    config.instrument = "bass_ukulele"
    timeline = load_gp_file(Path("tests/fixtures/Demo_v5.gp5"),
                            instrument=config.get_instrument())
    set_look(look)
    design.clear_caches()
    screen = PlayingScreen(timeline, config=config)
    # Opening the real microphone and the real MIDI device a few hundred
    # times in a row takes the process down with an access violation inside
    # PortMidi. What is being tested is that the click arrives.
    for name in ("_start_audio", "_start_capture_only", "_stop_audio",
                 "stop_audio"):
        if hasattr(screen, name):
            setattr(screen, name, lambda *a, **k: None)
    screen._midi_player = None
    screen._audio_enabled = True
    if screen._matcher is None:
        screen._matcher = NoteMatcher(timeline)
    return screen


@pytest.mark.parametrize("look", LOOKS)
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("setup_open", [False, True])
class TestEveryControlInTheLibrary:
    def test_clicking_it_does_not_raise(self, tmp_path, look, size, setup_open):
        surface = pygame.Surface(size)
        probe = _library(tmp_path, look)
        probe._setup_open = setup_open
        probe.render(surface)
        targets = [(name, rect)
                   for name, rects in probe._last_page.hit.items()
                   for rect in rects]
        targets += [("row", rect) for rect, _i in probe._last_page.row_hit]
        assert targets, f"{look} at {size} reported nothing to click"
        for name, rect in targets:
            # A fresh screen each time, so one click cannot mask the next by
            # changing what is on screen.
            screen = _library(tmp_path, look)
            screen._setup_open = setup_open
            screen.render(surface)
            try:
                screen._handle_click(rect.center)
                screen.render(surface)
            except Exception as error:  # noqa: BLE001 - the point of the test
                pytest.fail(f"{look} {size} setup={setup_open}: clicking "
                            f"{name!r} at {rect} raised {error!r}")


@pytest.mark.parametrize("look", LOOKS)
@pytest.mark.parametrize("size", SIZES)
class TestEveryControlOnTheStage:
    def test_clicking_it_does_not_raise(self, look, size):
        surface = pygame.Surface(size)
        probe = _play(look)
        probe.render(surface)
        targets = [(name, rect)
                   for name, rects in probe._button_rects.items()
                   for rect in rects]
        assert targets, f"{look} at {size} reported nothing to click"
        for name, rect in targets:
            screen = _play(look)
            screen.render(surface)
            try:
                screen._handle_click(rect.center)
                screen.render(surface)
            except Exception as error:  # noqa: BLE001
                pytest.fail(f"{look} {size}: clicking {name!r} at {rect} "
                            f"raised {error!r}")


@pytest.mark.parametrize("look", LOOKS)
class TestEveryKey:
    def test_the_library_survives_every_key(self, tmp_path, look):
        surface = pygame.Surface((1280, 720))
        for key in KEYS:
            screen = _library(tmp_path, look)
            screen.render(surface)
            event = pygame.event.Event(pygame.KEYDOWN, key=key, unicode="x",
                                       mod=0)
            try:
                screen.handle_event(event)
                screen.render(surface)
            except Exception as error:  # noqa: BLE001
                pytest.fail(f"{look}: {pygame.key.name(key)} raised {error!r}")

    def test_the_stage_survives_every_key(self, look):
        surface = pygame.Surface((1280, 720))
        for key in KEYS:
            screen = _play(look)
            screen.render(surface)
            event = pygame.event.Event(pygame.KEYDOWN, key=key, unicode="x",
                                       mod=0)
            try:
                screen.handle_event(event)
                screen.render(surface)
            except Exception as error:  # noqa: BLE001
                pytest.fail(f"{look}: {pygame.key.name(key)} raised {error!r}")

    def test_a_key_while_the_settings_are_open(self, tmp_path, look):
        """The panel changes what the keys mean, and it is the state the
        crash lived in."""
        surface = pygame.Surface((1280, 720))
        for key in KEYS:
            screen = _library(tmp_path, look)
            screen._setup_open = True
            screen.render(surface)
            event = pygame.event.Event(pygame.KEYDOWN, key=key, unicode="x",
                                       mod=0)
            try:
                screen.handle_event(event)
                screen.render(surface)
            except Exception as error:  # noqa: BLE001
                pytest.fail(f"{look} with setup open: {pygame.key.name(key)} "
                            f"raised {error!r}")


class TestSwitchingLookMidClick:
    """The exact path that broke.

    Cycling the look repaints the next frame with a different painter, and
    that painter reads a hit map the previous one built. Every look must
    therefore survive inheriting every other look's map.
    """

    @pytest.mark.parametrize("look", LOOKS)
    def test_cycling_from_here_lands_everywhere(self, tmp_path, look):
        from pickhero.ui.colors import cycle_look

        surface = pygame.Surface((1280, 720))
        screen = _library(tmp_path, look)
        screen._setup_open = True
        screen.render(surface)
        for _ in range(len(LOOKS) * 2):
            cycle_look()
            screen.render(surface)
            assert screen._last_page.hit, "the new look reported nothing"

    @pytest.mark.parametrize("look", LOOKS)
    def test_the_setting_that_switches_it_can_be_clicked(self, tmp_path, look):
        """This is the click the player made. It is worth its own name."""
        surface = pygame.Surface((1280, 720))
        screen = _library(tmp_path, look)
        screen._setup_open = True
        screen.render(surface)
        rects = screen._last_page.hit.get("theme")
        if not rects:
            pytest.skip(f"{look} draws no look switch at this size")
        before = get_look().key
        screen._handle_click(rects[0].center)
        screen.render(surface)
        assert get_look().key != before, "the look did not change"


class TestTheClickChainKnowsItsOwnNames:
    """HANDLED and the if-chain beneath it must agree.

    A name in the chain but not in HANDLED is a control that is drawn, looks
    live and is skipped. A name in HANDLED but not in the chain is a region
    that swallows the click it should have blocked. The list was written by
    reading the chain once, which caught nine of the twelve names in it --
    the three on a shared `elif name in (...)` line were missed.
    """

    @staticmethod
    def _chain_names(method):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) \
                    and node.left.id == "name":
                for value in node.comparators:
                    if isinstance(value, ast.Constant):
                        found.add(value.value)
                    elif isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                        found.update(e.value for e in value.elts
                                     if isinstance(e, ast.Constant))
        return found

    def test_the_library_chain_matches_its_list(self):
        from pickhero.ui.menu import MenuScreen

        chain = self._chain_names(MenuScreen._handle_click)
        listed = set(MenuScreen.HANDLED)
        assert chain - listed == set(), (
            f"the chain answers to {sorted(chain - listed)}, which HANDLED "
            f"does not list -- those controls are skipped")
        assert listed - chain == set(), (
            f"HANDLED lists {sorted(listed - chain)}, which the chain does "
            f"nothing with -- those swallow clicks")

    def test_the_stage_chain_matches_its_list(self):
        from pickhero.ui.scrolling import PlayingScreen

        chain = self._chain_names(PlayingScreen._handle_click)
        listed = set(PlayingScreen.HANDLED)
        assert chain - listed == set(), sorted(chain - listed)
        assert listed - chain == set(), sorted(listed - chain)


@pytest.mark.parametrize("look", LOOKS)
class TestTheTempoCanBeSetWithTheMouse:
    """The player reported the Hi-fi dial did nothing.

    Three separate causes, and the sweep missed all three because it only
    pressed buttons and keys. The painter reported "tempo", the screen only
    answered to "tempo_down" and "tempo_up", so the dial was skipped. The
    wheel handler read the hit map as a single rect, which the list change
    had made a list -- an AttributeError in the one handler nothing swept.
    And the wheel took its position from pygame.mouse.get_pos(), which
    answers for the display rather than for this surface and which no test
    can set.
    """

    @staticmethod
    def _targets(screen):
        return [n for n in ("tempo", "tempo_down", "tempo_up")
                if screen._button_rects.get(n)]

    def test_the_wheel_over_it_changes_the_tempo(self, look):
        screen = _play(look)
        surface = pygame.Surface((1280, 720))
        screen.render(surface)
        names = self._targets(screen)
        if not names:
            pytest.skip(f"{look} draws no tempo control -- its drawing has none")
        rect = screen._button_rects[names[0]][0]
        screen.handle_event(pygame.event.Event(
            pygame.MOUSEMOTION, pos=rect.center, rel=(0, 0), buttons=(0, 0, 0)))
        screen._tempo_factor = 1.0
        screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=-1))
        assert screen._tempo_factor < 1.0, f"{look}: the wheel did nothing"
        slower = screen._tempo_factor
        screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=1))
        assert screen._tempo_factor > slower, f"{look}: it would not go back up"

    def test_the_wheel_elsewhere_leaves_it_alone(self, look):
        """Otherwise scrolling anywhere on the stage changes their tempo."""
        screen = _play(look)
        surface = pygame.Surface((1280, 720))
        screen.render(surface)
        screen.handle_event(pygame.event.Event(
            pygame.MOUSEMOTION, pos=(2, 2), rel=(0, 0), buttons=(0, 0, 0)))
        screen._tempo_factor = 0.9
        screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=-1))
        assert screen._tempo_factor == 0.9, f"{look}: the wheel acted off-target"

    def test_the_dial_turns_both_ways(self, look):
        """Hi-fi draws one dial where Studio draws a minus and a plus, so the
        dial has to carry both directions itself: its left half anticlockwise
        and its right half clockwise, the way a real one does."""
        screen = _play(look)
        surface = pygame.Surface((1280, 720))
        screen.render(surface)
        rects = screen._button_rects.get("tempo")
        if not rects:
            pytest.skip(f"{look} draws no single-dial tempo control")
        rect = rects[0]
        screen._tempo_factor = 0.8
        screen._handle_click((rect.left + 3, rect.centery))
        assert screen._tempo_factor < 0.8, "the left of the dial did not slow it"
        screen._tempo_factor = 0.8
        screen._handle_click((rect.right - 3, rect.centery))
        assert screen._tempo_factor > 0.8, "the right of the dial did not speed it"

    def test_the_wheel_never_raises_anywhere_on_the_stage(self, look):
        """The crash this found was an AttributeError on a list, and it fired
        wherever the pointer happened to be."""
        screen = _play(look)
        surface = pygame.Surface((1280, 720))
        screen.render(surface)
        for x in range(0, 1280, 97):
            for y in range(0, 720, 83):
                screen.handle_event(pygame.event.Event(
                    pygame.MOUSEMOTION, pos=(x, y), rel=(0, 0),
                    buttons=(0, 0, 0)))
                for direction in (-1, 1):
                    screen.handle_event(pygame.event.Event(
                        pygame.MOUSEWHEEL, x=0, y=direction))

    def test_the_library_wheel_never_raises(self, tmp_path, look):
        screen = _library(tmp_path, look)
        surface = pygame.Surface((1280, 720))
        screen.render(surface)
        for y in range(0, 720, 61):
            for direction in (-1, 1):
                screen.handle_event(pygame.event.Event(
                    pygame.MOUSEWHEEL, x=0, y=direction))
                screen.render(surface)


class TestTheDialCanBeDragged:
    """The player asked for drag-and-drop on the tempo dial, which is the gesture a
    knob actually invites.

    Vertically, which is how every dial in every audio application works.
    Following the angle instead means the value leaps the moment the pointer
    crosses the centre of the knob, and the centre is where their hand is.
    """

    @staticmethod
    def _knob(look):
        screen = _play(look)
        surface = pygame.Surface((1280, 720))
        screen.render(surface)
        rects = screen._button_rects.get("tempo")
        if not rects:
            pytest.skip(f"{look} draws a stepper, not a dial")
        return screen, rects[0]

    @staticmethod
    def _press(screen, pos):
        screen.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, pos=pos, button=1))

    @staticmethod
    def _move(screen, pos, held=True):
        screen.handle_event(pygame.event.Event(
            pygame.MOUSEMOTION, pos=pos, rel=(0, 0),
            buttons=(1 if held else 0, 0, 0)))

    @staticmethod
    def _release(screen, pos):
        screen.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONUP, pos=pos, button=1))

    @pytest.mark.parametrize("look", LOOKS)
    def test_dragging_down_slows_it_and_up_speeds_it(self, look):
        screen, knob = self._knob(look)
        start = knob.center
        screen._tempo_factor = 0.7
        self._press(screen, start)
        self._move(screen, (start[0], start[1] + 80))
        slower = screen._tempo_factor
        assert slower < 0.7, "dragging down did not slow it"
        self._move(screen, (start[0], start[1] - 80))
        assert screen._tempo_factor > slower, "dragging back up did not undo it"
        self._release(screen, (start[0], start[1] - 80))

    @pytest.mark.parametrize("look", LOOKS)
    def test_the_travel_is_proportional(self, look):
        """Twice the movement is twice the change, so the player can learn the
        gesture rather than hunting for a value."""
        screen, knob = self._knob(look)
        start = knob.center

        def after(dy):
            screen._tempo_factor = 1.0
            self._press(screen, start)
            self._move(screen, (start[0], start[1] + dy))
            value = screen._tempo_factor
            self._release(screen, (start[0], start[1] + dy))
            return 1.0 - value

        near, far = after(60), after(120)
        assert far > near
        assert abs(far - near * 2) <= 0.06, f"{near} then {far} is not linear"

    @pytest.mark.parametrize("look", LOOKS)
    def test_the_whole_range_takes_the_whole_travel(self, look):
        """Linearity alone says nothing about how far the player has to move: a
        scale twice as sensitive is still perfectly linear. This pins it.

        DRAG_TRAVEL from the top lands on the floor and no further, so the
        full sweep is one comfortable forearm movement and a single 0.05 step
        is sixteen pixels -- findable without being twitchy."""
        from pickhero.ui.scrolling import PlayingScreen, TEMPO_MIN

        screen, knob = self._knob(look)
        start = knob.center
        screen._tempo_factor = 1.0
        self._press(screen, start)
        half = int(PlayingScreen.DRAG_TRAVEL // 2)
        self._move(screen, (start[0], start[1] + half))
        middle = screen._tempo_factor
        self._move(screen, (start[0], start[1] + int(PlayingScreen.DRAG_TRAVEL)))
        bottom = screen._tempo_factor
        self._release(screen, (start[0], start[1]))
        assert abs(bottom - TEMPO_MIN) < 0.051, (
            f"a full travel of {PlayingScreen.DRAG_TRAVEL} px reached "
            f"{bottom}, not the floor of {TEMPO_MIN}")
        assert abs(middle - (1.0 + TEMPO_MIN) / 2) < 0.051, (
            f"half the travel reached {middle}, not the middle of the range")

    @pytest.mark.parametrize("look", LOOKS)
    def test_a_press_that_does_not_move_is_still_a_tap(self, look):
        """The click behaviour the player already had must survive a hand that
        twitches two pixels on the way down."""
        screen, knob = self._knob(look)
        screen._tempo_factor = 0.8
        spot = (knob.left + 3, knob.centery)
        self._press(screen, spot)
        self._move(screen, (spot[0] + 1, spot[1] + 1))
        self._release(screen, (spot[0] + 1, spot[1] + 1))
        assert screen._tempo_factor < 0.8, "the tap was swallowed by the drag"

    @pytest.mark.parametrize("look", LOOKS)
    def test_letting_go_outside_the_window_releases_it(self, look):
        """There is no MOUSEBUTTONUP for a button released off the window, so
        without the button state on the motion event the dial would stay
        stuck to the pointer and quietly change their tempo for the rest of the
        session."""
        screen, knob = self._knob(look)
        start = knob.center
        screen._tempo_factor = 0.9
        self._press(screen, start)
        self._move(screen, (start[0], start[1] + 40))
        self._move(screen, (start[0], start[1] + 40), held=False)
        settled = screen._tempo_factor
        for dy in (100, 200, 300):
            self._move(screen, (start[0], start[1] + dy), held=False)
        assert screen._tempo_factor == settled, "the dial is still being dragged"

    @pytest.mark.parametrize("look", LOOKS)
    def test_a_drag_that_starts_elsewhere_does_not_turn_it(self, look):
        screen, _knob = self._knob(look)
        screen._tempo_factor = 0.9
        self._press(screen, (4, 4))
        for dy in (20, 60, 120):
            self._move(screen, (4, 4 + dy))
        self._release(screen, (4, 124))
        assert screen._tempo_factor == 0.9, "dragging the board changed the tempo"
