from longest import longest_run


def test_longest_run():
    assert longest_run([1, 1, 2]) == 2


def test_longest_run_of_a_longer_run():
    assert longest_run([1, 1, 2, 2, 2]) == 3
