"""Recorded fixture through native aubio and the real capture callback.

Only the host clocks are simulated. No audio device is opened.
"""
from itertools import cycle
from types import SimpleNamespace
import wave

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero.audio import input as audio_input
from pickhero.config import Config
from tests._recordings import open_strings


@pytest.mark.parametrize("rate", [44100, 48000])
@pytest.mark.parametrize("adc", [True, False])
def test_recorded_onsets_keep_timing_through_large_variable_jittered_callbacks(monkeypatch, rate, adc):
    with wave.open(str(open_strings()), "rb") as source:
        original_rate = source.getframerate()
        signal = np.frombuffer(source.readframes(source.getnframes()), "<i2").astype(np.float32) / 32768
    if rate != original_rate:
        signal = np.interp(np.arange(0, len(signal), original_rate / rate),
                           np.arange(len(signal)), signal).astype(np.float32)
    signal = np.concatenate([np.zeros(rate // 2, np.float32), signal, np.zeros(rate // 2, np.float32)])
    config = Config()
    config.instrument = "bass_ukulele"
    config.audio.sample_rate = rate
    capture = audio_input.AudioCapture(config)
    capture._start_time = 100
    capture._input_latency_ms = 400
    clock = [100.0]
    monkeypatch.setattr(audio_input.time, "perf_counter", lambda: clock[0])
    offset = 0
    found = []
    for index, block_size in enumerate(cycle([16384, 137, 2048, 333])):
        if offset >= len(signal):
            break
        count = min(block_size, len(signal) - offset)
        # Reported PortAudio input latency already spans callback to FIRST
        # ADC sample. Later callback jitter must not shift the sample clock.
        clock[0] = max(clock[0] + .000001, 100 + offset / rate + .400 + (index % 3) * .007)
        info = (SimpleNamespace(inputBufferAdcTime=2 + offset / rate,
                                currentTime=2 + clock[0] - 100) if adc else None)
        capture._audio_callback(signal[offset:offset + count].reshape(-1, 1), count, info, False)
        found.extend(item for item in capture.get_notes() if item.note.is_onset)
        offset += count
    expected = [(500 + i * 500, [28, 33, 38, 43][i // 3]) for i in range(12)]
    # One weak G in the 48-kHz resampling can remain unresolved. Timing must
    # nevertheless be correct for every emitted attack, with no duplicates.
    assert len(found) >= 11
    for item in found:
        candidates = [ref for ref in expected if ref[1] == item.note.midi_note
                      and abs(ref[0] - item.timestamp_ms) <= 100]
        assert candidates, (item.timestamp_ms, item.note.midi_note)
        expected.remove(min(candidates, key=lambda ref: abs(ref[0] - item.timestamp_ms)))
    assert capture.timing_diagnostics()["clock_source"] == ("adc" if adc else "fallback")
