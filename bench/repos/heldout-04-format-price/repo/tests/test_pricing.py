from pricing import format_price


def test_format_price():
    assert format_price(1234) == "EUR 12.34"
