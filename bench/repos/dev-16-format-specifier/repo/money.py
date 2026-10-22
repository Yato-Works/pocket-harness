"""Money."""


def format_cents(cents):
    """Return the amount with two decimals."""
    return f"{cents / 100:.0f}"
