"""Library grouping and explicit previews; browsing never opens audio input."""
from __future__ import annotations

import time
from pathlib import Path

from pickhero.audio.midi_playback import MidiPlayer
from pickhero.curriculum import build_demo, build_file_demo, exercise_for_lesson, lesson_for_path
from pickhero.exercises import EXERCISE_DIRNAME
from pickhero.i18n import current_language, t
from pickhero.music import MUSIC_DIRNAME, build_music, build_music_demo, music_for_path

CATEGORIES = ("music", "technique", "files")
CATEGORY_KEYS = {"music": "browse.music", "technique": "browse.technique", "files": "browse.files"}


class Catalogue:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.category = None
        self.versions = {}
        self.saved = {}
        self.detail_open = False
        self.expanded = False
        self.detail_scroll = self.detail_maximum = 0
        self.detail_rect = None
        self.filters_open = False
        self.filter_scroll = self.filter_maximum = 0
        self._selection = None
        self._metadata = {}
        self._player = self._demo = None
        self._started = 0.
        self.status = "idle"
        # A preview of the player's own file needs the file decoded first,
        # and that happens in the import worker, never here.
        self._job = None
        self._job_key = self._job_instrument = None
        self._file_demos = {}

    def kind(self, song):
        # Only direct managed folders belong to the built-in catalogue.
        # An imported copy with a similar name remains an independent file.
        path = song.file
        if path.parent == self.folder / MUSIC_DIRNAME and music_for_path(path):
            return "music"
        if path.parent == self.folder / EXERCISE_DIRNAME and lesson_for_path(path):
            return "technique"
        return "files"

    def entries(self, songs, category=None):
        category = category or self.category
        if category is None:
            category = next((key for key in CATEGORIES if any(self.kind(s) == key for s in songs)), "music")
        chosen = [song for song in songs if self.kind(song) == category]
        if category != "music":
            return chosen
        groups = {}
        for song in chosen:
            piece, version = music_for_path(song.file)
            groups.setdefault(piece.slug, {})[version] = song
        return [versions.get(self.versions.get(slug, "start"), next(iter(versions.values())))
                for slug, versions in groups.items()]

    def initialise(self, songs):
        if self.category is None and songs:
            self.category = next((key for key in CATEGORIES if self.entries(songs, key)), "music")

    def sync(self, song, instrument):
        identity = (song.path, song.content_id, instrument.key, current_language()) if song else None
        if identity != self._selection:
            self.stop()
            self.status = "idle"
            self.detail_scroll = 0
            self.expanded = False
            self._selection = identity

    def metadata(self, song, instrument):
        key = (song.path, song.content_id, instrument.key, current_language())
        if key in self._metadata:
            return self._metadata[key]
        kind = self.kind(song)
        data = {"title": song.display_title, "subtitle": song.summary(), "description": "",
                "instructions": [], "exercise": None, "piece": None, "version": None,
                "requirements": "", "kind": kind, "order": None}
        if kind == "music":
            piece, version = music_for_path(song.file)
            exercise = build_music(piece, version, instrument)
            data.update(title=piece.title, piece=piece, version=version, exercise=exercise,
                        subtitle=f"{t(f'music.style.{piece.style}')} · {piece.bpm} BPM",
                        description=t(f"music.{piece.slug}.{version}"),
                        instructions=[*[t(f"music.{piece.slug}.section.{bar}") for bar in piece.section_bars],
                                      t("music.practice_hint")])
        elif kind == "technique":
            lesson = lesson_for_path(song.file)
            exercise = exercise_for_lesson(lesson, instrument)
            data.update(title=lesson.title, order=lesson.order, exercise=exercise, description=lesson.goal,
                        subtitle=f"{song.level_name} · {exercise.bpm} BPM",
                        instructions=[lesson.instruction, lesson.criterion])
        else:
            data["description"] = t("browse.file_hint")
            data["requirements"] = t("browse.file_part", strings=song.strings,
                                     track=song.track_name or str(song.track_index + 1))
            data["instructions"] = [str(song.file)]
        if data["exercise"]:
            ex = data["exercise"]
            notes = [step for bar in ex.bars for step in bar if step.string is not None]
            strings = sorted({step.string for step in notes}, reverse=True)
            from pickhero.audio.note_utils import midi_to_name
            names = " · ".join(midi_to_name(instrument.tuning[string]) for string in strings)
            frets = [step.fret for step in notes]
            data["requirements"] = t("browse.requirements", strings=names,
                                     low=min(frets, default=0), high=max(frets, default=0),
                                     seconds=round(len(ex.bars)*240/ex.bpm))
        self._metadata[key] = data
        return data

    def toggle_preview(self, song, instrument):
        if self._player or self._job is not None:
            self.stop()
            self.status = "idle"
            return
        if song is None:
            return
        data = self.metadata(song, instrument)
        if data["exercise"] is not None:
            self._play(build_music_demo(data["piece"], data["version"], instrument) if data["piece"]
                       else build_demo(data["exercise"], instrument))
            return
        if data["kind"] != "files":
            return
        key = (str(song.file), song.track_index, song.content_id, instrument.key)
        if key in self._file_demos:
            self._play(self._file_demos[key])
            return
        # Hearing a file is worth the wait of a load. Reading a file from
        # elsewhere in this process is not worth the risk that made the
        # worker necessary, so the preview takes the same road as playing.
        from pickhero.import_jobs import ImportJob
        self._job = ImportJob("load", song.file, instrument.key,
                              track_index=song.track_index,
                              content_id=song.content_id or None)
        self._job_key, self._job_instrument = key, instrument
        self.status = "loading"

    def _finish_load(self):
        """Turn a finished import into a demo, or say that there is none."""
        from pickhero.import_worker import decode_song
        job, self._job = self._job, None
        demo = None
        if not job.error and not job.cancelled:
            try:
                prepared = getattr(job, "prepared_song", None)
                timeline, _ = prepared if prepared is not None else decode_song(job.result["song"])
                demo = build_file_demo(timeline, self._job_instrument)
            except (ValueError, TypeError, KeyError):
                demo = None
        if demo is None:
            self.status = "failed"
            return
        self._file_demos[self._job_key] = demo
        self._play(demo)

    def _play(self, demo):
        player = MidiPlayer(demo.track)
        if not player.open():
            player.close()
            self.status = "unavailable"
            return
        self._player, self._demo = player, demo
        self._started = time.monotonic()
        self.status = "playing"
        player.update(0.)

    def update(self):
        if self._job is not None and self._job.poll():
            self._finish_load()
        if self._player:
            elapsed = (time.monotonic() - self._started) * 1000
            self._player.update(elapsed)
            if elapsed >= self._demo.duration_ms:
                self.stop()
                self.status = "done"

    def stop(self):
        # Leaving a file while it loads must end the worker too, not just
        # forget it: cancel terminates the process.
        if self._job is not None:
            self._job.cancel()
            self._job = None
        if self._player:
            self._player.close()
        self._player = self._demo = None
        # "failed" belongs to the file that failed; the next one starts fresh.
        if self.status in ("playing", "loading", "failed"):
            self.status = "idle"
