from lists import dedupe


def test_dedupe_keeps_the_first_occurrence():
    assert dedupe([1, 2, 1, 3, 2]) == [1, 2, 3]


def test_dedupe_of_an_empty_list():
    assert dedupe([]) == []
