"""Tests for instrument-aware track selection.

A Guitar Pro file normally holds several tracks. Which one the player should
be shown depends on the instrument they are holding. The original selector
required exactly six strings and a General MIDI guitar program, so a bass
track could never be chosen, and a bass ukulele player was handed a guitar
part they cannot play.
"""

from pathlib import Path

import pytest

from pickhero import instruments
from pickhero.tabs.loader import (
    list_tracks,
    load_gp_file,
    extract_backing_track,
    score_track,
    select_track_index,
)

FIXTURES = Path(__file__).parent / "fixtures"
DEMO = FIXTURES / "Demo_v5.gp5"
ENSEMBLE = FIXTURES / "ensemble.gp5"
GP7_BASS = FIXTURES / "bass_only.gp7"

BASS_TUNING = {1: 43, 2: 38, 3: 33, 4: 28}  # G2 D2 A1 E1


class TestListTracks:
    def test_reports_tuning_and_instrument_family(self):
        tracks = list_tracks(DEMO)
        by_name = {t["name"]: t for t in tracks}
        bass = by_name["Bass"]
        assert bass["strings"] == 4
        assert bass["is_bass"] is True
        assert bass["is_guitar"] is False
        assert bass["tuning"] == BASS_TUNING
        rhythm = by_name["Rhythm Guitar"]
        assert rhythm["is_guitar"] is True
        assert rhythm["is_bass"] is False

    def test_string_one_is_the_highest_pitch(self):
        """This project's convention, inherited from pyguitarpro."""
        for t in list_tracks(DEMO):
            tuning = t["tuning"]
            if len(tuning) > 1 and len(set(tuning.values())) > 1:
                assert tuning[1] == max(tuning.values())
                assert tuning[len(tuning)] == min(tuning.values())

    def test_does_not_crash_on_gp7(self):
        """It used to call guitarpro.parse() unconditionally, which raises."""
        tracks = list_tracks(GP7_BASS)
        assert len(tracks) == 1
        assert tracks[0]["tuning"] == BASS_TUNING
        assert tracks[0]["is_bass"] is True


class TestSelection:
    @pytest.mark.parametrize("path,expected_name", [
        (DEMO, "Rhythm Guitar"),
        (ENSEMBLE, "Lead Guitar"),
    ])
    def test_guitar_still_picks_the_guitar(self, path, expected_name):
        """Guarding against a regression in the behaviour that already worked."""
        tracks = list_tracks(path)
        idx = select_track_index(tracks, instruments.GUITAR)
        assert tracks[idx]["name"] == expected_name

    @pytest.mark.parametrize("path,expected_name", [
        (DEMO, "Bass"),
        (ENSEMBLE, "Bass"),
    ])
    def test_bass_ukulele_picks_the_bass_track(self, path, expected_name):
        tracks = list_tracks(path)
        idx = select_track_index(tracks, instruments.BASS_UKULELE)
        assert tracks[idx]["name"] == expected_name
        assert tracks[idx]["tuning"] == BASS_TUNING

    def test_percussion_is_never_selected(self):
        for path in (DEMO, ENSEMBLE):
            tracks = list_tracks(path)
            for prof in (instruments.GUITAR, instruments.BASS_UKULELE):
                assert tracks[select_track_index(tracks, prof)]["is_percussion"] is False

    def test_an_exact_tuning_match_outranks_a_mere_family_match(self):
        tracks = list_tracks(DEMO)
        by_name = {t["name"]: t for t in tracks}
        u = instruments.BASS_UKULELE
        assert score_track(by_name["Bass"], u) > score_track(by_name["Rhythm Guitar"], u)

    def test_falls_back_to_a_playable_track_not_blindly_to_track_zero(self):
        """With nothing suitable, pick the first non-percussion track."""
        fake = [
            {"index": 0, "name": "Drums", "strings": 6, "instrument": 0,
             "tuning": {}, "is_percussion": True, "is_guitar": False, "is_bass": False},
            {"index": 1, "name": "Organ", "strings": 0, "instrument": 19,
             "tuning": {}, "is_percussion": False, "is_guitar": False, "is_bass": False},
        ]
        assert select_track_index(fake, instruments.GUITAR) == 1


class TestLoading:
    def test_loading_with_a_bass_profile_yields_the_bass_part(self):
        tl = load_gp_file(DEMO, instrument=instruments.BASS_UKULELE)
        assert tl.metadata.track_name == "Bass"
        assert tl.metadata.tuning == BASS_TUNING
        assert len(tl) > 0
        # Every note must sit on one of the four strings, and within range.
        u = instruments.BASS_UKULELE
        for note in tl.notes:
            assert 1 <= note.string <= 4
            assert u.midi_min <= note.midi_note <= u.midi_max + 12

    def test_loading_without_a_profile_is_unchanged(self):
        """Default stays guitar, so existing callers keep their behaviour."""
        assert load_gp_file(DEMO).metadata.track_name == "Rhythm Guitar"

    def test_explicit_track_index_still_wins(self):
        tl = load_gp_file(DEMO, track_index=1, instrument=instruments.BASS_UKULELE)
        assert tl.metadata.track_name == "Solo Guitar"

    def test_gp7_loads_with_a_bass_profile(self):
        tl = load_gp_file(GP7_BASS, instrument=instruments.BASS_UKULELE)
        assert tl.metadata.tuning == BASS_TUNING


class TestBackingTrack:
    def test_backing_excludes_the_played_track_not_all_guitars(self):
        """A bass player should still hear the guitars behind them.

        The old behaviour excluded every guitar track regardless of what the
        player was playing.
        """
        tl = load_gp_file(DEMO, instrument=instruments.BASS_UKULELE)
        played = tl.metadata.track_index
        bass_backing = extract_backing_track(DEMO, exclude_track_indices={played})
        guitar_tl = load_gp_file(DEMO, instrument=instruments.GUITAR)
        guitar_backing = extract_backing_track(
            DEMO, exclude_track_indices={guitar_tl.metadata.track_index}
        )
        assert len(bass_backing) > 0
        assert len(guitar_backing) > 0
        # Different track excluded, so the accompaniments must differ.
        assert len(bass_backing) != len(guitar_backing)

    def test_default_exclusion_follows_the_instrument(self):
        auto = extract_backing_track(DEMO, instrument=instruments.BASS_UKULELE)
        tl = load_gp_file(DEMO, instrument=instruments.BASS_UKULELE)
        explicit = extract_backing_track(
            DEMO, exclude_track_indices={tl.metadata.track_index}
        )
        assert len(auto) == len(explicit)
