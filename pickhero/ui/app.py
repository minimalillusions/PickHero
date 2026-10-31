"""PickHero application — PyGame game loop and state machine.

Two states: MENU (song selection) and PLAYING (scrolling display).
"""

from __future__ import annotations

from pathlib import Path
import time

import pygame

from pickhero.audio.input import validate_device_index
from pickhero.config import Config, log_error
from pickhero.i18n import available_languages, current_language, set_language, t
from pickhero.progress import ProgressTracker, song_key
from pickhero.curriculum import LessonPractice, practice_state
from pickhero.tabs.loader import extract_backing_track, load_gp_file
from pickhero.ui import design
from pickhero.ui.colors import get_look, set_look
from pickhero.exercises import ensure_exercises
from pickhero.ui.calibration_menu import CalibrationMenuScreen
from pickhero.ui.check_menu import CheckScreen
from pickhero.ui.device_menu import DeviceMenuScreen
from pickhero.ui.menu import MenuScreen
from pickhero.ui.scrolling import PlayingScreen
from pickhero import library
from pickhero.import_jobs import ImportJob
from pickhero.import_worker import decode_song
from pickhero.ui.import_menu import ImportScreen
from pickhero.timing_diagnostics import TimingDiagnostics
from pickhero.ui.timing_menu import TimingScreen
from pickhero.ui.test_recording import RecordingRequest
from pickhero.ui.recording_series import BANNER_HEIGHT, RecordingSeries


class App:
    """Main application with game loop."""

    def __init__(self, config: Config | None = None, *, background: bool = False):
        self._config = config or Config.load()
        self._progress = ProgressTracker()
        self._songs_dir = Path(self._config.songs_dir)
        self._running = False
        self._state = "menu"
        self._menu: MenuScreen | None = None
        self._playing_screen: PlayingScreen | None = None
        self._device_menu: DeviceMenuScreen | None = None
        self._calibration_menu: CalibrationMenuScreen | None = None
        self._check_menu: CheckScreen | None = None
        self._timing_menu = None
        self._frame_timing = TimingDiagnostics()
        self._last_timing_report = None
        self._utility_origin = "menu"
        self._load_error: tuple[Path, bool] | None = None
        self._error_focus = 0
        self._error_buttons: list[pygame.Rect] = []
        self._background = background
        self._import_job = None
        self._import_screen = None
        self._import_content_id = ""
        self._import_resume = None
        self._lesson_screen = None
        self._lesson_practice: LessonPractice | None = None
        self._recording_requested = False
        self._recording_series = RecordingSeries()
        self._lesson_return: LessonPractice | None = None
        self._setup_guide = None
        self._guide_origin = "menu"
        self._guide_check_purpose = "level"
        self._guide_instrument_changed = False

    def run(self) -> None:
        """Initialize PyGame, run main loop, clean up."""
        # Apply saved theme and language
        set_look(self._config.look)
        set_language(self._config.language)

        # Validate saved audio device — fall back to default if unavailable
        if not validate_device_index(self._config.audio.device_index):
            log_error(
                f"Saved audio device #{self._config.audio.device_index} not available, "
                "falling back to system default."
            )
            self._config.audio.device_index = None
            self._config.save()

        pygame.init()
        try:
            self._run_window()
        finally:
            self._close_screens()
            pygame.quit()

    def _run_window(self) -> None:
        """Keep resource cleanup around initialization and every event path."""
        pygame.key.set_repeat(300, 40)  # 300ms delay, then repeat every 40ms
        pygame.display.set_caption("PickHero")

        dc = self._config.display
        surface = pygame.display.set_mode(
            (dc.width, dc.height), pygame.RESIZABLE
        )
        clock = pygame.time.Clock()

        self._songs_dir = Path(self._config.songs_dir)
        # A beginner starts with an empty library, and real tabs are far too
        # hard to start on. Generate a graded set for the chosen instrument.
        try:
            ensure_exercises(self._config.get_instrument(), self._songs_dir,
                             progress=self._progress)
        except Exception as e:
            log_error(f"Could not generate exercises: {e}")
        self._build_menu()
        self._state = "menu"
        # Nobody has been through the input guide yet, so open it rather than
        # leaving it to be found. It walks instrument, device, level, tuning
        # and a test phrase -- exactly the route past a microphone that is too
        # quiet or a gate set too high.
        #
        # Here and not in run(): the menu has to exist first, so leaving the
        # guide lands somewhere, and the state assignment above would
        # otherwise overwrite it. The guide also builds its panel on
        # construction, which needs the fonts pygame.init provides.
        if not self._config.setup_done:
            self._open_guide()
        self._running = True

        previous_frame = None
        while self._running:
            frame_start = time.perf_counter_ns()
            measuring_frame = self._state == "playing" and self._playing_screen._playing
            if measuring_frame and previous_frame is not None:
                self._frame_timing.record("frame_interval_ms", (frame_start - previous_frame) / 1_000_000)
            previous_frame = frame_start if measuring_frame else None
            self._process_events(surface)
            if not self._running:
                break
            surface = pygame.display.get_surface()
            self._update()
            self._render(surface)
            pygame.display.flip()
            if measuring_frame and self._state == "playing" and self._playing_screen._playing:
                self._frame_timing.record("frame_processing_ms", (time.perf_counter_ns() - frame_start) / 1_000_000)
            clock.tick(60)

    def _build_menu(self) -> None:
        if self._menu is not None:
            self._menu.close()
        from pickhero.music import ensure_music
        try:
            ensure_music(self._config.get_instrument(), self._songs_dir, progress=self._progress)
        except (OSError, ValueError) as exc:
            log_error(f"Could not generate music: {exc}")
        self._menu = MenuScreen(self._songs_dir, config=self._config,
                                progress=self._progress, background=self._background)

    def _cycle_language(self) -> None:
        """Step to the next installed language and remember it.

        The menu is rebuilt rather than redrawn. It reads the device name and
        the sort labels once, when it is constructed, so simply carrying on
        would leave half the screen in the old language and look like the key
        had done nothing.
        """
        codes = [code for code, _ in available_languages()]
        if len(codes) < 2:
            return
        try:
            nxt = codes[(codes.index(current_language()) + 1) % len(codes)]
        except ValueError:
            nxt = codes[0]
        set_language(nxt)
        self._config.language = nxt
        self._config.save()
        kept = self._menu.carry_state() if self._menu is not None else None
        self._build_menu()
        if kept is not None and self._menu is not None:
            self._menu.restore_state(kept)

    def _close_screens(self) -> None:
        """Let go of the microphone, whichever screen is holding it."""
        self._recording_series.end()
        if self._import_job is not None:
            self._import_job.cancel()
            self._import_job = None
        for screen in (self._playing_screen, self._check_menu, self._timing_menu,
                       self._calibration_menu, self._menu, self._lesson_screen):
            if screen is None:
                continue
            closer = getattr(screen, "close", None) or getattr(screen, "stop_audio", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception as exc:
                log_error(f"Audio konnte nicht freigegeben werden: {exc}")

    def _process_events(self, surface: pygame.Surface) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self._running = False
                self._close_screens()
                return

            if event.type == pygame.VIDEORESIZE:
                pygame.display.set_mode(
                    (event.w, event.h), pygame.RESIZABLE
                )

            searching = self._state == 'menu' and self._menu is not None and self._menu.is_searching
            if self._recording_series.handle_event(event, searching=searching):
                continue
            if (self._recording_series.visible and event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP)
                    and event.pos[1] >= surface.get_height()-BANNER_HEIGHT):
                continue
            if self._load_error is not None:
                self._handle_load_error(event)
                continue

            if self._state == "menu":
                self._handle_menu_event(event)
            elif self._state == "playing":
                self._handle_playing_event(event)
            elif self._state == "device":
                self._handle_device_event(event)
            elif self._state == "check":
                self._handle_check_event(event)
            elif self._state == "calibration":
                self._handle_calibration_event(event)
            elif self._state == "timing":
                result = self._timing_menu.handle_event(event)
                if result == "back":
                    self._return_from_utility()
            elif self._state == "import":
                self._handle_import_event(event)
            elif self._state == "lessons":
                result = self._lesson_screen.handle_event(event)
                if isinstance(result, RecordingRequest):
                    if self._recording_series.visible:
                        self._recording_series.end()
                    else:
                        self._lesson_screen.close()
                        self._load_song(result.path, track_index=0, practice=result.practice, recording=True)
                elif isinstance(result, LessonPractice):
                    self._lesson_screen.close()
                    self._load_song(result.path, track_index=0, practice=result)
                elif isinstance(result, Path):
                    self._lesson_screen.close()
                    self._load_song(result)
                elif result == "music_library":
                    self._lesson_screen.close()
                    self._open_lessons()
                elif result in ("menu", "back"):
                    self._lesson_screen.close()
                    self._state = "menu"
                    if self._lesson_screen.generated_music:
                        self._menu.scan_files()
            elif self._state == "wizard":
                self._handle_guide_event(event)

    def _handle_menu_event(self, event: pygame.event.Event) -> None:
        if (event.type == pygame.KEYDOWN and not self._menu.is_searching
                and getattr(self._menu, "_resume_panel", None) is None):
            utility = {pygame.K_d: "device", pygame.K_c: "check",
                       pygame.K_g: "calibrate", pygame.K_F3: "timing"}.get(event.key)
            if utility:
                self._menu.catalogue.stop()
                self._open_utility(utility)
                return
            if event.key == pygame.K_l:
                self._cycle_language()
                return

        result = self._menu.handle_event(event)
        if result in ("escape", "quit"):
            self._running = False
        elif result == "language":
            self._cycle_language()
        elif result in ("check", "calibrate", "device", "timing"):
            self._open_utility(result)
        elif result == "wizard":
            self._open_guide()
        elif result == "lessons":
            self._menu.select_category("technique")
        elif result == "lesson_steps":
            from pickhero.curriculum import lesson_for_path
            path = self._menu._selected_path()
            lesson = lesson_for_path(path) if path else None
            if lesson:
                self._open_lessons(music_first=False)
                self._lesson_screen._show_lesson(lesson.order-1)
        elif result == "resume":
            self._resume_latest()
        elif result == "record":
            if self._recording_series.visible:
                self._recording_series.end()
                return
            path = self._menu._selected_path()
            if path is not None:
                self._load_song(path, recording=True, practice=self._menu.practice_for(path))
            else:
                self._open_lessons()
        elif isinstance(result, Path):
            practice = self._menu.practice_for(result)
            self._load_song(result, **({"practice": practice} if practice else {}))

    def _handle_playing_event(self, event: pygame.event.Event) -> None:
        result = self._playing_screen.handle_event(event)
        completed = self._playing_screen._song_completed
        if result == "menu":
            self._playing_screen.stop_audio()
            self._playing_screen = None
            if self._lesson_return is not None and not completed:
                self._open_lessons(practice=self._lesson_return)
            else:
                self._state = "menu"
                self._menu.scan_files()
        elif result in ("check", "calibrate", "device", "timing"):
            self._open_utility(result)
        elif result == "wizard":
            self._open_guide()
        elif result == "next_lesson":
            request = self._playing_screen._next_lesson_practice
            if request is not None:
                self._playing_screen.stop_audio()
                self._playing_screen = None
                self._lesson_return = None
                # A failed synchronous import must still leave a usable menu.
                self._state = "menu"
                self._load_song(request.path, track_index=0, practice=request)
        elif result == "lessons":
            self._playing_screen.stop_audio()
            music_first = self._playing_screen._lesson is None
            self._playing_screen = None
            self._open_lessons(practice=None if completed else self._lesson_return,
                               music_first=music_first)

    def _open_guide(self):
        from pickhero.ui.setup_guide import SetupGuideScreen
        self._guide_origin = self._state
        self._guide_instrument_changed = False
        self._guide_check_purpose = "level"
        instrument = None
        if self._state == "playing" and self._playing_screen is not None:
            self._playing_screen.suspend_for_setup()
            instrument = self._playing_screen._instrument
        self._setup_guide = SetupGuideScreen(self._config, instrument=instrument,
                                             can_change_instrument=self._state != "playing")
        self._state = "wizard"

    def _mark_setup_seen(self) -> None:
        """Remember that the guide was used, so it stops opening itself.

        Only when the visit actually produced a check. Walking through with
        the arrow keys measures nothing, and recording that as done would hide
        the guide for good from the one person it exists for. test_setup_guide
        states the same rule from the other side: opening and skipping the
        guide must not change a setting, because a report is an observation
        from this visit and not a certificate.

        So the guide keeps coming back until one level, tuning or phrase check
        has been run in it. That is not nagging -- it is a setup step that has
        not been done. A failed save is not worth interrupting anyone over;
        the worst case is the guide opening once more.
        """
        guide = self._setup_guide
        if self._config.setup_done or guide is None or not guide.reports:
            return
        self._config.setup_done = True
        try:
            self._config.save()
        except Exception:
            pass

    def _handle_guide_event(self, event):
        action = self._setup_guide.handle_event(event)
        if action == "back":
            self._mark_setup_seen()
            self._setup_guide = None
            if self._guide_origin == "playing" and self._playing_screen is not None:
                self._playing_screen.resume_from_setup()
                self._state = "playing"
            else:
                self._state = "menu"
                if self._guide_instrument_changed:
                    self._build_menu()
        elif action == "language" and self._guide_origin != "playing":
            # Explicit, like the instrument: a key press, not a measurement,
            # so it does not count as having used the guide.
            self._cycle_language()
            self._setup_guide.refresh()
        elif action == "instrument" and self._guide_origin != "playing":
            from pickhero.instruments import PROFILES
            keys = list(PROFILES)
            current = keys.index(self._config.instrument) if self._config.instrument in keys else -1
            next_key = keys[(current + 1) % len(keys)]
            self._config.switch_instrument(next_key)
            self._config.active_strings = [True] * self._config.get_instrument().string_count
            self._config.max_fret = None
            self._config.save()
            try:
                ensure_exercises(self._config.get_instrument(), self._songs_dir, progress=self._progress)
            except Exception as exc:
                log_error(f"Could not generate exercises: {exc}")
            self._setup_guide.instrument_changed(self._config.get_instrument())
            self._guide_instrument_changed = True
        elif action == "device":
            self._open_utility("device")
        elif action in ("level", "tune", "phrase"):
            self._guide_check_purpose = action
            self._open_utility("check")

    def _open_lessons(self, *, practice=None, music_first=True):
        from pickhero.music import music_for_path
        if (music_first and practice is None) or (practice is not None and music_for_path(practice.path)):
            if self._lesson_screen is not None:
                self._lesson_screen.close()
            if self._menu is None:
                self._build_menu()
            self._menu.select_category("music")
            if practice is not None:
                self._menu.focus_path(practice.path)
            self._state = "menu"
            return
        from pickhero.ui.lesson_menu import LessonScreen
        self._lesson_screen = LessonScreen(config=self._config, progress=self._progress,
                                           songs_dir=self._songs_dir,
                                           music_first=False, library_navigation=True)
        if practice is not None:
            self._lesson_screen.show_practice(practice)
        self._state = "lessons"

    def _resume_latest(self):
        saved = self._progress.latest_session(self._config.instrument)
        if saved is None:
            return
        # Prefer the indexed content identity: a moved file retains the same
        # key, while a different score at the old path must not inherit it.
        path = Path(saved["song_path"])
        identity = saved.get("song_key", "")
        for song in self._menu._songs:
            if song_key(song.file, self._config.instrument, saved["track_index"],
                        content_id=song.content_id) == identity:
                path = song.file
                break
        self._load_song(path, track_index=saved["track_index"], resume=saved)

    def _close_utility(self) -> None:
        """Release a utility's stream before another screen opens an input."""
        for name in ("_device_menu", "_check_menu", "_calibration_menu", "_timing_menu"):
            screen = getattr(self, name)
            if screen is not None:
                close = getattr(screen, "close", None)
                if close is not None:
                    close()
                setattr(self, name, None)

    def _open_utility(self, action: str) -> None:
        if action == "timing":
            source = self._check_menu if self._state == "check" else self._playing_screen
            capture = getattr(source, "_capture", None) or getattr(source, "_audio_capture", None)
            snapshot = getattr(capture, "diagnostic_snapshot", None)
            self._last_timing_report = snapshot() if callable(snapshot) else None
        if self._state not in ("device", "check", "calibration", "timing"):
            self._utility_origin = self._state
            if self._state == "playing" and self._playing_screen is not None:
                self._playing_screen.suspend_for_setup()
        self._close_utility()
        factories = {"device": ("device", "_device_menu", DeviceMenuScreen),
                     "check": ("check", "_check_menu", CheckScreen),
                     "calibrate": ("calibration", "_calibration_menu", CalibrationMenuScreen),
                     "timing": ("timing", "_timing_menu", TimingScreen)}
        state, attribute, factory = factories[action]
        options = {}
        if action != "device" and self._utility_origin == "playing" and self._playing_screen is not None:
            options["instrument"] = self._playing_screen._instrument
        elif action != "device" and self._utility_origin == "wizard" and self._setup_guide is not None:
            options["instrument"] = self._setup_guide.instrument
        if action == "check" and self._utility_origin == "wizard":
            options["purpose"] = self._guide_check_purpose
        if action == "timing":
            options["report"] = {"audio": self._last_timing_report,
                "frames": self._frame_timing.snapshot({"look": self._config.look})}
        setattr(self, attribute, factory(self._config, **options))
        self._state = state

    def _return_from_utility(self) -> None:
        self._close_utility()
        if self._menu is not None:
            self._menu.refresh_device_name()
        if self._utility_origin == "wizard" and self._setup_guide is not None:
            self._setup_guide.refresh()
            self._state = "wizard"
        elif self._utility_origin == "playing" and self._playing_screen is not None:
            self._playing_screen.resume_from_setup()
            self._state = "playing"
        else:
            self._state = "menu"

    def _handle_device_event(self, event: pygame.event.Event) -> None:
        result = self._device_menu.handle_event(event)
        if result in ("back", "selected"):
            if result == "selected" and self._utility_origin == "wizard" and self._setup_guide is not None:
                self._setup_guide.device_changed()
            self._return_from_utility()
        elif result in ("check", "calibrate"):
            self._open_utility(result)

    def _handle_calibration_event(self, event: pygame.event.Event) -> None:
        result = self._calibration_menu.handle_event(event)
        if result == "back":
            self._return_from_utility()
        elif result in ("device", "check"):
            self._open_utility(result)
        elif result == "complete":
            # Save calibration results to config
            from datetime import datetime
            results = self._calibration_menu.get_results()
            # Tag the calibration with the instrument it was taken on, so a
            # guitar calibration is never applied to a bass ukulele.
            self._config.calibration = {
                "strings": {},
                "calibrated_at": datetime.now().isoformat(),
                "instrument": self._config.instrument,
            }
            for string_num, cal in results.items():
                self._config.set_string_calibration(string_num, cal)
            self._config.save()
            if self._utility_origin == "wizard" and self._setup_guide is not None:
                self._setup_guide.reports.clear()
            self._return_from_utility()

    def _handle_check_event(self, event: pygame.event.Event) -> None:
        if self._check_menu is None:
            return
        report = (self._check_menu.diagnostic_snapshot()
                  if self._utility_origin == "wizard" else None)
        result = self._check_menu.handle_event(event)
        if result == "back":
            if report is not None and self._setup_guide is not None:
                self._setup_guide.record_check(self._check_menu._purpose, report)
            self._return_from_utility()
        elif result in ("device", "calibrate", "timing"):
            self._open_utility(result)

    def _load_song(self, path: Path, *, track_index=None, resume=None, practice=None,
                   recording=False) -> None:
        """Load a GP file and switch to playing state."""
        self._load_error = None
        self._lesson_practice = practice
        self._recording_requested = recording
        if self._background:
            if self._import_job is not None:
                self._import_job.cancel()
            self._import_resume = resume
            self._import_content_id = ""
            self._import_screen = ImportScreen(path)
            operation = "inspect" if track_index is None else "load"
            self._import_job = ImportJob(operation, path, self._config.instrument,
                                         track_index=track_index)
            self._state = "import"
            return
        try:
            options = {"track_index": track_index} if track_index is not None else {}
            timeline = load_gp_file(path, instrument=self._config.get_instrument(), **options)
        except Exception as e:
            try:
                log_error(f"Error loading {path}: {e}")
            except UnicodeEncodeError:
                log_error(f"Error loading {path}: {type(e).__name__}")
            self._load_error = (Path(path), False)
            self._error_focus = 0
            return

        if not len(timeline):
            self._load_error = (Path(path), True)
            self._error_focus = 0
            return

        # Extract backing track (non-guitar tracks as MIDI)
        backing_track = None
        try:
            backing_track = extract_backing_track(
                path, exclude_track_indices={timeline.metadata.track_index},
            )
        except Exception as e:
            log_error(f"Backing track extraction failed: {e}")

        try:
            content_id = library.content_identity(path)
        except (OSError, ValueError):
            content_id = ""
        self._start_song(path, timeline, backing_track, content_id, resume)

    def _start_song(self, path, timeline, backing_track, content_id="", resume=None):
        practice, self._lesson_practice = self._lesson_practice, None
        prepared_state = None
        if practice is not None:
            try:
                if Path(path).resolve() != practice.path.resolve():
                    raise ValueError("Lesson practice belongs to a different file")
                prepared_state = practice_state(practice, timeline, self._config.get_instrument())
            except ValueError:
                self._state = "menu"
                self._load_error = (Path(path), False)
                self._error_focus = 0
                return
        if not len(timeline):
            self._state = "menu"
            self._load_error = (Path(path), True)
            self._error_focus = 0
            return
        index = timeline.metadata.track_index
        self._progress.migrate_content_identity(path, self._config.instrument, index, content_id)
        key = song_key(path, self._config.instrument, index, content_id=content_id)
        if resume is not None and resume.get("song_key") != key:
            self._state = "menu"
            self._load_error = (Path(path), False)
            self._error_focus = 0
            return
        dc = self._config.display
        self._frame_timing.reset()
        self._last_timing_report = None
        self._playing_screen = PlayingScreen(
            timeline,
            visible_beats=dc.visible_beats,
            hit_zone_fraction=dc.hit_zone_fraction,
            config=self._config,
            backing_track=backing_track,
            progress_tracker=self._progress,
            song_key=key, song_path=str(path), track_index=index,
            lesson_practice=practice,
        )
        self._state = "playing"
        self._playing_screen._test_recording.series = self._recording_series

        # Skip ahead so the first note is just entering the visible window
        if timeline.notes:
            first_note_ms = timeline.notes[0].timestamp_ms
            seek_to = max(0.0, first_note_ms - self._playing_screen._visible_window_ms)
            if seek_to > 0:
                self._playing_screen.seek(seek_to)
        if resume is not None:
            self._playing_screen.restore_practice_state(resume)
        elif prepared_state is not None:
            self._playing_screen.restore_practice_state(prepared_state)
        self._lesson_return = practice
        if self._recording_requested:
            self._recording_requested = False
            self._playing_screen._test_recording.open()

    def _handle_import_event(self, event):
        action = self._import_screen.handle_event(event)
        if action == "cancel":
            self._lesson_practice = None
            self._recording_requested = False
            if self._import_job is not None:
                self._import_job.cancel()
                self._import_job = None
            self._import_screen = None
            self._state = "menu"
        elif action == "selection":
            self._refresh_import_resume()
        elif action in ("start", "resume"):
            screen = self._import_screen
            index = screen.track_index
            if index is None:
                return
            key = song_key(screen.path, self._config.instrument, index,
                           content_id=self._import_content_id)
            self._import_resume = self._progress.get_session(key) if action == "resume" else None
            self._import_job = ImportJob("load", screen.path, self._config.instrument,
                track_index=index, content_id=self._import_content_id)
            self._import_screen = ImportScreen(screen.path)

    def _refresh_import_resume(self):
        screen = self._import_screen
        index = screen.track_index
        if index is None:
            screen.resume_available = False
            return
        self._progress.migrate_content_identity(screen.path, self._config.instrument,
                                                index, self._import_content_id)
        key = song_key(screen.path, self._config.instrument, index,
                       content_id=self._import_content_id)
        screen.resume_state = self._progress.get_session(key)
        screen.resume_available = screen.resume_state is not None

    def _poll_import(self):
        job = self._import_job
        if job is None:
            return
        done = job.poll()
        self._import_screen.status = job.status
        if not done:
            return
        self._import_job = None
        path = self._import_screen.path
        if job.error:
            self._state = "menu"
            self._load_error = (path, False)
            self._error_focus = 0
            return
        if job.operation == "inspect":
            self._import_content_id = job.result["content_id"]
            self._import_screen = ImportScreen(path, tracks=job.result["tracks"],
                                               selected=job.result["selected"])
            self._refresh_import_resume()
        else:
            try:
                prepared = getattr(job, "prepared_song", None)
                timeline, backing = prepared if prepared is not None else decode_song(job.result["song"])
                self._start_song(path, timeline, backing, job.result["content_id"], self._import_resume)
            except (ValueError, TypeError, KeyError):
                self._state = "menu"
                self._load_error = (path, False)
                self._error_focus = 0
            self._import_screen = None

    def _update(self) -> None:
        self._recording_series.update()
        if self._menu is not None:
            self._menu.update()
        if self._load_error is not None:
            return
        if self._state == "playing" and self._playing_screen is not None:
            self._playing_screen.update()
        elif self._state == "check" and self._check_menu is not None:
            self._check_menu.update()
        elif self._state == "calibration" and self._calibration_menu is not None:
            self._calibration_menu.update()
        elif self._state == "timing" and self._timing_menu is not None:
            if self._timing_menu.update() == "back":
                self._return_from_utility()
        elif self._state == "import":
            self._poll_import()
        elif self._state == "lessons":
            self._lesson_screen.update()

    def _render(self, surface: pygame.Surface) -> None:
        full_surface = surface
        if self._recording_series.visible:
            surface = surface.subsurface((0, 0, surface.get_width(), max(1, surface.get_height()-BANNER_HEIGHT)))
        if self._state == "menu" and self._menu is not None:
            self._menu.render(surface)
        elif self._state == "playing" and self._playing_screen is not None:
            self._playing_screen.render(surface)
        elif self._state == "device" and self._device_menu is not None:
            self._device_menu.render(surface)
        elif self._state == "check" and self._check_menu is not None:
            self._check_menu.render(surface)
        elif self._state == "calibration" and self._calibration_menu is not None:
            self._calibration_menu.render(surface)
        elif self._state == "timing" and self._timing_menu is not None:
            self._timing_menu.render(surface)
        elif self._state == "import" and self._import_screen is not None:
            if self._menu is not None:
                self._menu.render(surface)
            self._import_screen.render(surface, background=self._menu is not None)
        elif self._state == "lessons":
            self._lesson_screen.render(surface)
        elif self._state == "wizard":
            self._setup_guide.render(surface)
        if self._load_error is not None:
            self._draw_load_error(surface)
        self._recording_series.draw(full_surface)

    def _handle_load_error(self, event: pygame.event.Event) -> None:
        action = None
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                action = 0
            elif event.key in (pygame.K_TAB, pygame.K_LEFT, pygame.K_RIGHT):
                self._error_focus = 1 - self._error_focus
            elif event.key in (pygame.K_RETURN, pygame.K_SPACE):
                action = self._error_focus
            elif event.key == pygame.K_r:
                action = 1
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            action = next((i for i, rect in enumerate(self._error_buttons)
                           if rect.collidepoint(event.pos)), None)
        if action is not None and self._load_error is not None:
            path = self._load_error[0]
            self._load_error = None
            if action == 1:
                if self._lesson_practice is not None:
                    self._load_song(path, track_index=0, practice=self._lesson_practice,
                                    recording=self._recording_requested)
                else:
                    self._load_song(path, recording=self._recording_requested)
            else:
                self._lesson_practice = None
                self._recording_requested = False

    def _draw_load_error(self, surface: pygame.Surface) -> None:
        """Explain failed imports without exposing a traceback or losing selection."""
        look = get_look()
        width, height = surface.get_size()
        veil = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
        veil.fill((*design.scrim(look.bg), 200))
        surface.blit(veil, (0, 0))
        panel = pygame.Rect(0, 0, max(1, min(580, width - 20)), max(1, min(310, height - 20)))
        panel.center = surface.get_rect().center
        radius = 0 if look.key == "hifi" else 12 if look.key == "studio" else 4
        pygame.draw.rect(surface, look.popover, panel, border_radius=radius)
        pygame.draw.rect(surface, look.hud_accent if look.key == "stage" else look.hairline_loud,
                         panel, width=2 if look.key == "stage" else 1, border_radius=radius)
        path, empty = self._load_error
        title = t("app.load_empty_title" if empty else "app.load_error_title")
        body = t("app.load_empty_body" if empty else "app.load_error_body")
        title_size = 23 if panel.width >= 480 else 17
        x, y = panel.left + 16, panel.top + 14
        available = max(1, panel.width - 32)

        def lines(value, size):
            face = design.font(size)
            pending, result = "", []
            for char in value:
                if pending and face.size(pending + char)[0] > available:
                    # Prefer a word boundary, but still bound a long filename.
                    cut = pending.rfind(" ")
                    if cut > 0:
                        result.append(pending[:cut]); pending = pending[cut + 1:]
                    else:
                        result.append(pending); pending = ""
                pending += char
            return result + ([pending] if pending else [])

        for line in lines(title, title_size)[:2]:
            design.text(surface, line, (x, y), look.ink_1, title_size)
            y += design.font(title_size).get_linesize()
        filename = path.name
        while filename and design.font(13).size(filename)[0] > available:
            filename = filename[:-2] + "…" if len(filename) > 2 else ""
        design.text(surface, filename, (x, y + 5), look.ink_3, 13)
        y += 29
        previous_clip = surface.get_clip()
        body_rect = pygame.Rect(x, y, available, max(0, panel.bottom - 66 - y))
        surface.set_clip(previous_clip.clip(body_rect))
        for line in lines(body, 14):
            design.text(surface, line, (x, y), look.ink_2, 14)
            y += design.font(14).get_linesize()
        surface.set_clip(previous_clip)
        button_width = (panel.width - 40) // 2
        self._error_buttons = []
        for i, label in enumerate((t("app.back_library"), t("app.retry"))):
            box = pygame.Rect(panel.left + 16 + i * (button_width + 8),
                              panel.bottom - 52, button_width, 36)
            self._error_buttons.append(box)
            focused = i == self._error_focus
            pygame.draw.rect(surface, look.select_fill, box, border_radius=radius)
            pygame.draw.rect(surface, look.hud_accent if focused else look.hairline_loud,
                             box, width=2 if focused else 1, border_radius=radius)
            size = 14
            while size > 10 and design.font(size).size(label)[0] > box.width - 12:
                size -= 1
            design.text(surface, label, (box.centerx, box.centery - design.font(size).get_height() // 2),
                        look.ink_1, size, align="center")
