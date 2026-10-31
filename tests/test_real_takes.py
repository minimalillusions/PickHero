"""Replay real recorded takes and hold the detector to them.

The 813 detection tests feed the detector synthesised plucks -- Karplus-Strong
and harmonic sums with anti-click fades -- plus one short fixture WAV. That
catches a great deal, but not everything: a bass ukulele through a room mic
carries string noise, a decaying body resonance, the hum of the room and their
hands, and no synthesiser produces that mixture by accident.

The practice recorder has been storing real takes all along. They live outside
the repository (.gitignore: "Personal test takes") because they are 25 MB of
them playing. What is committed is tests/fixtures/real_takes_baseline.json: per
take, the onsets today's detector reports. This replays the audio and compares.

So a change that alters what the app hears in real playing fails here, instead
of being discovered weeks later from a practice session that feels wrong.

Reading only. The takes directory is never written to, and the autouse
isolation in conftest keeps the app's own settings out of reach regardless.

Without the takes -- a fresh clone, another machine, CI -- every case skips
with a reason naming what is missing. A skip is honest; a silent pass is not.
Point PICKHERO_TAKES at a directory to use takes from somewhere else.

To accept new behaviour deliberately: python scripts/freeze-real-takes.py,
read the diff, commit it. See that script's docstring.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "fixtures" / "real_takes_baseline.json"


def _freezer():
    """The freeze script itself, so replay exists once and cannot drift.

    If this test grew its own copy of the hop-feeding loop, the baseline could
    be frozen by one implementation and checked by another, and the two would
    agree with each other about the wrong thing.
    """
    spec = importlib.util.spec_from_file_location(
        "real_takes_freezer", ROOT / "scripts" / "freeze-real-takes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _takes_dir() -> pathlib.Path:
    override = os.environ.get("PICKHERO_TAKES")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".pickhero" / "recordings"


def _baseline() -> dict:
    if not BASELINE.is_file():
        return {"takes": []}
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"takes": []}


def _available() -> dict[str, pathlib.Path]:
    """Takes on this machine, indexed by the PCM hash the recorder stored.

    By hash, not by directory name: a baseline must never be checked against
    audio that merely sits in the same place.
    """
    folder = _takes_dir()
    found: dict[str, pathlib.Path] = {}
    if not folder.is_dir():
        return found
    for take in sorted(folder.iterdir()):
        session = take / "session.json"
        if not session.is_file() or not (take / "input.wav").is_file():
            continue
        try:
            data = json.loads(session.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        digest = data.get("pcm_sha256")
        if isinstance(digest, str) and digest:
            found[digest] = take
    return found


BASE = _baseline()
TAKES = BASE.get("takes", [])
IDS = [str(entry.get("take", index)) for index, entry in enumerate(TAKES)]


def test_the_baseline_itself_is_substantive():
    """A wiped or truncated baseline must fail here, not pass everywhere else.

    Every case below skips when its audio is absent, which is correct but also
    means an empty baseline would produce a fully green run that checked
    nothing. This is the one assertion that does not depend on the audio.
    """
    assert BASE.get("schema_version") == 1
    played = [t for t in TAKES if t.get("onsets")]
    assert len(played) >= 2, "fewer than two takes with any onset in them"
    assert sum(len(t["onsets"]) for t in played) >= 40
    for entry in TAKES:
        assert entry.get("pcm_sha256"), entry.get("take")
        assert entry.get("profile"), entry.get("take")
        assert entry.get("sample_rate", 0) > 0, entry.get("take")


@pytest.mark.skipif(not TAKES, reason="no baseline to check against")
@pytest.mark.parametrize("entry", TAKES, ids=IDS)
def test_detector_still_hears_the_same_notes_in_her_own_playing(entry):
    takes = _available()
    take = takes.get(entry["pcm_sha256"])
    if take is None:
        pytest.skip(f"take {entry['take']} is not in {_takes_dir()} "
                    f"(its audio stays outside the repository)")

    freezer = _freezer()
    raw = (take / "input.wav").read_bytes()
    signal, rate = freezer._benchmark().load_pcm(
        take / "input.wav", hashlib.sha256(raw).hexdigest())
    assert rate == entry["sample_rate"]

    # Identical audio, so the loudness summary must come out identical too.
    # This guards the helper that decides whether a take was played at all.
    loud, windows = freezer.loud_windows(signal, rate, entry.get("noise_gate_db"))
    assert (loud, windows) == (entry["windows_above_gate"], entry["windows"])

    found = freezer.onsets(signal, rate, entry["profile"],
                           entry.get("noise_gate_db"))
    expected = [[float(ms), int(midi)] for ms, midi in entry["onsets"]]

    # The notes first: a different pitch, an extra strike or a lost one is the
    # regression that matters, and it reads clearly in the failure output.
    assert [midi for _, midi in found] == [midi for _, midi in expected], (
        f"{entry['take']}: the notes heard in this playing changed")

    # Then the timing. One hop of tolerance absorbs float noise across numpy
    # builds without admitting a real shift: at 48 kHz this profile steps
    # 11.6 ms, against a timing window of 100 ms. Asked of the detector rather
    # than assumed to be 512 samples, which it is not at this rate.
    hop = freezer.hop_ms(entry["profile"], rate)
    for (got_ms, midi), (want_ms, _) in zip(found, expected):
        assert abs(got_ms - want_ms) <= hop, (
            f"{entry['take']}: note {midi} moved from {want_ms} ms "
            f"to {got_ms} ms (more than one hop of {hop:.1f} ms)")


# Four of the six takes are the record button being tried out: a knock
# against the microphone, then room noise. Their baselines are empty, so the
# case above asserts they stay empty, and that is as much false-positive guard
# as this corpus can offer. It is a weak one. Forcing the gate to -90 dB,
# dropping the vote requirement from eight to one, and finally setting
# is_onset unconditionally all left those takes silent: at their gate the
# detector produces no report in them at all, so there is nothing for a
# loosened confirmation to let through. A separate "a knock is not a note"
# test was written here and deleted again -- it asserted only that a closed
# gate stays closed, and no mutation could make it fail.
