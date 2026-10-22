from search import contains_word


def test_contains_word():
    assert contains_word("Hello World", "world") is True
