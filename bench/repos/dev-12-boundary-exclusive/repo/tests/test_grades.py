from grades import has_passed


def test_has_passed():
    assert has_passed(50) is True
    assert has_passed(49) is False
