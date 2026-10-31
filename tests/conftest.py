"""Keep the test run out of the real user's files -- and off their desktop.

Config.save() and the library cache write to fixed paths in the user's home
directory. Any test that constructs a default Config and saves it therefore
overwrites the settings of whoever is running the tests: their instrument,
their measured noise gate, their audio device. That happened, and it is the
kind of damage a test suite must never do.

Both modules look their paths up at call time, so redirecting them here
covers every test without any of them having to remember.

The second fixture is the same lesson learnt a second time. The sweep clicks
every control the app reports, and one of them is "Ordner öffnen", which calls
os.startfile -- so running the suite put about forty Explorer windows on their
desktop while the player was working. Stubbing it on the screen under test is not
enough: the next test file to click everything would do it again. This lives
in conftest because an autouse fixture only reaches the module it is written
in, which is how the first attempt at this guard passed while doing nothing.
"""

# Windows asyncio subclasses subprocess.Popen during import. Load it before
# the process guard replaces Popen, so isolated test selections work too.
import asyncio  # noqa: F401
import os
import subprocess

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")


@pytest.fixture(autouse=True)
def _no_physical_audio(monkeypatch):
    """Regression tests may simulate devices, never open the user's devices."""
    import sounddevice
    import pygame.midi

    def unavailable(*args, **kwargs):
        raise RuntimeError("Physical audio is disabled in tests; use a fake device")

    monkeypatch.setattr(sounddevice, "InputStream", unavailable)
    monkeypatch.setattr(sounddevice, "Stream", unavailable)
    monkeypatch.setattr(pygame.midi, "Output", unavailable)

    def devices(device=None, *args, **kwargs):
        if device is not None:
            raise ValueError("No physical devices in automated tests")
        return []

    # Device enumeration/PortMidi initialization can block in native drivers
    # even when opening streams is disabled. Device tests install their own
    # fakes; the default environment has no hardware.
    monkeypatch.setattr(sounddevice, "query_devices", devices)
    monkeypatch.setattr(sounddevice, "query_hostapis", lambda *a, **k: [])
    monkeypatch.setattr(sounddevice, "check_input_settings", unavailable)
    monkeypatch.setattr(sounddevice, "check_output_settings", unavailable)
    monkeypatch.setattr(pygame.midi, "init", lambda: None)
    monkeypatch.setattr(pygame.midi, "quit", lambda: None)
    monkeypatch.setattr(pygame.midi, "get_default_output_id", lambda: -1)


@pytest.fixture(autouse=True)
def _isolate_user_files(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("pickhero-home")

    from pickhero import config as config_module
    monkeypatch.setattr(config_module, "CONFIG_DIR", home, raising=False)
    monkeypatch.setattr(config_module, "CONFIG_FILE", home / "settings.json",
                        raising=False)

    from pickhero import library as library_module
    monkeypatch.setattr(library_module, "CONFIG_DIR", home, raising=False)
    monkeypatch.setattr(library_module, "CACHE_FILE", home / "library.json",
                        raising=False)

    from pickhero import progress as progress_module
    for name in ("PROGRESS_FILE", "PROGRESS_DIR", "CONFIG_DIR"):
        if hasattr(progress_module, name):
            current = getattr(progress_module, name)
            replacement = home if "DIR" in name else home / "progress.json"
            monkeypatch.setattr(progress_module, name, replacement, raising=False)

    yield


ESCAPES: list[tuple] = []


@pytest.fixture(autouse=True)
def _seal_the_process(monkeypatch):
    """No test may start a program outside this process.

    The app's own _open_folder swallows every exception, by design -- a file
    manager that will not open must not take the practice session down with
    it. So raising here is not enough on its own: the attempt is recorded as
    well, and the record is what the assertion below reads.
    """
    def blocked(name):
        def guard(*args, **kwargs):
            ESCAPES.append((name, args, kwargs))
            raise AssertionError(
                f"a test called {name}{args!r}. Stub it on the object under "
                f"test; nothing here may reach the operating system.")
        return guard

    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", blocked("os.startfile"),
                            raising=False)
    monkeypatch.setattr(subprocess, "Popen", blocked("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "run", blocked("subprocess.run"))
    monkeypatch.setattr(os, "system", blocked("os.system"))
    before = len(ESCAPES)
    yield
    assert len(ESCAPES) == before, (
        f"this test tried to leave the process: {ESCAPES[before:]}")
