from lookup import label_for


def test_label_for():
    assert label_for({"title": "a"}) == "a"


def test_label_for_without_a_title():
    assert label_for({}) == "untitled"
