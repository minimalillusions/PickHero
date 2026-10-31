"""Audio input capture using sounddevice.

Runs a sounddevice InputStream that feeds audio buffers to the pitch detector.
Detected notes are pushed to a thread-safe queue for consumption by the main thread.
"""

import math
import queue
import time
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from pickhero.audio.detector import PitchDetector, DetectedNote, MAX_ANALYSIS_RATE
from pickhero.config import Config, log_error
from pickhero.instruments import Instrument
from pickhero.timing_diagnostics import TimingDiagnostics


@dataclass
class TimestampedNote:
    """A detected note with a timestamp (ms from session start)."""
    note: DetectedNote
    timestamp_ms: float
    queued_at_ns: int | None = None


class AudioCapture:
    """Captures audio from an input device and runs pitch detection.

    Detected notes are pushed to `note_queue` for consumption by other threads.
    The sounddevice callback runs in a separate thread automatically.
    """

    def __init__(self, config: Config | None = None, *, instrument: Instrument | None = None):
        if config is None:
            config = Config()
        self.config = config
        ac = config.audio

        calibration = getattr(config, 'calibration', None) or None
        selected = config.get_instrument()
        self.instrument = instrument or selected
        # Calibration is per instrument. Applying a guitar's stored open-string
        # frequencies to a bass ukulele would corrupt octave correction, so an
        # calibration must agree with the strings actually being played.
        self._calibration = None
        if isinstance(calibration, dict) and calibration.get("instrument") in (None, config.instrument):
            strings = calibration.get("strings", {})
            if isinstance(strings, dict) and strings:
                expected = {str(string): midi for string, midi in self.instrument.tuning.items()}
                matches = all(isinstance(value, dict) and string in expected
                              and value.get("midi_note") == expected[string]
                              for string, value in strings.items())
                # Partial legacy calibrations still work for the configured
                # tuning. A different tuning needs a complete matching set;
                # unchanged upper strings cannot certify a retuned low string.
                complete = set(strings) == set(expected)
                if matches and (self.instrument.tuning == selected.tuning or complete):
                    self._calibration = calibration
        # Anything left as None in AudioConfig follows the instrument profile.
        self.sample_rate = ac.sample_rate
        self.device_index = ac.device_index
        self.detector = self._make_detector(min(ac.sample_rate, MAX_ANALYSIS_RATE))
        self.note_queue: queue.Queue[TimestampedNote] = queue.Queue()
        self._timing = TimingDiagnostics()
        self._stream: sd.InputStream | None = None
        self._tail = np.zeros(0, dtype=np.float32)
        self._tail_start_ms: float = 0.0
        self._next_input_ms: float | None = None
        self._portaudio_clock_offset: float | None = None
        self._start_time: float = 0.0
        self._signal_db: float = -120.0
        self._tuner_freq: float = 0.0
        self._tuner_confidence: float = 0.0
        # Used only when the host API supplies no usable ADC timestamps.
        self._input_latency_ms: float = 0.0
        self._timing_clock_source: str = "not_started"
        self._callback_frames_min: int = 0
        self._callback_frames_max: int = 0
        self._callback_frames_last: int = 0
        self._stream_start_duration_ms: float | None = None
        self._stream_activation_duration_ms: float | None = None
        self.dropped_blocks: int = 0
        self.last_status: str = ""
        self._last_callback_time: float | None = None
        self._last_overflow_time: float | None = None
        self._last_clipping_time: float | None = None
        self._clipped_duration: float = 0.0
        self._health_since: float | None = None
        self.test_recording = None  # attached only by an explicit practice take

    def _make_detector(self, sample_rate: int) -> PitchDetector:
        ac = self.config.audio
        buffer_size = ac.buf_size
        if (buffer_size is not None
                and self.instrument.buf_size > self.config.get_instrument().buf_size):
            # Retaining a short standard-tuning override would put low B/D
            # outside the usable analysis window. Grow only the live window;
            # the saved override and all measured thresholds stay unchanged.
            minimum = self.instrument.buf_size * sample_rate / self.instrument.sample_rate
            buffer_size = max(buffer_size, int(math.ceil(minimum / 64.0)) * 64)
        return PitchDetector(
            buf_size=buffer_size,
            hop_size=ac.hop_size,
            sample_rate=sample_rate,
            confidence_threshold=ac.confidence_threshold,
            onset_threshold=ac.onset_threshold,
            noise_gate_db=ac.noise_gate_db,
            calibration=self._calibration or None,
            instrument=self.instrument,
        )

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status):
        """Sounddevice callback - runs in audio thread."""
        callback_time = time.perf_counter()
        diagnostic_start = time.perf_counter_ns()
        self._last_callback_time = callback_time
        input_overflow = bool(getattr(status, "input_overflow", False))
        if input_overflow:
            self._last_overflow_time = callback_time
        if status:
            # Keep the received audio. An input overflow invalidates history
            # before this block, not the new samples in the block itself.
            self.dropped_blocks += 1
            self.last_status = str(status)

        # indata shape: (frames, channels) - take first channel
        # Validate the same float32 representation consumed by the detector.
        # The live stream already uses float32; conversion overflow in an
        # alternate caller is still missing input, not a measured silence.
        with np.errstate(over="ignore", invalid="ignore"):
            mono = indata[:, 0].astype(np.float32, copy=True)
        invalid_input = not np.isfinite(mono).all()
        if invalid_input:
            # Reuse the lost-input reliability path, but retain the known
            # sample clock: unlike a driver overflow, no duration is unknown.
            self._last_overflow_time = callback_time
            if not status:
                self.dropped_blocks += 1
            self.last_status = (f"{status}; Invalid audio samples" if status
                                else "Invalid audio samples")
        # One isolated peak is normal for a pluck. Flag only sustained hard
        # clipping: at least 1% rail samples in consecutive blocks for 100 ms.
        # This diagnostic never changes the waveform or detector thresholds.
        # Within two 16-bit steps of a digital rail, not merely a loud peak.
        # A clean near-full-scale sine spends >1% above .995 and must remain
        # valid. Requiring rail density excludes that rounded waveform.
        if (not invalid_input and len(mono)
                and np.count_nonzero(np.abs(mono) >= 0.99995) >= max(1, len(mono) * 0.01)):
            self._clipped_duration += len(mono) / self.detector.sample_rate
            if self._clipped_duration >= 0.1:
                self._last_clipping_time = callback_time
        else:
            self._clipped_duration = 0.0
        self._callback_frames_last = len(mono)
        self._callback_frames_min = (min(self._callback_frames_min, len(mono))
                                     if self._callback_frames_min else len(mono))
        self._callback_frames_max = max(self._callback_frames_max, len(mono))
        sample_ms = 1000.0 / self.detector.sample_rate
        block_start_ms = self._block_timestamp_ms(
            time_info, len(mono), callback_time, input_overflow=input_overflow)
        recording = self.test_recording
        if recording is not None:
            recording.audio(mono, block_start_ms, str(status) if status else '')
        self._timing.record_clock(self._timing_clock_source)
        self._timing.record("callback_budget_ms", len(mono) * sample_ms)
        self._timing.record("capture_delivery_ms",
                            (callback_time - self._start_time) * 1000.0 - block_start_ms)
        previous_end = self._next_input_ms
        if (input_overflow or (previous_end is not None
                and abs(block_start_ms - previous_end) > max(1.0, sample_ms * 2))):
            # An overflow can leave a real gap in the capture clock. Do not
            # join its two sides into one analysis window or keep onset ages
            # from before the missing audio.
            self._tail = np.zeros(0, dtype=np.float32)
            self.detector.reset()
        self._next_input_ms = block_start_ms + len(mono) * sample_ms

        # Process in hop_size chunks. The block may hold several hops, and a
        # leftover tail shorter than one hop is carried into the next block
        # rather than silently discarded.
        hop = self.detector.hop_size
        analysis_start_ms = block_start_ms
        if self._tail.size:
            analysis_start_ms = self._tail_start_ms
            mono = np.concatenate([self._tail, mono])
        n_full = (len(mono) // hop) * hop
        self._tail = mono[n_full:].copy()
        self._tail_start_ms = analysis_start_ms + n_full * sample_ms
        mono = mono[:n_full]
        for i in range(0, len(mono) - hop + 1, hop):
            chunk = mono[i:i + hop]
            if invalid_input and not np.isfinite(chunk).all():
                self._reset_invalid_audio()
                continue
            result = self.detector.process(chunk)
            self._signal_db = self.detector.last_signal_db
            self._tuner_freq = self.detector.last_freq
            self._tuner_confidence = self.detector.last_confidence
            if result is not None:
                # A hop describes audio through its final sample, regardless
                # of how many other hops arrived in this callback or how long
                # detection took to process them.
                elapsed_ms = analysis_start_ms + (i + hop) * sample_ms
                # The pitch may be read several hops after the string was
                # actually struck, so date the event to the strike, not to the
                # moment the pitch became certain. Otherwise every note looks
                # late by up to one analysis window.
                elapsed_ms -= result.onset_age_hops * (hop / self.detector.sample_rate) * 1000.0
                # ...and for the onset detector's own measured lag.
                elapsed_ms -= self.detector.onset_latency_ms
                queued_at = time.perf_counter_ns()
                self.note_queue.put(TimestampedNote(result, elapsed_ms, queued_at))
                if recording is not None:
                    recording.event('detected', capture_ms=float(elapsed_ms),
                        midi=int(result.midi_note), frequency=float(result.frequency),
                        confidence=float(result.confidence), onset=bool(result.is_onset),
                        timing_uncertain=bool(result.timing_uncertain))
                if result.is_onset:
                    self._timing.record("onset_correction_ms",
                        result.onset_age_hops * hop * sample_ms + self.detector.onset_latency_ms)
                    self._timing.record("note_report_lag_ms",
                        (callback_time - self._start_time) * 1000.0
                        + (queued_at - diagnostic_start) / 1_000_000 - elapsed_ms)
        if invalid_input and not np.isfinite(self._tail).all():
            # Even a partial bad hop ends continuity immediately. Never
            # carry it into the next callback or replace it with zeroes.
            self._tail = np.zeros(0, dtype=np.float32)
            self._reset_invalid_audio()
        self._timing.record("callback_processing_ms",
                            (time.perf_counter_ns() - diagnostic_start) / 1_000_000)

    def _reset_invalid_audio(self) -> None:
        self.detector.reset()
        self._signal_db = -120.0
        self._tuner_freq = 0.0
        self._tuner_confidence = 0.0

    def _block_timestamp_ms(self, time_info, frames: int, callback_time: float,
                            *, input_overflow: bool = False) -> float:
        """First sample's capture time on the session's perf_counter clock.

        PortAudio's ADC clock already includes input latency. Map its clock
        once per stream, then let sample positions carry the timing; callback
        scheduling and detector CPU time must not move an earlier strike.
        """
        try:
            adc_time = float(time_info.inputBufferAdcTime)
            current_time = float(time_info.currentTime)
            valid = (math.isfinite(adc_time) and math.isfinite(current_time)
                     and adc_time > 0.0 and current_time >= adc_time)
        except (AttributeError, TypeError, ValueError):
            valid = False
        if valid:
            self._timing_clock_source = "adc"
            if self._portaudio_clock_offset is None:
                self._portaudio_clock_offset = callback_time - current_time
            return (adc_time + self._portaudio_clock_offset - self._start_time) * 1000.0
        # Some host APIs omit ADC timestamps. Anchor the first block using
        # the reported latency, then advance by samples rather than callback
        # arrival time. After lost input, sample counting cannot recover the
        # gap: reanchor this block, then continue counting from its new time.
        self._timing_clock_source = "fallback"
        if self._next_input_ms is not None and not input_overflow:
            return self._next_input_ms
        # PortAudio's inputLatency already measures callback time back to the
        # FIRST input sample. Subtracting the buffer duration again makes all
        # events one host buffer early (which can be hundreds of ms on MME).
        return ((callback_time - self._start_time) * 1000.0
                - self._input_latency_ms)

    def start(self):
        """Start audio capture.

        Any stream still open is closed first. This used to overwrite
        self._stream without closing it, so a second start left the first
        stream running and feeding the same detector from a second thread:
        two readings per hop, doubled onsets, and a device that stayed busy
        until the app was killed. The screens call start() on play, on seek
        and on every loop, so this happened routinely.
        """
        operation_started = time.perf_counter()
        self.stop()
        ac = self.config.audio
        self._timing.reset()
        self.detector.reset()

        # Drain any leftover notes
        while not self.note_queue.empty():
            try:
                self.note_queue.get_nowait()
            except queue.Empty:
                break

        self._tail = np.zeros(0, dtype=np.float32)
        self._tail_start_ms = 0.0
        self._next_input_ms = None
        self._portaudio_clock_offset = None
        self._input_latency_ms = 0.0
        self._timing_clock_source = "not_started"
        self._callback_frames_min = 0
        self._callback_frames_max = 0
        self._callback_frames_last = 0
        self._stream_start_duration_ms = None
        self._stream_activation_duration_ms = None
        self._signal_db = -120.0
        self._tuner_freq = 0.0
        self._tuner_confidence = 0.0
        self.dropped_blocks = 0
        self.last_status = ""
        self._last_callback_time = None
        self._last_overflow_time = None
        self._last_clipping_time = None
        self._clipped_duration = 0.0
        self._health_since = None

        device = ac.device_index
        if device is None:
            # Windows exposes the same microphone through several host APIs
            # with wildly different latency. Left to the system default this
            # lands on MME at 90 to 180 ms; the WASAPI path to the same device
            # is 3 to 10. Pick by measurement, not by whatever is first.
            device = best_input_device()
        rate = negotiate_samplerate(device, ac.sample_rate)
        if rate != self.detector.sample_rate:
            self.detector = self._make_detector(rate)
        self.sample_rate = rate
        self.device_index = device

        # blocksize=0 lets PortAudio pick a size the driver is comfortable
        # with. Forcing 512 on MME provokes constant input overflows. The hop
        # loop above already handles a block holding many hops.
        self._stream = sd.InputStream(
            device=device,
            channels=1,
            samplerate=rate,
            blocksize=0,
            dtype="float32",
            latency="low",
            callback=self._audio_callback,
        )
        try:
            self._input_latency_ms = float(self._stream.latency) * 1000.0
            if not math.isfinite(self._input_latency_ms) or self._input_latency_ms < 0:
                self._input_latency_ms = 0.0
        except (AttributeError, TypeError, ValueError):
            self._input_latency_ms = 0.0
        self._start_time = time.perf_counter()
        try:
            self._stream.start()
        except Exception:
            # A constructed stream can own the microphone even when its
            # start fails. Release it before the UI offers another attempt.
            self.stop()
            raise
        finally:
            finished = time.perf_counter()
            self._stream_activation_duration_ms = (finished - self._start_time) * 1000.0
            self._stream_start_duration_ms = (finished - operation_started) * 1000.0

    def stop(self):
        """Stop audio capture."""
        if self._stream is not None:
            stream = self._stream
            self._stream = None
            try:
                stream.stop()
            except Exception as exc:
                log_error(f"Audio stop failed: {exc}")
            finally:
                try:
                    stream.close()
                except Exception as exc:
                    log_error(f"Audio close failed: {exc}")

    def set_noise_gate_db(self, db: float) -> None:
        """Update the noise gate threshold on the detector.

        Thread-safe: single float attribute write is atomic under the GIL.
        """
        self.detector.set_noise_gate_db(db)

    def capture_elapsed_ms(self) -> float:
        """Milliseconds of wall clock since start(), on the same clock the
        detected notes are stamped with. 0.0 while stopped."""
        if self._stream is None:
            return 0.0
        return (time.perf_counter() - self._start_time) * 1000.0

    def timing_diagnostics(self) -> dict:
        """Timing metadata for the session log; never records audio samples."""
        return {
            "clock_source": self._timing_clock_source,
            "callback_frames_min": self._callback_frames_min,
            "callback_frames_max": self._callback_frames_max,
            "callback_frames_last": self._callback_frames_last,
            "input_latency_ms": self._input_latency_ms,
            "stream_start_duration_ms": self._stream_start_duration_ms,
            "stream_activation_duration_ms": self._stream_activation_duration_ms,
            "sample_rate": self.detector.sample_rate,
            "hop_size": self.detector.hop_size,
            # The effective input settings matter as much as the clocks when
            # investigating lost notes. No waveform is recorded here.
            "device_index": self.device_index,
            "noise_gate_db": getattr(self.detector, "noise_gate_db", None),
            "dropped_blocks": self.dropped_blocks,
            "input_status": self.last_status,
            "health_issue": self.health_issue(),
        }

    def diagnostic_snapshot(self) -> dict:
        """Bounded software measurements, separate from driver-reported latency."""
        return self._timing.snapshot(self.timing_diagnostics())

    def set_health_window(self, start_ms: float) -> None:
        """Observe faults from a scored boundary on the capture's own clock.

        The caller sets this at capture start and adjusts it if a preroll
        tempo changes. Keeping the boundary fixed after play starts preserves
        events even if a UI frame takes longer than their transient duration.
        """
        if math.isfinite(start_ms):
            self._health_since = self._start_time + max(0.0, start_ms) / 1000.0

    def health_issue(self) -> str | None:
        """Report observable input faults; silence is not an input fault.

        Consumers latch faults per scored attempt. Scored playback sets an
        explicit boundary; live diagnostics otherwise show recent events.
        A stopped capture is intentional; it must not invalidate pause/demo.
        """
        if self._stream is None:
            return None
        now = time.perf_counter()
        grace = max(0.75, self._input_latency_ms / 1000.0 * 3)
        if now - self._start_time >= grace:
            try:
                if not getattr(self._stream, "active", True):
                    return "input_disconnected"
            except Exception:
                return "input_disconnected"
            last = self._last_callback_time
            if last is None or now - last > grace:
                return "input_stalled"
        since = self._health_since if self._health_since is not None else now - 1.0
        if self._last_overflow_time is not None and self._last_overflow_time >= since:
            return "input_overflow"
        if self._last_clipping_time is not None and self._last_clipping_time >= since:
            return "input_clipping"
        return None

    def get_signal_db(self) -> float:
        """Return the latest signal level in dB. Thread-safe (single float read under GIL)."""
        return self._signal_db

    def get_tuner_data(self) -> tuple[float, float]:
        """Return (frequency_hz, confidence) for tuner display. Thread-safe."""
        return (self._tuner_freq, self._tuner_confidence)

    def get_notes(self) -> list[TimestampedNote]:
        """Drain all pending detected notes from the queue (non-blocking)."""
        notes = []
        while True:
            try:
                note = self.note_queue.get_nowait()
                if note.note.is_onset and note.queued_at_ns is not None:
                    self._timing.record("queue_wait_ms", (time.perf_counter_ns() - note.queued_at_ns) / 1_000_000)
                notes.append(note)
            except queue.Empty:
                break
        return notes


def list_audio_devices() -> list[dict]:
    """List available audio input devices.

    Windows offers the same physical microphone through several host APIs, and
    listing only the name shows four identical-looking rows that behave very
    differently. Measured on one RODE NT-USB: MME reports 90 to 180 ms of
    input latency, DirectSound 120 to 240, WDM-KS 10 to 85, and WASAPI 3 to
    10. So the host API and the latency are part of the identity of a device,
    not decoration.
    """
    try:
        hostapis = sd.query_hostapis()
    except Exception:
        hostapis = []
    devices = sd.query_devices()
    inputs = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] <= 0:
            continue
        api = ""
        try:
            api = hostapis[dev["hostapi"]]["name"]
        except (IndexError, KeyError, TypeError):
            pass
        inputs.append({
            "index": i,
            "name": dev["name"],
            "hostapi": api,
            "channels": dev["max_input_channels"],
            "sample_rate": dev["default_samplerate"],
            "latency_ms": float(dev.get("default_low_input_latency", 0.0)) * 1000.0,
        })
    return inputs


# Lower is better. PortAudio reports the numbers; these only break ties when
# two paths report the same latency.
_HOSTAPI_RANK = {
    "Windows WASAPI": 0,
    "Windows WDM-KS": 1,
    "Windows DirectSound": 2,
    "MME": 3,
}


def best_input_device() -> int | None:
    """Pick the input device with the lowest reported latency.

    Returns None when there is nothing to choose, so the caller falls back to
    the system default.
    """
    try:
        devices = list_audio_devices()
    except Exception:
        return None
    if not devices:
        return None
    default_index = None
    try:
        default_index = sd.default.device[0]
    except Exception:
        pass
    default_name = ""
    for dev in devices:
        if dev["index"] == default_index:
            default_name = dev["name"]
            break
    # Prefer another path to the same physical device the user already has
    # selected as the Windows default, rather than switching their microphone.
    same = [d for d in devices if default_name and d["name"] == default_name]
    pool = same or devices
    pool = sorted(pool, key=lambda d: (d["latency_ms"],
                                       _HOSTAPI_RANK.get(d["hostapi"], 9)))
    return pool[0]["index"] if pool else None


def negotiate_samplerate(device: int | None, preferred: int) -> int:
    """Return a sample rate the device actually accepts.

    The app used to ask every device for 44100 Hz. WASAPI on this hardware
    offers only 48000, so the request raised inside a bare except, audio was
    silently switched off, and the app fell back to the MME path with twenty
    times the latency. Asking the device what it supports costs nothing.
    """
    # Honor an explicitly supported preference first. A host's default can
    # be 96/192 kHz even when 48 kHz works, so try the standard capture rates
    # before that default when the requested rate is unavailable.
    candidates = [preferred, 48000, 44100]
    try:
        info = sd.query_devices(device if device is not None else sd.default.device[0])
        candidates.append(int(info["default_samplerate"]))
    except Exception:
        pass
    candidates += [32000, 22050, 16000]
    seen = set()
    for rate in candidates:
        if rate in seen or not 8000 <= rate <= MAX_ANALYSIS_RATE:
            continue
        seen.add(rate)
        try:
            sd.check_input_settings(device=device, samplerate=rate,
                                    channels=1, dtype="float32")
            return int(rate)
        except Exception:
            continue
    raise RuntimeError(
        f"No supported mono input sample rate found (maximum {MAX_ANALYSIS_RATE} Hz).")


def validate_device_index(index: int | None) -> bool:
    """Check if a device index exists and has input channels.

    Returns True for None (system default) or a valid input device index.
    """
    if index is None:
        return True
    try:
        info = sd.query_devices(index)
        return info["max_input_channels"] > 0
    except (sd.PortAudioError, IndexError, ValueError):
        return False


def run_console_demo():
    """Interactive console demo for testing pitch detection.

    Lists audio devices, lets user pick one, then prints detected notes in real-time.
    """
    print("Available audio input devices:")
    print("-" * 50)
    devices = list_audio_devices()
    if not devices:
        print("No audio input devices found!")
        return

    for dev in devices:
        marker = " *" if dev["index"] == sd.default.device[0] else ""
        print(f"  [{dev['index']}] {dev['name']} ({dev['channels']}ch, {dev['sample_rate']:.0f}Hz){marker}")
    print()

    choice = input("Select device index (Enter for default): ").strip()
    config = Config()
    if choice:
        try:
            config.audio.device_index = int(choice)
        except ValueError:
            print("Invalid input, using default device.")

    print()
    print("Listening... play some notes! (Ctrl+C to stop)")
    capture_probe = AudioCapture(config)
    d = capture_probe.detector
    print(f"  Instrument: {d.instrument.describe()}")
    print(f"  Config: method={d.pitch_method}, buf={d.buf_size}, hop={d.hop_size}, "
          f"confidence>={d.confidence_threshold}, noise_gate={d.noise_gate_db}dB, "
          f"range MIDI {d.midi_min}-{d.midi_max}")
    print("-" * 60)
    print(f"{'Time':>8}  {'Note':>5}  {'MIDI':>4}  {'Freq':>8}  {'Conf':>5}  {'Onset'}")
    print("-" * 60)

    capture = AudioCapture(config)
    capture.start()

    last_note = ""
    try:
        while True:
            notes = capture.get_notes()
            for tn in notes:
                n = tn.note
                # Only print on onset or note change to reduce spam
                current = n.name
                if n.is_onset or current != last_note:
                    onset_marker = ">>>" if n.is_onset else "   "
                    print(f"{tn.timestamp_ms:7.0f}ms  {n.name:>5}  {n.midi_note:>4}  "
                          f"{n.frequency:7.1f}Hz  {n.confidence:.2f}  {onset_marker}")
                    last_note = current
            time.sleep(0.01)  # ~100Hz polling, avoid busy-wait
    finally:
        capture.stop()
