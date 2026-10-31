"""Detection settings belong to the instrument, not to the person."""

import json

import pytest

from pickhero import config as config_module
from pickhero.config import Config, PROFILE_AUDIO_FIELDS


class TestStaleSettings:
    def test_an_old_file_does_not_impose_guitar_values_on_a_bass(self):
        """Settings written before instruments existed carry a 2048 buffer and
        a 0.8 confidence threshold because that version wrote them always, not
        because anyone chose them. On the bass ukulele profile that is the
        configuration that made detection feel like a coin toss."""
        config_module.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        config_module.CONFIG_FILE.write_text(json.dumps({
            "audio": {"device_index": 4, "sample_rate": 48000,
                      "buf_size": 2048, "confidence_threshold": 0.8,
                      "onset_threshold": 0.3, "noise_gate_db": -45.0},
        }), encoding="utf-8")
        loaded = Config.load()
        assert loaded.audio.device_index == 4
        assert loaded.audio.sample_rate == 48000
        for name in PROFILE_AUDIO_FIELDS:
            assert getattr(loaded.audio, name) is None

    def test_a_current_file_keeps_what_the_player_set(self):
        config_module.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        config_module.CONFIG_FILE.write_text(json.dumps({
            "instrument": "bass_ukulele",
            "audio": {"noise_gate_db": -37.0},
        }), encoding="utf-8")
        loaded = Config.load()
        assert loaded.audio.noise_gate_db == -37.0


class TestSwitchInstrument:
    def test_switching_hands_detection_back_to_the_profile(self):
        config = Config()
        config.instrument = "bass_ukulele"
        config.audio.noise_gate_db = -37.0
        config.switch_instrument("guitar")
        assert config.instrument == "guitar"
        assert config.audio.noise_gate_db is None

    def test_a_measurement_comes_back_with_its_instrument(self):
        """The player sits still for three seconds to measure their room. Losing that
        because the player looked at the guitar profile once would be rude."""
        config = Config()
        config.instrument = "bass_ukulele"
        config.audio.noise_gate_db = -37.0
        config.switch_instrument("guitar")
        config.audio.noise_gate_db = -52.0
        config.switch_instrument("bass_ukulele")
        assert config.audio.noise_gate_db == -37.0

    def test_switching_to_the_same_instrument_changes_nothing(self):
        config = Config()
        config.instrument = "bass_ukulele"
        config.audio.noise_gate_db = -37.0
        config.switch_instrument("bass_ukulele")
        assert config.audio.noise_gate_db == -37.0

    def test_the_stash_survives_a_save_and_load(self):
        config = Config()
        config.instrument = "bass_ukulele"
        config.audio.noise_gate_db = -37.0
        config.switch_instrument("guitar")
        assert config.save() is True
        again = Config.load()
        again.switch_instrument("bass_ukulele")
        assert again.audio.noise_gate_db == -37.0
