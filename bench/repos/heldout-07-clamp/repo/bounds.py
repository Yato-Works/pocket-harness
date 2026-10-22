"""Numeric helpers."""


def clamp(value, low, high):
    """Return the value limited to the range."""
    if value < low:
        return high
    if value > high:
        return low
    return value
