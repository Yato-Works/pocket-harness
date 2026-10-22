"""Ranking."""


def by_score(entries):
    """Return the entries ordered by their score."""
    return sorted(entries, key=lambda entry: entry["score"])
