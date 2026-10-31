"""Reading a JSON file that may not be what it claims to be.

Their settings file came back one morning starting with 0xFF, and the app died
before the window opened: `UnicodeDecodeError: 'utf-8' codec can't decode byte
0xff in position 0`. Four places in this project read JSON and three of them
caught `(OSError, json.JSONDecodeError)`, which does not include
UnicodeDecodeError -- a file that is valid JSON in the wrong encoding goes
straight past that guard and out of the process.

0xFF 0xFE is a UTF-16 byte order mark. Several ordinary things on Windows
write that: PowerShell's `>` redirect on 5.1, Notepad's "Unicode" option, and
anything that round-trips a file through a tool that defaults to it. So this
is not an attack and not a corrupt disk; it is a file somebody else touched.

Two rules follow from that:

  * Never raise. A practice session must not be lost because a settings file
    is in the wrong encoding.
  * Try to read it anyway before giving up. The content is almost always
    intact -- it is the encoding that is wrong -- and those are their measured
    noise gate, their device and their progress.

What cannot be read at all is moved aside rather than overwritten, so the
next save does not destroy whatever was in there.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from pickhero.bounded_io import FileTooLargeError, read_bounded
from typing import Any


def log_error(message: str) -> None:
    """Imported where it is used, not at the top.

    log_error lives in config.py and config.py reads its file through this
    module, so importing it at the top is a cycle. Resolving it per call
    costs a dictionary lookup on a path that runs a handful of times at
    startup, and it keeps this module importable on its own.
    """
    from pickhero.config import log_error as real

    real(message)


# In order of how likely they are to be the truth on a Windows machine.
# utf-8 first because that is what this app writes; utf-8-sig for a file some
# editor added a BOM to; utf-16 for PowerShell and Notepad; cp1252 last,
# because it decodes almost any byte sequence and would mask the others.
ENCODINGS = ("utf-8", "utf-8-sig", "utf-16", "cp1252")
MAX_JSON_BYTES = 16 * 1024 * 1024


def read(path: Path | str, *, what: str = "Datei") -> Any | None:
    """The JSON in `path`, or None. Never raises.

    `what` names the file in the log in the user's own words, because the log
    is the only place the player will see that something was wrong.
    """
    path = Path(path)
    try:
        with path.open("rb") as handle:
            raw = read_bounded(handle, MAX_JSON_BYTES)
    except FileTooLargeError:
        quarantine(path, what=what)
        return None
    except FileNotFoundError:
        return None
    except OSError as exc:
        log_error(f"{what} konnte nicht gelesen werden: {exc}")
        return None
    if not raw.strip():
        return None

    for encoding in ENCODINGS:
        try:
            value = json.loads(raw.decode(encoding))
        except (UnicodeError, ValueError, RecursionError):
            # One clause, because the two that were here caught the same
            # things twice: UnicodeDecodeError IS a ValueError. That is the
            # whole bug -- the guard it replaced caught OSError and
            # json.JSONDecodeError, and UnicodeDecodeError is a ValueError
            # but is NOT a JSONDecodeError, so it walked straight past.
            # ValueError also covers what json raises for a number too long
            # to convert, which JSONDecodeError does not.
            continue
        if encoding != "utf-8":
            log_error(f"{what} war {encoding} statt UTF-8 und wurde gerettet; "
                      f"beim nächsten Speichern wird sie korrigiert.")
        return value

    quarantine(path, what=what)
    return None


def quarantine(path: Path | str, *, what: str = "Datei") -> Path | None:
    """Move an unreadable file aside and say where it went.

    Not deleted and not overwritten in place. Whatever is in there is theirs,
    the next save would destroy it, and a file the player can still send me is worth
    more than a tidy directory.
    """
    path = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns()}"
    spoilt = path.with_name(f"{path.name}.unlesbar-{stamp}")
    try:
        path.replace(spoilt)
    except OSError as exc:
        log_error(f"{what} ist unlesbar und ließ sich nicht beiseitelegen: {exc}")
        return None
    log_error(f"{what} war unlesbar. Sie liegt jetzt unter {spoilt.name}; "
              f"es wird mit den Standardwerten weitergemacht.")
    return spoilt
