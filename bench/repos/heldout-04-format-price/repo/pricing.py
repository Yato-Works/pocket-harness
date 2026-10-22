"""Price formatting."""


def format_price(cents, currency="EUR"):
    """Format a price given in cents."""
    return f"{currency} {cents / 100:.0f}"

