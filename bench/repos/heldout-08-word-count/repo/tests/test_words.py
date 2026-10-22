from words import count_words


def test_count_words():
    assert count_words("one two three") == 3


def test_count_words_of_an_empty_text():
    assert count_words("") == 0
