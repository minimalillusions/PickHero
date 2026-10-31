"""The translation layer, which runs inside the draw loop.

t() is called tens of times per frame from inside draw methods. An exception
there kills the frame and then the app, so every test here is really one
question: can this ever raise, and can it ever return nothing useful?
"""

import json

import pytest

from pickhero import i18n


@pytest.fixture(autouse=True)
def _reset():
    i18n._catalogues.clear()
    i18n._names.clear()
    i18n._warned.clear()
    i18n.set_language("de")
    yield
    i18n.set_language("de")


class TestLookup:
    def test_german_is_the_default(self):
        assert i18n.current_language() == "de"

    def test_a_known_key_comes_back_translated(self):
        assert i18n.t("menu.play") == "Spielen"
        i18n.set_language("en")
        assert i18n.t("menu.play") == "Play"

    def test_an_unknown_language_falls_back_to_german(self):
        i18n.set_language("klingonisch")
        assert i18n.current_language() == "de"
        assert i18n.t("menu.play") == "Spielen"

    def test_a_key_missing_from_english_falls_back_to_german(self):
        """An unfinished translation shows German prose, not a raw key."""
        i18n._catalogues["en"] = {"menu.play": "Play"}
        i18n.set_language("en")
        assert i18n.t("menu.folder") == i18n._load("de")["menu.folder"]

    def test_a_key_missing_everywhere_returns_itself(self):
        assert i18n.t("gibt.es.nicht") == "gibt.es.nicht"

    def test_a_missing_key_is_logged_only_once(self):
        """This runs in the draw loop. Logging every call would write
        thousands of lines a second into the log file."""
        i18n.t("fehlt.auch")
        i18n.t("fehlt.auch")
        assert i18n._warned == {"fehlt.auch"}


class TestInterpolation:
    def test_values_are_substituted(self):
        assert "17" in i18n.t("hud.bar", bar=17)

    def test_an_unknown_placeholder_renders_instead_of_raising(self):
        i18n._catalogues["de"] = {"x": "Takt {nummer}"}
        assert i18n.t("x") == "Takt {nummer}"

    def test_a_wrong_placeholder_name_does_not_raise(self):
        i18n._catalogues["de"] = {"x": "Takt {nummer}"}
        assert i18n.t("x", bar=3) == "Takt {nummer}"


class TestPlurals:
    def test_one_and_many_differ(self):
        assert i18n.t("song.bars", count=1) == "1 Takt"
        assert i18n.t("song.bars", count=8) == "8 Takte"

    def test_zero_uses_the_plural_form(self):
        assert i18n.t("song.bars", count=0) == "0 Takte"

    def test_a_count_that_is_not_a_number_does_not_raise(self):
        assert isinstance(i18n.t("song.bars", count="viele"), str)


class TestCatalogues:
    def test_german_and_english_have_the_same_keys(self):
        """English may be incomplete while it is being written, but it must
        never carry a key German does not have -- that key would be
        unreachable from the reference language and silently dead."""
        de, en = i18n._load("de"), i18n._load("en")
        assert set(en) - set(de) == set()

    def test_english_is_complete(self):
        de, en = i18n._load("de"), i18n._load("en")
        missing = sorted(set(de) - set(en))
        assert missing == [], f"{len(missing)} Texte fehlen auf Englisch: {missing[:12]}"

    def test_both_languages_are_offered(self):
        codes = [code for code, _ in i18n.available_languages()]
        assert codes[:2] == ["de", "en"]

    def test_no_value_is_empty(self):
        for code in ("de", "en"):
            for key, value in i18n._load(code).items():
                if isinstance(value, dict):
                    assert all(str(v).strip() for v in value.values()), key
                else:
                    assert str(value).strip(), key

    def test_the_same_placeholders_appear_in_both_languages(self):
        """A translation that drops a placeholder loses the number it was
        supposed to show, and one that invents a placeholder renders braces."""
        import re
        de, en = i18n._load("de"), i18n._load("en")

        def holes(value):
            if isinstance(value, dict):
                value = " ".join(str(v) for v in value.values())
            return set(re.findall(r"\{(\w+)", str(value)))

        for key, german in de.items():
            if key in en:
                assert holes(german) == holes(en[key]), key


class TestUserCatalogue:
    def test_a_json_file_in_the_config_dir_is_offered(self, tmp_path,
                                                      monkeypatch):
        from pickhero import config as config_module
        folder = config_module.CONFIG_DIR / "lang"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "nl.json").write_text(json.dumps({
            "name": "Nederlands", "strings": {"menu.play": "Spelen"}}),
            encoding="utf-8")
        i18n._catalogues.clear()
        assert ("nl", "Nederlands") in i18n.available_languages()
        i18n.set_language("nl")
        assert i18n.t("menu.play") == "Spelen"
        assert i18n.t("menu.folder") == i18n._load("de")["menu.folder"]

    def test_a_corrupt_json_file_is_ignored(self, tmp_path):
        from pickhero import config as config_module
        folder = config_module.CONFIG_DIR / "lang"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "xx.json").write_text("{kaputt", encoding="utf-8")
        i18n._catalogues.clear()
        assert i18n.t("menu.play") == "Spielen"


class TestEveryKeyTheCodeAsksFor:
    """A key the code uses but the catalogue does not have renders as itself.

    The player would see "check.too_quiet" where a sentence belongs. Nothing
    crashes, which is exactly why it would ship: the failure is silent and
    only visible on the screen where it happens.
    """

    @staticmethod
    def _keys_in_source():
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "pickhero"
        found: dict[str, list[str]] = {}
        for path in sorted(root.rglob("*.py")):
            if path.parent.name == "lang" or path.name == "i18n.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name != "t" or not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.setdefault(first.value, []).append(path.name)
        return found

    def test_every_literal_key_exists_in_german(self):
        catalogue = i18n._load("de")
        missing = {k: v for k, v in self._keys_in_source().items()
                   if k not in catalogue}
        assert missing == {}, f"{len(missing)} Texte fehlen: {sorted(missing)[:12]}"

    @staticmethod
    def _literals_in_source():
        """Every string literal anywhere in the package.

        A key does not always reach t() directly. Several screens pick one
        into a variable first -- row_key = "check.string.row_thickest" -- and
        a scanner that only reads t()'s first argument calls those dead.
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "pickhero"
        out: set[str] = set()
        for path in sorted(root.rglob("*.py")):
            if path.parent.name == "lang":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    out.add(node.value)
                elif isinstance(node, ast.JoinedStr):
                    # An f-string key: keep the fixed prefix so "exercise.{slug}
                    # .title" still vouches for every exercise.* entry.
                    prefix = ""
                    for part in node.values:
                        if isinstance(part, ast.Constant) and isinstance(part.value, str):
                            prefix += part.value
                        else:
                            break
                    if prefix:
                        out.add(prefix)
        return out

    def test_the_catalogue_has_no_keys_nothing_asks_for(self):
        """Dead entries are not harmful in themselves, but they are usually
        the other half of a rename that only happened on one side -- and the
        half that is still in the code renders as a raw key."""
        literals = self._literals_in_source()
        unused = []
        for key in sorted(i18n._load("de")):
            if key in literals:
                continue
            # Built from a variable part: any literal that is a prefix of the
            # key vouches for it.
            if any(key.startswith(lit) and lit.count(".") >= 1 for lit in literals):
                continue
            unused.append(key)
        assert unused == [], f"{len(unused)} unbenutzte Texte: {unused[:12]}"
