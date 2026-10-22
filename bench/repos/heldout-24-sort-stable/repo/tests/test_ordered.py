from ordered import by_length


def test_by_length():
    assert by_length(["aa", "b", "ccc"]) == ["b", "aa", "ccc"]
