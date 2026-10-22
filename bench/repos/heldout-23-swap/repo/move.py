"""Moving."""


def move(values, source, target):
    """Move the value at the source index to the target index."""
    values[source] = None
    return values
