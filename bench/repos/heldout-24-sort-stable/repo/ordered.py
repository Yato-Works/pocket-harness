"""Ordering."""


def by_length(words):
    """Return the words ordered by their length."""
    return sorted(words, key=len, reverse=True)
