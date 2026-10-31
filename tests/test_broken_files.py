"""A file on disk is not always what it claims to be.

Their settings came back one morning starting with 0xFF and the app died before
the window opened: UnicodeDecodeError, which is not JSONDecodeError and was
not caught. 0xFF 0xFE is a UTF-16 byte order mark, and several ordinary
Windows things write it -- PowerShell's `>` on 5.1, Notepad's "Unicode",
anything that round-trips the file through a tool that defaults to it.

So the cases here are not attacks. They are a file somebody else touched, a
write interrupted by a power cut, and a disk returning what it has left. None
of them may stop their practising, and the ones that still contain their settings
should be read rather than discarded.
"""

import json
import os

import pytest

from pickhero import jsonfile

GOOD = {"look": "hifi", "language": "de", "tempo_factor": 0.75}

# Each is (name, bytes). The first four hold their real settings in a form the
# old reader died on; the rest hold nothing worth saving.
HOSTILE = [
    ("utf-16 mit BOM", json.dumps(GOOD).encode("utf-16")),
    ("utf-16-le ohne BOM", json.dumps(GOOD).encode("utf-16-le")),
    ("utf-8 mit BOM", json.dumps(GOOD).encode("utf-8-sig")),
    ("cp1252 mit Umlaut", json.dumps({**GOOD, "songs_dir": "C:/Übungen"},
                                     ensure_ascii=False).encode("cp1252")),
    ("abgeschnitten", b'{"look": "hif'),
    ("leer", b""),
    ("nur Leerzeichen", b"   \n\t  "),
    ("null-Bytes", b"\x00" * 64),
    ("Zufallsbytes", bytes(range(256))),
    ("eine Liste statt eines Objekts", b"[1, 2, 3]"),
    ("eine nackte Zahl", b"42"),
    ("JSON in JSON", json.dumps(json.dumps(GOOD)).encode()),
    ("sehr tief verschachtelt", b"[" * 400 + b"]" * 400),
]


class TestTheReader:
    @pytest.mark.parametrize("name,raw", HOSTILE, ids=[n for n, _ in HOSTILE])
    def test_it_never_raises(self, tmp_path, name, raw):
        path = tmp_path / "settings.json"
        path.write_bytes(raw)
        jsonfile.read(path, what=name)

    @pytest.mark.parametrize("name,raw", HOSTILE[:4], ids=[n for n, _ in HOSTILE[:4]])
    def test_it_rescues_the_ones_that_still_hold_her_settings(self, tmp_path,
                                                              name, raw):
        """These are the whole reason it tries more than one encoding. The
        content is intact; only the encoding is wrong, and those are their
        measured noise gate, their device and their tempo."""
        path = tmp_path / "settings.json"
        path.write_bytes(raw)
        found = jsonfile.read(path, what=name)
        assert isinstance(found, dict), f"{name} was thrown away"
        assert found["look"] == "hifi"
        assert found["tempo_factor"] == 0.75

    def test_a_file_it_cannot_read_is_moved_aside_not_overwritten(self, tmp_path):
        """Whatever is in there is theirs, and the next save would destroy it."""
        path = tmp_path / "settings.json"
        path.write_bytes(b"\x00" * 64)
        assert jsonfile.read(path, what="Test") is None
        assert not path.exists(), "the unreadable file is still in the way"
        spoilt = list(tmp_path.glob("settings.json.unlesbar-*"))
        assert len(spoilt) == 1, f"it was not kept: {list(tmp_path.iterdir())}"
        assert spoilt[0].read_bytes() == b"\x00" * 64

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert jsonfile.read(tmp_path / "nothing.json", what="Test") is None
        assert not list(tmp_path.glob("*unlesbar*"))

    def test_a_directory_in_its_place_does_not_raise(self, tmp_path):
        (tmp_path / "settings.json").mkdir()
        assert jsonfile.read(tmp_path / "settings.json", what="Test") is None

    @pytest.mark.skipif(os.name != "nt", reason="Windows locking")
    def test_a_file_it_cannot_move_does_not_raise(self, tmp_path):
        """Quarantine can fail too -- an antivirus holding the file open, a
        read-only directory. It must not become the new crash."""
        path = tmp_path / "settings.json"
        path.write_bytes(b"\x00" * 8)
        with open(path, "rb"):
            assert jsonfile.read(path, what="Test") is None


class TestTheAppStartsAnyway:
    """The three files read before the window opens."""

    @pytest.mark.parametrize("name,raw", HOSTILE, ids=[n for n, _ in HOSTILE])
    def test_the_settings_do_not_stop_it(self, tmp_path, monkeypatch, name, raw):
        from pickhero import config as config_module

        path = tmp_path / "settings.json"
        path.write_bytes(raw)
        monkeypatch.setattr(config_module, "CONFIG_FILE", path)
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
        loaded = config_module.Config.load()
        assert loaded.look, "no look at all"
        assert loaded.language, "no language at all"

    @pytest.mark.parametrize("name,raw", HOSTILE, ids=[n for n, _ in HOSTILE])
    def test_the_progress_does_not_stop_it(self, tmp_path, monkeypatch, name, raw):
        from pickhero import progress as progress_module

        path = tmp_path / "progress.json"
        path.write_bytes(raw)
        monkeypatch.setattr(progress_module, "PROGRESS_FILE", path)
        tracker = progress_module.ProgressTracker()
        assert isinstance(tracker._data, dict)

    @pytest.mark.parametrize("name,raw", HOSTILE, ids=[n for n, _ in HOSTILE])
    def test_the_library_cache_does_not_stop_it(self, tmp_path, monkeypatch,
                                                name, raw):
        from pickhero import library as library_module

        path = tmp_path / "library.json"
        path.write_bytes(raw)
        monkeypatch.setattr(library_module, "CACHE_FILE", path)
        assert isinstance(library_module._load_cache(), dict)

    def test_her_settings_survive_being_written_in_utf_16(self, tmp_path,
                                                          monkeypatch):
        """The exact failure, end to end: the file is their real settings in
        the wrong encoding, and the player gets them back rather than defaults."""
        from pickhero import config as config_module

        path = tmp_path / "settings.json"
        path.write_bytes(json.dumps({"look": "hifi", "language": "de",
                                     "tempo_factor": 0.75}).encode("utf-16"))
        monkeypatch.setattr(config_module, "CONFIG_FILE", path)
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
        loaded = config_module.Config.load()
        assert loaded.look == "hifi", "the look was reset"
        assert loaded.tempo_factor == 0.75, "the tempo was reset"

    def test_the_next_save_writes_it_back_as_utf_8(self, tmp_path, monkeypatch):
        from pickhero import config as config_module

        path = tmp_path / "settings.json"
        path.write_bytes(json.dumps(GOOD).encode("utf-16"))
        monkeypatch.setattr(config_module, "CONFIG_FILE", path)
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
        loaded = config_module.Config.load()
        assert loaded.save()
        raw = path.read_bytes()
        assert not raw.startswith(b"\xff\xfe"), "it is still UTF-16"
        json.loads(raw.decode("utf-8"))


def test_the_exception_that_started_this_is_not_a_json_error():
    """The one fact the old guard got wrong, stated so it cannot drift back.

    `except (OSError, json.JSONDecodeError)` looks like it covers a bad file.
    It does not: UnicodeDecodeError is a ValueError and not a JSONDecodeError,
    so a file in the wrong encoding walks past it and out of the process.
    """
    assert issubclass(UnicodeDecodeError, ValueError)
    assert not issubclass(UnicodeDecodeError, json.JSONDecodeError)
    assert issubclass(json.JSONDecodeError, ValueError)


def test_no_reader_in_the_app_catches_only_the_narrow_one():
    """Four files read JSON and three of them had this guard. Any new one
    must go through jsonfile, or at least not repeat the mistake."""
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path("pickhero").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "json.load" not in text:
            continue
        for match in re.finditer(r"except \(([^)]*)\)", text):
            clause = match.group(1)
            if "JSONDecodeError" in clause and "ValueError" not in clause \
                    and "UnicodeDecodeError" not in clause:
                line = text[:match.start()].count("\n") + 1
                offenders.append(f"{path}:{line}")
    assert not offenders, (
        "these catch JSONDecodeError without ValueError, so a file in the "
        f"wrong encoding escapes them: {offenders}")
