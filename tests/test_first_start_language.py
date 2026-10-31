"""The very first start speaks the computer's language; every later start keeps the choice.

Decided in main.py, the real entry point, and not in Config.load(): the
tests build Config objects everywhere, and a load() that asked the operating
system would make the whole suite answer in English on an English machine.
"""

import json
import sys

import pytest

from pickhero import config as config_module
from pickhero import i18n
from pickhero.config import Config
from pickhero.main import _load_config

VARIABLES = ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG")


@pytest.fixture
def system(monkeypatch):
    def set_to(code):
        monkeypatch.setattr(i18n, "system_language", lambda: code)
    return set_to


@pytest.fixture
def bare_environment(monkeypatch):
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    return monkeypatch


@pytest.mark.parametrize("system_code,expected", [
    ("de", "de"), ("en", "en"),
    ("fr", "en"),          # no French catalogue: English, which more people read
    (None, "en"),          # nothing found at all: the same
])
def test_the_first_start_takes_the_computers_language(system, system_code, expected):
    system(system_code)
    assert not config_module.CONFIG_FILE.exists(), "conftest gives every test an empty home"
    assert _load_config().language == expected
    saved = json.loads(config_module.CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["language"] == expected, "decided once, then it is the player's"


def test_a_later_start_keeps_the_choice_that_was_saved(system):
    Config(language="de").save()
    system("en")
    assert _load_config().language == "de"


def test_an_older_file_without_a_language_stays_german(system):
    """Written before there was a choice, by someone who was using German."""
    config_module.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config_module.CONFIG_FILE.write_text(json.dumps({"instrument": "bass_ukulele"}),
                                         encoding="utf-8")
    system("en")
    assert _load_config().language == "de"


@pytest.mark.parametrize("environment,expected", [
    ({"LANG": "en_US.UTF-8"}, "en"),
    ({"LANGUAGE": "fr:en"}, "fr"),
    ({"LC_ALL": "de_DE.UTF-8", "LANG": "en_US.UTF-8"}, "de"),
    ({"LANG": "C"}, None),
    ({"LANG": "POSIX"}, None),
    ({}, None),
])
def test_elsewhere_the_environment_names_the_language(bare_environment, environment, expected):
    bare_environment.setattr(sys, "platform", "linux")
    for name, value in environment.items():
        bare_environment.setenv(name, value)
    assert i18n.system_language() == expected


@pytest.mark.parametrize("display,expected", [("de-DE", "de"), ("en-GB", "en"), ("fr-FR", "fr")])
def test_windows_is_asked_for_its_display_language(bare_environment, display, expected):
    bare_environment.setattr(sys, "platform", "win32")
    bare_environment.setattr(i18n, "_windows_display_language", lambda: display)
    assert i18n.system_language() == expected


def test_a_failed_windows_lookup_falls_back_to_the_environment(bare_environment):
    bare_environment.setattr(sys, "platform", "win32")

    def broken():
        raise OSError("no kernel32 here")
    bare_environment.setattr(i18n, "_windows_display_language", broken)
    bare_environment.setenv("LANG", "en_US.UTF-8")
    assert i18n.system_language() == "en"


def test_the_real_lookup_on_this_machine_gives_a_short_code():
    """Not mocked: whatever this computer says has to come back as a code."""
    code = i18n.system_language()
    assert code is None or (code.isalpha() and 2 <= len(code) <= 3)
