from window import window


def test_window():
    assert window([1, 2, 3, 4], 2) == [1, 2]


def test_window_of_a_short_list():
    assert window([1], 5) == [1]
