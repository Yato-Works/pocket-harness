from mappings import merge


def test_merge():
    assert merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_merge_overrides():
    assert merge({"a": 1}, {"a": 2}) == {"a": 2}
