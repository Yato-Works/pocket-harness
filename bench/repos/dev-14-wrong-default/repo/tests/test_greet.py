from greet import greet


def test_greet():
    assert greet("Ada") == "Hello Ada."


def test_greet_with_punctuation():
    assert greet("Ada", "?") == "Hello Ada?"
