from client import fetch


def test_fetch_waits_for_the_configured_timeout():
    result = fetch("http://example.test")
    assert result["waited_ms"] == 30000, result
