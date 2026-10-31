"""Bounded readers stay small for tiny files and detect concurrent growth."""

import tracemalloc

import pytest

from pickhero import jsonfile
from pickhero.bounded_io import FileTooLargeError, READ_CHUNK_BYTES, read_bounded
from pickhero.tabs.import_safety import bounded_file


class ObservedReader:
    def __init__(self, stream, before_read=None):
        self.stream = stream
        self.before_read = before_read
        self.requests = []

    def fileno(self):
        return self.stream.fileno()

    def read(self, count):
        self.requests.append(count)
        if self.before_read is not None:
            callback, self.before_read = self.before_read, None
            callback()
        return self.stream.read(count)


def test_exact_budget_is_accepted_without_a_large_read_request(tmp_path):
    path = tmp_path / "exact.bin"
    contents = b"a" * (READ_CHUNK_BYTES * 2 + 3)
    path.write_bytes(contents)
    with path.open("rb") as stream:
        observed = ObservedReader(stream)
        assert read_bounded(observed, len(contents)) == contents
    assert max(observed.requests) <= READ_CHUNK_BYTES
    assert observed.requests[-1] == 1  # one-byte EOF probe at the budget


def test_oversized_file_is_refused_before_reading_bytes(tmp_path):
    path = tmp_path / "too-large.bin"
    path.write_bytes(b"a" * 10)
    with path.open("rb") as stream:
        observed = ObservedReader(stream)
        with pytest.raises(FileTooLargeError):
            read_bounded(observed, 9)
    assert observed.requests == []


@pytest.mark.parametrize("growth,accepted", [(4, True), (10, False)])
def test_growth_after_fstat_is_counted_and_never_silently_truncated(tmp_path, growth, accepted):
    path = tmp_path / "growing.bin"
    path.write_bytes(b"a")
    def append_after_stat():
        with path.open("ab") as writer:
            writer.write(b"b" * growth)
    with path.open("rb") as stream:
        observed = ObservedReader(stream, append_after_stat)
        if accepted:
            assert read_bounded(observed, 8) == b"a" + b"b" * growth
        else:
            with pytest.raises(FileTooLargeError):
                read_bounded(observed, 8)
    assert max(observed.requests) <= 9


@pytest.mark.parametrize("reader,contents,expected", [
    (bounded_file, b"tiny score", b"tiny score"),
    (jsonfile.read, b'{"value": 1}', {"value": 1}),
])
def test_small_files_do_not_allocate_the_16_or_32_mib_budget(tmp_path, reader, contents, expected):
    path = tmp_path / "tiny.data"
    path.write_bytes(contents)
    tracemalloc.start()
    try:
        assert reader(path) == expected
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # A generous allocation bound, independent of CPU/storage speed. The old
    # stream.read(limit + 1) allocated 16/32 MiB even for a ten-byte file.
    assert peak < 1024 * 1024
