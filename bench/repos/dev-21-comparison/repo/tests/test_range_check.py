from range_check import in_range


def test_in_range():
    assert in_range(5, 1, 10) is True


def test_in_range_of_the_lower_bound():
    assert in_range(1, 1, 10) is True
