from names import has_prefix


def test_has_prefix():
    assert has_prefix("abcdef", "abc") is True


def test_has_prefix_of_a_middle_word():
    assert has_prefix("xxabc", "abc") is False
