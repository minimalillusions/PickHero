"""The build smoke check must reject old runtimes and release SDL on failure."""

import json

import pytest

from pickhero import selftest


@pytest.mark.parametrize("version,bits,expat", [
    ((3, 12, 13), 64, (2, 8, 3)),
    ((3, 13, 0), 64, (2, 8, 3)),
    ((3, 12, 14), 32, (2, 8, 3)),
    ((3, 12, 14), 64, (2, 7, 1)),
])
def test_runtime_gate_rejects_incompatible_abi_or_unpatched_library(
        monkeypatch, version, bits, expat):
    monkeypatch.setattr(selftest.sys, "version_info", version)
    monkeypatch.setattr(selftest.struct, "calcsize", lambda _: bits // 8)
    monkeypatch.setattr(selftest.pyexpat, "version_info", expat)
    with pytest.raises(RuntimeError):
        selftest.check_runtime()


def test_runtime_gate_accepts_tested_runtime(monkeypatch):
    monkeypatch.setattr(selftest.sys, "version_info", (3, 12, 14))
    monkeypatch.setattr(selftest.struct, "calcsize", lambda _: 8)
    monkeypatch.setattr(selftest.pyexpat, "version_info", (2, 8, 3))
    selftest.check_runtime()


def test_render_failure_releases_pygame_before_temp_cleanup(tmp_path, monkeypatch):
    import pygame
    from pickhero.ui.scrolling import PlayingScreen

    events = []
    real_tempdir = selftest.tempfile.TemporaryDirectory
    real_quit = pygame.quit

    class TrackedTempdir:
        def __init__(self, *args, **kwargs):
            self.inner = real_tempdir(*args, **kwargs)

        def __enter__(self):
            return self.inner.__enter__()

        def __exit__(self, *args):
            events.append("temp_cleanup")
            return self.inner.__exit__(*args)

    def quit_pygame():
        events.append("pygame_quit")
        real_quit()

    def broken_render(*args):
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr(selftest, "check_runtime", lambda: None)
    monkeypatch.setattr(selftest.tempfile, "TemporaryDirectory", TrackedTempdir)
    monkeypatch.setattr(pygame, "quit", quit_pygame)
    monkeypatch.setattr(PlayingScreen, "render", broken_render)
    report = tmp_path / "failure.json"
    assert selftest.run(report) is False
    result = json.loads(report.read_text(encoding="utf-8"))
    assert not result["ok"] and "simulated render failure" in result["error"]
    assert events.index("pygame_quit") < events.index("temp_cleanup")
