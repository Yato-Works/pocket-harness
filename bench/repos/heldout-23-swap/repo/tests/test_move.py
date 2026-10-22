from move import move


def test_move():
    assert move([1, 2, 3], 0, 2) == [None, 2, 1]
