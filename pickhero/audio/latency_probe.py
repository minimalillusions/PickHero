"""Explicit, short audio roundtrip measurement using three known quiet bursts.

Nothing opens a device until ``start`` is called. A worker owns one duplex
stream and at most 4.5 seconds of transient mono input; only a numeric result
survives completion. Input is never routed back to output. The UI must wait
for ``safe_to_resume`` after cancellation before opening another stream.

The measured sample lag runs from the output buffer to the returning input
buffer, including their buffering and the speaker/microphone or cable route.
It does not isolate input latency, measure display latency, or calibrate
musical scoring. A duplex audio test also cannot measure a separate MIDI
synthesizer's output latency.

Callback/cleanup semantics follow python-sounddevice's official Stream API:
https://python-sounddevice.readthedocs.io/en/0.5.3/api/streams.html
"""

from __future__ import annotations

from copy import deepcopy
import math
from threading import Event, Lock, Thread, current_thread
import time

import numpy as np
import sounddevice as sd


DURATION_SECONDS = 4.5
PULSE_SECONDS = (0.5, 1.8, 3.1)
PULSE_LENGTH_SECONDS = 0.064
MAX_DELAY_SECONDS = 0.5
OUTPUT_PEAK = 0.045  # About -27 dBFS, never monitoring/amplifying live input.
MIN_CORRELATION = 0.4
MIN_PEAK_RATIO = 1.3
MAX_SPREAD_MS = 5.0
TIMEOUT_PADDING_SECONDS = 3.0
_RATES = (48000, 44100, 32000, 22050, 16000)
_FINAL_STATES = frozenset({"complete", "failed", "cancelled"})


class _ProbeError(Exception):
    """Stable, localizable reason without arbitrary device error strings."""


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _base_report() -> dict:
    return {
        "schema_version": 1,
        "status": "failed",
        "reason": None,
        "measurement": "audio_roundtrip",
        "method": "duplex_sample_correlation",
        "reference": "output_buffer_to_input_buffer",
        "roundtrip_ms": None,
        "spread_ms": None,
        "pulse_delays_ms": [],
        "correlations": [],
        "sample_rate": None,
        "input_device": None,
        "output_device": None,
        "driver_input_latency_ms": None,
        "driver_output_latency_ms": None,
        "maximum_delay_ms": MAX_DELAY_SECONDS * 1000,
        "unmeasured": ["input_latency_alone", "output_latency_alone",
                       "display_latency", "midi_synthesizer_latency"],
        "automatic_offset_applied": False,
        "stream_closed": True,
    }


def _probe_signal(sample_rate: int) -> tuple[np.ndarray, np.ndarray, tuple[int, ...]]:
    """A tapered chirp has one sharp correlation peak, unlike a metronome tone."""
    length = round(PULSE_LENGTH_SECONDS * sample_rate)
    t = np.arange(length, dtype=np.float64) / sample_rate
    # A broad audible range survives ordinary speakers while its envelope
    # fades at both ends. Its volume is fixed, independent of microphone gain.
    phase = 2 * np.pi * (650 * t + (3600 - 650) / (2 * PULSE_LENGTH_SECONDS) * t * t)
    pulse = np.sin(phase) * np.hanning(length)
    pulse *= OUTPUT_PEAK / max(float(np.max(np.abs(pulse))), 1e-12)
    pulse = pulse.astype(np.float32)
    output = np.zeros(round(DURATION_SECONDS * sample_rate), dtype=np.float32)
    starts = tuple(round(seconds * sample_rate) for seconds in PULSE_SECONDS)
    for start in starts:
        output[start:start + length] = pulse
    return output, pulse, starts


def _correlation(segment: np.ndarray, pulse: np.ndarray) -> np.ndarray:
    """Normalized, mean-removed correlation at each valid sample offset."""
    reference = pulse.astype(np.float64)
    reference -= np.mean(reference)
    samples = segment.astype(np.float64)
    size = len(samples) + len(reference) - 1
    nfft = 1 << (size - 1).bit_length()
    convolution = np.fft.irfft(np.fft.rfft(samples, nfft)
                              * np.fft.rfft(reference[::-1], nfft), nfft)
    dots = convolution[len(reference) - 1:len(samples)]
    sums = np.concatenate(([0.0], np.cumsum(samples)))
    squares = np.concatenate(([0.0], np.cumsum(samples * samples)))
    width = len(reference)
    energy = squares[width:] - squares[:-width]
    energy -= (sums[width:] - sums[:-width]) ** 2 / width
    denominator = np.sqrt(np.maximum(energy, 0.0) * np.dot(reference, reference))
    scores = np.zeros_like(dots)
    np.divide(np.abs(dots), denominator, out=scores, where=denominator > 1e-12)
    return np.clip(scores, 0.0, 1.0)


def analyse_roundtrip(recording: np.ndarray, sample_rate: int) -> dict:
    """Analyse an isolated probe capture; never read a device or persist PCM.

    All three bursts must have a strong, unambiguous, consistent return.
    Missing or questionable evidence yields no proposed millisecond offset.
    """
    report = _base_report()
    if (isinstance(sample_rate, bool) or not isinstance(sample_rate, int)
            or not 8000 <= sample_rate <= 96000):
        report["reason"] = "unsupported_format"
        return report
    report["sample_rate"] = sample_rate
    expected = round(DURATION_SECONDS * sample_rate)
    if (not isinstance(recording, np.ndarray) or recording.ndim != 1
            or len(recording) != expected or recording.dtype.kind not in "fi"):
        report["reason"] = "invalid_capture"
        return report
    if not np.all(np.isfinite(recording)):
        report["reason"] = "invalid_capture"
        return report
    peak = float(np.max(np.abs(recording)))
    if peak >= 0.9995:
        report["reason"] = "clipping"
        return report
    if peak < 0.0001:
        report["reason"] = "no_signal"
        return report
    output, pulse, starts = _probe_signal(sample_rate)
    # The full reference is generated for the same public test contract but
    # is not needed for correlation and can be discarded immediately.
    del output
    max_delay = round(MAX_DELAY_SECONDS * sample_rate)
    exclusion = max(1, round(0.003 * sample_rate))
    delays = []
    scores = []
    for start in starts:
        segment = recording[start:start + max_delay + len(pulse)]
        correlations = _correlation(segment, pulse)
        index = int(np.argmax(correlations))
        best = float(correlations[index])
        if best < MIN_CORRELATION:
            report["reason"] = "weak_correlation"
            return report
        # Equal competing echoes cannot establish which route the user hears.
        # Exclude the sharp main lobe, not the rest of the search interval.
        competitor = correlations.copy()
        competitor[max(0, index - exclusion):index + exclusion + 1] = 0
        if float(np.max(competitor)) * MIN_PEAK_RATIO > best:
            report["reason"] = "ambiguous"
            return report
        # A maximum at the search boundary may be a truncated, longer return.
        if index >= max_delay:
            report["reason"] = "out_of_range"
            return report
        delays.append(index / sample_rate * 1000.0)
        scores.append(best)
    spread = max(delays) - min(delays)
    if spread > MAX_SPREAD_MS:
        report["reason"] = "inconsistent"
        return report
    report.update(status="complete", reason=None, roundtrip_ms=float(np.median(delays)),
                  spread_ms=spread, pulse_delays_ms=delays, correlations=scores)
    return report


def _select_devices(input_device: int | None) -> tuple[int, int, dict, dict]:
    try:
        selected = input_device
        if selected is None:
            # Match AudioCapture's Automatic selection instead of quietly
            # testing the OS's often much slower alternate host API.
            from pickhero.audio.input import best_input_device
            selected = best_input_device()
        if selected is None:
            selected = sd.default.device[0]
        if isinstance(selected, bool) or int(selected) < 0:
            raise _ProbeError("no_input")
        selected = int(selected)
        input_info = sd.query_devices(selected)
        if input_info["max_input_channels"] < 1:
            raise _ProbeError("no_input")
        host = int(input_info["hostapi"])
        host_info = sd.query_hostapis(host)
        output = int(host_info["default_output_device"])
        if output < 0:
            raise _ProbeError("no_compatible_output")
        output_info = sd.query_devices(output)
        if output_info["max_output_channels"] < 1 or int(output_info["hostapi"]) != host:
            raise _ProbeError("no_compatible_output")
        return selected, output, input_info, output_info
    except _ProbeError:
        raise
    except Exception as exc:
        raise _ProbeError("device_error") from exc


def _sample_rate(input_device: int, output_device: int, preferred: int,
                 input_info: dict, output_info: dict, output_channels: int) -> int:
    candidates = [preferred, input_info.get("default_samplerate"),
                  output_info.get("default_samplerate"), *_RATES]
    tried = set()
    for candidate in candidates:
        rate = _finite_number(candidate)
        if rate is None or not 8000 <= rate <= 96000 or not rate.is_integer():
            continue
        rate = int(rate)
        if rate in tried:
            continue
        tried.add(rate)
        try:
            sd.check_input_settings(device=input_device, channels=1,
                                    dtype="float32", samplerate=rate)
            sd.check_output_settings(device=output_device, channels=output_channels,
                                     dtype="float32", samplerate=rate)
        except Exception:
            continue
        return rate
    raise _ProbeError("unsupported_format")


def _close_stream(stream) -> bool:
    """Bounded worker-side cleanup attempts; never treat an exception as closed."""
    try:
        stream.abort()
    except Exception:
        pass  # close() also discards pending buffers; it still must be tried.
    for _ in range(2):
        try:
            stream.close()
            if getattr(stream, "closed", True):
                return True
        except Exception:
            # Some backends report an error after actually releasing the
            # stream. A confirmed closed handle is safe; an uncertain one is
            # retained for a later explicit cleanup retry.
            try:
                if stream.closed:
                    return True
            except Exception:
                pass
    return False


class LatencyProbe:
    """One explicit measurement, asynchronously owned and cleaned up.

    ``update`` and ``poll`` never perform audio work. ``stop`` requests
    cancellation without blocking the UI; wait for ``safe_to_resume`` before
    resuming normal capture. ``close`` offers a bounded join for shutdown.
    Native driver open/close calls cannot safely be killed from another
    Python thread, so ``close`` returns False if the worker has not finished.
    """

    def __init__(self, input_device: int | None = None, sample_rate: int = 48000):
        self.input_device = input_device
        self.sample_rate = sample_rate
        self._lock = Lock()
        self._cancel = Event()
        self._thread: Thread | None = None
        self._status = "idle"
        self._progress = 0.0
        self._result: dict | None = None
        self._stream = None
        self._safe_to_resume = True

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def safe_to_resume(self) -> bool:
        """Only True once this probe has released its audio device handles."""
        with self._lock:
            return self._safe_to_resume

    def start(self) -> bool:
        """Launch the worker; repeated starts cannot open overlapping streams."""
        with self._lock:
            if self.is_running or not self._safe_to_resume:
                return False
            self._cancel = Event()
            self._result = None
            self._status = "starting"
            self._progress = 0.0
            self._safe_to_resume = False
            self._thread = Thread(target=self._run, name="pickhero-latency-probe", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        self._cancel.set()
        with self._lock:
            # An unsuccessful native close must not be forgotten just because
            # its worker exited. Retrying is explicit and stays off the UI.
            if not self.is_running and self._stream is not None:
                self._status = "closing"
                self._thread = Thread(target=self._retry_cleanup,
                                      name="pickhero-latency-cleanup", daemon=True)
                self._thread.start()

    cancel = stop

    def close(self, timeout: float = 1.0) -> bool:
        self.stop()
        thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout=max(0.0, min(float(timeout), 5.0)))
        return not self.is_running and self.safe_to_resume

    def update(self) -> dict:
        with self._lock:
            report = deepcopy(self._result) if self._result is not None else {}
            report.update(status=self._status, progress=self._progress,
                          running=self._status not in _FINAL_STATES and self._status != "idle",
                          stream_closed=self._safe_to_resume)
            report.setdefault("reason", None)
            return report

    def poll(self) -> dict | None:
        """Result after cleanup attempts; check stream_closed before resuming."""
        with self._lock:
            return deepcopy(self._result) if self._status in _FINAL_STATES else None

    def _state(self, status: str) -> None:
        with self._lock:
            self._status = status

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise _ProbeError("cancelled")

    def _retry_cleanup(self) -> None:
        with self._lock:
            stream = self._stream
            report = deepcopy(self._result) if self._result is not None else _base_report()
        closed = stream is None or _close_stream(stream)
        report.update(status="cancelled" if closed else "failed",
                      reason="cancelled" if closed else "cleanup_failed", stream_closed=closed,
                      roundtrip_ms=None, spread_ms=None, pulse_delays_ms=[], correlations=[])
        with self._lock:
            self._stream = None if closed else stream
            self._safe_to_resume = closed
            self._result = report
            self._status = report["status"]

    def _run(self) -> None:
        report = _base_report()
        stream = None
        recording = output = pulse = None
        try:
            self._check_cancelled()
            selected, output_device, input_info, output_info = _select_devices(self.input_device)
            self._check_cancelled()
            channels = min(2, int(output_info["max_output_channels"]))
            rate = _sample_rate(selected, output_device, self.sample_rate,
                                input_info, output_info, channels)
            self._check_cancelled()
            report.update(input_device=selected, output_device=output_device, sample_rate=rate)
            output, pulse, _ = _probe_signal(rate)
            recording = np.zeros_like(output)
            total = len(output)
            position = 0
            callback_fault = None
            finished = Event()

            def callback(indata, outdata, frames, time_info, status):
                nonlocal position, callback_fault
                # PortAudio requires every output sample initialized even if
                # this callback aborts. Never monitor the recorded microphone.
                outdata.fill(0)
                if self._cancel.is_set():
                    raise sd.CallbackAbort
                if status:
                    callback_fault = "stream_error"
                    raise sd.CallbackAbort
                if (frames <= 0 or indata.shape[0] != frames or outdata.shape[0] != frames
                        or indata.ndim != 2 or indata.shape[1] < 1 or outdata.ndim != 2):
                    callback_fault = "invalid_capture"
                    raise sd.CallbackAbort
                count = min(frames, total - position)
                recording[position:position + count] = indata[:count, 0]
                outdata[:count, :] = output[position:position + count, None]
                position += count
                self._progress = position / total
                if position == total:
                    raise sd.CallbackStop

            stream = sd.Stream(device=(selected, output_device), channels=(1, channels),
                               samplerate=rate, dtype="float32", blocksize=0, latency="low",
                               callback=callback, finished_callback=finished.set)
            with self._lock:
                self._stream = stream
            try:
                incoming, outgoing = stream.latency
                for key, value in (("driver_input_latency_ms", incoming),
                                   ("driver_output_latency_ms", outgoing)):
                    numeric = _finite_number(value)
                    if numeric is not None and 0 <= numeric <= 10:
                        report[key] = numeric * 1000
            except (AttributeError, TypeError, ValueError):
                pass
            self._check_cancelled()
            self._state("measuring")
            deadline = time.monotonic() + DURATION_SECONDS + TIMEOUT_PADDING_SECONDS
            stream.start()
            while not finished.wait(0.025):
                self._check_cancelled()
                if time.monotonic() > deadline:
                    raise _ProbeError("timeout")
                if not stream.active:
                    raise _ProbeError(callback_fault or "stream_error")
            self._check_cancelled()
            if callback_fault:
                raise _ProbeError(callback_fault)
            if position != total:
                raise _ProbeError("invalid_capture")
            self._state("analysing")
            analysis = analyse_roundtrip(recording, rate)
            self._check_cancelled()
            report.update({key: value for key, value in analysis.items()
                           if key not in {"input_device", "output_device",
                                          "driver_input_latency_ms", "driver_output_latency_ms"}})
        except _ProbeError as exc:
            reason = str(exc)
            report.update(status="cancelled" if reason == "cancelled" else "failed", reason=reason)
        except Exception:
            report.update(status="failed", reason="device_error")
        finally:
            closed = stream is None or _close_stream(stream)
            # No recorded content is kept on the instance, in exceptions or
            # exported reports. Also erase buffers held by callback closures.
            for buffer in (recording, output, pulse):
                if buffer is not None:
                    buffer.fill(0)
            if not closed:
                self._cancel.set()  # Any surviving callback now emits silence and aborts.
                report.update(status="failed", reason="cleanup_failed", roundtrip_ms=None,
                              spread_ms=None, pulse_delays_ms=[], correlations=[])
            elif self._cancel.is_set():
                report.update(status="cancelled", reason="cancelled", roundtrip_ms=None,
                              spread_ms=None, pulse_delays_ms=[], correlations=[])
            report["stream_closed"] = closed
            with self._lock:
                self._stream = None if closed else stream
                self._safe_to_resume = closed
                self._result = report
                self._status = report["status"]
