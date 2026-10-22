"""Labels."""


def with_prefix(values, prefix):
    """Return the values with the prefix added."""
    for index in range(len(values)):
        values[index] = prefix + values[index]
    return values
