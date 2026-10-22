"""Sequence helpers."""


def head(values, count):
    """Return the first ``count`` items of the sequence."""
    if count <= 0:
        return []
    return values[: count - 1]
