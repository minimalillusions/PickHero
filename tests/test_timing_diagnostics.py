"""Timing evidence is bounded, thread-safe, unit-labelled and never calibration."""

import json
import math
from concurrent.futures import ThreadPoolExecutor

import pytest

from pickhero.timing_diagnostics import MAX_SAMPLES, METRIC_NAMES, TimingDiagnostics


def test_empty_is_unmeasured_not_zero_latency():
    report = TimingDiagnostics().snapshot()
    assert report["schema_version"] == 1
    assert set(report["metrics"]) == set(METRIC_NAMES)
    for metric in report["metrics"].values():
        assert metric["count"] == metric["window_count"] == 0
        assert all(metric[key] is None for key in ("last", "min", "max", "mean", "median", "p95"))
    assert report["clock"] == {
        "last": "not_started", "counts": {"not_started": 0, "adc": 0, "fallback": 0}}
    assert report["unmeasured"] == ["physical_input_latency", "physical_output_latency",
                                    "display_latency", "roundtrip_latency"]
    json.dumps(report, allow_nan=False)


def test_recent_statistics_do_not_hide_lifetime_count_or_keep_all_samples():
    store = TimingDiagnostics(capacity=4)
    for value in (999, 1, 2, 3, 4):
        assert store.record("callback_processing_ms", value)
    metric = store.snapshot()["metrics"]["callback_processing_ms"]
    assert metric["count"] == 5
    assert metric["window_count"] == 4
    assert metric["last"] == metric["max"] == 4
    assert metric["min"] == 1
    assert metric["mean"] == metric["median"] == 2.5
    assert metric["p95"] == pytest.approx(3.85)
    assert "values" not in metric


@pytest.mark.parametrize("capacity", [0, -1, MAX_SAMPLES + 1])
def test_capacity_cannot_be_unbounded(capacity):
    with pytest.raises(ValueError):
        TimingDiagnostics(capacity)


@pytest.mark.parametrize("capacity", [None, True, 4.0, "4"])
def test_capacity_must_be_an_integer(capacity):
    with pytest.raises(TypeError):
        TimingDiagnostics(capacity)


def test_single_sample_has_complete_statistics():
    store = TimingDiagnostics(capacity=1)
    store.record("frame_interval_ms", 16.5)
    metric = store.snapshot()["metrics"]["frame_interval_ms"]
    assert all(metric[key] == 16.5 for key in ("last", "min", "max", "mean", "median", "p95"))


@pytest.mark.parametrize("invalid", [None, True, "3", [], {}, object(),
                                    math.nan, math.inf, -math.inf, 10**500, -1])
def test_invalid_durations_do_not_poison_a_valid_stream(invalid):
    store = TimingDiagnostics()
    assert store.record("callback_processing_ms", 3)
    assert not store.record("callback_processing_ms", invalid)
    report = store.snapshot()
    assert report["rejected_samples"] == 1
    assert report["metrics"]["callback_processing_ms"]["count"] == 1
    assert report["metrics"]["callback_processing_ms"]["mean"] == 3
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("invalid", ["raw_audio", "C:/Users/private.wav", "", None, [], {}])
def test_unknown_metrics_cannot_allocate_memory_or_keep_private_keys(invalid):
    store = TimingDiagnostics()
    assert not store.record(invalid, 3)
    report = store.snapshot()
    assert report["rejected_samples"] == 1
    assert set(report["metrics"]) == set(METRIC_NAMES)
    assert "private" not in json.dumps(report)


def test_real_time_song_time_and_pitch_are_explicitly_separate():
    store = TimingDiagnostics()
    assert store.record("timing_residual_song_ms", -50)
    assert store.record("timing_residual_wall_ms", -100)
    assert store.record("pitch_error_cents", -12)
    report = store.snapshot({"tempo_factor": 0.5, "manual_offset_ms": 0})
    song = report["metrics"]["timing_residual_song_ms"]
    wall = report["metrics"]["timing_residual_wall_ms"]
    pitch = report["metrics"]["pitch_error_cents"]
    assert song["clock_domain"] == "song"
    assert wall["clock_domain"] == "wall"
    assert song["mean"] == -50 and wall["mean"] == -100
    assert pitch["unit"] == "cents" and pitch["clock_domain"] == "pitch"
    assert song["evidence"] == wall["evidence"] == "performance_residual"
    assert report["context"]["manual_offset_ms"] == 0
    assert "roundtrip_latency" in report["unmeasured"]


def test_negative_clock_observations_remain_visible_for_diagnosing_clock_errors():
    store = TimingDiagnostics()
    assert store.record("capture_delivery_ms", -10)
    assert store.record("note_report_lag_ms", -12)
    report = store.snapshot()
    assert report["metrics"]["capture_delivery_ms"]["mean"] == -10
    assert report["metrics"]["note_report_lag_ms"]["mean"] == -12


def test_mixed_clock_sources_are_retained_even_if_last_block_was_adc():
    store = TimingDiagnostics()
    assert store.record_clock("adc")
    assert store.record_clock("fallback")
    assert store.record_clock("adc")
    assert not store.record_clock("device path or private data")
    assert not store.record_clock([])
    assert store.snapshot()["clock"] == {
        "last": "adc", "counts": {"not_started": 0, "adc": 2, "fallback": 1}}


def test_context_never_traverses_or_serializes_waveforms_paths_and_private_text():
    context = {"sample_rate": 48000, "device_index": None, "clock_source": "adc",
               "input_latency_ms": 8, "health_issue": None, "instrument": "bass_guitar",
               "raw_audio": [1, 2, 3], "path": "C:/Users/private/lesson.gp5",
               "device_name": "Private device", "nested": {"pcm": object()},
               "noise_gate_db": math.nan, "hop_size": True,
               "manual_offset_ms": "0", "frame_processing_ms": object()}
    report = TimingDiagnostics().snapshot(context)
    assert report["context"] == {
        "sample_rate": 48000, "device_index": None, "clock_source": "adc",
        "input_latency_ms": 8, "health_issue": None, "instrument": "bass_guitar"}
    text = json.dumps(report, allow_nan=False)
    assert all(word not in text for word in ("private", "Private", "pcm", "raw_audio"))
    assert math.isnan(context["noise_gate_db"])
    assert context["raw_audio"] == [1, 2, 3]


def test_allowlisted_context_keys_still_reject_untrusted_values():
    assert TimingDiagnostics().snapshot({"instrument": "C:/private", "clock_source": [],
                                         "health_issue": {"secret": True}})["context"] == {}


def test_snapshot_is_independent_and_reset_clears_all_observations():
    store = TimingDiagnostics()
    store.record("onset_correction_ms", 38)
    store.record("onset_correction_ms", None)
    store.record_clock("fallback")
    old = store.snapshot()
    old["metrics"]["onset_correction_ms"]["mean"] = 9000
    old["clock"]["counts"]["fallback"] = 9000
    assert store.snapshot()["metrics"]["onset_correction_ms"]["mean"] == 38
    assert store.snapshot()["clock"]["counts"]["fallback"] == 1
    store.reset()
    fresh = store.snapshot()
    assert fresh["rejected_samples"] == 0
    assert fresh["clock"]["counts"]["fallback"] == 0
    assert all(metric["count"] == metric["window_count"] == 0
               for metric in fresh["metrics"].values())


def test_parallel_writers_and_readers_keep_finite_bounded_coherent_snapshots():
    store = TimingDiagnostics(capacity=32)

    def writer(value):
        for _ in range(1000):
            assert store.record("callback_processing_ms", value)
            assert store.record_clock("adc")

    def reader():
        for _ in range(100):
            report = store.snapshot()
            metric = report["metrics"]["callback_processing_ms"]
            assert 0 <= metric["window_count"] <= 32
            assert metric["count"] >= metric["window_count"]
            if metric["window_count"]:
                assert metric["min"] <= metric["mean"] <= metric["max"]
            json.dumps(report, allow_nan=False)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(writer, value) for value in (1, 2, 3)]
        futures.append(pool.submit(reader))
        for future in futures:
            future.result()
    report = store.snapshot()
    assert report["metrics"]["callback_processing_ms"]["count"] == 3000
    assert report["metrics"]["callback_processing_ms"]["window_count"] == 32
    assert report["clock"]["counts"]["adc"] == 3000


def test_large_finite_scalars_still_produce_valid_json_aggregates():
    store = TimingDiagnostics()
    for _ in range(1000):
        assert store.record("timing_residual_song_ms", 1e12)
        assert store.record("timing_residual_song_ms", -1e12)
    report = store.snapshot()
    assert report["metrics"]["timing_residual_song_ms"]["mean"] == 0
    json.dumps(report, allow_nan=False)
