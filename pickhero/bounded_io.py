"""Read local files with a byte budget and small temporary allocations."""

import os
from typing import BinaryIO

READ_CHUNK_BYTES = 64 * 1024


class FileTooLargeError(ValueError):
    """The opened file exceeds the caller's byte budget."""


def read_bounded(stream: BinaryIO, limit: int) -> bytes:
    """Read to EOF, detecting growth without allocating the whole limit first.

    fstat checks the opened file, avoiding a race between path.stat and open.
    The actual bytes are still counted because another process can grow the
    file after that check. Even the final EOF probe stays within limit + 1.
    """
    if os.fstat(stream.fileno()).st_size > limit:
        raise FileTooLargeError(f"File exceeds {limit} byte limit")
    chunks = []
    total = 0
    while True:
        chunk = stream.read(min(READ_CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise FileTooLargeError(f"File exceeds {limit} byte limit")
        chunks.append(chunk)
