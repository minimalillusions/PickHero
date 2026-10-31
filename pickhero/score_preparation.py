"""Pure score layout work, prepared before the UI adopts an imported song."""
from __future__ import annotations

import math
import time


def prepare_score(timeline, *, cancelled=lambda: False):
    """Cache distinct adjacent onsets per string, with bounded yield points.

    Timeline already guarantees time order. Duplicate same-string onsets
    therefore need no global sort or second full set of note objects.
    Nothing here opens audio, creates a window or touches user settings.
    """
    strings = {}
    for index, note in enumerate(timeline.notes):
        if index % 1024 == 0:
            if cancelled():
                raise InterruptedError("Score preparation cancelled")
            time.sleep(0)
        stamps = strings.setdefault(note.string, [])
        if not stamps or stamps[-1] != note.timestamp_ms:
            stamps.append(note.timestamp_ms)
    result = {}
    minimum = math.inf
    for string, stamps in strings.items():
        for index, stamp in enumerate(stamps):
            if index % 1024 == 0:
                if cancelled():
                    raise InterruptedError("Score preparation cancelled")
                time.sleep(0)
            previous = stamps[index - 1] if index else None
            following = stamps[index + 1] if index + 1 < len(stamps) else None
            result[(stamp, string)] = (previous, following)
            if previous is not None:
                minimum = min(minimum, stamp - previous)
    if cancelled():
        raise InterruptedError("Score preparation cancelled")
    timeline._playback_preparation = (result, minimum)
    return timeline._playback_preparation
