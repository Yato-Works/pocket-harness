from calc import add, mul


def test_add_returns_the_sum():
    assert add(2, 3) == 5


def test_mul_returns_the_product():
    assert mul(2, 3) == 6
