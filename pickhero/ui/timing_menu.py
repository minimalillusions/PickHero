"""Local timing evidence and an explicitly started audio return-path test."""
from __future__ import annotations

import json
from uuid import uuid4

import pygame

from pickhero import config as config_module
from pickhero.audio.latency_probe import LatencyProbe
from pickhero.i18n import t
from pickhero.ui.colors import get_look
from pickhero.ui.practice_panels import Action, PracticePanel


class TimingScreen:
    def __init__(self, config, *, instrument=None, report=None):
        self._config = config
        self._report = report or {}
        effective_device = ((self._report.get("audio") or {}).get("context") or {}).get("device_index")
        if effective_device is None:
            effective_device = config.audio.device_index
        self._probe = LatencyProbe(None if effective_device is None else int(effective_device),
                                   sample_rate=config.audio.sample_rate)
        self._status = "idle"
        self._result = None
        self._leaving = False
        self._notice = ""
        self._panel = self._make_panel()

    def close(self):
        self._probe.close()

    def _make_panel(self):
        sections = [(t("timing.return_path"), [t("timing.instructions"), t("timing.limits")])]
        if self._status != "idle":
            line = t("timing.status." + self._status)
            if self._result:
                if self._result.get("status") == "complete":
                    line = t("timing.measured", value=f"{self._result['roundtrip_ms']:.1f}",
                             spread=f"{self._result['spread_ms']:.1f}")
                elif self._result.get("status") == "failed" and self._result.get("reason"):
                    line += " " + t("timing.reason." + self._result["reason"])
            sections.append((t("timing.test_result"), [line]))
            if self._result and self._result.get("input_device") is not None:
                sections.append(("", [t("timing.route", input=self._result["input_device"],
                    output=self._result.get("output_device"), rate=self._result.get("sample_rate"))]))
        audio = self._report.get("audio") or {}
        metrics = audio.get("metrics", {})
        context = audio.get("context", {})
        lines = []
        if context.get("clock_source") in ("adc", "fallback"):
            lines.append(t("timing.clock." + context["clock_source"]))
            lines.append(t("timing.driver", value=f"{context.get('input_latency_ms', 0):.1f}"))
            lines.append(t("timing.input", rate=int(context.get("sample_rate", 0)),
                           hop=int(context.get("hop_size", 0)),
                           drops=int(context.get("dropped_blocks", 0))))
        for name in ("callback_processing_ms", "callback_budget_ms", "note_report_lag_ms", "queue_wait_ms"):
            self._metric_line(lines, name, metrics.get(name))
        sections.append((t("timing.capture"), (lines or [t("timing.no_capture")])
                         + [t("timing.report_explanation")]))
        frames = (self._report.get("frames") or {}).get("metrics", {})
        lines = []
        for name in ("frame_interval_ms", "frame_processing_ms"):
            self._metric_line(lines, name, frames.get(name))
        sections.append((t("timing.frames"), (lines or [t("timing.no_frames")])
                         + [t("timing.frame_explanation")]))
        sections.append((t("timing.privacy"), [t("timing.privacy_text")]))
        if self._notice:
            sections.append(("", [self._notice]))
        running = self._probe.is_running or not self._probe.safe_to_resume
        actions = [Action("cancel" if running else "start",
                          t("timing.cancel" if running else "timing.start"), pygame.K_m),
                   Action("back", t("secondary.back")),
                   Action("export", t("timing.export"), pygame.K_e)]
        return PracticePanel("timing", t("timing.title"), t("timing.subtitle"), sections, actions)

    @staticmethod
    def _metric_line(lines, name, metric):
        if metric and metric.get("window_count", 0):
            lines.append(t("timing.metric", name=t("timing.metric." + name),
                           median=f"{metric['median']:.1f}", p95=f"{metric['p95']:.1f}",
                           count=metric["window_count"]))

    def _refresh(self):
        old = self._panel
        self._panel = self._make_panel()
        self._panel.scroll = old.scroll
        self._panel.focus = min(old.focus, len(self._panel.actions) - 1)

    def _activate(self, action):
        if action is None:
            return None
        if action in ("back", "close"):
            if self._probe.is_running or not self._probe.safe_to_resume:
                self._leaving = True
                self._probe.stop()
                self._status = "cancelling"
                self._refresh()
                return None
            return "back"
        if action == "start" and not self._probe.is_running and self._probe.safe_to_resume:
            self._notice = ""
            self._result = None
            self._probe.start()
            self._status = "starting"
        elif action == "cancel":
            self._probe.stop()
            self._status = "cancelling"
        elif action == "export":
            self._export()
        self._refresh()
        return None

    def handle_event(self, event):
        if self._leaving:
            return None
        return self._activate(self._panel.handle_event(event))

    def update(self):
        state = self._probe.update()
        if self._leaving:
            if not self._probe.is_running and self._probe.safe_to_resume:
                return "back"
            if not self._probe.is_running:
                # A failed device close needs an explicit retry; keep the
                # diagnosis open instead of restarting a competing capture.
                self._leaving = False
                self._status = state["status"]
                self._result = self._probe.poll()
                self._refresh()
            return None
        status = state["status"]
        result = self._probe.poll()
        if status != self._status or result != self._result:
            self._status = status
            self._result = result
            self._refresh()
        return None

    def _export(self):
        # Explicit local export, unique name, no waveform, song path or network.
        try:
            folder = config_module.CONFIG_DIR / "diagnostics"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"timing-{uuid4().hex}.json"
            text = json.dumps({"schema_version": 1, "software": self._report,
                               "roundtrip": self._result}, ensure_ascii=False,
                              allow_nan=False, indent=2)
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text + "\n")
            self._notice = t("timing.saved", path=str(path))
        except (OSError, TypeError, ValueError):
            self._notice = t("timing.save_failed")

    def render(self, surface):
        surface.fill(get_look().bg)
        self._panel.render(surface)
