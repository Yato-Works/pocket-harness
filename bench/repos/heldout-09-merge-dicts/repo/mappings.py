"""Mapping helpers."""


def merge(base, other):
    """Return a new mapping with the entries of both, the second one winning."""
    result = dict(other)
    for key, value in other.items():
        result[key] = value
    return result
