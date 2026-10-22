"""Pairs."""


def consecutive(values):
    """Return the pairs of neighbouring values."""
    result = []
    for index in range(len(values) - 2):
        result.append((values[index], values[index + 1]))
    return result
