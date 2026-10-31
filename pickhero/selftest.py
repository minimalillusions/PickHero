"""Bounded offline smoke test for source installations and packaged builds."""
from contextlib import ExitStack
import json
import importlib
import hashlib
import os
from pathlib import Path
import pyexpat
import struct
import sys
import tempfile
import traceback
from unittest.mock import patch


def check_runtime() -> None:
    """The tested native wheels require patched 64-bit CPython 3.12."""
    if (sys.version_info[:2] != (3, 12) or sys.version_info[:3] < (3, 12, 14)
            or struct.calcsize("P") != 8):
        raise RuntimeError("PickHero requires 64-bit Python 3.12.14 or newer within 3.12.")
    if pyexpat.version_info < (2, 8, 3):
        raise RuntimeError("PickHero requires the tested Expat 2.8.3 or newer.")


def run(output: Path) -> bool:
    """Exercise bundled resources in isolation, with no physical audio I/O."""
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    result = {"ok": False, "python": sys.version.split()[0],
              "expat": pyexpat.EXPAT_VERSION, "render_cases": 0,
              "interface_render_cases": 0, "expansion_render_cases": 0,
              "completion_render_cases": 0, "music_render_cases": 0,
              "library_render_cases": 0, "audio_profiles": 0}
    try:
        check_runtime()
        resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        notices = resource_root / "licenses"
        manifest = json.loads((notices / "manifest.json").read_text(encoding="utf-8"))
        assert (notices / "THIRD_PARTY_NOTICES.html").is_file()
        assert manifest["schema"] == 1 and manifest["notice_files"]
        for entry in manifest["notice_files"]:
            path = (notices / entry["path"]).resolve()
            assert path.is_relative_to(notices.resolve()), "Invalid notice path"
            assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], entry["path"]
        result["license_texts_verified"] = len(manifest["notice_files"])
        import numpy as np
        import pygame
        from pickhero.audio.detector import PitchDetector
        from pickhero import config, library, progress, instruments
        from pickhero.exercises import write_exercises
        from pickhero.curriculum import LessonPractice, practice_steps
        from pickhero.i18n import set_language, t
        from pickhero.tabs.loader import load_gp_file
        from pickhero.ui.colors import set_look
        from pickhero.ui.menu import MenuScreen
        from pickhero.ui.page import painter
        from pickhero.ui.scrolling import PlayingScreen
        from pickhero.ui.lesson_menu import LessonScreen
        from pickhero.ui.music_menu import MusicScreen
        from pickhero.music import PIECES, VERSIONS, build_music_demo
        from pickhero.ui.import_menu import ImportScreen
        from pickhero.ui.setup_guide import SetupGuideScreen
        from pickhero.ui.passage_menu import PassagePanel
        from pickhero.ui import device_menu, check_menu, calibration_menu, timing_menu
        from pickhero.audio import input as audio_input

        class TestCapture:
            """A deterministic input; a packaged smoke test never opens hardware."""
            def __init__(self, config, *, instrument=None):
                from types import SimpleNamespace
                self.instrument = instrument or config.get_instrument()
                self.detector = SimpleNamespace(
                    noise_gate_db=-45, expected_delay_ms=100,
                    set_noise_gate_db=lambda value: None, reset=lambda: None)
                self.sample_rate = 44100

            def start(self): pass
            def stop(self): pass
            def capture_elapsed_ms(self): return 0.0
            def get_notes(self): return []
            def get_signal_db(self): return -30.0
            def get_tuner_data(self): return (98.0, .95)
            def health_issue(self): return None

        with tempfile.TemporaryDirectory(prefix="pickhero-selftest-") as tmp, ExitStack() as stack:
            root = Path(tmp)
            # SDL can keep a font/file handle alive. Always release it before
            # TemporaryDirectory removes files, including on a render error.
            stack.callback(pygame.quit)
            for module in (config, library, progress):
                stack.enter_context(patch.object(module, "CONFIG_DIR", root))
            stack.enter_context(patch.object(config, "CONFIG_FILE", root / "settings.json"))
            stack.enter_context(patch.object(library, "CACHE_FILE", root / "library.json"))
            stack.enter_context(patch.object(progress, "PROGRESS_FILE", root / "progress.json"))
            stack.enter_context(patch.object(MenuScreen, "_resolve_device_name", return_value="self-test"))
            stack.enter_context(patch.object(PlayingScreen, "_init_midi_player", return_value=None))
            stack.enter_context(patch.object(audio_input, "AudioCapture", TestCapture))
            stack.enter_context(patch.object(audio_input, "list_audio_devices", return_value=[]))
            stack.enter_context(patch.object(audio_input, "best_input_device", return_value=None))
            for screen_module in (check_menu, calibration_menu):
                stack.enter_context(patch.object(screen_module, "AudioCapture", TestCapture))
            stack.enter_context(patch.object(check_menu, "list_audio_devices", return_value=[]))
            stack.enter_context(patch.object(device_menu, "list_audio_devices", return_value=[]))
            pygame.display.init()
            pygame.font.init()
            pygame.display.set_mode((1280, 720))
            for profile in instruments.PROFILES.values():
                detector = PitchDetector(instrument=profile)
                detector.process(np.zeros(detector.hop_size, dtype=np.float32))
                result["audio_profiles"] += 1
                songs = root / profile.key
                files = write_exercises(profile, songs / "Uebungen")
                cfg = config.Config(instrument=profile.key, songs_dir=str(songs))
                timeline = load_gp_file(files[0], instrument=profile)
                assert len(timeline) > 0
                for language in ("de", "en"):
                    catalogue = importlib.import_module(f"pickhero.lang.{language}")
                    assert "check.title" in catalogue.STRINGS
                    set_language(language)
                    assert t("check.title") != "check.title", f"Missing catalogue: {language}"
                    for look in ("studio", "stage", "hifi"):
                        set_look(look)
                        assert painter(look) is not None, f"Missing painter: {look}"
                        menu = MenuScreen(songs, cfg)
                        play = PlayingScreen(timeline, config=cfg)
                        for size in ((1280, 720), (640, 480)):
                            for screen in (menu, play):
                                screen.render(pygame.Surface(size))
                                result["render_cases"] += 1
                            for factory in (device_menu.DeviceMenuScreen, check_menu.CheckScreen,
                                            calibration_menu.CalibrationMenuScreen, timing_menu.TimingScreen):
                                utility = factory(cfg)
                                try:
                                    utility.render(pygame.Surface(size))
                                    result["interface_render_cases"] += 1
                                finally:
                                    close = getattr(utility, "close", None)
                                    if close is not None:
                                        close()
                            help_screen = PlayingScreen(timeline, config=cfg)
                            help_screen.handle_event(pygame.event.Event(
                                pygame.KEYDOWN, key=pygame.K_h, mod=0, unicode="h"))
                            help_screen.render(pygame.Surface(size))
                            result["interface_render_cases"] += 1
                            result_screen = PlayingScreen(timeline, config=cfg, song_path=str(files[0]))
                            result_screen._song_completed = True
                            result_screen.render(pygame.Surface(size))
                            assert result_screen._panel.actions[0].name == "next_lesson"
                            result["interface_render_cases"] += 1
                            count_screen = PlayingScreen(timeline, config=cfg)
                            count_screen._playback_ms = -1000
                            count_screen._playing = True
                            count_screen.render(pygame.Surface(size))
                            result["interface_render_cases"] += 1
                        # These features must be included in the actual EXE,
                        # with real translated text even in the smallest view.
                        for size in ((1280, 720), (320, 240)):
                            target = pygame.Surface(size)
                            music = MusicScreen(cfg, progress.ProgressTracker(), songs)
                            assert not music.error
                            browser = MenuScreen(songs, cfg, progress.ProgressTracker())
                            try:
                                for piece_index, piece in enumerate(PIECES):
                                    for version in VERSIONS:
                                        music._select(piece_index)
                                        music.version = version
                                        music._refresh()
                                        assert browser.focus_path(music.path)
                                        browser.catalogue.detail_open = True
                                        browser.render(target)
                                        assert browser._button_rects.get("play")
                                        assert browser._button_rects.get("preview")
                                        result["music_render_cases"] += 1
                                        music_timeline = load_gp_file(music.path, track_index=0, instrument=profile)
                                        demo = build_music_demo(piece, version, profile)
                                        assert len(music_timeline) and len(demo.track)
                                        played = PlayingScreen(music_timeline, config=cfg, song_path=str(music.path))
                                        played._song_completed = True
                                        played.render(target)
                                        result["music_render_cases"] += 1
                                for category in ("music", "technique", "files"):
                                    browser.select_category(category)
                                    browser.catalogue.detail_open = False
                                    for opened in (False, True):
                                        browser._setup_open = opened
                                        browser.render(target)
                                        result["library_render_cases"] += 1
                                        assert browser._button_rects.get("setup")
                            finally:
                                music.close()
                                browser.close()
                            lesson = LessonScreen(config=cfg, progress=progress.ProgressTracker(),
                                                  songs_dir=songs)
                            try:
                                for key in (None, pygame.K_o, pygame.K_ESCAPE, pygame.K_s):
                                    if key is not None:
                                        lesson.handle_event(pygame.event.Event(
                                            pygame.KEYDOWN, key=key, mod=0, unicode=""))
                                    if key != pygame.K_ESCAPE:
                                        lesson.render(target)
                                        result["expansion_render_cases"] += 1
                                for state in lesson.plan.lessons:
                                    for step in practice_steps(state.lesson, profile):
                                        assert lesson.show_practice(LessonPractice(state.path, step))
                                        lesson.render(target)
                                        result["expansion_render_cases"] += 1
                            finally:
                                lesson.close()
                            for tracks in (None, [{"index": 0, "name": "Bass / Guitar",
                                                   "strings": profile.string_count,
                                                   "compatible": True}]):
                                ImportScreen(files[0], tracks=tracks).render(target)
                                result["expansion_render_cases"] += 1
                            play._history_panel().render(target)
                            result["expansion_render_cases"] += 1
                            result_screen.mark_attempt_unreliable("input_overflow")
                            result_screen._panel = None
                            result_screen.render(target)
                            result["expansion_render_cases"] += 1
                            guide = SetupGuideScreen(cfg)
                            for stage in range(5):
                                guide.stage = stage
                                guide.refresh()
                                guide.render(target)
                                result["completion_render_cases"] += 1
                            for purpose in ("tune", "phrase"):
                                check = check_menu.CheckScreen(cfg, purpose=purpose)
                                try:
                                    check.update()
                                    check.render(target)
                                    result["completion_render_cases"] += 1
                                finally:
                                    check.close()
                            tracker = progress.ProgressTracker()
                            key = progress.song_key(files[0], profile.key, 0,
                                                    content_id=library.content_identity(files[0]))
                            saved = {"song_path": str(files[0].resolve()), "instrument": profile.key,
                                     "track_index": 0, "position_ms": 0, "tempo_factor": .75,
                                     "loop_start_ms": 0, "loop_end_ms": timeline.measures[1].end_ms,
                                     "loop_enabled": True, "active_strings": [True] * profile.string_count,
                                     "max_fret": profile.max_fret, "wait_mode": False,
                                     "chord_partial_credit": True}
                            passage = PassagePanel(tracker, key, saved, timeline.metadata.title)
                            try:
                                passage.render(target)
                                result["completion_render_cases"] += 1
                                passage.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_s, mod=0))
                                assert passage.mode == "name"
                                passage.render(target)
                                result["completion_render_cases"] += 1
                                passage.handle_event(pygame.event.Event(pygame.TEXTINPUT,
                                    text=f"Refrain {language} {look} {size[0]}"))
                                passage.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN, mod=0))
                                assert passage.mode == "detail", passage.error
                                passage.render(target)
                                result["completion_render_cases"] += 1
                                passage.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DELETE, mod=0))
                                assert passage.mode == "delete"
                                passage.render(target)
                                result["completion_render_cases"] += 1
                            finally:
                                passage.close()
                            for kind in ("ease", "raise"):
                                play._section_advice = {"kind": kind}
                                play._shorter_passage = (0, timeline.measures[1].end_ms) if kind == "ease" else None
                                play._advice_panel(.7 if kind == "ease" else .8).render(target)
                                result["completion_render_cases"] += 1
            result["ok"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        if "pygame" in sys.modules:
            sys.modules["pygame"].quit()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result["ok"]
