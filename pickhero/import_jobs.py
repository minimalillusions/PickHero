"""A bounded child process with explicit lifetime and nonblocking polling."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from pickhero.bounded_io import read_bounded
from pickhero.import_worker import (
    MAX_RESULT_BYTES,
    DecodeCancelled,
    decode_song,
    read_result,
    write_json,
)


def _resume_windows_process(process):
    """Resume its primary thread only after the child belongs to our Job.

    Popen closes CreateProcess's thread handle. Toolhelp gives us that thread
    back using documented Win32 APIs; the suspended process cannot yet spawn
    PyInstaller's interpreter child outside the Job.
    """
    from ctypes import wintypes
    class ThreadEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("thread", wintypes.DWORD), ("owner", wintypes.DWORD),
                    ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                    ("flags", wintypes.DWORD)]
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenThread.restype = wintypes.HANDLE
    api.ResumeThread.argtypes = [wintypes.HANDLE]
    api.ResumeThread.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise OSError("Could not find suspended score worker")
    try:
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        available = api.Thread32First(snapshot, ctypes.byref(entry))
        while available:
            if entry.owner == process.pid:
                thread = api.OpenThread(0x2, False, entry.thread)  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise OSError("Could not open suspended score thread")
                try:
                    if api.ResumeThread(thread) == 0xFFFFFFFF:
                        raise OSError("Could not resume constrained score worker")
                    return
                finally:
                    api.CloseHandle(thread)
            available = api.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        api.CloseHandle(snapshot)
    raise OSError("Suspended score worker has no primary thread")


class _WindowsJob:
    """Kill descendants on close and bound aggregate committed memory."""
    def __init__(self, process):
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("per_process", ctypes.c_int64), ("per_job", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                        ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read", "write", "other")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api = api
        self.handle = api.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000 | 0x200 | 0x8  # kill on close, job memory, active processes
        limits.basic.active = 4  # frozen bootloader plus interpreter descendants
        limits.job_memory = 768 * 1024 * 1024
        if (not self.handle or not api.SetInformationJobObject(self.handle, 9,
                ctypes.byref(limits), ctypes.sizeof(limits)) or not
                api.AssignProcessToJobObject(self.handle, int(process._handle))):
            self.close()
            raise OSError("Could not constrain score worker")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class ImportJob:
    """Poll until done; cancel also terminates the real worker, not just its UI."""
    def __init__(self, operation, path, instrument, *, cache_file=None,
                 track_index=None, content_id=None, timeout=None, executable=None):
        self.operation = operation
        self.status = {"current": 0, "total": 0, "name": Path(path).name}
        self.result = None
        self.prepared_song = None
        self.error = None
        self.failure_type = None
        self.done = False
        self.cancelled = False
        self._process = None
        self._job = None
        self._temporary = None
        self.directory = None
        self._decode_thread = None
        self._decode_cancel = threading.Event()
        self._decode_ready = threading.Event()
        self._decoded_result = None
        self._decoded_song = None
        self._decode_error = None
        self._next_status_poll = 0.0
        self._deadline = time.monotonic() + (timeout if timeout is not None else
                                            180 if operation == "scan" else 40)
        try:
            self._temporary = tempfile.TemporaryDirectory(prefix="pickhero-import-")
            self.directory = Path(self._temporary.name)
            request = {"operation": operation, "path": str(Path(path).resolve()),
                       "instrument": instrument, "track_index": track_index, "content_id": content_id}
            if cache_file is not None:
                request["cache_file"] = str(Path(cache_file).resolve())
            write_json(self.directory / "request.json", request)
            command = ([str(executable)] if executable is not None else
                       [sys.executable] if getattr(sys, "frozen", False)
                       else [sys.executable, "-m", "pickhero"])
            command += ["--import-worker", str(self.directory)]
            self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0,
                cwd=str(Path(__file__).resolve().parent.parent))
            if os.name == "nt":
                self._job = _WindowsJob(self._process)
            (self.directory / "start").touch()
            if os.name == "nt":
                _resume_windows_process(self._process)
        except (OSError, ValueError):
            self.error = "start"
            self.done = True
            self._cleanup()

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    def poll(self):
        if self.done:
            return True
        if self._decode_thread is not None:
            if time.monotonic() > self._deadline:
                self.error = "timeout"
                self._decode_cancel.set()
            if not self._decode_ready.is_set():
                return False
            self.done = True
            if not self.error and not self.cancelled:
                self.result, self.prepared_song = self._decoded_result, self._decoded_song
                self.error = self._decode_error
            return True
        try:
            if time.monotonic() >= self._next_status_poll:
                self._next_status_poll = time.monotonic() + .05
                with (self.directory / "status.json").open("rb") as stream:
                    value = json.loads(read_bounded(stream, 16384))
                    if (isinstance(value, dict) and type(value.get("current")) is int
                            and type(value.get("total")) is int
                            and 0 <= value["current"] <= value["total"] <= 10000):
                        self.status = value
        except (OSError, ValueError):
            pass
        code = self._process.poll()
        if code is None and time.monotonic() <= self._deadline:
            return False
        if code is None:
            self.done = True
            self.error = "timeout"
            self._cleanup()
            return True
        # The process has exited. Decoding and Timeline construction still
        # belong outside the frame loop, and remain explicitly cancellable.
        if self._job is not None:
            self._job.close()
            self._job = None
        self.status = dict(self.status, phase="prepare")
        self._decode_thread = threading.Thread(target=self._decode_result,
                                               name="pickhero-import-decode", daemon=True)
        self._decode_thread.start()
        return False

    def _decode_result(self):
        try:
            packets = self.directory / "result.jsonl"
            if packets.exists():
                value, prepared = read_result(packets, cancelled=self._decode_cancel.is_set)
            else:
                # Error replies are small. This also permits a separately
                # selected older executable in the process regression tool.
                with (self.directory / "result.json").open("rb") as stream:
                    raw = json.loads(read_bounded(stream, MAX_RESULT_BYTES))
                if not isinstance(raw, dict):
                    raise ValueError("Malformed worker response")
                if raw.get("ok") is not True or not isinstance(raw.get("value"), dict):
                    self.failure_type = raw.get("error")
                    raise ValueError("Import failed")
                value = raw["value"]
                prepared = decode_song(value.pop("song")) if "song" in value else None
            if not self._decode_cancel.is_set():
                if prepared is not None:
                    from pickhero.score_preparation import prepare_score
                    prepare_score(prepared[0], cancelled=self._decode_cancel.is_set)
                self._decoded_result, self._decoded_song = value, prepared
        except DecodeCancelled:
            pass
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            self._decode_error = "import"
            self.failure_type = self.failure_type or type(exc).__name__
        finally:
            self._cleanup_directory()
            self._decode_ready.set()

    def cancel(self):
        if not self.done:
            self.cancelled = True
            self.done = True
        self._decode_cancel.set()
        self._cleanup()

    close = cancel

    def _cleanup(self):
        if self._job is not None:
            self._job.close()
            self._job = None
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.kill()
            except OSError:
                pass  # A simultaneous Job termination may have won the race.
            try:
                self._process.wait(timeout=.5)
            except (OSError, subprocess.TimeoutExpired):
                self.error = self.error or "cleanup"
        if self._decode_thread is not None and self._decode_thread.is_alive():
            # Packet checks end the decoder shortly; let its finally block
            # remove files after it has closed the read handle.
            self._decode_thread.join(timeout=.02)
            return
        self._cleanup_directory()

    def _cleanup_directory(self):
        if self._temporary is not None:
            try:
                self._temporary.cleanup()
            except OSError:
                # Antivirus/indexing can hold a just-closed file briefly.
                # Retain this private directory for its finalizer rather than
                # losing the UI or aborting the rest of application cleanup.
                self.error = self.error or "cleanup"
