"""Text in the player's language.

German is the reference language. Every key exists in de.py; anything missing
from another catalogue falls back to the German rather than showing a raw key,
so a half-finished translation degrades to prose instead of to debris.

Catalogues are Python modules, not data files. PickHero ships as a one-file
PyInstaller build, where a .py inside the package is swept into the archive
automatically while a .json needs a datas entry and a _MEIPASS-aware path
helper. The problem disappears instead of being handled. Someone who wants a
language that is not shipped drops a JSON file into ~/.pickhero/lang/ and it
is merged over the built-ins at startup.

Two rules for whoever edits a catalogue:

  A value is a WHOLE SENTENCE, never a fragment. Word order and agreement
  differ between languages, so "tuned {n} semitones" + "higher" cannot be
  assembled -- English wants the adjective at the end, German wants it in the
  middle. Two complete sentences, two keys.

  Note names are NOT translated. German musical convention writes B as H, but
  every Guitar Pro file, every tuner and every tab online uses the scientific
  names, and the app has to agree with the material the player is learning from.
"""

from __future__ import annotations

from pickhero import jsonfile

DEFAULT_LANGUAGE = "de"

_catalogues: dict[str, dict[str, object]] = {}
_names: dict[str, str] = {}
_current = DEFAULT_LANGUAGE
_warned: set[str] = set()


class _Forgiving(dict):
    """A mapping that renders an unknown placeholder instead of raising.

    t() is called from inside draw methods, tens of times per frame. A
    KeyError from a translator's typo would kill the frame and then the app,
    so a missing placeholder shows as {name} and the screen still draws.
    """

    def __missing__(self, key):
        return "{" + str(key) + "}"


def _builtin(code: str) -> tuple[dict, str] | None:
    try:
        module = __import__(f"pickhero.lang.{code}", fromlist=["STRINGS"])
    except Exception:
        return None
    strings = getattr(module, "STRINGS", None)
    if not isinstance(strings, dict):
        return None
    return strings, str(getattr(module, "NAME", code))


def _safe_template(text: str) -> bool:
    """A string from a hand-made language pack that format_map may be given.

    Formatting is not inert. "{n:99999999}" pads to a hundred million
    characters and stalls the frame; "{n.__class__}" reaches into the object.
    The built-in catalogues use plain fields and short specs like "{number:02d}",
    so that is what a pack may use too. Anything else costs that one string,
    which falls back to German, and never the whole language.
    """
    import re
    import string
    try:
        fields = list(string.Formatter().parse(text))
    except ValueError:
        return False
    for _, field, spec, conversion in fields:
        if field is None:
            continue
        if not field.isidentifier() or conversion is not None:
            return False
        if spec and (len(spec) > 4 or any(int(n) > 99 for n in re.findall(r"\d+", spec))):
            return False
    return True


def _safe_entry(value) -> bool:
    if isinstance(value, str):
        return _safe_template(value)
    if isinstance(value, dict):          # plural forms
        return all(isinstance(v, str) and _safe_template(v) for v in value.values())
    return False


def _user_catalogues() -> dict[str, tuple[dict, str]]:
    """Languages the player dropped into ~/.pickhero/lang/ as JSON."""
    out: dict[str, tuple[dict, str]] = {}
    try:
        from pickhero.config import CONFIG_DIR
        folder = CONFIG_DIR / "lang"
        if not folder.is_dir():
            return out
        for path in sorted(folder.glob("*.json")):
            # A translation the player or somebody else wrote by hand, which is
            # exactly the kind of file an editor saves as UTF-16. Through
            # jsonfile, so one bad catalogue costs that catalogue and not
            # the language menu.
            data = jsonfile.read(path, what=f"Die Sprachdatei {path.name}")
            if not isinstance(data, dict):
                continue
            strings = data.get("strings")
            if not isinstance(strings, dict):
                continue
            strings = {key: value for key, value in strings.items()
                       if isinstance(key, str) and _safe_entry(value)}
            out[path.stem] = (strings, str(data.get("name", path.stem)))
    except Exception:
        pass
    return out


def _load(code: str) -> dict[str, object]:
    if code in _catalogues:
        return _catalogues[code]
    found = _builtin(code)
    merged: dict[str, object] = dict(found[0]) if found else {}
    name = found[1] if found else code
    user = _user_catalogues().get(code)
    if user is not None:
        merged.update(user[0])
        name = user[1]
    _catalogues[code] = merged
    _names[code] = name
    return merged


def available_languages() -> list[tuple[str, str]]:
    """[(code, name)] for everything that can be selected, German first."""
    codes = ["de", "en"]
    for code in _user_catalogues():
        if code not in codes:
            codes.append(code)
    out = []
    for code in codes:
        if _load(code):
            out.append((code, _names.get(code, code)))
    return out


def set_language(code: str) -> None:
    global _current
    _current = code if _load(code) else DEFAULT_LANGUAGE


def current_language() -> str:
    return _current


def _windows_display_language() -> str | None:
    """The language Windows draws its own menus in, e.g. "de-DE".

    The display language, not the regional format: a German living in London
    may well have English dates and German menus, and the menus are what
    the player reads.
    """
    import ctypes
    langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    name = ctypes.create_unicode_buffer(85)     # LOCALE_NAME_MAX_LENGTH
    if not ctypes.windll.kernel32.LCIDToLocaleName(langid, name, 85, 0):
        return None
    return name.value or None


def system_language() -> str | None:
    """The computer's language as a bare code -- "de", "en" -- or None."""
    import os
    import sys
    candidates = []
    if sys.platform == "win32":
        try:
            candidates.append(_windows_display_language())
        except (AttributeError, OSError, ValueError):
            pass
    for variable in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        candidates.append(os.environ.get(variable))
    for value in candidates:
        # "de-DE", "en_US.UTF-8", "fr:en" -> "de", "en", "fr"
        code = (value or "").split(":")[0].split(".")[0].replace("_", "-").split("-")[0].lower()
        if code and code not in ("c", "posix"):
            return code
    return None


def t(key: str, /, **kwargs) -> str:
    """The text for `key`, in the active language.

    Total by construction: it never raises and never returns nothing. A key
    missing everywhere comes back as itself, logged once -- once, because this
    runs inside the draw loop and an unguarded log call there would write
    thousands of lines a second.
    """
    try:
        value = _load(_current).get(key)
        if value is None and _current != DEFAULT_LANGUAGE:
            value = _load(DEFAULT_LANGUAGE).get(key)
        if value is None:
            if key not in _warned:
                _warned.add(key)
                try:
                    from pickhero.config import log_error
                    log_error(f"Kein Text fuer '{key}' in '{_current}'.")
                except Exception:
                    pass
            return key
        if isinstance(value, dict):
            count = kwargs.get("count", kwargs.get("n", 0))
            try:
                plural = "one" if abs(int(count)) == 1 else "other"
            except (TypeError, ValueError):
                plural = "other"
            value = value.get(plural) or value.get("other") or next(iter(value.values()))
        text = str(value)
        return text.format_map(_Forgiving(kwargs)) if kwargs else text
    except Exception:
        return key
