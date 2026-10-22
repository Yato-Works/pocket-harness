from text_utils import normalize, titlecase


def test_normalize():
    assert normalize("  Hi ") == "hi"


def test_titlecase():
    assert titlecase("hello world") == "Hello World"
