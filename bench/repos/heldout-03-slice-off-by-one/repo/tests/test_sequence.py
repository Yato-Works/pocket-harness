from sequence import head


def test_head_returns_the_requested_count():
    assert head([1, 2, 3, 4], 2) == [1, 2]


def test_head_of_a_short_sequence():
    assert head([1], 5) == [1]


def test_head_of_zero():
    assert head([1, 2], 0) == []
