"""The packaged-app check pins the self-test's counts; keep them in step.

scripts/test-packaged.ps1 runs only when the EXE is built, so a stale count
there went unnoticed until the build on GitHub failed: four pieces were added
to the music catalogue, the self-test rendered twice as many music views, and
the script still expected the old number. This test fails in the normal suite
instead.
"""
import re
from pathlib import Path

from pickhero import instruments
from pickhero.music import PIECES, VERSIONS

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "test-packaged.ps1"


def test_packaged_check_expects_every_music_view_the_self_test_renders():
    expected = re.search(r"music_render_cases -ne (\d+)", SCRIPT.read_text(encoding="ascii"))
    assert expected, "test-packaged.ps1 no longer checks music_render_cases"
    # selftest.py: profiles x languages (de, en) x looks (studio, stage, hifi)
    # x sizes (1280x720, 320x240) x pieces x versions x two views (detail, result).
    rendered = len(instruments.PROFILES) * 2 * 3 * 2 * len(PIECES) * len(VERSIONS) * 2
    assert int(expected.group(1)) == rendered
