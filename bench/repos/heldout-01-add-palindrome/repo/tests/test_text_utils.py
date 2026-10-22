from text_utils import is_palindrome


def test_palindrome():
    assert is_palindrome("Racecar") is True


def test_not_palindrome():
    assert is_palindrome("pocket") is False


def test_empty_string():
    assert is_palindrome("") is True
