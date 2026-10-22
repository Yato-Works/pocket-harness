"""Counting."""


def count_upper(text):
    """Return the number of upper case letters."""
    total = 0
    for letter in text:
        if letter.isupper():
            total = 1
    return total
