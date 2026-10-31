"""Guided exercises and unscored, explicitly started MIDI examples."""
from __future__ import annotations

import time
from pathlib import Path

import pygame

from pickhero.audio.midi_playback import MidiPlayer
from pickhero.curriculum import (
    LessonPractice,
    build_demo,
    build_learning_plan,
    exercise_for_lesson,
    practice_steps,
)
from pickhero.i18n import t
from pickhero.ui import design, shapes
from pickhero.ui.colors import get_look
from pickhero.ui.practice_panels import Action, PracticePanel, wrap
from pickhero.ui.test_recording import RecordingRequest


class LessonScreen:
    """Return a lesson Path, a configured LessonPractice, 'back', or None.

    Only listening opens MIDI. No capture, matcher or progress writer exists
    in this screen, so a speaker playing the example cannot score itself.
    """

    def __init__(self, config, progress, songs_dir, *, music_first=False, library_navigation=False):
        self._library_navigation = library_navigation
        self._music_options = (config, progress, songs_dir)
        self._music = None
        self._instrument = config.get_instrument()
        self.plan = build_learning_plan(self._instrument, songs_dir, progress)
        self._mode = "catalog"
        self._selected = self._initial_selection(progress)
        self._step_index = 0
        self._detail_origin = "catalog"
        self._catalog_scroll = 0
        self._reveal_selected = True
        self._row_height = 58
        self._catalog_body = pygame.Rect(0, 0, 0, 0)
        self._catalog_layout = None
        self._last_catalog_click = (None, 0.0)
        self._tempos = tuple(exercise_for_lesson(state.lesson, self._instrument).bpm
                            for state in self.plan.lessons)
        self._player = None
        self._demo = None
        self._demo_started = 0.0
        self._demo_status = "idle"
        self._panel = self._make_panel()
        if music_first:
            self._show_music()

    def _show_music(self):
        from pickhero.ui.music_menu import MusicScreen
        self.close()
        if self._music is None:
            self._music = MusicScreen(*self._music_options)
        self._mode = "music"

    @property
    def generated_music(self):
        return self._music is not None and self._music.generated

    def _initial_selection(self, progress):
        """Remember the visited exercise without turning it into an achievement."""
        session = progress.latest_session(self._instrument.key)
        if session:
            try:
                saved_path = Path(session["song_path"]).resolve()
                for index, state in enumerate(self.plan.lessons):
                    if state.path.resolve() == saved_path:
                        return index
            except (OSError, ValueError):
                pass
        practiced = [(index, state) for index, state in enumerate(self.plan.lessons)
                     if state.attempts]
        if practiced:
            return max(practiced, key=lambda item: item[1].last_played)[0]
        return self.plan.next_lesson.lesson.order - 1

    def _make_panel(self):
        panel = self._make_content_panel()
        panel.header_action = Action("record", t("record.header"), pygame.K_u, "U")
        return panel

    def _make_content_panel(self):
        if self._mode == "catalog":
            return PracticePanel("lessons", t("lesson.title"), "", [], [
                Action("play", t("lesson.catalog_play"), pygame.K_p),
                Action("open", t("lesson.catalog_details"), pygame.K_o),
                Action("steps", t("lesson.catalog_steps"), pygame.K_s),
                Action("back", t("lesson.catalog_library"), pygame.K_b),
            ])
        state = self.plan.lessons[self._selected]
        lesson = state.lesson
        exercise = exercise_for_lesson(lesson, self._instrument)
        steps = practice_steps(lesson, self._instrument)
        if self._mode == "steps":
            step = steps[self._step_index]
            mode = ("lesson.step_wait" if step.wait_mode else
                    "lesson.step_pitch_only" if not lesson.timing_required else "lesson.step_timed")
            subtitle = lesson.title if self._demo_status == "idle" else self._step_status_text()
            panel = PracticePanel("lesson", t("lesson.steps_title", number=f"{lesson.order:02d}",
                                              current=self._step_index + 1,
                                              total=len(steps)), subtitle, [
                (step.title, [step.instruction]),
                (t("lesson.step_setup"), [t(
                    "lesson.step_settings", bpm=f"{exercise.bpm * step.tempo_factor:g}",
                    percent=round(step.tempo_factor * 100), first=step.first_bar + 1,
                    last=step.stop_bar),
                    t(mode),
                    t("lesson.step_loop" if step.loop else "lesson.step_full")]),
                (t("lesson.example_heading"), [t("lesson.step_demo")]),
            ], [
                Action("step_previous", t("lesson.step_previous"), pygame.K_LEFT),
                Action("step_next", t("lesson.step_next"), pygame.K_RIGHT),
                Action("play", t("lesson.play"), pygame.K_p),
                Action("demo", t("lesson.step_stop") if self._player else t("lesson.listen"), pygame.K_d),
                Action("back", t("lesson.step_back")),
            ])
            if not step.loop:
                # Show the actual target before setup/listening advice, so a
                # full attempt never hides its completion criteria below it.
                panel.sections[0] = (step.title, [lesson.criterion,
                                                  t("lesson.criterion_quality")])
            panel.focus = 2
            return panel
        sections = [
            (t("lesson.goal_heading"), [lesson.goal]),
            (t("lesson.instruction_heading"), [lesson.instruction,
                                              t("lesson.slow_instruction")]),
            (t("lesson.example_heading"), [t("lesson.example_instruction"), self._status_text()]),
            (t("lesson.criterion_heading"), [lesson.criterion,
                                            t("lesson.criterion_quality")]),
        ]
        if state.attempts:
            sections.append((t("lesson.history_heading"), [t(
                "lesson.history", attempts=state.attempts, accuracy=round(state.best_accuracy))]))
        if steps:
            sections.insert(0, (t("lesson.steps_heading"), [t("lesson.steps_intro")]))
        actions = [
            Action("play", t("lesson.play"), pygame.K_p),
            Action("demo", t("lesson.stop") if self._player else t("lesson.listen"), pygame.K_d),
        ]
        if steps:
            actions.append(Action("steps", t("lesson.steps"), pygame.K_s))
        actions.append(Action("back", t("lesson.back")))
        panel = PracticePanel("lesson", t("lesson.detail_title", number=lesson.order),
                              lesson.title, sections, actions)
        panel.metrics = [
            (t("lesson.tempo"), str(exercise.bpm), t("lesson.bpm")),
            (t("lesson.status"), t("lesson.done") if state.complete else t("lesson.open_status"), ""),
        ]
        return panel

    def _status_text(self):
        return t({"idle": "lesson.demo_idle", "playing": "lesson.demo_playing",
                  "done": "lesson.demo_done", "unavailable": "lesson.demo_unavailable"}[self._demo_status])

    def _step_status_text(self):
        return t(f"lesson.step_demo_{self._demo_status}")

    def _refresh_panel(self, *, preserve=False):
        scroll, focus = self._panel.scroll, self._panel.focus
        self._panel = self._make_panel()
        if preserve:
            self._panel.scroll, self._panel.focus = scroll, focus

    def _show_lesson(self, index):
        self.close()
        self._detail_origin = self._mode
        self._selected = max(0, min(len(self.plan.lessons) - 1, index))
        self._step_index = 0
        self._mode = "detail"
        self._demo_status = "idle"
        self._refresh_panel()

    def show_practice(self, request: LessonPractice) -> bool:
        """Return to the explicitly selected step with freshly loaded progress."""
        if not isinstance(request, LessonPractice):
            return False
        from pickhero.music import music_for_path, music_practice
        if music_for_path(request.path) is not None:
            if music_practice(request.path, self._instrument) != request:
                return False
            self._show_music()
            return self._music.show_path(request.path)
        try:
            selected = next(index for index, state in enumerate(self.plan.lessons)
                            if state.path.resolve() == request.path.resolve())
            steps = practice_steps(self.plan.lessons[selected].lesson, self._instrument)
            step_index = steps.index(request.step)
        except (AttributeError, OSError, StopIteration, ValueError):
            return False
        self.close()
        self._selected = selected
        self._step_index = step_index
        self._detail_origin = "catalog"
        self._mode = "steps"
        self._demo_status = "idle"
        self._refresh_panel()
        return True

    def _scroll_small_step(self, event):
        """Keep every line reachable when five controls leave a shallow body."""
        panel = self._panel
        if self._mode != "steps" or not 0 < panel.body.height < 84:
            return False
        amount = max(1, panel.body.height // 2)
        delta = None
        if event.type == pygame.MOUSEWHEEL:
            delta = -event.y * amount
        elif event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_UP, pygame.K_PAGEUP):
                delta = -amount
            elif event.key in (pygame.K_DOWN, pygame.K_PAGEDOWN):
                delta = amount
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for name, direction in (("scroll_up", -1), ("scroll_down", 1)):
                if name in panel.hit and panel.hit[name].collidepoint(event.pos):
                    delta = amount * direction
                    break
        if delta is None:
            return False
        panel.scroll = max(0, min(panel.max_scroll, panel.scroll + delta))
        return True

    def handle_event(self, event):
        if self._mode == "music":
            action = self._music.handle_event(event)
            if action == "technique":
                self._mode = "catalog"
                self._reveal_selected = True
                self._refresh_panel()
                return None
            return action
        if self._scroll_small_step(event):
            return None
        if self._mode == "catalog":
            if event.type == pygame.KEYDOWN and event.key == pygame.K_m:
                if self._library_navigation:
                    self.close()
                    return "music_library"
                self._show_music()
                return None
            if event.type == pygame.MOUSEWHEEL:
                amount = min(self._row_height, max(1, self._catalog_body.height // 2))
                self._catalog_scroll -= event.y * amount
                self._clamp_catalog()
                return None
            if event.type == pygame.KEYDOWN:
                navigation = {pygame.K_UP: -1, pygame.K_DOWN: 1,
                              pygame.K_PAGEUP: -5, pygame.K_PAGEDOWN: 5}
                if event.key in navigation or event.key in (pygame.K_HOME, pygame.K_END):
                    if event.key == pygame.K_HOME:
                        self._selected = 0
                    elif event.key == pygame.K_END:
                        self._selected = len(self.plan.lessons) - 1
                    else:
                        self._selected = max(0, min(len(self.plan.lessons) - 1,
                                                   self._selected + navigation[event.key]))
                    self._reveal_selected = True
                    self._panel.focus = 0
                    return None
        action = self._panel.handle_event(event)
        if not action:
            return None
        if not action.startswith("lesson:"):
            self._last_catalog_click = (None, 0.0)
        if action in ("close", "back"):
            self.close()
            if self._mode == "catalog":
                return "back"
            self._mode = ("detail" if self._mode == "steps" else
                          self._detail_origin if self._mode == "detail" else "catalog")
            self._demo_status = "idle"
            self._reveal_selected = True
            self._refresh_panel()
        elif action == "music":
            if self._library_navigation:
                self.close()
                return "music_library"
            self._show_music()
        elif action == "catalog":
            self._mode = "catalog"
            self._reveal_selected = True
            self._refresh_panel()
        elif action == "open":
            self._show_lesson(self._selected)
        elif action.startswith("lesson:"):
            self._selected = int(action.split(":", 1)[1])
            self._panel.focus = 0
            previous_index, previous_time = self._last_catalog_click
            now = time.monotonic()
            self._last_catalog_click = (self._selected, now)
            if previous_index == self._selected and 0 <= now - previous_time < .4:
                self._last_catalog_click = (None, 0.0)
                return self.plan.lessons[self._selected].path
        elif action in ("catalog_up", "catalog_down"):
            direction = -1 if action == "catalog_up" else 1
            self._catalog_scroll += direction * self._row_height
            self._clamp_catalog()
        elif action == "steps":
            self.close()
            if self._mode == "catalog":
                self._detail_origin = "catalog"
                self._step_index = 0
            self._mode = "steps"
            self._demo_status = "idle"
            self._refresh_panel()
        elif action in ("step_previous", "step_next"):
            steps = practice_steps(self.plan.lessons[self._selected].lesson, self._instrument)
            index = max(0, min(len(steps) - 1, self._step_index
                              + (-1 if action == "step_previous" else 1)))
            if index != self._step_index:
                self.close()
                self._step_index = index
                self._demo_status = "idle"
                self._refresh_panel()
        elif action in ("play", "record"):
            self.close()
            state = self.plan.lessons[self._selected]
            if self._mode == "steps":
                step = practice_steps(state.lesson, self._instrument)[self._step_index]
                practice = LessonPractice(state.path, step)
                return RecordingRequest(state.path, practice) if action == "record" else practice
            return RecordingRequest(state.path) if action == "record" else state.path
        elif action == "demo":
            if self._player:
                self.close()
                self._demo_status = "idle"
            else:
                self._start_demo()
            self._refresh_panel(preserve=True)
            if self._mode == "steps":
                # In small windows the subtitle belongs to the scrolling
                # body. Bring the listening result into view immediately.
                self._panel.scroll = 0
        return None

    def _start_demo(self):
        exercise = exercise_for_lesson(self.plan.lessons[self._selected].lesson, self._instrument)
        if self._mode == "steps":
            step = practice_steps(self.plan.lessons[self._selected].lesson,
                                  self._instrument)[self._step_index]
            demo = build_demo(exercise, self._instrument, step.stop_bar - step.first_bar,
                              first_bar=step.first_bar, tempo_factor=step.tempo_factor)
        else:
            demo = build_demo(exercise, self._instrument)
        player = MidiPlayer(demo.track)
        if not player.open():
            player.close()
            self._demo_status = "unavailable"
            return
        self._player, self._demo = player, demo
        self._demo_started = time.monotonic()
        self._demo_status = "playing"
        player.update(0.0)

    def update(self, dt: float = 0.0):
        if self._mode == "music":
            self._music.update(dt)
            return
        if self._player is None:
            return
        elapsed = (time.monotonic() - self._demo_started) * 1000.0
        self._player.update(elapsed)
        if elapsed >= self._demo.duration_ms:
            self.close()
            self._demo_status = "done"
            self._refresh_panel(preserve=True)
            if self._mode == "steps":
                self._panel.scroll = 0

    def close(self):
        if self._music is not None:
            self._music.close()
        if self._player is not None:
            self._player.close()
        self._player = None
        self._demo = None

    def render(self, surface):
        if self._mode == "music":
            self._music.render(surface)
            return
        surface.fill(get_look().bg)
        if self._mode == "catalog":
            # Use the existing subtitle line on normal windows. Compact
            # panels put subtitles into their body, which belongs to the list.
            self._panel.subtitle = self._instrument.name if surface.get_height() >= 360 else ""
        self._panel.render(surface)
        if self._mode == "catalog":
            self._render_catalog(surface)

    def _clamp_catalog(self):
        body = getattr(self, "_catalog_body", self._panel.body)
        maximum = max(0, len(self.plan.lessons) * self._row_height - body.height)
        self._catalog_scroll = max(0, min(maximum, self._catalog_scroll))

    def _render_catalog(self, surface):
        look = get_look()
        bounds = self._panel.body
        summary_face = design.font(13 if bounds.width < 450 else 15)
        summary = t("lesson.catalog_summary", completed=self.plan.completed,
                    total=len(self.plan.lessons))
        summary_height = summary_face.get_linesize() + 9
        surface.blit(summary_face.render(summary, True, look.ink_2), bounds.topleft)
        music_label = summary_face.render(t("music.catalog_action"), True, look.hud_accent)
        if summary_face.size(summary)[0] + music_label.get_width() + 20 <= bounds.width:
            link = music_label.get_rect(topright=(bounds.right-6, bounds.top))
        else:
            link = music_label.get_rect(topleft=(bounds.left, bounds.top+summary_height))
            summary_height += summary_face.get_linesize()+8
        surface.blit(music_label, link)
        self._panel.hit["music"] = link.inflate(6, 4)
        body = pygame.Rect(bounds.left, bounds.top + summary_height, bounds.width,
                           max(8, bounds.height - summary_height))
        self._catalog_body = body
        wide = body.width >= 720
        face = design.font(18 if wide else 15 if body.width < 450 else 17, bold=True)
        small = design.font(14 if body.width < 450 else 15)
        number_face = design.font(16 if body.width < 450 else 18, mono=True)
        title_left = body.left + (37 if body.width < 450 else 45)
        state_width = 185 if wide else 0
        tempo_width = 90 if wide else 0
        title_width = body.width - (title_left - body.left) - 30 - state_width - tempo_width
        wrapped = [wrap(state.lesson.title, face, max(40, title_width))
                   for state in self.plan.lessons]
        # On normal desktop windows all ten lessons fit. Narrow screens keep
        # readable type and complete titles, with the same scroll contract.
        lines_height = max(map(len, wrapped)) * face.get_linesize()
        if wide:
            self._row_height = max(lines_height + 12, min(48, body.height // 10))
        else:
            self._row_height = lines_height + small.get_linesize() + 15
        layout = (body.size, self._row_height)
        if layout != self._catalog_layout:
            self._reveal_selected = True
            self._catalog_layout = layout
        if self._reveal_selected:
            top = self._selected * self._row_height
            if top < self._catalog_scroll or self._row_height >= body.height:
                self._catalog_scroll = top
            elif top + self._row_height > self._catalog_scroll + body.height:
                self._catalog_scroll = top + self._row_height - body.height
            self._reveal_selected = False
        self._clamp_catalog()
        old_clip = surface.get_clip()
        surface.set_clip(body.clip(old_clip))
        for index, (state, lines) in enumerate(zip(self.plan.lessons, wrapped)):
            rect = pygame.Rect(body.left, body.top + index * self._row_height - self._catalog_scroll,
                               body.width - 20, self._row_height - 4)
            hit = rect.clip(body)
            if hit.height < 1:
                continue
            self._panel.hit[f"lesson:{index}"] = hit
            if index == self._selected:
                shapes.rounded_rect(surface, look.select_fill, rect,
                                    border_radius=7 if look.key == "studio" else 0)
                shapes.rounded_rect(surface, look.hud_accent, rect, 1,
                                    border_radius=7 if look.key == "studio" else 0)
            else:
                pygame.draw.line(surface, look.hairline, rect.bottomleft, rect.bottomright)
            y = rect.top + (max(4, (rect.height - lines_height) // 2) if wide else 6)
            number = number_face.render(f"{state.lesson.order:02d}", True,
                                        look.hud_accent if index == self._selected else look.ink_2)
            surface.blit(number, (rect.left + 7, y))
            for line in lines:
                surface.blit(face.render(line, True, look.ink_1), (title_left, y))
                y += face.get_linesize()
            label = (t("lesson.catalog_done") if state.complete else
                     t("lesson.catalog_practiced", accuracy=round(state.best_accuracy))
                     if state.attempts else t("lesson.catalog_new"))
            tempo = f"{self._tempos[index]} {t('lesson.bpm')}"
            if wide:
                baseline = rect.centery - small.get_linesize() // 2
                surface.blit(small.render(tempo, True, look.ink_2),
                             (rect.right - state_width - tempo_width, baseline))
                surface.blit(small.render(label, True, look.hud_accent if state.complete else look.ink_2),
                             (rect.right - state_width, baseline))
            else:
                surface.blit(small.render(f"{tempo}  ·  {label}", True, look.ink_2), (title_left, y + 1))
        surface.set_clip(old_clip)
        total = len(self.plan.lessons) * self._row_height
        if total > body.height:
            rail = pygame.Rect(body.right - 8, body.top + 18, 4, max(4, body.height - 36))
            pygame.draw.rect(surface, look.hairline_loud, rail)
            thumb_height = max(8, round(rail.height * body.height / total))
            thumb = pygame.Rect(rail.x, rail.y + round((rail.height - thumb_height)
                                * self._catalog_scroll / (total - body.height)), 4, thumb_height)
            pygame.draw.rect(surface, look.hud_accent, thumb)
            for name, cy, direction in (("catalog_up", body.top + 7, -1),
                                         ("catalog_down", body.bottom - 7, 1)):
                hit = pygame.Rect(body.right - 18, cy - 7, 18, 14)
                self._panel.hit[name] = hit
                shapes.polygon(surface, look.ink_2, [
                    (hit.centerx - 4, cy - direction * 3),
                    (hit.centerx + 4, cy - direction * 3),
                    (hit.centerx, cy + direction * 3),
                ])
