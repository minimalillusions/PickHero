"""The app must not die from things outside its control.

Every case here was found by running it, and every one of them killed the
whole process from inside the pygame event loop, which means the window
vanishes with no message: in a windowed PyInstaller build there is not even a
traceback to read.
"""

import os
import zipfile

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

from pickhero import instruments  # noqa: E402
from pickhero.config import Config  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _display():
    pygame.init()
    pygame.display.set_mode((1280, 720))
    yield
    pygame.quit()


class TestAudioFailures:
    def test_calibration_survives_a_microphone_that_will_not_open(self, monkeypatch):
        from pickhero.ui import calibration_menu

        def boom(*args, **kwargs):
            raise OSError("Gerät wird von einem anderen Programm verwendet")

        monkeypatch.setattr(calibration_menu, "AudioCapture", boom)
        screen = calibration_menu.CalibrationMenuScreen(Config())
        screen._start_capture()
        assert screen._error is not None
        screen.update()
        screen.render(pygame.Surface((1280, 720)))

    def test_device_picker_survives_enumeration_failing(self, monkeypatch):
        from pickhero.ui import device_menu

        def boom():
            raise RuntimeError("PortAudio: no host API")

        monkeypatch.setattr(device_menu, "list_audio_devices", boom)
        screen = device_menu.DeviceMenuScreen(Config())
        assert screen._error is not None
        screen.render(pygame.Surface((1280, 720)))

    def test_check_screen_survives_a_microphone_that_will_not_open(self, monkeypatch):
        from pickhero.ui import check_menu

        def boom(*args, **kwargs):
            raise OSError("no such device")

        monkeypatch.setattr(check_menu, "AudioCapture", boom)
        screen = check_menu.CheckScreen(Config())
        assert screen._error is not None
        screen.update()
        screen.render(pygame.Surface((1280, 720)))


class TestFileFailures:
    def test_a_gp7_file_with_no_tracks_loads_as_empty(self, tmp_path):
        """It used to raise IndexError from inside the event loop."""
        from pickhero.tabs.loader import load_gp_file

        path = tmp_path / "leer.gp7"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Content/score.gpif",
                             "<GPIF><Score><Title>Leer</Title></Score></GPIF>")
        timeline = load_gp_file(path, instrument=instruments.BASS_UKULELE)
        assert len(timeline) == 0

    def test_a_songs_directory_that_cannot_be_created_does_not_stop_startup(
            self, tmp_path):
        """An install under Program Files cannot create its songs folder.

        Reproduced without monkeypatching pathlib: a plain file where the
        directory should be makes mkdir fail exactly as a permission denial
        does. Patching Path.mkdir globally also breaks pygame and takes the
        interpreter down with an access violation, which is a good reminder
        that a blunt patch is its own bug.
        """
        from pickhero.ui.menu import MenuScreen

        blocked = tmp_path / "songs"
        blocked.write_text("not a directory", encoding="utf-8")
        screen = MenuScreen(blocked, config=Config())
        assert screen._songs == []
        screen.render(pygame.Surface((1280, 720)))


class TestErrorsReachSomewhere:
    def test_log_error_writes_a_file_even_with_no_stdout(self, monkeypatch):
        """console=False in the exe makes sys.stdout None, so print() is void."""
        import sys
        from pickhero import config as config_module

        monkeypatch.setattr(sys, "stdout", None)
        config_module.log_error("etwas ging schief")
        log = config_module.CONFIG_DIR / "pickhero.log"
        assert log.exists()
        assert "etwas ging schief" in log.read_text(encoding="utf-8")

    def test_log_error_never_raises(self, tmp_path, monkeypatch):
        from pickhero import config as config_module

        blocked = tmp_path / "not-a-dir"
        blocked.write_text("x", encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_DIR", blocked)
        config_module.log_error("kein Absturz bitte")
