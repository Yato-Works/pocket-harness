from labels import with_prefix


def test_with_prefix():
    assert with_prefix(["a"], "x-") == ["x-a"]


def test_with_prefix_does_not_change_the_input():
    original = ["a"]
    with_prefix(original, "x-")
    assert original == ["a"]
