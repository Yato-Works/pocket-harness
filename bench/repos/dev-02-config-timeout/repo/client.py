"""A tiny client that uses the configured timeout."""

from settings import REQUEST_TIMEOUT_MS


def fetch(url):
    """Pretend to fetch a URL and report how long we waited."""
    return {"url": url, "waited_ms": REQUEST_TIMEOUT_MS}
