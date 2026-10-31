"""Reproducible 1,000-score and 100,000-note responsiveness exercise.

Uses real isolated importer processes, software Pygame rendering and the real
DSP callback fed generated PCM. Never opens physical audio/MIDI/display or
reads/writes the user's settings, library cache or progress. This is a local
synthetic workload, not a hardware/audio-device or learner acceptance test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import struct
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import guitarpro
import numpy as np
import pygame
import pygame.midi
import sounddevice

from pickhero import config, library, progress
from pickhero.audio.input import AudioCapture
from pickhero.exercises import _build_song, build_exercises, ensure_exercises
from pickhero.import_jobs import ImportJob
from pickhero.instruments import BASS_UKULELE
from pickhero.ui.app import App
from pickhero.ui.colors import set_look
from pickhero.ui.scrolling import PlayingScreen


def summary(values):
    data = np.asarray(values, dtype=float)
    return {"samples": len(values), "median_ms": float(np.median(data)),
            "p95_ms": float(np.percentile(data, 95)), "max_ms": float(np.max(data)),
            "over_33_ms": int(np.sum(data > 33))} if values else {}


class SimulatedCapture(AudioCapture):
    """The production callback and detector, driven at 44.1 kHz in memory."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stop_simulation = threading.Event()
        self._thread = None
        self.callback_ms = []
        self.callback_late_ms = []
        self.frames = 0

    def start(self):
        self.stop()
        self.detector.reset()
        self._tail = np.zeros(0, np.float32)
        self._next_input_ms = None
        self._portaudio_clock_offset = None
        self._last_callback_time = None
        self._last_overflow_time = self._last_clipping_time = None
        self._clipped_duration = 0
        self.get_notes()
        self._start_time = time.perf_counter()
        self._stop_simulation.clear()
        self._stream = SimpleNamespace(active=True, stop=lambda: None, close=lambda: None)
        self._thread = threading.Thread(target=self._run, daemon=True, name="simulated-audio-callback")
        self._thread.start()

    def _run(self):
        # Four open strings, repeated every 0.8 s with a decaying envelope;
        # partials create real pitch/onset analysis work, not a sleep stub.
        rate, hop = self.detector.sample_rate, self.detector.hop_size
        duration = 3.2
        at = np.arange(round(rate * duration)) / rate
        part = (at // .8).astype(int).clip(0, 3)
        frequencies = np.array([41.2034, 55., 73.4162, 97.9989])[part]
        phase = 2 * np.pi * np.cumsum(frequencies) / rate
        envelope = .16 * np.exp(-3.5 * (at % .8))
        pcm = (envelope * (np.sin(phase) + .45 * np.sin(2*phase) + .2 * np.sin(3*phase))).astype(np.float32)
        offset = 0
        deadline = time.perf_counter()
        while not self._stop_simulation.is_set():
            indexes = (np.arange(hop) + offset) % len(pcm)
            data = pcm[indexes, None]
            started = time.perf_counter()
            self.callback_late_ms.append(max(0., (started-deadline)*1000))
            self._audio_callback(data, hop, None, None)
            self.callback_ms.append((time.perf_counter()-started)*1000)
            self.frames += hop
            offset += hop
            deadline += hop / rate
            if self.note_queue.qsize() > 500:
                self.get_notes()
            self._stop_simulation.wait(max(0., deadline-time.perf_counter()))

    def stop(self):
        if hasattr(self, "_stop_simulation"):
            self._stop_simulation.set()
            if self._thread is not None:
                self._thread.join(timeout=1)
                self._thread = None
        super().stop()


def make_library(root, count):
    ensure_exercises(BASS_UKULELE, root)
    exercises = build_exercises(BASS_UKULELE)
    for index in range(count - len(exercises)):
        song = _build_song(exercises[index % len(exercises)], BASS_UKULELE)
        song.title = f"Benchmark piece {index:04d}"
        song.artist = f"Generated performance fixture {index % 20:02d}"
        folder = root / f"Set {index % 20:02d}"
        folder.mkdir(exist_ok=True)
        guitarpro.write(song, str(folder / f"piece-{index:04d}.gp5"), version=(5, 1, 0))


def make_large_score(path, count):
    # Standard format-0 MIDI, without any additional benchmark dependency.
    name = b"Large bass score"
    track = bytearray(b"\x00\xff\x03" + bytes([len(name)]) + name + b"\x00\xc0\x21")
    for index in range(count):
        note = (28, 33, 38, 43)[index % 4]
        track.extend(bytes([0, 0x90, note, 80, 120, 0x80, note, 0]))
    track.extend(b"\x00\xff\x2f\x00")
    header = struct.pack(">4sIHHH", b"MThd", 6, 0, 1, 480)
    path.write_bytes(header + b"MTrk" + struct.pack(">I", len(track)) + track)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--songs", type=int, default=1000)
    parser.add_argument("--notes", type=int, default=100000)
    parser.add_argument("--frames", type=int, default=120)
    args = parser.parse_args()
    report = {"environment": {"platform": platform.platform(), "python": platform.python_version(),
                             "logical_cpus": os.cpu_count(), "surface": [1280, 720]},
              "scope": "Generated GP5/MIDI scores, real child processes, software offscreen rendering, real DSP on synthetic PCM; no physical audio/MIDI/display.",
              "songs": args.songs, "large_score_notes": args.notes, "cases": {}}
    report["captured_at_utc"] = datetime.now(timezone.utc).isoformat()
    project = Path(__file__).resolve().parents[1]
    report["source_sha256"] = {name: hashlib.sha256((project / name).read_bytes()).hexdigest()
        for name in ("pickhero/import_jobs.py", "pickhero/import_worker.py", "pickhero/score_preparation.py",
                     "pickhero/library.py", "pickhero/progress.py", "pickhero/ui/menu.py", "pickhero/ui/app.py",
                     "pickhero/ui/import_menu.py", "pickhero/ui/paint_stage.py", "pickhero/ui/scrolling.py",
                     "pickhero/audio/input.py", "pickhero/audio/detector.py", "scripts/benchmark-library.py")}
    options = {"executable": args.exe.resolve()} if args.exe else {}
    report["worker_mode"] = "frozen" if args.exe else "source"
    with tempfile.TemporaryDirectory(prefix="pickhero-library-benchmark-") as folder, ExitStack() as stack:
        root = Path(folder)
        home, songs = root / "home", root / "scores"
        home.mkdir(); songs.mkdir()
        for module, name, value in (
                (config, "CONFIG_DIR", home), (config, "CONFIG_FILE", home / "settings.json"),
                (progress, "CONFIG_DIR", home), (progress, "PROGRESS_FILE", home / "progress.json"),
                (library, "CONFIG_DIR", home), (library, "CACHE_FILE", home / "library.json")):
            stack.enter_context(patch.object(module, name, value))
        def forbidden(*_a, **_k):
            raise AssertionError("The benchmark must not open physical devices")
        stack.enter_context(patch.object(sounddevice, "InputStream", forbidden))
        stack.enter_context(patch.object(sounddevice, "query_devices", lambda *_a, **_k: []))
        stack.enter_context(patch.object(pygame.midi, "Output", forbidden))
        stack.enter_context(patch.object(PlayingScreen, "_init_midi_player", lambda *_a: None))
        factory = lambda *a, **k: ImportJob(*a, **k, **options)
        stack.enter_context(patch("pickhero.ui.app.ImportJob", factory))
        stack.enter_context(patch("pickhero.import_jobs.ImportJob", factory))
        print(f"Generating {args.songs} distinct GP5 files…", flush=True)
        make_library(songs, args.songs)
        large_path = root / "large.mid"
        make_large_score(large_path, args.notes)
        pygame.font.init()
        surface = pygame.Surface((1280, 720))
        cfg = config.Config(instrument="bass_ukulele", songs_dir=str(songs))
        capture = SimulatedCapture(cfg)
        capture.start()
        stack.callback(capture.stop)
        stack.enter_context(patch("pickhero.audio.input.AudioCapture", lambda *_a, **_k: capture))
        app = App(cfg, background=True)
        stack.callback(app._close_screens)
        adoptions = []
        original_start = app._start_song
        def timed_start(*a, **k):
            started = time.perf_counter()
            result = original_start(*a, **k)
            adoptions.append((time.perf_counter()-started)*1000)
            return result
        app._start_song = timed_start

        def frame(interact=False, index=0):
            started = time.perf_counter()
            if interact and index % 15 == 0 and app._state == "menu":
                app._menu.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-3))
            if interact and index % 40 == 0 and app._state == "menu":
                app._menu.handle_event(pygame.event.Event(pygame.KEYDOWN,
                                       key=pygame.K_END if index % 80 == 0 else pygame.K_HOME))
            app._update()
            app._render(surface)
            work = (time.perf_counter()-started)*1000
            time.sleep(max(0., 1/60 - work/1000))
            return work, (time.perf_counter()-started)*1000

        def while_busy(predicate, label):
            work, intervals = [], []
            deadline = time.monotonic() + 180
            while predicate():
                a, b = frame(True, len(work))
                work.append(a); intervals.append(b)
                if time.monotonic() > deadline:
                    raise TimeoutError(label)
            report["cases"][label] = {"frame_work": summary(work), "frame_interval": summary(intervals)}
            return work

        set_look("studio")
        app._build_menu()
        print("Measuring cold scan and publication…", flush=True)
        while_busy(lambda: app._menu._scan_job is not None, "cold_scan")
        assert len(app._menu._songs) == args.songs, len(app._menu._songs)
        # Include established progress and resume choices, not only a fresh
        # library whose progress-dependent rows all take the empty fast path.
        # Batch saves affect this temporary test home only; the final save
        # goes through the production validation/capacity/atomic-write path.
        with patch.object(app._progress, "_save", lambda: True):
            for index, song in enumerate(app._menu._songs):
                key = progress.song_key(song.file, BASS_UKULELE.key,
                                        song.track_index, content_id=song.content_id)
                for attempt in range(3):
                    app._progress.record_detailed_result(key,
                        {"accuracy_percent": 60 + (index + attempt) % 40,
                         "hits": 12, "total": 16, "timing_assessed": True,
                         "timing_percent": 65, "reliable": True},
                        [(0, 3, 65.)], .8 + attempt * .05)
                if index % 10 == 0:
                    app._progress.save_session(key,
                        {"song_path": str(song.file), "instrument": BASS_UKULELE.key,
                         "track_index": song.track_index, "position_ms": 4000,
                         "measure": 1, "tempo_factor": .8, "wait_mode": False,
                         "loop_enabled": True, "loop_start_ms": 0,
                         "loop_end_ms": 8000, "active_strings": [True] * 4})
        assert app._progress._save(), app._progress.last_save_error
        report["progress_records"] = len(app._progress._data)
        report["resume_records"] = sum(bool(row.practice_state) for row in app._progress._data.values())
        for look in ("studio", "stage", "hifi"):
            set_look(look)
            for _ in range(8):
                frame()
            work, intervals = [], []
            for index in range(args.frames):
                a, b = frame(True, index)
                work.append(a); intervals.append(b)
            report["cases"]["library_" + look] = {"frame_work": summary(work), "frame_interval": summary(intervals)}
        app._menu.scan_files()
        while_busy(lambda: app._menu._scan_job is not None, "warm_scan")
        print("Measuring 100,000-note import including PlayingScreen construction…", flush=True)
        app._load_song(large_path, track_index=0)
        while_busy(lambda: app._import_job is not None, "large_import")
        assert app._state == "playing", app._load_error
        assert len(app._playing_screen._timeline) == args.notes
        report["start_song_including_constructor_ms"] = adoptions
        # An explicitly started real matcher consumes generated detected notes.
        app._playing_screen.toggle_play()
        for look in ("studio", "stage", "hifi"):
            set_look(look)
            for _ in range(8):
                frame()
            work, intervals = [], []
            for _ in range(args.frames):
                a, b = frame()
                work.append(a); intervals.append(b)
            report["cases"]["playing_" + look] = {"frame_work": summary(work), "frame_interval": summary(intervals)}
        app._playing_screen.stop_audio()
        app._playing_screen = None
        capture.start()
        print("Measuring cancellation of live worker and decoder…", flush=True)
        cancellation = []
        for _ in range(3):
            job = ImportJob("load", large_path, BASS_UKULELE.key, track_index=0, **options)
            time.sleep(.1)
            started = time.perf_counter(); job.cancel()
            cancellation.append((time.perf_counter()-started)*1000)
            assert job._process.poll() is not None and not job.directory.exists()
        report["worker_cancellation_ms"] = cancellation
        job = ImportJob("load", large_path, BASS_UKULELE.key, track_index=0, **options)
        deadline = time.monotonic() + 90
        while job._decode_thread is None:
            job.poll()
            assert time.monotonic() < deadline and not job.done, job.error
            time.sleep(.005)
        started = time.perf_counter(); job.cancel()
        report["decoder_cancellation_ms"] = (time.perf_counter()-started)*1000
        assert job._decode_ready.wait(1) and not job.directory.exists()
        report["simulated_audio"] = {"samples_processed": capture.frames,
                                     "callback_work": summary(capture.callback_ms),
                                     "callback_schedule_lateness": summary(capture.callback_late_ms)}
        report["checks"] = {
            "thousand_scores_loaded": len(app._menu._songs) == args.songs,
            "all_measured_frame_p95_under_33ms": all(case["frame_interval"].get("p95_ms", math.inf) < 33
                                                     for case in report["cases"].values()),
            "score_adoption_under_33ms": max(adoptions) < 33,
            "cancellation_under_one_second": max(cancellation + [report["decoder_cancellation_ms"]]) < 1000,
        }
        set_look("studio")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["checks"], indent=2))
    return 0 if all(report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
