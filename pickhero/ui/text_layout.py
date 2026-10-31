"""Measured text wrapping with a comfortable maximum reading line length."""
from __future__ import annotations

MAX_PROSE_CHARACTERS = 60


def wrap(text, face, width, *, max_chars=MAX_PROSE_CHARACTERS):
    """Keep every word, respecting both the character and pixel limits.

    A single glyph wider than the available space cannot be split further.
    Explicit line breaks are retained, including blank paragraphs. Non-prose
    callers can pass ``max_chars=None`` for purely geometric wrapping.
    """
    width = max(1, width)
    if max_chars is not None and max_chars < 1:
        raise ValueError("max_chars must be positive or None")

    def fits(value):
        return (max_chars is None or len(value) <= max_chars) and face.size(value)[0] <= width

    lines = []
    for paragraph in str(text).splitlines() or [""]:
        line = ""
        for word in paragraph.split():
            trial = f"{line} {word}".strip()
            if fits(trial):
                line = trial
                continue
            if line:
                lines.append(line)
            line = ""
            for char in word:
                if line and not fits(line + char):
                    lines.append(line)
                    line = ""
                line += char
        lines.append(line)
    return lines
