import pathlib

p = pathlib.Path("scripts/generate_task_suite.py")
s = p.read_text(encoding="utf-8")

# 1) the script tasks: the numbers file ends with a blank line, so the script
#    has to skip empty lines.
s = s.replace(
    '''            "    numbers.write_text(\\"1\\\\n2\\\\n3\\\\n\\", encoding=\\"utf-8\\")\\n"''',
    '''            "    numbers.write_text(\\"1\\\\n2\\\\n3\\\\n\\\\n\\", encoding=\\"utf-8\\")\\n"''',
)
s = s.replace(
    '''            "    numbers.write_text(\\"2\\\\n4\\\\n\\", encoding=\\"utf-8\\")\\n"''',
    '''            "    numbers.write_text(\\"2\\\\n4\\\\n\\\\n\\", encoding=\\"utf-8\\")\\n"''',
)
s = s.replace(
    '''            "            running += int(line)",
            "            running += int(line.strip())",''',
    '''            "            running += int(line)",
            "            if line.strip():\\n                running += int(line)",''',
)

# 2) the longest run: the bug must be visible.
s = s.replace(
    '''            "        if current > best:\\n            best = current\\n"
            "    return best\\n",''',
    '''            "        best = current\\n"
            "    return best\\n",''',
)
s = s.replace(
    '''            "from longest import longest_run\\n\\n\\n"
            "def test_longest_run():\\n"
            "    assert longest_run([1, 1, 2, 2, 2]) == 3\\n",''',
    '''            "from longest import longest_run\\n\\n\\n"
            "def test_longest_run():\\n"
            "    assert longest_run([1, 1, 2]) == 2\\n\\n\\n"
            "def test_longest_run_of_a_longer_run():\\n"
            "    assert longest_run([1, 1, 2, 2, 2]) == 3\\n",''',
)
s = s.replace(
    '''            "        if current > best:\\n            best = current",
            "        if current >= best:\\n            best = current",''',
    '''            "        best = current",
            "        if current > best:\\n            best = current",''',
)

# 3) the ranking: descending is the intended behaviour.
s = s.replace(
    '''            "def test_by_score():\\n"
            '    assert by_score([{"score": 1}, {"score": 3}])[0]["score"] == 1\\n\\n\\n' ''',
    '''            "def test_by_score():\\n"
            '    assert by_score([{"score": 1}, {"score": 3}])[0]["score"] == 3\\n\\n\\n' ''',
)

p.write_text(s, encoding="utf-8")
print("ok")
