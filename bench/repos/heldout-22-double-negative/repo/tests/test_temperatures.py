from temperatures import is_freezing


def test_is_freezing():
    assert is_freezing(-1) is True


def test_zero_is_not_freezing():
    assert is_freezing(0) is False
