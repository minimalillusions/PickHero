"""Real process lifecycle regression, deliberately outside pytest's OS seal.

Usage: python scripts/test-import-worker.py [--exe dist/PickHero.exe]
All cache, status, generated scores and output stay in TemporaryDirectory.
No device enumeration, audio stream, MIDI output, settings or progress writes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pickhero.exercises import write_exercises
from pickhero.import_jobs import ImportJob
from pickhero.instruments import BASS_UKULELE


def wait(job):
    deadline = time.monotonic() + 50
    polls = 0
    while not job.poll():
        polls += 1
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert not job.directory.exists()
    assert job._process is None or job._process.poll() is not None
    assert job._job is None
    return polls


def app_flow(root, path, options):
    """Exercise the production App(background=True) route using real workers."""
    from contextlib import ExitStack
    from unittest.mock import patch
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    import pygame
    import pygame.midi
    import sounddevice

    from pickhero import config, library, progress
    from pickhero.ui.app import App
    from pickhero.ui.colors import set_look
    from pickhero.ui.scrolling import PlayingScreen
    home = root/"app-home"
    home.mkdir()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Worker integration test must not open real audio")
    def factory(*args, **kwargs):
        return ImportJob(*args, **kwargs, **options)
    pygame.font.init()
    with ExitStack() as stack:
        for module, name, value in (
            (config, "CONFIG_DIR", home), (config, "CONFIG_FILE", home/"settings.json"),
            (progress, "CONFIG_DIR", home), (progress, "PROGRESS_FILE", home/"progress.json"),
            (library, "CONFIG_DIR", home), (library, "CACHE_FILE", home/"library.json")):
            stack.enter_context(patch.object(module, name, value))
        stack.enter_context(patch.object(sounddevice, "InputStream", forbidden))
        stack.enter_context(patch.object(sounddevice, "query_devices", lambda *_a, **_k: []))
        stack.enter_context(patch.object(pygame.midi, "Output", forbidden))
        stack.enter_context(patch.object(PlayingScreen, "_init_midi_player", lambda *_a: None))
        stack.enter_context(patch("pickhero.ui.app.ImportJob", factory))
        stack.enter_context(patch("pickhero.import_jobs.ImportJob", factory))
        app = App(config.Config(songs_dir=str(root/"scores"), instrument="bass_ukulele"), background=True)
        try:
            app._build_menu()
            deadline = time.monotonic()+45
            while app._menu._scan_job is not None:
                app._update()
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert len(app._menu._songs) == 10
            app._load_song(path)
            while app._import_job is not None:
                app._update()
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert app._state == "import" and app._playing_screen is None
            for look in ("studio", "stage", "hifi"):
                set_look(look)
                for size in ((1280, 720), (640, 480), (320, 240)):
                    surface = pygame.Surface(size)
                    app._render(surface)
                    assert all(surface.get_rect().contains(rect) for rect in app._import_screen.view.buttons.values())
            app._handle_import_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))
            while app._import_job is not None:
                app._update()
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert app._state == "playing" and app._playing_screen is not None
            assert app._playing_screen._song_key.startswith("song-v3|bass_ukulele|")
        finally:
            app._close_screens()
            set_look("studio")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    options = {"executable": args.exe.resolve()} if args.exe else {}
    report = {"mode": "frozen" if args.exe else "source", "cases": {}}
    with tempfile.TemporaryDirectory(prefix="pickhero-worker-check-") as folder:
        root = Path(folder)
        songs = root / "scores"
        write_exercises(BASS_UKULELE, songs)
        job = ImportJob("scan", songs, BASS_UKULELE.key, cache_file=root/"cache.json", **options)
        report["polls"] = wait(job)
        assert not job.error, (job.error, job.failure_type, job._process.returncode)
        assert len(job.result["songs"]) == 10
        assert (root/"cache.json").exists()
        report["cases"]["scan"] = True
        path = Path(job.result["songs"][0]["path"])
        job = ImportJob("inspect", path, BASS_UKULELE.key, **options)
        wait(job)
        assert not job.error, job.error
        track_index = job.result["selected"]
        content_id = job.result["content_id"]
        assert job.result["tracks"][0]["compatible"]
        report["cases"]["inspect"] = True
        job = ImportJob("load", path, BASS_UKULELE.key, track_index=track_index,
                        content_id=content_id, **options)
        wait(job)
        assert not job.error, job.error
        timeline, _backing = job.prepared_song
        assert len(timeline) > 0 and timeline.metadata.track_index == track_index
        report["cases"]["explicit_track_load"] = True
        job = ImportJob("load", path, BASS_UKULELE.key, content_id="0"*64, **options)
        wait(job)
        assert job.error == "import"
        report["cases"]["changed_file_rejected"] = True
        job = ImportJob("load", path, BASS_UKULELE.key, **options)
        process = job._process
        job.cancel()
        assert process is not None and process.poll() is not None
        assert not job.directory.exists() and job._job is None and job.cancelled
        report["cases"]["cancel_terminates_process"] = True
        job = ImportJob("load", path, BASS_UKULELE.key, timeout=0, **options)
        wait(job)
        assert job.error == "timeout"
        report["cases"]["timeout_terminates_process"] = True
        assert len(list(root.iterdir())) == 2
        app_flow(root, path, options)
        report["cases"]["production_app_scan_choose_load"] = True
        report["cases"]["three_looks_three_sizes"] = True
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
