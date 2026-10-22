from pairs import consecutive


def test_consecutive():
    assert consecutive([1, 2, 3]) == [(1, 2), (2, 3)]
