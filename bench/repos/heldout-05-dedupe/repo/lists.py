"""Sequence helpers."""


def unique(values):
    """Return the values in order, without the repetitions."""
    return list(dict.fromkeys(values))
