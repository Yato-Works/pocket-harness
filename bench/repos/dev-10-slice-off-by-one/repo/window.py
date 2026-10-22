"""Sliding window helpers."""


def window(values, size):
    """Return the first `size` values."""
    if size <= 0:
        return []
    return values[: size - 1]
