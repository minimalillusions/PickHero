"""The seal itself, checked.

The guard lives in tests/conftest.py, because an autouse fixture only reaches
the module it is written in -- which is how the first version of this passed
while doing nothing at all. These two tests prove the seal is real and that
the feature it guards still exists.
"""

import os
import subprocess

import pytest

from tests.conftest import ESCAPES


@pytest.fixture
def _tripped():
    """These two tests trip the seal on purpose, so they clear the record
    they leave behind -- otherwise the fixture that checks it fails them in
    teardown for doing exactly what they were written to do."""
    before = len(ESCAPES)
    yield
    del ESCAPES[before:]


def test_the_seal_is_in_place(_tripped):
    """If this ever fails the suite can start programs again."""
    with pytest.raises(AssertionError, match="may reach the operating system"):
        subprocess.Popen(["cmd", "/c", "echo", "hello"])


@pytest.mark.skipif(not hasattr(os, "startfile"), reason="Windows only")
def test_the_door_the_sweep_walked_through_is_shut(_tripped):
    with pytest.raises(AssertionError):
        os.startfile(".")


def test_the_app_still_knows_how_to_open_the_folder():
    """The guard must not be read as "this feature is gone": it is one of the
    key hints on every library screen."""
    import inspect

    from pickhero.ui.menu import MenuScreen

    source = inspect.getsource(MenuScreen._open_folder)
    assert "startfile" in source or "xdg-open" in source
    assert "except" in source, (
        "opening the folder must not be able to take the app down with it")
