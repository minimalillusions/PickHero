"""Real app event loop with synthetic devices and temporary persistence."""
from unittest.mock import Mock

import pygame
import pytest

from pickhero.config import Config
from pickhero.ui.app import App


def test_quit_does_not_update_or_render_closed_screens(tmp_path, monkeypatch):
    from pickhero.ui import app as module
    monkeypatch.setattr(module, "validate_device_index", lambda index: True)
    monkeypatch.setattr(App, "_build_menu", lambda self: None)
    app = App(Config(songs_dir=str(tmp_path)))
    app._playing_screen = Mock()
    monkeypatch.setattr(pygame.event, "get", lambda: [pygame.event.Event(pygame.QUIT)])
    update, render = Mock(), Mock()
    monkeypatch.setattr(app, "_update", update)
    monkeypatch.setattr(app, "_render", render)
    app.run()
    update.assert_not_called()
    render.assert_not_called()
    app._playing_screen.close.assert_called()
    assert not pygame.get_init()


def test_unexpected_loop_error_still_closes_capture(tmp_path, monkeypatch):
    from pickhero.ui import app as module
    monkeypatch.setattr(module, "validate_device_index", lambda index: True)
    monkeypatch.setattr(App, "_build_menu", lambda self: None)
    monkeypatch.setattr(pygame.event, "get", lambda: [])
    app = App(Config(songs_dir=str(tmp_path)))
    capture_screen = Mock()
    app._check_menu = capture_screen
    monkeypatch.setattr(app, "_update", Mock(side_effect=RuntimeError("simulated update failure")))
    with pytest.raises(RuntimeError, match="simulated"):
        app.run()
    capture_screen.close.assert_called_once()
    assert not pygame.get_init()
