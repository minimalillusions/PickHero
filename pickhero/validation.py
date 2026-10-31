"""Small validators for persisted values; invalid fields keep their defaults."""
import math


def number(value, minimum: float, maximum: float) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and minimum <= value <= maximum and math.isfinite(value))


def integer(value, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def text(value, maximum: int = 4096) -> bool:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        return False
    # JSON accepts isolated UTF-16 surrogate escapes. Filesystem/UI encoders
    # and SDL's font renderer do not, so reject them at the persisted boundary.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
