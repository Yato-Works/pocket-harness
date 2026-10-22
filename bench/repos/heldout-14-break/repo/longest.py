"""Runs."""


def longest_run(values):
    """Return the length of the longest run of equal values."""
    best = 0
    current = 0
    for index, value in enumerate(values):
        if index and values[index - 1] == value:
            current += 1
        else:
            current = 1
        best = current
    return best
