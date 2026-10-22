"""Number helpers."""


def clamp(value, low, high):
    """Return the value limited to the range."""
    if value < low:
        return low
    if value > high:
        return high
    return value
