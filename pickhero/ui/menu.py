"""The song browser.

A list of filenames is useless once there are more than about twenty pieces.
This screen answers the questions a player actually has: what can I play at my
level, what have I already worked on, and what should I do right now.

Everything shown beyond the filename comes from pickhero.library, which parses
each tab once and caches the result, so opening this screen stays instant
even with a few hundred files.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pygame

from pickhero import library
from pickhero.config import Config, log_error
from pickhero.i18n import current_language, t
from pickhero.progress import ProgressTracker, song_key
from pickhero.ui import design
from pickhero.ui.colors import cycle_look, get_look, look_name_key
from pickhero.ui.page import Page, painter

GP_EXTENSIONS = library.GP_EXTENSIONS

SORT_MODES = ["name_asc", "difficulty", "accuracy", "last_played"]
# The sort button reads "Sortieren: Name", and each mode is one whole
# catalogue entry rather than a prefix with a word spliced onto it. These are
# keys, not text: the label is looked up when the button is drawn, so changing
# language takes effect at once instead of at the next start.
SORT_LABEL_KEYS = {
    "name_asc": "menu.sort_name",
    "difficulty": "menu.sort_difficulty",
    "accuracy": "menu.sort_accuracy",
    "last_played": "menu.sort_last_played",
}

# A piece counts as learned above this, and stops being suggested.
PASSED_PERCENT = 85.0
HEADER_H = 64
FOOTER_H = 56
SIDEBAR_W = 224
# 80 px carried a stacked difficulty glyph with its own name written under
# it -- 48 px of height to say an integer from 1 to 5 that the meta line and
# the sidebar filter both say again. Taking the stack apart is what buys the
# row back: at 56 px their ten pieces fit their window with nothing hidden,
# where 80 showed seven and said nothing about the other three.
ROW_H = 56
# How much of the row that falls off the bottom must remain visible. A row
# clipped by one pixel is not a cue; this is the cue.
ROW_PEEK = 18
# The scroll rail down the right-hand edge. Drawn at every size, including
# when nothing is hidden -- otherwise "no rail" means both "nothing below"
# and "this look forgot to draw one".
RAIL_W = 12


class MenuScreen:
    """Browse, filter and choose a piece to play."""

    def __init__(self, songs_dir: Path, config: Config | None = None,
                 progress: ProgressTracker | None = None, *, background=False):
        self._songs_dir = Path(songs_dir)
        from pickhero.ui.library_catalogue import Catalogue
        self.catalogue = Catalogue(self._songs_dir)
        self._config = config
        self._progress = progress
        self._background = background
        self._scan_job = None
        self._pending_focus = None
        self._scan_status = ""
        self._resume_panel = None
        self._cached_resume = (None, None)
        self._cached_filters = (None, None)
        self._cached_page_data = (None, None)
        self._progress_keys: dict[tuple[Path, str, int], str] = {}
        self._sort_mode: str = config.sort_mode if config else "name_asc"
        if self._sort_mode not in SORT_MODES:
            self._sort_mode = "name_asc"

        self._songs: list[library.SongInfo] = []
        self._scanned_songs: list[library.SongInfo] = []
        self._rows: list[library.SongInfo] = []
        self._filter: tuple[str, object] = ("all", None)
        self._search_text: str = ""
        self._search_active: bool = False
        self._selected = 0
        self._scroll_px = 0
        self._reveal_selection = True
        self._last_click_time = -400
        self._row_rects: list[tuple[pygame.Rect, int]] = []
        # Everything that is not the list lives behind one key. Four buttons
        # in the header plus Thema and Sprache in the footer were nine
        # controls around seven rows, and the last two of them silently
        # vanished on a narrow window while their keys kept firing.
        self._setup_open = False
        self._setup_scroll = 0
        self._pointer = None
        self._last_page: Page | None = None
        self._side_rects: list[tuple[pygame.Rect, tuple[str, object]]] = []
        self._button_rects: dict[str, pygame.Rect] = {}
        self._device_name = self._resolve_device_name()
        self.scan_files()

    # ── data ────────────────────────────────────────────────────────────

    @property
    def _files(self) -> list[Path]:
        """Every indexed file, for callers that only want paths."""
        return [song.file for song in self._songs]

    @property
    def _display_files(self) -> list[Path]:
        return [song.file for song in self._rows]

    @property
    def is_searching(self) -> bool:
        return self._search_active

    def refresh_device_name(self) -> None:
        self._device_name = self._resolve_device_name()

    def _resolve_device_name(self) -> str:
        if self._config is None:
            return t("device.name_default")
        index = self._config.audio.device_index
        try:
            from pickhero.audio.input import list_audio_devices
            devices = list_audio_devices()
        except Exception:
            return t("device.name_unknown")
        if index is None:
            return t("device.name_auto")
        for device in devices:
            if device["index"] == index:
                return device["name"]
        return t("device.name_numbered", index=index)

    def _instrument(self):
        if self._config is None:
            from pickhero import instruments
            return instruments.GUITAR
        return self._config.get_instrument()

    def _instrument_name(self) -> str:
        return self._instrument().name if self._config else ""

    def _instrument_short_name(self) -> str:
        """The name for the header pill, short enough to sit inside it.

        The header used to cut the long name at " (" -- a format contract
        hidden in the caller, which breaks the moment the long name is worded
        differently. Profiles carry their own short name; the split survives
        only as a fallback for a profile built before they did.
        """
        if self._config is None:
            return ""
        instrument = self._instrument()
        short = getattr(instrument, "short_name", "")
        return short or instrument.name.split(" (")[0]

    def scan_files(self) -> None:
        """Re-index the songs directory."""
        if self._scan_job is not None:
            self._scan_job.cancel()
            self._scan_job = None
        self._progress_keys.clear()
        self._cached_resume = self._cached_filters = self._cached_page_data = (None, None)
        # Program Files is read-only for a normal user, and this used to be
        # an unguarded mkdir in the constructor, so the app died before the
        # window appeared.
        try:
            self._songs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log_error(f"Songordner nicht anlegbar ({self._songs_dir}): {exc}")
        if self._background:
            from pickhero.import_jobs import ImportJob
            self._scan_status = t("import.preparing")
            self._scan_job = ImportJob("scan", self._songs_dir, self._instrument().key,
                                       cache_file=library.CACHE_FILE)
            return
        try:
            self._scanned_songs = library.scan(
                self._songs_dir, self._instrument(), include_incompatible=True)
            self._songs = [song for song in self._scanned_songs if song.compatible]
        except Exception as exc:
            log_error(f"Songordner nicht lesbar: {exc}")
            self._scanned_songs = []
            self._songs = []
        self._finish_scan()

    def _finish_scan(self):
        previous = self._selected_path()
        self.catalogue.initialise(self._songs)
        self.catalogue._metadata.clear()
        if self._progress is not None:
            self._progress.migrate_content_identities(self._scanned_songs, self._instrument().key)
        available = {value for _, value, _, heading in self._filter_entries() if not heading}
        if self._filter not in available:
            self._filter = ("all", None)
        if not self._background:
            self._search_text = ""
            self._search_active = False
        self._apply_filter()
        if previous is not None:
            self._selected = next((index for index, song in enumerate(self._rows)
                                   if song.file == previous), self._selected)
        if self._pending_focus is not None:
            pending, self._pending_focus = self._pending_focus, None
            self.focus_path(pending)

    def update(self):
        self._sync_catalogue()
        self.catalogue.update()
        if self._scan_job is None:
            return
        job = self._scan_job
        done = job.poll()
        status = job.status
        total = status.get("total", 0)
        self._scan_status = (t("import.progress", current=status.get("current", 0), total=total)
                             if total else t("import.preparing"))
        if done:
            self._scan_job = None
            if job.error:
                self._scan_status = t("import.scan_failed")
                return
            self._scanned_songs = [library.SongInfo(**row) for row in job.result["songs"]]
            self._songs = [song for song in self._scanned_songs if song.compatible]
            self._scan_status = ""
            self._finish_scan()

    def close(self):
        self.catalogue.stop()
        if self._scan_job is not None:
            self._scan_job.cancel()
            self._scan_job = None

    def _cancel_scan(self):
        self.close()
        self._scan_status = t("import.scan_cancelled")

    # ── filtering ───────────────────────────────────────────────────────

    def _accuracy(self, song: library.SongInfo) -> float | None:
        if self._progress is None:
            return None
        record = self._record_for(song)
        if record is None or record.attempts <= 0:
            return None
        return float(record.best_accuracy)

    def _record_for(self, song: library.SongInfo):
        if self._progress is None:
            return None
        identity = (song.file, self._instrument().key, song.track_index)
        key = self._progress_keys.get(identity)
        if key is None:
            # Resolving the canonical file path can touch the filesystem.
            # Do it once per library scan, never repeatedly for each frame.
            key = self._progress_keys[identity] = song_key(*identity, content_id=song.content_id)
        return self._progress.get_best(key)

    def _bucket(self, song: library.SongInfo) -> str:
        accuracy = self._accuracy(song)
        if accuracy is None:
            return "new"
        return "done" if accuracy >= PASSED_PERCENT else "wip"

    def _folder_of(self, song: library.SongInfo) -> str:
        """Which folder a piece sits in; "" for the songs directory itself.

        This is an identity, not a label. It is stored in the active filter and
        compared against, so it must not change when the language does -- the
        sidebar turns "" into the translated name only when it draws it.
        """
        try:
            relative = song.file.relative_to(self._songs_dir)
        except ValueError:
            return ""
        folder = relative.parent.as_posix()
        return "" if folder in (".", "") else folder

    def _folder_label(self, song: library.SongInfo) -> str:
        """What the folder is called on screen, and what search matches."""
        return self._folder_of(song) or t("menu.no_folder")

    def _matches_filter(self, song: library.SongInfo) -> bool:
        kind, value = self._filter
        if kind == "level":
            return song.difficulty == value
        if kind == "progress":
            return self._bucket(song) == value
        if kind == "folder":
            return self._folder_of(song) == value
        return True

    def _apply_filter(self) -> None:
        if self._progress is not None:
            # An incompatible file with the same stem still makes an old
            # stem-only record ambiguous. Keep the one scan's complete set.
            self._progress.migrate_legacy(
                self._scanned_songs or self._songs, self._instrument().key)
        query = self._search_text.lower().strip()
        rows = [s for s in self.catalogue.entries(self._songs) if self._matches_filter(s)]
        if query:
            rows = [s for s in rows
                    if query in s.display_title.lower()
                    or query in s.artist.lower()
                    or query in self._folder_label(s).lower()]
        self._rows = rows
        self._sort_rows()
        self._selected = 0
        self._scroll_px = 0
        self._reveal_selection = True

    def _sync_catalogue(self):
        song = self._rows[self._selected] if self._rows else None
        self.catalogue.sync(song, self._instrument())
        return song

    def carry_state(self):
        """What a rebuild must not lose: where the player was and what the player had open.

        Switching language rebuilds this screen, because it reads several
        labels once at construction. The rebuild used to drop the filter, the
        search, the selection and the settings panel -- and since the language
        row sits in that panel, choosing a language closed the panel the player
        chose it in.
        """
        return (self.catalogue.category, self._filter, self._search_text,
                self._selected_path(), self._scroll_px,
                self._setup_open, self._setup_scroll)

    def restore_state(self, state):
        category, chosen, search, path, scroll, setup_open, setup_scroll = state
        if category is not None:
            self.select_category(category)
        self._filter, self._search_text = chosen, search
        self._cached_filters = self._cached_page_data = (None, None)
        self._apply_filter()
        if path is not None:
            self._selected = next((i for i, row in enumerate(self._rows) if row.file == path),
                                  self._selected)
        self._scroll_px = scroll
        self._setup_open, self._setup_scroll = setup_open, setup_scroll
        self._sync_catalogue()

    def focus_path(self, path):
        """Reveal an explicit continuation even if its old filter hid it."""
        path = Path(path)
        song = next((row for row in self._songs if row.file == path), None)
        if song is None:
            if self._scan_job is not None:
                self._pending_focus = path
            return False
        category = self.catalogue.kind(song)
        if category == "music":
            from pickhero.music import music_for_path
            piece, version = music_for_path(path)
            self.catalogue.versions[piece.slug] = version
        self.select_category(category)
        self._filter, self._search_text = ("all", None), ""
        self._cached_filters = self._cached_page_data = (None, None)
        self._apply_filter()
        self._selected = next((i for i, row in enumerate(self._rows) if row.file == path), 0)
        self._sync_catalogue()
        return True

    def select_category(self, category):
        from pickhero.ui.library_catalogue import CATEGORIES
        if category not in CATEGORIES or category == self.catalogue.category:
            return
        self.catalogue.saved[self.catalogue.category] = (
            self._filter, self._search_text, self._selected_path(), self._scroll_px)
        self.catalogue.stop()
        self.catalogue.category = category
        self.catalogue.detail_open = False
        self.catalogue.filters_open = False
        self._setup_open = self._search_active = False
        self._last_click_time = -400
        saved = self.catalogue.saved.get(category, (("all", None), "", None, 0))
        self._filter, self._search_text = saved[:2]
        self._cached_filters = self._cached_page_data = (None, None)
        self._apply_filter()
        self._selected = next((i for i, song in enumerate(self._rows) if song.file == saved[2]), 0)
        self._scroll_px = saved[3]
        self._reveal_selection = saved[2] is None
        self._sync_catalogue()

    def _catalogue_action(self, name):
        if name.startswith("category."):
            self.select_category(name.split(".", 1)[1])
        elif name.startswith("version."):
            from pickhero.music import music_for_path
            song = self._sync_catalogue()
            if song and self.catalogue.kind(song) == "music":
                piece, _ = music_for_path(song.file)
                self.catalogue.versions[piece.slug] = name.split(".", 1)[1]
                self._cached_filters = self._cached_page_data = (None, None)
                self._apply_filter()
                if not any(music_for_path(row.file)[0] == piece for row in self._rows):
                    self._filter, self._search_text = ("all", None), ""
                    self._apply_filter()
                self._selected = next((i for i, row in enumerate(self._rows)
                                       if music_for_path(row.file)[0] == piece), 0)
                self._sync_catalogue()
        elif name == "preview":
            self.catalogue.toggle_preview(self._sync_catalogue(), self._instrument())
        elif name == "details":
            self.catalogue.detail_open = True
        elif name == "details.back":
            self.catalogue.detail_open = False
        elif name == "instructions":
            self.catalogue.expanded = not self.catalogue.expanded
        elif name in ("details.up", "details.down"):
            self.catalogue.detail_scroll = max(0, min(self.catalogue.detail_maximum,
                self.catalogue.detail_scroll + (-48 if name == "details.up" else 48)))
        elif name == "filters":
            self.catalogue.filters_open = not self.catalogue.filters_open
        elif name in ("filters.up", "filters.down"):
            self.catalogue.filter_scroll = max(0, min(self.catalogue.filter_maximum,
                self.catalogue.filter_scroll + (-36 if name == "filters.up" else 36)))

    def _sort_rows(self) -> None:
        mode = self._sort_mode
        # The file stem breaks ties, so two pieces with the same title keep a
        # stable order instead of swapping between runs.
        if mode == "name_asc":
            self._rows.sort(key=lambda s: (s.display_title.lower(),
                                           s.file.stem.lower()))
        elif mode == "difficulty":
            self._rows.sort(key=lambda s: (s.difficulty, s.display_title.lower(),
                                           s.file.stem.lower()))
        elif mode == "accuracy":
            self._rows.sort(key=lambda s: (
                0 if self._accuracy(s) is not None else 1,
                -(self._accuracy(s) or 0.0), s.display_title.casefold(), str(s.file)))
        elif mode == "last_played":
            # Newest first, never played last. The rank that used to lead this
            # key was reversed along with everything else, which put every
            # piece the player had never opened at the top of a list whose
            # whole purpose is to show what they were last working on.
            def key(song):
                record = (self._record_for(song)
                          if self._progress else None)
                return record.last_played if record else ""
            self._rows.sort(key=lambda s: s.display_title.lower())
            self._rows.sort(key=key, reverse=True)

    def _cycle_sort(self) -> None:
        selected = self._selected_path()
        index = SORT_MODES.index(self._sort_mode)
        self._sort_mode = SORT_MODES[(index + 1) % len(SORT_MODES)]
        self._sort_rows()
        self._selected = next((i for i, song in enumerate(self._rows) if song.file == selected), 0)
        self._scroll_px = 0
        self._reveal_selection = True
        if self._config:
            self._config.sort_mode = self._sort_mode
            self._config.save()

    def _filter_entries(self) -> list[tuple[str, tuple[str, object], int, bool]]:
        """(label, filter, count, is_heading) for the sidebar."""
        songs = self.catalogue.entries(self._songs)
        cache_key = (id(self._songs), len(self._songs), self._instrument().key,
                     self.catalogue.category, tuple(sorted(self.catalogue.versions.items())),
                     getattr(self._progress, "revision", 0), current_language())
        if self._cached_filters[0] == cache_key:
            return self._cached_filters[1]
        entries: list[tuple[str, tuple[str, object], int, bool]] = [
            (t("menu.all_songs"), ("all", None), len(songs), False),
            (t("menu.heading_difficulty"), ("heading", None), -1, True),
        ]
        counts = library.counts_by_level(songs)
        for level in sorted(library.LEVEL_KEYS):
            entries.append((library.level_name(level), ("level", level),
                            counts.get(level, 0), False))
        entries.append((t("menu.heading_progress"), ("heading", None), -1, True))
        buckets = {"new": 0, "wip": 0, "done": 0}
        for song in songs:
            buckets[self._bucket(song)] += 1
        for key, label_key in (("new", "menu.progress_new"),
                               ("wip", "menu.progress_wip"),
                               ("done", "menu.progress_done")):
            entries.append((t(label_key), ("progress", key), buckets[key],
                            False))
        # Counted here rather than through library.counts_by_folder, because
        # the count and the filter have to agree on what a folder is, and the
        # filter asks _folder_of.
        folders: dict[str, int] = {}
        for song in songs:
            name = self._folder_of(song)
            folders[name] = folders.get(name, 0) + 1
        if folders:
            entries.append((t("menu.heading_folder"), ("heading", None), -1,
                            True))
            for name in sorted(folders):
                entries.append((name or t("menu.no_folder"), ("folder", name),
                                folders[name], False))
        self._cached_filters = (cache_key, entries)
        return entries

    def _filter_name(self) -> str:
        """What the active filter is called, in the words the sidebar uses.

        Taken from the same list the sidebar draws, so the footer cannot end
        up naming a filter differently from the row that switched it on.
        """
        for label, value, _count, _heading in self._filter_entries():
            if value == self._filter:
                return label
        return str(self._filter[1])

    def _set_filter(self, value: tuple[str, object]) -> None:
        self._filter = value
        self._apply_filter()

    def _cycle_filter(self, step: int) -> None:
        options = [f for _, f, _, heading in self._filter_entries()
                   if not heading]
        if not options:
            return
        try:
            index = options.index(self._filter)
        except ValueError:
            index = 0
        self._set_filter(options[(index + step) % len(options)])

    # ── what to play next ───────────────────────────────────────────────

    def suggestion(self) -> Path | None:
        """The piece to practise next.

        A beginner facing hundreds of files does not need a better sort order,
        they need one answer. The rule is deliberately simple so the answer
        never looks arbitrary: easiest first, and offer the first piece not yet
        played well. If everything has been, offer the weakest.
        """
        # A file that would not open is no suggestion at all.
        playable = [s for s in self._songs if s.playable]
        if not playable:
            return None
        ordered = sorted(playable,
                         key=lambda s: (s.difficulty, s.display_title.lower(),
                                        s.file.stem.lower()))
        weakest: tuple[float, Path] | None = None
        for song in ordered:
            accuracy = self._accuracy(song) or 0.0
            if accuracy < PASSED_PERCENT:
                return song.file
            if weakest is None or accuracy < weakest[0]:
                weakest = (accuracy, song.file)
        return weakest[1] if weakest else ordered[0].file

    # ── events ──────────────────────────────────────────────────────────

    def handle_event(self, event: pygame.event.Event) -> Path | str | None:
        result = self._handle_event(event)
        self._sync_catalogue()
        if result is not None:
            self.catalogue.stop()
        return result

    def _handle_event(self, event: pygame.event.Event) -> Path | str | None:
        if self._resume_panel is not None:
            action = self._resume_panel.handle_event(event)
            if action == "resume":
                self._resume_panel = None
                return "resume"
            if action == "close":
                self._resume_panel = None
            return None
        if event.type == pygame.KEYDOWN:
            return self._handle_key(event)
        if event.type == pygame.MOUSEMOTION:
            self._pointer = event.pos
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self._pointer = event.pos
            return self._handle_click(event.pos)
        if event.type == pygame.MOUSEWHEEL:
            if event.y:
                pointer = self._pointer if self._pointer is not None else pygame.mouse.get_pos()
                if self._setup_open and any(r.collidepoint(pointer) for r in
                                           self._button_rects.get("setup.card", [])):
                    maximum = self._last_page.setup_maximum if self._last_page else 0
                    self._setup_scroll = max(0, min(maximum, self._setup_scroll - event.y * 52))
                    return None
                detail = self.catalogue.detail_rect
                if self.catalogue.filters_open and any(r.collidepoint(pointer) for r in
                                                       self._button_rects.get("filters.card", [])):
                    self.catalogue.filter_scroll = max(0, min(self.catalogue.filter_maximum,
                        self.catalogue.filter_scroll-event.y*36))
                    return None
                if detail and detail.collidepoint(pointer):
                    self.catalogue.detail_scroll = max(0, min(self.catalogue.detail_maximum,
                        self.catalogue.detail_scroll - event.y * 48))
                    return None
                pitch = self._last_page.hit_row_h if self._last_page is not None else ROW_H
                self._scroll_px = max(0, self._scroll_px - event.y * max(1, pitch // 2))
                self._reveal_selection = False
        return None

    def _escape_rung(self) -> str:
        """What Esc would undo right now: search, then filter, then the app.

        It used to be search, then the app. So a filter narrowing ten pieces
        to four could sit there while the footer counted all ten, and the
        only key that looks like "never mind" closed the program instead of
        the filter. The footer names the live rung before it is pressed.
        """
        if self._setup_open:
            return "setup"
        if self._search_active or self._search_text:
            return "search"
        if self._filter != ("all", None):
            return "filter"
        return "quit"

    def _handle_key(self, event) -> Path | str | None:
        self._last_click_time = -400
        if event.key == pygame.K_ESCAPE and self.catalogue.filters_open:
            self.catalogue.filters_open = False
            return None
        if not self._search_active and not self._setup_open:
            categories = {pygame.K_1: "music", pygame.K_2: "technique", pygame.K_3: "files"}
            if event.key in categories:
                self.select_category(categories[event.key])
                return None
            if event.key == pygame.K_SPACE:
                self._catalogue_action("preview")
                return None
            if event.key == pygame.K_RIGHT:
                self._catalogue_action("details")
                return None
            if event.key in (pygame.K_LEFT, pygame.K_ESCAPE) and self.catalogue.detail_open:
                self.catalogue.detail_open = False
                return None
            if self.catalogue.detail_open and event.key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
                self._catalogue_action("details.up" if event.key == pygame.K_PAGEUP else "details.down")
                return None
            if event.key == pygame.K_v:
                song = self._sync_catalogue()
                if song and self.catalogue.kind(song) == "music":
                    data = self.catalogue.metadata(song, self._instrument())
                    self._catalogue_action("version.groove" if data["version"] == "start" else "version.start")
                return None
        if event.key == pygame.K_F2:
            self._setup_open = False
            return "wizard"
        if not self._search_active:
            if event.key == pygame.K_u:
                return "record"
            if event.key == pygame.K_k:
                return "lessons"
            if event.key == pygame.K_r:
                if getattr(event, "mod", 0) & pygame.KMOD_SHIFT:
                    self._open_resume_details()
                    return None
                return "resume" if self._resume_state() is not None else None
            if event.key == pygame.K_F5:
                self.scan_files()
                return None
            if event.key == pygame.K_ESCAPE and self._scan_job is not None:
                self._cancel_scan()
                return None
        if event.key == pygame.K_ESCAPE:
            rung = self._escape_rung()
            if rung == "setup":
                self._setup_open = False
                return None
            if rung == "search":
                self._search_active = False
                self._search_text = ""
                self._apply_filter()
                return None
            if rung == "filter":
                self._set_filter(("all", None))
                return None
            return "quit"

        if event.key == pygame.K_s and not self._search_active:
            self.catalogue.filters_open = False
            self._setup_open = not self._setup_open
            return None

        if self._navigate_list(event.key):
            return None

        if self._search_active:
            if event.key == pygame.K_BACKSPACE:
                self._search_text = self._search_text[:-1]
                self._apply_filter()
                return None
            if event.key == pygame.K_RETURN:
                return self._selected_path()
            character = event.unicode
            if character and character.isprintable() and character not in "\r\n\t":
                self._search_text += character
                self._apply_filter()
            return None

        if event.key in (pygame.K_f, pygame.K_SLASH):
            self._search_active = True
            self._search_text = ""
            self._apply_filter()
            return None
        if event.key == pygame.K_TAB:
            mods = pygame.key.get_mods()
            self._cycle_filter(-1 if mods & pygame.KMOD_SHIFT else 1)
        elif event.key == pygame.K_RETURN:
            return self._selected_path()
        elif event.key == pygame.K_w:
            return self.suggestion()
        elif event.key == pygame.K_n:
            self._cycle_sort()
        elif event.key == pygame.K_i:
            self._cycle_instrument()
        elif event.key == pygame.K_t:
            name = cycle_look()
            if self._config:
                self._config.look = name
                self._config.save()
        elif event.key == pygame.K_o:
            self._open_folder()
        # D, G, C and S open other screens and are intercepted by App before
        # this runs, so they are deliberately absent here.
        return None

    def _navigate_list(self, key: int) -> bool:
        """Use the same list navigation while browsing and entering a search."""
        steps = {pygame.K_UP: -1, pygame.K_DOWN: 1,
                 pygame.K_PAGEUP: -8, pygame.K_PAGEDOWN: 8}
        if key not in steps and key not in (pygame.K_HOME, pygame.K_END):
            return False
        if key in steps:
            self._move(steps[key])
        else:
            self._selected = 0 if key == pygame.K_HOME else max(0, len(self._rows) - 1)
            self._reveal_selection = True
        return True

    # Every name the click chain below answers to. A painter that reports
    # anything else has drawn a region, not a control -- and a region must
    # not be able to act, only to block what is under it.
    HANDLED = ("hero", "setup", "sort", "folder", "play", "suggest",
               "instrument", "language", "theme", "search",
               "check", "calibrate", "device", "lessons", "resume", "rescan", "cancel_scan",
               "wizard", "resume_details", "timing", "record", "setup.up", "setup.down", "lesson_steps")

    CATALOGUE_ACTIONS = ("category.music", "category.technique", "category.files",
                         "version.start", "version.groove", "preview", "details", "details.back",
                         "instructions", "filters", "details.up", "details.down", "lesson_steps")
    CATALOGUE_ACTIONS += ("filters.up", "filters.down")

    def _handle_click(self, pos) -> Path | None:
        if self._setup_open and not any(rect.collidepoint(pos) for rect in
                                       self._button_rects.get("setup.card", [])):
            self._setup_open = False
            # Dismissal consumes this click and ends any earlier double-click.
            self._last_click_time = -400
            return None
        if self.catalogue.filters_open and not any(rect.collidepoint(pos) for rect in
                                                  self._button_rects.get("filters.card", []) +
                                                  self._button_rects.get("filters", [])):
            self.catalogue.filters_open = False
            self._last_click_time = -400
            return None
        for rect, value in self._side_rects:
            if rect.collidepoint(pos):
                self._set_filter(value)
                return None
        # Only names this chain answers to. A painter marks regions that are
        # not controls -- Studio marks its setup card so a click cannot fall
        # through to the row underneath -- and an unknown name used to fall
        # into the chain, match nothing, and return, which swallowed every
        # click on the panel it was covering.
        filters = {f"filter.{value[0]}.{value[1]}": value
                   for _, value, _, heading in self._filter_entries() if not heading}
        for name, rects in reversed(list(self._button_rects.items())):
            if name not in self.HANDLED and name not in filters and name not in self.CATALOGUE_ACTIONS:
                continue
            if isinstance(rects, pygame.Rect):
                rects = [rects]
            if not any(rect.collidepoint(pos) for rect in rects):
                continue
            self._last_click_time = -400
            if name == "lesson_steps":
                self.catalogue.stop()
                return name
            if name in self.CATALOGUE_ACTIONS:
                self._catalogue_action(name)
            elif name in filters:
                self._set_filter(filters[name])
                self.catalogue.filters_open = False
            elif name == "search":
                self._search_active = True
            elif name == "hero":
                carry = self._continue_song()
                if carry is not None:
                    return carry[0].file
            elif name == "setup":
                self._setup_open = not self._setup_open
            elif name in ("setup.up", "setup.down"):
                maximum = self._last_page.setup_maximum if self._last_page else 0
                self._setup_scroll = max(0, min(maximum, self._setup_scroll +
                    (-52 if name == "setup.up" else 52)))
            elif name == "sort":
                self._cycle_sort()
            elif name == "folder":
                self._open_folder()
            elif name == "play":
                return self._selected_path()
            elif name == "suggest":
                return self.suggestion()
            elif name == "instrument":
                self._cycle_instrument()
            elif name == "language":
                return "language"
            elif name == "theme":
                new_theme = cycle_look()
                if self._config:
                    self._config.look = new_theme
                    self._config.save()
            elif name in ("check", "calibrate", "device", "timing"):
                return name
            elif name in ("lessons", "resume", "wizard", "record"):
                return name
            elif name == "resume_details":
                self._open_resume_details()
            elif name == "rescan":
                self.scan_files()
            elif name == "cancel_scan":
                self._cancel_scan()
            return None
        for rect, index in self._row_rects:
            if rect.collidepoint(pos):
                now = pygame.time.get_ticks()
                if index == self._selected and now - self._last_click_time < 400:
                    return self._selected_path()
                self._selected = index
                self._reveal_selection = True
                self._last_click_time = now
                return None
        return None

    def _move(self, step: int) -> None:
        self._reveal_selection = True
        if not self._rows:
            return
        self._selected = max(0, min(len(self._rows) - 1, self._selected + step))

    def _selected_path(self) -> Path | None:
        if not self._rows:
            return None
        return self._rows[self._selected].file

    def practice_for(self, path):
        from pickhero.music import music_practice
        song = next((row for row in self._songs if row.file == path), None)
        return (music_practice(path, self._instrument())
                if song and self.catalogue.kind(song) == "music" else None)

    def _next_language_name(self) -> str:
        """The name of the language the button would switch to."""
        from pickhero.i18n import available_languages, current_language
        languages = available_languages()
        if not languages:
            return ""
        codes = [code for code, _ in languages]
        try:
            index = codes.index(current_language())
        except ValueError:
            index = -1
        return languages[(index + 1) % len(languages)][1]

    def _cycle_instrument(self) -> None:
        """Step through the instrument profiles.

        The instrument decides which track of a tab is loaded, how many string
        lanes are drawn and which pitch detector runs, so the library has to be
        re-indexed with it: difficulty depends on which part you are playing.
        """
        if self._config is None:
            return
        from pickhero import instruments
        keys = list(instruments.PROFILES)
        try:
            index = keys.index(self._config.instrument)
        except ValueError:
            index = -1
        self._config.switch_instrument(keys[(index + 1) % len(keys)])
        self._config.save()
        if self._background:
            # Do not show a previous instrument's parts while its replacement
            # index is being prepared in the background.
            self._songs = []
            self._scanned_songs = []
            self._rows = []
        try:
            from pickhero.exercises import ensure_exercises
            ensure_exercises(self._config.get_instrument(), self._songs_dir,
                             progress=self._progress)
            from pickhero.music import ensure_music
            ensure_music(self._config.get_instrument(), self._songs_dir, progress=self._progress)
        except Exception as exc:
            log_error(f"Could not regenerate exercises: {exc}")
        self.scan_files()

    def _open_folder(self) -> None:
        """Show the songs directory in the file manager."""
        # os.startfile runs whatever it is given. songs_dir comes from the
        # settings file, and a path that is not a folder must not be opened
        # -- an .exe there would be started, not shown.
        if not Path(self._songs_dir).is_dir():
            log_error(f"Not a folder, not opened: {self._songs_dir}")
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(self._songs_dir))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self._songs_dir)])
            else:
                subprocess.Popen(["xdg-open", str(self._songs_dir)])
        except Exception as exc:
            log_error(f"Could not open {self._songs_dir}: {exc}")

    # ── drawing ─────────────────────────────────────────────────────────

    @staticmethod
    def _action_width(label: str, minimum: int = 92) -> int:
        """How wide a button has to be to hold its own label.

        The widths used to be literals, tuned against the German words. The
        moment the interface could speak English, "Mikrofon" became
        "Microphone" and the button truncated it to "Micropho..." -- a label
        chosen for being the shortest true thing, cut until it was neither.
        """
        return max(minimum, design.font(design.TEXT_SM, bold=True).size(label)[0]
                   + design.SPACE_5)

    def _action(self, surface: pygame.Surface, rect: pygame.Rect, label: str,
                key: str, name: str, *, primary: bool = False) -> None:
        """A labelled button with its shortcut, registered for the mouse.

        The shortcuts used to live only in a line of grey text at the bottom,
        which is how the check screen and the tuner became unreachable for
        anyone who had not been told they existed.
        """
        look = get_look()
        if primary:
            pygame.draw.rect(surface, look.hud_accent, rect,
                             border_radius=design.RADIUS_MD)
            label_colour = design.on_accent(look.hud_accent)
            key_colour = design.mix(design.on_accent(look.hud_accent), look.hud_accent, 0.32)
        else:
            pygame.draw.rect(surface, design.surface(look.menu_bg, 1),
                             rect, border_radius=design.RADIUS_MD)
            pygame.draw.rect(surface, design.stroke(look.menu_bg, 1),
                             rect, width=1, border_radius=design.RADIUS_MD)
            label_colour = design.ink(look.menu_bg, 1.0)
            key_colour = design.ink(look.menu_bg, 0.55)
        if key:
            # Trim the label rather than let it run past its own button.
            face = design.font(design.TEXT_SM, bold=True)
            shown = label
            while shown and face.size(shown)[0] > rect.width - 14:
                shown = shown[:-1]
            if shown != label:
                shown = shown[:-1] + "…"
            design.text(surface, shown, (rect.centerx, rect.y + 4), label_colour,
                        design.TEXT_SM, bold=True, align="center")
            design.text(surface, key, (rect.centerx, rect.y + 23), key_colour,
                        design.TEXT_XS, align="center")
        else:
            design.text(surface, label, (rect.centerx, rect.centery - 9),
                        label_colour, design.TEXT_SM, bold=True, align="center")
        self._button_rects.setdefault(name, []).append(rect)

    def _page(self, surface: pygame.Surface) -> Page:
        """One frame's worth of everything the look is allowed to see.

        Built fresh rather than kept, because it is a view of state that is
        about to change and a layout that is stale only sometimes is worse
        than one that is always stale.
        """
        width, height = surface.get_size()
        cache_key = (id(self._rows), len(self._rows), id(self._songs), len(self._songs),
                     self._sort_mode, getattr(self._progress, "revision", 0))
        if self._cached_page_data[0] != cache_key:
            scores: dict[int, float | None] = {}
            history: dict[int, tuple[int, str]] = {}
            for index, song in enumerate(self._rows):
                record = self._record_for(song) if self._progress else None
                scores[index] = float(record.best_accuracy) if record and record.attempts else None
                if record is not None:
                    history[index] = (record.attempts, (record.last_played or "")[:10])
            played = sum(1 for song in self._songs if self._accuracy(song) is not None)
            self._cached_page_data = (cache_key, (scores, history, played, self._continue_song()))
        scores, history, played, carry = self._cached_page_data[1]
        instrument = self._instrument()
        empty_title, empty_hint = self._empty_state()
        return Page(
            width=width, height=height,
            rows=self._rows, selected=self._selected, scroll_px=self._scroll_px,
            reveal_selection=self._reveal_selection,
            filters=[(label, value, count, value == self._filter)
                     for label, value, count, heading in self._filter_entries()
                     if not heading],
            carry=carry,
            scores=scores, history=history,
            search_active=self._search_active, search_text=self._search_text,
            setup_open=self._setup_open,
            setup_scroll=self._setup_scroll,
            instrument_name=self._instrument_name(),
            instrument_short=self._instrument_short_name(),
            tuning_label=self._tuning_label(instrument),
            device_name=self._device_name,
            device_ready=bool(self._device_name),
            counts=(len(self._songs), played),
            sort_label=t(SORT_LABEL_KEYS[self._sort_mode]),
            empty_title=empty_title, empty_hint=empty_hint,
        )

    def _empty_state(self) -> tuple[str, str]:
        category_songs = self.catalogue.entries(self._songs)
        if not category_songs and self.catalogue.entries(self._scanned_songs):
            return (t("menu.empty_instrument", instrument=self._instrument_short_name()),
                    t("menu.empty_instrument_hint"))
        if self._songs:
            title = (t("menu.empty_search") if self._search_text
                     else t("menu.empty_filter", name=self._filter_name()))
            return title, t("menu.empty_filter_hint")
        if self._scanned_songs:
            return (t("menu.empty_instrument", instrument=self._instrument_short_name()),
                    t("menu.empty_instrument_hint"))
        return t("menu.empty_library"), t("menu.empty_hint")

    @staticmethod
    def _tuning_label(instrument) -> str:
        """"E A D G", lowest string last, the way the drawings write it.

        Built from the instrument's own tuning rather than from its name, so
        an alternate tuning read out of a GP file says what it actually is
        instead of repeating what the instrument is called.
        """
        if instrument is None or not getattr(instrument, "tuning", None):
            return ""
        from pickhero.audio.note_utils import midi_to_name

        names = []
        for string in sorted(instrument.tuning, reverse=True):
            name = midi_to_name(instrument.tuning[string])
            names.append(name[:-1] if name and name[-1].isdigit() else name)
        return " ".join(names)

    def render(self, surface: pygame.Surface) -> None:
        from pickhero.ui.library_browser import render
        self._sync_catalogue()
        render(surface, self)
        return

    def _render_legacy(self, surface: pygame.Surface) -> None:
        full_surface = surface
        # Keep workflow actions and their context at a readable size. The
        # library painters receive the remaining height, so no footer covers
        # the last song and all hit regions still describe visible controls.
        shelf_h = 76 if surface.get_width() < 650 else 52
        if self._resume_state():
            shelf_h += 32
        if surface.get_width() < 1000 and self._scan_status:
            shelf_h += 26
        surface = full_surface.subsurface((0, 0, surface.get_width(), max(1, surface.get_height()-shelf_h)))
        look = get_look()
        surface.fill(look.panel_right)
        width, height = surface.get_size()
        self._row_rects.clear()
        self._side_rects.clear()
        self._button_rects.clear()

        paint = painter(look.key)
        if paint is None or not hasattr(paint, "library"):
            # No painter for this look yet. Draw the plain version rather
            # than nothing: a missing module is a packaging mistake and a
            # packaging mistake that blanks the screen stops their practising.
            self._draw_header(surface, width)
            sidebar = SIDEBAR_W if width >= 860 else 0
            if sidebar:
                self._draw_sidebar(surface, sidebar, height)
            self._draw_rows(surface, sidebar, width, height)
            self._draw_footer(surface, width, height)
            if self._setup_open:
                self._draw_setup(surface, width, height)
            self._draw_workflows(full_surface, shelf_h)
            if self._resume_panel is not None:
                self._resume_panel.render(full_surface)
            return

        page = self._page(surface)
        if height < 220 and not self._setup_open:
            self._draw_compact_library(surface, page)
        else:
            paint.library(surface, page)
        self._last_page = page
        self._scroll_px = page.scroll_px
        self._setup_scroll = page.setup_scroll
        # The painter reports what it actually drew, so a control that was
        # not drawn this frame cannot be clicked. That is the whole reason
        # the hit map is rebuilt rather than kept.
        self._button_rects = {name: rects for name, rects in page.hit.items()}
        self._row_rects = list(page.row_hit)
        self._draw_workflows(full_surface, shelf_h)
        if self._resume_panel is not None:
            self._resume_panel.render(full_surface)

    def _draw_compact_library(self, surface, page):
        """Keep real song rows reachable when the action shelf leaves little room."""
        from pickhero.ui.secondary import SetupView, panel, text
        look = get_look()
        pad, gap, row_h, top = 10, 6, 40, 44
        from pickhero.ui import library_setup
        setup = library_setup.button(surface, page)
        search = pygame.Rect(pad, setup.y, max(1, setup.x - pad - gap), setup.height)
        top = setup.bottom + 8
        buttons = SetupView()
        buttons.button(surface, search, (self._search_text + "_" if self._search_active
                                         else t("menu.search_placeholder")))
        page.mark("search", search)
        band = max(1, page.height - top - 4)
        scroll = page.clamp_scroll(row_h, band)
        clip = surface.get_clip()
        area = pygame.Rect(pad, top, max(1, page.width - 2 * pad - 12), band)
        surface.set_clip(clip.clip(area))
        first, last = scroll // row_h, min(len(page.rows), (scroll + band) // row_h + 1)
        for index in range(first, last):
            song = page.rows[index]
            box = pygame.Rect(area.x, top + index * row_h - scroll, area.width, row_h - 3)
            panel(surface, box, accent=index == page.selected)
            title = song.display_title.upper() if look.key == "stage" else song.display_title
            text(surface, f"{index + 1:02d}  {title}",
                 (box.x + 7, box.y + 2, box.width - 14, 19), size=14,
                 bold=look.key != "hifi", colour=look.ink_1)
            text(surface, song.summary(), (box.x + 7, box.y + 21, box.width - 14, 14),
                 size=10, colour=look.ink_2)
            page.mark_row(box.clip(area), index)
        if not page.rows:
            text(surface, page.empty_title, area, size=13, colour=look.ink_2)
        surface.set_clip(clip)
        self._draw_rail(surface, page.width - pad, top, band, len(page.rows) * row_h, scroll)

    def _resume_state(self):
        key = (self._instrument().key, getattr(self._progress, "revision", 0))
        if self._cached_resume[0] != key:
            state = self._progress.latest_session(key[0]) if self._progress is not None else None
            self._cached_resume = (key, state)
        return self._cached_resume[1]

    def _open_resume_details(self):
        from pickhero.ui.import_menu import resume_description
        from pickhero.ui.practice_panels import Action, PracticePanel
        state = self._resume_state()
        if state is None:
            return
        path = Path(state["song_path"])
        title = next((song.display_title for song in self._songs if song.file == path), path.stem)
        self._resume_panel = PracticePanel("resume", t("library.resume_title"), title,
            [(t("library.resume_saved"), resume_description(state)),
             ("", [t("library.resume_countin")])],
            [Action("resume", t("import.resume_short"), pygame.K_r),
             Action("close", t("practice.close"))])

    def _draw_workflows(self, surface, shelf_h):
        from pickhero.ui.secondary import SetupView, text
        look = get_look()
        width, height = surface.get_size()
        top = height - shelf_h
        pygame.draw.rect(surface, look.panel_left, (0, top, width, shelf_h))
        pygame.draw.line(surface, look.hairline_loud, (0, top), (width, top))
        state = self._resume_state()
        # The saved practice is the immediate continuation. The learning
        # path remains the first choice only before there is one to resume.
        actions = ([("resume", t("import.resume_short"))] if state else [])
        actions.extend([("lessons", t("music.library_short") if width < 650 else t("import.lessons")),
                        ("wizard", t("passage.guide_short") if width < 1000 else t("setup.guide"))])
        if width >= 650:
            actions.append(("record", t("record.header")))
        actions.append(("cancel_scan", t("import.cancel_scan")) if self._scan_job else
                       ("rescan", t("import.rescan")))
        gap, pad = 7, 10
        available = width-2*pad
        status_w = min(270, width//3) if width >= 1000 and self._scan_status else 0
        columns = min(len(actions), 2 if width < 650 else len(actions))
        bw = max(1, (available-status_w-gap*(columns-1))//columns)
        controls = SetupView()
        button_h, pitch = (32, 36) if width < 650 else (36, 40)
        primary = "resume" if state else "lessons"
        for index, (name, label) in enumerate(actions):
            box = pygame.Rect(pad+(index % columns)*(bw+gap),
                              top+6+(index // columns)*pitch, bw, button_h)
            self._button_rects[name] = box
            controls.button(surface, box, label, primary=name == primary)
        row_top = top + (76 if width < 650 else 52)
        if state:
            from pickhero.ui.import_menu import resume_description
            description = resume_description(state)
            path = Path(state["song_path"])
            title = next((song.display_title for song in self._songs if song.file == path), path.stem)
            if width >= 900:
                info = " · ".join((title, description[0], description[2]))
            elif width >= 650:
                info = " · ".join((title, description[0]))
            else:
                info = f"{description[0]} · {round(state.get('tempo_factor', 1) * 100)}%"
            details_w = 172 if width >= 900 else 82
            details = pygame.Rect(width - pad - details_w, row_top, details_w, 28)
            self._button_rects["resume_details"] = details
            controls.button(surface, details, t("library.resume_details_key") if width >= 900
                            else t("library.resume_details"))
            text(surface, info, (pad + 2, row_top, details.x - pad - gap - 2, 28),
                 size=14, colour=look.ink_1)
            row_top += 32
        if self._scan_status:
            rect = ((width-status_w+4, top+10, status_w-14, 28) if status_w
                    else (pad + 2, row_top, available - 4, 22))
            text(surface, self._scan_status, rect, size=14, colour=look.ink_2)

    # Every key the browser answers to, with the label and hint it shows.
    # This list is the key-hint contract: a key may exist only if it appears
    # here, and this panel is always one keystroke away, so no key can end up
    # live but invisible. Two of them were exactly that before -- Thema and
    # Sortieren dropped out of the footer below 1180 and 1020 px while T and
    # N kept working.
    SETUP_ROWS = (
        ("record.title", "key.u", "record"),
        ("setup.guide", "key.f2", "wizard"),
        ("timing.title", "key.f3", "timing"),
        ("menu.instrument", "key.i", "instrument"),
        ("menu.device", "key.d", "device"),
        ("menu.check", "key.c", "check"),
        ("menu.tuner", "key.g", "calibrate"),
        ("menu.theme", "key.t", "theme"),
        ("menu.language", "key.l", "language"),
        ("menu.sort", "key.n", "sort"),
        ("menu.folder", "key.o", "folder"),
    )

    def _draw_setup(self, surface: pygame.Surface, width: int,
                    height: int) -> None:
        look = get_look()
        # scrim() is an opaque colour; the veil is that colour at an alpha.
        # Filling with it directly hid the whole browser behind a black wall
        # and made the panel look like a different screen rather than
        # something laid over this one.
        tint = design.with_alpha(design.scrim(look.menu_bg), 168)
        surface.blit(
            design.sprite(("menu-veil", (width, height), tint),
                          (width, height), lambda layer: layer.fill(tint)),
            (0, 0))

        rows = len(self.SETUP_ROWS)
        panel_w = min(420, width - design.SPACE_8)
        panel_h = min(height - design.SPACE_8, 78 + rows * 46 + 34)
        panel = pygame.Rect(width - panel_w - design.SPACE_5, HEADER_H + 6,
                            panel_w, panel_h)
        panel.bottom = min(panel.bottom, height - FOOTER_H - design.SPACE_3)
        design.card(surface, panel, design.surface(look.menu_bg, 2),
                    radius=design.RADIUS_LG,
                    ring=design.stroke(look.menu_bg, 1), ring_width=1)

        design.text(surface, t("menu.setup"),
                    (panel.x + design.SPACE_5, panel.y + design.SPACE_4),
                    design.ink(look.menu_bg, 1.0), design.TEXT_LG, bold=True)
        design.text(surface, t("menu.setup_hint"),
                    (panel.x + design.SPACE_5, panel.y + 44),
                    design.ink(look.menu_bg, 0.55), design.TEXT_XS)

        y = panel.y + 74
        for label_key, key_key, name in self.SETUP_ROWS:
            if y + 42 > panel.bottom - 30:
                break
            row = pygame.Rect(panel.x + design.SPACE_3, y,
                              panel.width - design.SPACE_3 * 2, 42)
            pygame.draw.rect(surface, design.surface(look.menu_bg, 1), row,
                             border_radius=design.RADIUS_MD)
            if name == "sort":
                label = t(SORT_LABEL_KEYS[self._sort_mode])
            elif name == "theme":
                label = t("menu.look",
                          name=t(look_name_key(get_look().key)))
            else:
                label = t(label_key)
            design.text(surface, label,
                        (row.x + design.SPACE_4, row.centery - 9),
                        design.ink(look.menu_bg, 0.95), design.TEXT_SM)
            cap = pygame.Rect(row.right - 42, row.centery - 13, 30, 26)
            pygame.draw.rect(surface, design.surface(look.menu_bg, 3), cap,
                             border_radius=design.RADIUS_SM)
            design.text(surface, t(key_key), (cap.centerx, cap.y + 4),
                        design.ink(look.menu_bg, 0.9), design.TEXT_XS,
                        mono=True, align="center")
            self._button_rects.setdefault(name, []).append(row)
            y += 46

        design.text(surface, f"{t('key.escape')}  ·  {t('menu.close')}",
                    (panel.x + design.SPACE_5, panel.bottom - 26),
                    design.ink(look.menu_bg, 0.55), design.TEXT_XS)

    def _draw_header(self, surface: pygame.Surface, width: int) -> None:
        look = get_look()
        pygame.draw.rect(surface, design.chrome(look.menu_bg),
                         (0, 0, width, HEADER_H))
        pygame.draw.line(surface, design.stroke(look.menu_bg, 0),
                         (0, HEADER_H - 1), (width, HEADER_H - 1), 1)
        design.text(surface, "PickHero", (design.SPACE_5, 20), look.hud_accent,
                    design.TEXT_XL, bold=True)

        right = width - design.SPACE_5
        # One control where there were four. It says which instrument is
        # live -- the thing the player actually needs to see from the browser -- and
        # opens everything else. Nothing here is allowed to drop out at a
        # narrow width any more: this button is the guarantee that every key
        # the screen answers to has its hint somewhere the player can reach.
        name = self._instrument_short_name()
        pill_w = min(230, max(160, design.font(design.TEXT_SM).size(name)[0] + 86))
        pill = pygame.Rect(right - pill_w, 12, pill_w, 42)
        pygame.draw.rect(surface,
                         design.surface(look.menu_bg, 2 if self._setup_open else 1),
                         pill, border_radius=design.RADIUS_PILL)
        pygame.draw.rect(surface,
                         look.hud_accent if self._setup_open
                         else design.stroke(look.menu_bg, 1),
                         pill, width=1, border_radius=design.RADIUS_PILL)
        design.toggle_dot(surface, (pill.x + 18, pill.centery), True,
                          on_colour=look.hud_accent)
        design.text(surface, name, (pill.x + 32, pill.centery - 9),
                    design.ink(look.menu_bg, 1.0), design.TEXT_SM)
        design.text(surface, t("key.s"), (pill.right - 16, pill.centery - 8),
                    design.ink(look.menu_bg, 0.62), design.TEXT_XS,
                    align="right")
        self._button_rects.setdefault("setup", []).append(pill)
        right = pill.x - design.SPACE_3

        box_w = min(420, max(180, right - 180))
        box = pygame.Rect(180, 16, box_w, 32)
        focus = look.hud_accent if self._search_active else design.shade(
            look.menu_bg, 1.5)
        pygame.draw.rect(surface, design.surface(look.menu_bg, 1),
                         box, border_radius=design.RADIUS_PILL)
        pygame.draw.rect(surface, focus, box, width=1,
                         border_radius=design.RADIUS_PILL)
        if self._search_active:
            label = f"{self._search_text}_"
            colour = look.hud_text
        else:
            label = t("menu.search_placeholder")
            colour = design.ink(look.menu_bg, 0.55)
        design.text(surface, label, (box.x + design.SPACE_4, box.centery - 9),
                    colour, design.TEXT_SM)
        if not self._search_active:
            design.text(surface, t("key.f"),
                        (box.right - design.SPACE_3, box.centery - 8),
                        design.ink(look.menu_bg, 0.5), design.TEXT_XS,
                        align="right")

    def _draw_sidebar(self, surface: pygame.Surface, sidebar: int,
                      height: int) -> None:
        look = get_look()
        top, bottom = HEADER_H, height - FOOTER_H
        pygame.draw.rect(surface, design.chrome(look.menu_bg),
                         (0, top, sidebar, bottom - top))
        pygame.draw.line(surface, design.stroke(look.menu_bg, 0),
                         (sidebar - 1, top), (sidebar - 1, bottom), 1)
        y = HEADER_H + design.SPACE_3
        for label, value, count, heading in self._filter_entries():
            if y > height - FOOTER_H - 24:
                break
            if heading:
                y += design.SPACE_3
                design.text(surface, label, (design.SPACE_4, y),
                            design.ink(look.menu_bg, 0.5), design.TEXT_XS)
                y += 22
                continue
            rect = pygame.Rect(design.SPACE_2, y - 4, sidebar - design.SPACE_4, 28)
            active = self._filter == value
            if active:
                pygame.draw.rect(surface, design.surface(look.menu_bg, 1),
                                 rect, border_radius=design.RADIUS_SM)
                pygame.draw.rect(surface, look.hud_accent,
                                 (rect.x, rect.y, 3, rect.height))
            text_x = design.SPACE_4 + 6
            if value[0] == "level":
                # One dot, not five bars. The bars spent 48 px repeating what
                # the word beside them already said, and the word is the part
                # that survives being small or being photographed.
                level = int(value[1])
                pygame.draw.circle(surface, design.difficulty_colour(level),
                                   (text_x + 4, y + 9), 4)
                text_x += 18
            design.text(surface, label, (text_x, y),
                        look.hud_text if active
                        else design.ink(look.menu_bg, 0.85),
                        design.TEXT_SM)
            design.text(surface, str(count), (sidebar - design.SPACE_4, y + 1),
                        design.ink(look.menu_bg, 0.55), design.TEXT_XS,
                        align="right")
            self._side_rects.append((rect, value))
            y += 30

    def _draw_rows(self, surface: pygame.Surface, sidebar: int, width: int,
                   height: int) -> None:
        look = get_look()
        left = sidebar + design.SPACE_4
        top = HEADER_H + design.SPACE_3
        bottom = height - FOOTER_H - design.SPACE_2
        right = width - design.SPACE_4
        # The hero only earns its space when there is something to continue
        # and the list is not so cramped that it would hide more than it
        # shows: below two visible rows it costs more than it gives.
        carry = self._continue_song() if not self._search_text else None
        if carry is not None and (bottom - top) >= self.HERO_H + ROW_H * 3:
            top = self._draw_hero(surface, left, top,
                                  right - left - RAIL_W - design.SPACE_2,
                                  carry[0], carry[1])
        band = self.row_band(bottom - top, len(self._rows))
        page = Page(width, height, rows=self._rows, selected=self._selected,
                    scroll_px=self._scroll_px, reveal_selection=self._reveal_selection)
        self._scroll_px = page.clamp_scroll(ROW_H, band)
        self._last_page = page

        if not self._rows:
            message, hint = self._empty_state()
            design.text(surface, message, ((left + width) // 2, top + 60),
                        design.ink(look.menu_bg, 0.75), design.TEXT_MD,
                        align="center")
            design.text(surface, hint, ((left + width) // 2, top + 88),
                        design.ink(look.menu_bg, 0.6), design.TEXT_SM,
                        align="center")
            self._draw_rail(surface, right, top, band, band, 0)
            return

        total = len(self._rows) * ROW_H
        # Rows are laid at their own pixel positions and the band's edge cuts
        # the last one. That is the whole fix: a clipped row exists if and
        # only if there is more list, so the screen cannot look finished
        # while it is not. row_band() guarantees the slice is worth seeing.
        clip = surface.get_clip()
        surface.set_clip(pygame.Rect(0, top, width, band))
        first = max(0, self._scroll_px // ROW_H)
        last = min(len(self._rows), (self._scroll_px + band) // ROW_H + 1)
        for index in range(first, last):
            y = top + index * ROW_H - self._scroll_px
            self._draw_row(surface, self._rows[index], index, left, y,
                           right - left - RAIL_W - design.SPACE_2)
        surface.set_clip(clip)

        self._draw_rail(surface, right, top, band, total, self._scroll_px)

    @staticmethod
    def row_band(available: int, count: int) -> int:
        """How tall the list band is, given the room and how many pieces.

        When everything fits, the band is simply the room. When it does not,
        the band is trimmed to whole rows plus ROW_PEEK, so the row that
        falls off the bottom always shows a slice of itself that is large
        enough to read as a row. Flooring to whole rows, which is what this
        replaces, produced a clean bottom edge and therefore a screen that
        looked complete while three pieces sat below it.
        """
        if available <= 0:
            return 0
        if count * ROW_H <= available:
            return available
        band = (available // ROW_H) * ROW_H + ROW_PEEK
        while band > available and band > ROW_H:
            band -= ROW_H
        return min(band, available)

    def _draw_rail(self, surface: pygame.Surface, right: int, top: int,
                   band: int, total: int, scroll_px: int) -> None:
        """How much list there is, and where in it you are.

        Always drawn, full height when nothing is hidden. A rail that only
        appears on overflow makes its absence ambiguous -- nothing below, or
        nobody painted it?
        """
        if band <= 0:
            return
        look = get_look()
        track = pygame.Rect(right - RAIL_W, top, 6, band)
        pygame.draw.rect(surface, design.sunken(look.menu_bg), track,
                         border_radius=3)
        span = max(1, total)
        height = max(28, int(band * min(1.0, band / span)))
        offset = 0 if total <= band else int(
            (band - height) * scroll_px / max(1, total - band))
        thumb = pygame.Rect(track.x, top + offset, 6, height)
        pygame.draw.rect(surface, design.ink(look.menu_bg, 0.55), thumb,
                         border_radius=3)

    # The one geometry a look may not move: where the six things in a row
    # sit. Measured against the real faces, so a row is provably taller than
    # what is drawn in it -- see TestRowFits, which is the guard nobody had.
    # Setting ROW_H to 80, 56 or 40 all left the suite green before it.
    ROW_GAP = 4                  # between cards, so the card is ROW_H - 4
    ORDINAL_W = 34
    TEXT_X = 114
    TITLE_Y = 4
    META_Y = 28
    SCORE_W = 190
    SCORE_GAP = 58               # of SCORE_W, the part the percentage keeps
    LEVEL_W = 118

    HERO_H = 96

    def _continue_song(self):
        """The piece the player was last working on and has not finished.

        Not the suggestion: that answers "what should I do next", which is a
        different question and already has its own key. This answers "what
        was I in the middle of", and being in the middle of something is the
        single most likely reason the player opened the app at all.
        """
        if not self._progress:
            return None
        best = None
        for song in self._rows:
            record = self._record_for(song)
            if record is None or not record.last_played:
                continue
            if float(record.best_accuracy) >= PASSED_PERCENT:
                continue
            if best is None or record.last_played > best[1].last_played:
                best = (song, record)
        return best

    def _draw_hero(self, surface: pygame.Surface, x: int, y: int,
                   width: int, song, record) -> int:
        """Pick up where the player left off, before the list the player would scan for it."""
        look = get_look()
        rect = pygame.Rect(x, y, width, self.HERO_H)
        design.card(surface, rect, design.surface(look.menu_bg, 1),
                    radius=design.RADIUS_LG,
                    ring=design.stroke(look.menu_bg, 0), ring_width=1)
        design.text(surface, t("menu.continue").upper(),
                    (rect.x + design.SPACE_5, rect.y + design.SPACE_4),
                    look.hud_accent, design.TEXT_XS, bold=True)
        design.text(surface, song.display_title,
                    (rect.x + design.SPACE_5, rect.y + 34),
                    design.ink(look.menu_bg, 1.0), design.TEXT_XL, bold=True)
        design.text(surface,
                    f"{song.summary()}  ·  "
                    + t("menu.best", percent=f"{float(record.best_accuracy):.0f}"),
                    (rect.x + design.SPACE_5, rect.bottom - 26),
                    design.ink(look.menu_bg, 0.6), design.TEXT_XS)

        play = pygame.Rect(rect.right - 150, rect.centery - 21, 130, 42)
        pygame.draw.rect(surface, look.hud_accent, play,
                         border_radius=design.RADIUS_MD)
        tri = play.x + 22
        pygame.draw.polygon(surface, design.on_accent(look.hud_accent), [
            (tri, play.centery - 7), (tri, play.centery + 7),
            (tri + 12, play.centery)])
        design.text(surface, t("menu.play"), (play.centerx + 12, play.centery - 9),
                    design.on_accent(look.hud_accent), design.TEXT_SM,
                    bold=True, align="center")
        self._button_rects.setdefault("hero", []).append(play)
        return rect.bottom + design.SPACE_3

    def _draw_row(self, surface: pygame.Surface, song: library.SongInfo,
                  index: int, x: int, y: int, width: int) -> None:
        """One piece, flat.

        It used to be a filled card with a five-bar difficulty glyph and its
        own name stacked underneath. Two of the three said the same thing,
        the card put a box round every line so none of them stood out, and
        between the title and the score sat a 563 px corridor with nothing
        in it. This is the mockup's row: an ordinal, the piece, how hard it
        is, how far the player got, separated by a hairline rather than boxed.
        """
        look = get_look()
        rect = pygame.Rect(x, y, width, ROW_H - self.ROW_GAP)
        selected = index == self._selected
        if selected:
            pygame.draw.rect(surface, design.surface(look.menu_bg, 1), rect,
                             border_radius=design.RADIUS_MD)
            pygame.draw.rect(surface, look.hud_accent,
                             (rect.x, rect.y + 6, 3, rect.height - 12))
        else:
            pygame.draw.line(surface, design.stroke(look.menu_bg, 0),
                             (rect.x + design.SPACE_3, rect.bottom),
                             (rect.right - design.SPACE_3, rect.bottom), 1)
        visible = rect.clip(surface.get_clip())
        if visible.width > 0 and visible.height > 0:
            self._row_rects.append((visible, index))

        # Where the player is in the library, written on the piece rather than on a
        # widget at the edge: it survives a photograph, a colourblind eye and
        # all three looks, and at two hundred pieces it is the only cue left.
        design.text(surface, f"{index + 1:02d}",
                    (rect.x + design.SPACE_3 + self.ORDINAL_W, rect.centery - 9),
                    design.ink(look.menu_bg, 0.45), design.TEXT_SM,
                    mono=True, align="right")

        text_x = rect.x + self.TEXT_X
        design.text(surface, song.display_title, (text_x, rect.y + self.TITLE_Y),
                    design.ink(look.menu_bg, 1.0), design.TEXT_MD, bold=True)
        # The mockup puts the history on the same line as the tempo, which is
        # where it belongs: it is about this piece, not about the score.
        summary = t("menu.no_track") if not song.playable else song.summary()
        record = self._record_for(song) if self._progress else None
        if record is not None and song.playable:
            # One key per shape of the line. The date cannot be glued onto a
            # translated phrase: the count carries a plural, and the two
            # halves have to be worded together.
            history = (t("menu.attempts_date", count=record.attempts,
                         date=record.last_played[:10]) if record.last_played
                       else t("menu.attempts", count=record.attempts))
            summary = f"{summary}  ·  {history}"
        design.text(surface, summary, (text_x, rect.y + self.META_Y),
                    design.ink(look.menu_bg, 0.8 if selected else 0.6),
                    design.TEXT_XS)

        right = rect.right - design.SPACE_4
        accuracy = self._accuracy(song)

        # The difficulty gets a dot and its own word, in the column the
        # mockup puts it in, instead of a bar chart that needed a legend.
        if width >= 620:
            level_x = right - self.SCORE_W - self.LEVEL_W
            pygame.draw.circle(surface, design.difficulty_colour(song.difficulty),
                               (level_x, rect.centery), 4)
            design.text(surface, song.level_name, (level_x + 12, rect.centery - 9),
                        design.ink(look.menu_bg, 0.7), design.TEXT_XS)

        if accuracy is None:
            design.text(surface, t("menu.never_played"),
                        (right, rect.centery - 9),
                        design.ink(look.menu_bg, 0.5), design.TEXT_XS,
                        align="right")
            return

        # From the look, not from design.HIT: that green is tuned for a dark
        # page and measures 1.38:1 on Hi-fi's paper, which is a number the player
        # cannot read at all.
        colour = (look.feedback_hit if accuracy >= PASSED_PERCENT
                  else look.feedback_close if accuracy >= 50
                  else design.ink(look.menu_bg, 0.6))
        design.text(surface, t("menu.percent", percent=f"{accuracy:.0f}"),
                    (right, rect.centery - 11), colour, design.TEXT_MD,
                    bold=True, align="right")
        if width >= 520:
            bar = pygame.Rect(right - self.SCORE_W, rect.centery - 3,
                              self.SCORE_W - self.SCORE_GAP, 6)
            pygame.draw.rect(surface, design.sunken(look.menu_bg), bar,
                             border_radius=3)
            filled = int(bar.width * min(accuracy, 100) / 100)
            if filled > 0:
                pygame.draw.rect(surface, colour,
                                 (bar.x, bar.y, filled, bar.height),
                                 border_radius=3)

    def _draw_footer(self, surface: pygame.Surface, width: int,
                     height: int) -> None:
        look = get_look()
        top = height - FOOTER_H
        pygame.draw.line(surface, design.stroke(look.menu_bg, 0),
                         (0, top), (width, top), 1)
        pygame.draw.rect(surface, design.chrome(look.menu_bg),
                         (0, top, width, FOOTER_H))
        # Count what is on the screen, and say why it is fewer. At 820 px the
        # sidebar is gone, so a filter could hide six of ten pieces while
        # this line insisted there were ten and nothing named the filter.
        if self._filter == ("all", None):
            played = sum(1 for s in self._songs if self._accuracy(s) is not None)
            summary = t("menu.summary", count=len(self._songs), played=played)
        else:
            summary = t("menu.filter_on", name=self._filter_name())
        design.text(surface, summary, (design.SPACE_5, top + 12),
                    design.ink(look.menu_bg,
                               0.6 if self._filter == ("all", None) else 0.9),
                    design.TEXT_XS)
        # Name the rung before it is pressed. Esc used to quit the app while
        # a filter was narrowing ten pieces down to four and the line above
        # still counted all ten.
        rung = self._escape_rung()
        hint = {"search": "menu.esc_search", "filter": "menu.esc_filter",
                "setup": "menu.close"}.get(rung, "menu.quit_hint")
        design.text(surface, t(hint), (design.SPACE_5, top + 30),
                    design.ink(look.menu_bg, 0.5 if rung == "quit" else 0.8),
                    design.TEXT_XS)

        x = width - design.SPACE_5
        play = pygame.Rect(x - 132, top + 7, 132, 42)
        pygame.draw.rect(surface, look.hud_accent, play,
                         border_radius=design.RADIUS_MD)
        tri = play.x + 20
        pygame.draw.polygon(surface, design.on_accent(look.hud_accent), [
            (tri, play.centery - 7), (tri, play.centery + 7),
            (tri + 12, play.centery)])
        design.text(surface, t("menu.play"), (play.centerx + 12, play.y + 4),
                    design.on_accent(look.hud_accent), design.TEXT_SM, bold=True, align="center")
        design.text(surface, t("key.enter"), (play.centerx + 12, play.y + 23),
                    (44, 58, 78), design.TEXT_XS, align="center")
        self._button_rects.setdefault("play", []).append(play)
        x = play.x - design.SPACE_2

        suggestion = self.suggestion()
        if suggestion is not None and width >= 620:
            w = self._action_width(t("menu.suggest"), 150)
            rect = pygame.Rect(x - w, top + 7, w, 42)
            self._action(surface, rect, t("menu.suggest"), t("key.w"),
                         "suggest")
            x = rect.x - design.SPACE_2

        if width >= 800:
            w = self._action_width(t("menu.folder"), 118)
            rect = pygame.Rect(x - w, top + 7, w, 42)
            self._action(surface, rect, t("menu.folder"), t("key.o"),
                         "folder")
            x = rect.x - design.SPACE_2

        if width >= 1020:
            w = self._action_width(t(SORT_LABEL_KEYS[self._sort_mode]), 176)
            rect = pygame.Rect(x - w, top + 7, w, 42)
            self._action(surface, rect,
                         t(SORT_LABEL_KEYS[self._sort_mode]), t("key.n"),
                         "sort")
            x = rect.x - design.SPACE_2


