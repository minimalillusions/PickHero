"""Explicit local practice takes. No device access and no network access.

The callback only copies into a bounded queue; the writer owns all file I/O.
WAV contains channel zero before the detector/gate, at the negotiated rate.
Block timestamps preserve gaps instead of silently claiming a continuous clock.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import threading
import time
from uuid import uuid4
import wave

import numpy as np

MAX_SECONDS = 180
MAX_PACKETS = 128


def write_json(path, value):
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(payload, encoding='utf-8')
    temporary.replace(path)


class TestRecording:
    __test__ = False

    def __init__(self, root: Path, sample_rate: int, metadata: dict):
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 192000:
            raise ValueError('Unsupported recording rate')
        self.rate = sample_rate
        self.limit = sample_rate * MAX_SECONDS
        self.folder = Path(root) / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:12])
        self.folder.mkdir(parents=True, exist_ok=False)
        # Validate and detach the caller's metadata before starting a thread.
        self.metadata = json.loads(json.dumps(metadata, allow_nan=False))
        self.frames = 0
        self.problem = None
        self.error = None
        self.accepting = True
        self.done = threading.Event()
        self._stop = threading.Event()
        self._packets = queue.Queue(maxsize=MAX_PACKETS)
        self._submitted = 0
        self._finish_context = None
        self.started_at = time.perf_counter()
        self._thread = threading.Thread(target=self._write, name='PickHero-test-recording', daemon=True)
        write_json(self.folder / 'session.json', self._report('recording'))
        self._thread.start()

    def _report(self, status):
        return dict(schema_version=1, status=status, sample_rate=self.rate,
                    channels=1, input_channel=0, encoding='PCM_S32LE', frames=self.frames,
                    duration_seconds=self.frames / self.rate, maximum_seconds=MAX_SECONDS,
                    problem=self.problem, error=self.error, context=self.metadata,
                    finish=self._finish_context,
                    limits=['Score notes are intended notes, not independently annotated playing.',
                            'WAV stores received blocks; use events.jsonl for capture clock gaps.',
                            'No normalization, denoising, noise gate or backing mix is applied.',
                            'Driver/OS processing before the app is not controlled here.',
                            'PCM conversion rounds to 32-bit signed integers.'])

    def _put(self, packet):
        if not self.accepting:
            return False
        try:
            self._packets.put_nowait(packet)
            return True
        except queue.Full:
            self.problem = 'queue_full'
            self.accepting = False
            return False

    def audio(self, mono, capture_ms, status=''):
        """Called by the existing input callback; never waits for disk."""
        if not self.accepting:
            return
        if not np.isfinite(capture_ms):
            self.problem, self.accepting = 'invalid_timestamp', False
            return
        if mono.ndim != 1 or len(mono) > self.rate // 4:
            self.problem, self.accepting = 'oversized_block', False
            return
        if not np.isfinite(mono).all() or np.any(np.abs(mono) > 1):
            self.problem, self.accepting = 'invalid_audio', False
            return
        count = min(len(mono), self.limit - self._submitted)
        if count and self._put(('audio', mono[:count].copy(), float(capture_ms), str(status)[:200])):
            self._submitted += count
        if self._submitted >= self.limit:
            self.problem, self.accepting = 'duration_limit', False

    def event(self, kind, **values):
        return self._put(('event', dict(kind=kind, **values)))

    def finish(self, context=None, timeout=2.0):
        if not self._stop.is_set():
            self.accepting = False
            self._finish_context = json.loads(json.dumps(context, allow_nan=False))
            self._stop.set()
        self._thread.join(timeout)
        return self.done.is_set()

    def _write(self):
        digest = hashlib.sha256()
        try:
            with (self.folder / 'input.wav').open('xb') as raw, \
                    wave.open(raw, 'wb') as wav, \
                    (self.folder / 'events.jsonl').open('x', encoding='utf-8') as events:
                wav.setparams((1, 4, self.rate, 0, 'NONE', 'not compressed'))
                while not self._stop.is_set() or not self._packets.empty():
                    try:
                        packet = self._packets.get(timeout=.05)
                    except queue.Empty:
                        continue
                    if packet[0] == 'audio':
                        _, mono, capture_ms, status = packet
                        values = np.rint(mono.astype(np.float64) * 2147483648)
                        pcm = np.clip(values, -2147483648, 2147483647).astype('<i4').tobytes()
                        wav.writeframesraw(pcm)
                        digest.update(pcm)
                        row = dict(kind='audio_block', wav_frame=self.frames,
                                   frames=len(mono), capture_ms=capture_ms, input_status=status)
                        self.frames += len(mono)
                    else:
                        row = packet[1]
                    events.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
            report = self._report('partial' if self.problem else 'saved')
            if not self.frames:
                self.problem = self.problem or 'no_audio'
                report = self._report('partial')
            report['pcm_sha256'] = digest.hexdigest()
            write_json(self.folder / 'session.json', report)
        except Exception as exc:
            self.error = type(exc).__name__ + ': ' + str(exc)[:300]
            try:
                write_json(self.folder / 'session.json', self._report('failed'))
            except OSError:
                pass
        finally:
            self.accepting = False
            self.done.set()
