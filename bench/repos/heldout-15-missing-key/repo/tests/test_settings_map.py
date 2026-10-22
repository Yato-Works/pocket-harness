from settings_map import get_timeout


def test_get_timeout():
    assert get_timeout({"timeout": 5}) == 5


def test_get_timeout_of_a_default():
    assert get_timeout({}) == 30
