"""Recordings of real playing that are not part of the public repository."""

from pathlib import Path

import pytest

OPEN_STRINGS = Path(__file__).parent / "fixtures" / "bassuke_open_strings.wav"


def open_strings() -> Path:
    """Twelve plucks on four open strings, if this checkout has them.

    The recording is a person's own playing and stays out of the public
    repository. The tests that need it skip without it and say why, rather
    than failing on a file that was never meant to be there.
    """
    if not OPEN_STRINGS.exists():
        pytest.skip("the open-strings recording is private and not in this checkout")
    return OPEN_STRINGS
