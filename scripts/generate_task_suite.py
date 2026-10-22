"""Generate the repository task suite.

The suite is built from **archetypes**: a bug family with a fixed shape (a
module, a function and a failing test). The archetypes are partitioned between
the development split and the held-out split *before* any run, so that the
held-out tasks contain no family that was tuned on.

Every generated task carries a ``reference_fix.json`` with the exact edit a
reference solution makes, so ``scripts/check_task_suite.py`` can prove that the
task is broken before the fix and fixed after it.

Usage::

    python scripts/generate_task_suite.py            # write the tasks
    python scripts/check_task_suite.py                # verify every task
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "bench" / "repos"

CONFTEST = '''"""Make the project modules importable from the tests."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
'''

#: A task archetype: one module, one test file and the edit that fixes it.
Archetype = dict


def _task(
    task_id: str,
    split: str,
    category: str,
    module: str,
    source: str,
    test_module: str,
    test_source: str,
    old: str,
    new: str,
    prompt: str,
    description: str,
    forbidden: list[str] | None = None,
) -> Archetype:
    return {
        "task_id": task_id,
        "split": split,
        "category": category,
        "module": module,
        "source": source,
        "test_module": test_module,
        "test_source": test_source,
        "old": old,
        "new": new,
        "prompt": prompt,
        "description": description,
        "forbidden": forbidden or ["tests/"],
    }


def build_archetypes() -> list[Archetype]:
    """Return every archetype, development families first."""
    tasks: list[Archetype] = []

    # ---------------------------------------------------------------- dev ---
    tasks += [
        _task(
            "dev-10-slice-off-by-one",
            "dev",
            "failing-test repair",
            "window.py",
            '"""Sliding window helpers."""\n\n\n'
            "def window(values, size):\n"
            '    """Return the first `size` values."""\n'
            "    if size <= 0:\n"
            "        return []\n"
            "    return values[: size - 1]\n",
            "test_window",
            "from window import window\n\n\n"
            "def test_window():\n"
            "    assert window([1, 2, 3, 4], 2) == [1, 2]\n\n\n"
            "def test_window_of_a_short_list():\n"
            "    assert window([1], 5) == [1]\n",
            "values[: size - 1]",
            "values[:size]",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "window(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: window() returns one element too few.",
        ),
        _task(
            "dev-11-add-subtract",
            "dev",
            "failing-test repair",
            "totals.py",
            '"""Totals."""\n\n\n'
            "def total(first, second):\n"
            '    """Return the sum of two numbers."""\n'
            "    return first - second\n",
            "test_totals",
            "from totals import total\n\n\n"
            "def test_total():\n"
            "    assert total(2, 3) == 5\n",
            "return first - second",
            "return first + second",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "total(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: total() subtracts instead of adding.",
        ),
        _task(
            "dev-12-boundary-exclusive",
            "dev",
            "failing-test repair",
            "grades.py",
            '"""Grades."""\n\n\n'
            "def has_passed(score):\n"
            '    """Return whether the score is at least the pass mark."""\n'
            "    return score > 50\n",
            "test_grades",
            "from grades import has_passed\n\n\n"
            "def test_has_passed():\n"
            "    assert has_passed(50) is True\n"
            "    assert has_passed(49) is False\n",
            "return score > 50",
            "return score >= 50",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "has_passed(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the pass mark is exclusive.",
        ),
        _task(
            "dev-13-empty-input",
            "dev",
            "failing-test repair",
            "average.py",
            '"""Averages."""\n\n\n'
            "def average(values):\n"
            '    """Return the arithmetic mean."""\n'
            "    return sum(values) / len(values)\n",
            "test_average",
            "from average import average\n\n\n"
            "def test_average():\n"
            "    assert average([1, 2, 3]) == 2\n\n\n"
            "def test_average_of_an_empty_list():\n"
            "    assert average([]) == 0\n",
            "    return sum(values) / len(values)",
            "    if not values:\n        return 0\n    return sum(values) / len(values)",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "average(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the empty list is not handled.",
        ),
        _task(
            "dev-14-wrong-default",
            "dev",
            "failing-test repair",
            "greet.py",
            '"""Greetings."""\n\n\n'
            "def greet(name, punctuation=\"!\"):\n"
            '    """Return a greeting for the name."""\n'
            '    return f"Hello {name}{punctuation}"\n',
            "test_greet",
            "from greet import greet\n\n\n"
            "def test_greet():\n"
            '    assert greet("Ada") == "Hello Ada."\n\n\n'
            "def test_greet_with_punctuation():\n"
            '    assert greet("Ada", "?") == "Hello Ada?"\n',
            'punctuation="!"',
            'punctuation="."',
            "The tests in this project fail. Read the source, work out what is wrong with "
            "greet(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the default punctuation is wrong.",
        ),
        _task(
            "dev-15-accumulation",
            "dev",
            "failing-test repair",
            "counter.py",
            '"""Counting."""\n\n\n'
            "def count_upper(text):\n"
            '    """Return the number of upper case letters."""\n'
            "    total = 0\n"
            "    for letter in text:\n"
            "        if letter.isupper():\n"
            "            total = 1\n"
            "    return total\n",
            "test_counter",
            "from counter import count_upper\n\n\n"
            "def test_count_upper():\n"
            '    assert count_upper("aBcD") == 2\n',
            "            total = 1",
            "            total += 1",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "count_upper(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the counter overwrites instead of accumulating.",
        ),
        _task(
            "dev-16-format-specifier",
            "dev",
            "failing-test repair",
            "money.py",
            '"""Money."""\n\n\n'
            "def format_cents(cents):\n"
            '    """Return the amount with two decimals."""\n'
            '    return f"{cents / 100:.0f}"\n',
            "test_money",
            "from money import format_cents\n\n\n"
            "def test_format_cents():\n"
            '    assert format_cents(1234) == "12.34"\n',
            'f"{cents / 100:.0f}"',
            'f"{cents / 100:.2f}"',
            "The tests in this project fail. Read the source, work out what is wrong with "
            "format_cents(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the amount is formatted without decimals.",
        ),
        _task(
            "dev-17-rename-symbol",
            "dev",
            "symbol rename",
            "cart.py",
            '"""Shopping cart."""\n\n\n'
            "def count_items(items):\n"
            '    """Return the number of items."""\n'
            "    return len(items)\n",
            "test_cart",
            "from cart import item_count\n\n\n"
            "def test_item_count():\n"
            "    assert item_count([1, 2]) == 2\n",
            "def count_items(items):",
            "def item_count(items):",
            "The test imports a function that does not exist under that name. Rename the "
            "function in the source so that the import works and every test passes. "
            "Do not modify the tests.",
            "Symbol rename: count_items must become item_count.",
        ),
        _task(
            "dev-18-configuration",
            "dev",
            "configuration bug repair",
            "settings.py",
            '"""Service settings."""\n\n'
            "#: How long a request may take, in seconds.\n"
            "REQUEST_TIMEOUT_SECONDS = 30\n",
            "test_settings",
            "from settings import REQUEST_TIMEOUT_SECONDS\n\n\n"
            "def test_timeout():\n"
            "    assert REQUEST_TIMEOUT_SECONDS == 300\n",
            "REQUEST_TIMEOUT_SECONDS = 30",
            "REQUEST_TIMEOUT_SECONDS = 300",
            "One test fails because a configuration value is wrong. Find the value, work out "
            "what it should be from the test, and fix the configuration file. "
            "Do not modify the tests.",
            "Configuration bug repair: the timeout is ten times too small.",
        ),
        _task(
            "dev-19-script",
            "dev",
            "small script task",
            "scripts/report.py",
            '"""Print the total of the numbers in a file."""\n\n'
            "import sys\n\n\n"
            "def total(path):\n"
            '    """Return the sum of the numbers in the file."""\n'
            "    running = 0\n"
            "    with open(path, encoding=\"utf-8\") as handle:\n"
            "        for line in handle:\n"
            "            running += int(line)\n"
            "    return running\n\n\n"
            "def main(argv):\n"
            "    print(total(argv[1]))\n\n\n"
            "if __name__ == \"__main__\":\n"
            "    main(sys.argv)\n",
            "test_report",
            "import subprocess\nimport sys\nfrom pathlib import Path\n\n"
            "ROOT = Path(__file__).resolve().parent.parent\n\n\n"
            "def test_report_prints_the_total():\n"
            "    numbers = ROOT / \"numbers.txt\"\n"
            "    numbers.write_text(\"1\\n2\\n3\\n\\n\", encoding=\"utf-8\")\n"
            "    result = subprocess.run(\n"
            '        [sys.executable, str(ROOT / "scripts" / "report.py"), str(numbers)],\n'
            "        capture_output=True,\n"
            "        text=True,\n"
            "    )\n"
            '    assert result.returncode == 0, result.stderr\n'
            '    assert result.stdout.strip() == "6", result.stdout\n',
            "            running += int(line)",
            "            if line.strip():\n                running += int(line)",
            "The script scripts/report.py crashes on the numbers file. Run it, find the "
            "problem and fix the script so that it prints the total. Do not modify the "
            "tests or the data file.",
            "Small script task: the report script cannot parse the file.",
        ),
        _task(
            "dev-20-feature",
            "dev",
            "small feature plus tests",
            "text_utils.py",
            '"""Text helpers."""\n\n\n'
            "def normalize(text):\n"
            '    """Return the text without surrounding whitespace, in lower case."""\n'
            '    return text.strip().lower()\n',
            "test_text_utils",
            "from text_utils import normalize, titlecase\n\n\n"
            "def test_normalize():\n"
            '    assert normalize("  Hi ") == "hi"\n\n\n'
            "def test_titlecase():\n"
            '    assert titlecase("hello world") == "Hello World"\n',
            None,
            "def titlecase(text):\n"
            '    """Return the text in title case."""\n'
            "    return normalize(text).title()\n",
            "Add a new function titlecase(text) to text_utils.py. It must return the text "
            "in title case, with every word capitalised and the rest in lower case. "
            "Do not modify the tests.",
            "Small feature plus tests: add a titlecase() helper.",
        ),
        _task(
            "dev-21-comparison",
            "dev",
            "failing-test repair",
            "range_check.py",
            '"""Ranges."""\n\n\n'
            "def in_range(value, low, high):\n"
            '    """Return whether the value lies inside the range."""\n'
            "    return low < value < high\n",
            "test_range_check",
            "from range_check import in_range\n\n\n"
            "def test_in_range():\n"
            "    assert in_range(5, 1, 10) is True\n\n\n"
            "def test_in_range_of_the_lower_bound():\n"
            "    assert in_range(1, 1, 10) is True\n",
            "    return low < value < high",
            "    return low <= value <= high",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "in_range(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the bounds of the range are exclusive.",
        ),
        _task(
            "dev-22-prefix",
            "dev",
            "failing-test repair",
            "names.py",
            '"""Names."""\n\n\n'
            "def has_prefix(name, prefix):\n"
            '    """Return whether the name starts with the prefix."""\n'
            "    return prefix in name\n",
            "test_names",
            "from names import has_prefix\n\n\n"
            "def test_has_prefix():\n"
            '    assert has_prefix("abcdef", "abc") is True\n\n\n'
            "def test_has_prefix_of_a_middle_word():\n"
            '    assert has_prefix("xxabc", "abc") is False\n',
            "    return prefix in name",
            "    return name.startswith(prefix)",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "has_prefix(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the prefix is looked up anywhere in the name.",
        ),
        _task(
            "dev-23-loop-range",
            "dev",
            "failing-test repair",
            "pairs.py",
            '"""Pairs."""\n\n\n'
            "def consecutive(values):\n"
            '    """Return the pairs of neighbouring values."""\n'
            "    result = []\n"
            "    for index in range(len(values) - 2):\n"
            "        result.append((values[index], values[index + 1]))\n"
            "    return result\n",
            "test_pairs",
            "from pairs import consecutive\n\n\n"
            "def test_consecutive():\n"
            "    assert consecutive([1, 2, 3]) == [(1, 2), (2, 3)]\n",
            "    for index in range(len(values) - 2):",
            "    for index in range(len(values) - 2):",
            "    for index in range(len(values) - 1):",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "consecutive(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the last pair is missing.",
        ),
        _task(
            "dev-24-negative-index",
            "dev",
            "failing-test repair",
            "tail.py",
            '"""Tails."""\n\n\n'
            "def last(values):\n"
            '    """Return the last value."""\n'
            "    return values[-2]\n",
            "test_tail",
            "from tail import last\n\n\n"
            "def test_last():\n"
            "    assert last([1, 2, 3]) == 3\n",
            "    return values[-2]",
            "    return values[-1]",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "last(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the second to last value is returned.",
        ),
    ]

    # ----------------------------------------------------------- held-out ---
    tasks += [
        _task(
            "heldout-10-mutation",
            "held-out",
            "failing-test repair",
            "labels.py",
            '"""Labels."""\n\n\n'
            "def with_prefix(values, prefix):\n"
            '    """Return the values with the prefix added."""\n'
            "    for index in range(len(values)):\n"
            "        values[index] = prefix + values[index]\n"
            "    return values\n",
            "test_labels",
            "from labels import with_prefix\n\n\n"
            "def test_with_prefix():\n"
            '    assert with_prefix(["a"], "x-") == ["x-a"]\n\n\n'
            "def test_with_prefix_does_not_change_the_input():\n"
            '    original = ["a"]\n'
            "    with_prefix(original, \"x-\")\n"
            '    assert original == ["a"]\n',
            "    for index in range(len(values)):\n"
            "        values[index] = prefix + values[index]\n"
            "    return values",
            "    return [prefix + value for value in values]",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "with_prefix(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: with_prefix() changes the list it is given.",
        ),
        _task(
            "heldout-11-division",
            "held-out",
            "failing-test repair",
            "percent.py",
            '"""Percentages."""\n\n\n'
            "def percent(part, whole):\n"
            '    """Return the percentage of part in whole."""\n'
            "    return part / whole\n",
            "test_percent",
            "from percent import percent\n\n\n"
            "def test_percent():\n"
            "    assert percent(1, 4) == 25\n",
            "    return part / whole",
            "    return part / whole * 100",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "percent(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the percentage is missing its factor.",
        ),
        _task(
            "heldout-12-case",
            "held-out",
            "failing-test repair",
            "search.py",
            '"""Search."""\n\n\n'
            "def contains_word(text, word):\n"
            '    """Return whether the text contains the word."""\n'
            "    return word in text\n",
            "test_search",
            "from search import contains_word\n\n\n"
            "def test_contains_word():\n"
            '    assert contains_word("Hello World", "world") is True\n',
            "    return word in text",
            "    return word.lower() in text.lower()",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "contains_word(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the search is case sensitive.",
        ),
        _task(
            "heldout-13-index",
            "held-out",
            "failing-test repair",
            "second.py",
            '"""Elements."""\n\n\n'
            "def second(values):\n"
            '    """Return the second element."""\n'
            "    return values[0]\n",
            "test_second",
            "from second import second\n\n\n"
            "def test_second():\n"
            "    assert second([1, 2, 3]) == 2\n",
            "    return values[0]",
            "    return values[1]",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "second(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: second() returns the wrong element.",
        ),
        _task(
            "heldout-14-break",
            "held-out",
            "failing-test repair",
            "longest.py",
            '"""Runs."""\n\n\n'
            "def longest_run(values):\n"
            '    """Return the length of the longest run of equal values."""\n'
            "    best = 0\n"
            "    current = 0\n"
            "    for index, value in enumerate(values):\n"
            "        if index and values[index - 1] == value:\n"
            "            current += 1\n"
            "        else:\n"
            "            current = 1\n"
            "        best = current\n"
            "    return best\n",
            "test_longest",
            "from longest import longest_run\n\n\n"
            "def test_longest_run():\n"
            "    assert longest_run([1, 1, 2]) == 2\n\n\n"
            "def test_longest_run_of_a_longer_run():\n"
            "    assert longest_run([1, 1, 2, 2, 2]) == 3\n",
            "        best = current",
            "        if current > best:\n            best = current",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "longest_run(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the longest run is measured wrongly.",
        ),
        _task(
            "heldout-15-missing-key",
            "held-out",
            "failing-test repair",
            "settings_map.py",
            '"""Settings."""\n\n\n'
            "def get_timeout(settings):\n"
            '    """Return the timeout of the settings."""\n'
            '    return settings["timeout"]\n',
            "test_settings_map",
            "from settings_map import get_timeout\n\n\n"
            "def test_get_timeout():\n"
            '    assert get_timeout({"timeout": 5}) == 5\n\n\n'
            "def test_get_timeout_of_a_default():\n"
            "    assert get_timeout({}) == 30\n",
            '    return settings["timeout"]',
            '    return settings.get("timeout", 30)',
            "The tests in this project fail. Read the source, work out what is wrong with "
            "get_timeout(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: a missing key raises instead of using the default.",
        ),
        _task(
            "heldout-16-sort-key",
            "held-out",
            "failing-test repair",
            "ranking.py",
            '"""Ranking."""\n\n\n'
            "def by_score(entries):\n"
            '    """Return the entries ordered by their score."""\n'
            "    return sorted(entries, key=lambda entry: entry[\"score\"])\n",
            "test_ranking",
            "from ranking import by_score\n\n\n"
            "def test_by_score():\n"
            '    assert by_score([{"score": 1}, {"score": 3}])[0]["score"] == 3\n\n\n'
            "def test_by_score_is_descending():\n"
            '    assert by_score([{"score": 1}, {"score": 3}])[0]["score"] == 3\n',
            '    return sorted(entries, key=lambda entry: entry["score"])',
            '    return sorted(entries, key=lambda entry: -entry["score"])',
            "The tests in this project fail. Read the source, work out what is wrong with "
            "by_score(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the entries are sorted the wrong way round.",
        ),
        _task(
            "heldout-17-rename",
            "held-out",
            "symbol rename",
            "inventory.py",
            '"""Inventory."""\n\n\n'
            "def count_items(items):\n"
            '    """Return the number of items."""\n'
            "    return len(items)\n",
            "test_inventory",
            "from inventory import item_count\n\n\n"
            "def test_item_count():\n"
            "    assert item_count([1, 2]) == 2\n",
            "def count_items(items):",
            "def item_count(items):",
            "The test imports a function that does not exist under that name. Rename the "
            "function in the source so that the import works and every test passes. "
            "Do not modify the tests.",
            "Symbol rename: count_items must become item_count.",
        ),
        _task(
            "heldout-18-configuration",
            "held-out",
            "configuration bug repair",
            "limits.py",
            '"""Limits."""\n\n'
            "#: How many items may be requested at once.\n"
            "MAX_ITEMS = 5\n",
            "test_limits",
            "from limits import MAX_ITEMS\n\n\n"
            "def test_max_items():\n"
            "    assert MAX_ITEMS == 50\n",
            "MAX_ITEMS = 5",
            "MAX_ITEMS = 50",
            "One test fails because a configuration value is wrong. Find the value, work out "
            "what it should be from the test, and fix the configuration file. "
            "Do not modify the tests.",
            "Configuration bug repair: the limit is ten times too small.",
        ),
        _task(
            "heldout-19-script",
            "held-out",
            "small script task",
            "scripts/sum.py",
            '"""Print the sum of the numbers in a file."""\n\n'
            "import sys\n\n\n"
            "def total(path):\n"
            '    """Return the sum of the numbers in the file."""\n'
            "    running = 0\n"
            "    with open(path, encoding=\"utf-8\") as handle:\n"
            "        for line in handle:\n"
            "            running += int(line)\n"
            "    return running\n\n\n"
            "def main(argv):\n"
            "    print(total(argv[1]))\n\n\n"
            "if __name__ == \"__main__\":\n"
            "    main(sys.argv)\n",
            "test_sum",
            "import subprocess\nimport sys\nfrom pathlib import Path\n\n"
            "ROOT = Path(__file__).resolve().parent.parent\n\n\n"
            "def test_sum_prints_the_total():\n"
            '    numbers = ROOT / "numbers.txt"\n'
            '    numbers.write_text("2\\n4\\n\\n", encoding="utf-8")\n'
            "    result = subprocess.run(\n"
            '        [sys.executable, str(ROOT / "scripts" / "sum.py"), str(numbers)],\n'
            "        capture_output=True,\n"
            "        text=True,\n"
            "    )\n"
            '    assert result.returncode == 0, result.stderr\n'
            '    assert result.stdout.strip() == "6", result.stdout\n',
            "            running += int(line)",
            "            if line.strip():\n                running += int(line)",
            "The script scripts/sum.py crashes on the numbers file. Run it, find the "
            "problem and fix the script so that it prints the total. Do not modify the "
            "tests or the data file.",
            "Small script task: the sum script cannot parse the file.",
        ),
        _task(
            "heldout-20-feature",
            "held-out",
            "small feature plus tests",
            "numbers_utils.py",
            '"""Number helpers."""\n\n\n'
            "def clamp(value, low, high):\n"
            '    """Return the value limited to the range."""\n'
            "    if value < low:\n"
            "        return low\n"
            "    if value > high:\n"
            "        return high\n"
            "    return value\n",
            "test_numbers_utils",
            "from numbers_utils import clamp, median\n\n\n"
            "def test_clamp():\n"
            "    assert clamp(5, 0, 10) == 5\n\n\n"
            "def test_median():\n"
            "    assert median([1, 3, 2]) == 2\n\n\n"
            "def test_median_of_an_empty_list():\n"
            "    assert median([]) == 0\n",
            None,
            "def median(values):\n"
            '    """Return the median of the values."""\n'
            "    if not values:\n"
            "        return 0\n"
            "    ordered = sorted(values)\n"
            "    middle = len(ordered) // 2\n"
            "    if len(ordered) % 2:\n"
            "        return ordered[middle]\n"
            "    return (ordered[middle - 1] + ordered[middle]) / 2\n",
            "Add a new function median(values) to numbers_utils.py. It must return the "
            "median of the values; for an even number of values the average of the two "
            "middle ones, and 0 for an empty list. Do not modify the tests.",
            "Small feature plus tests: add a median() helper.",
        ),
        _task(
            "heldout-21-key",
            "held-out",
            "failing-test repair",
            "lookup.py",
            '"""Lookups."""\n\n\n'
            "def label_for(record):\n"
            '    """Return the label of the record."""\n'
            '    return record["title"]\n',
            "test_lookup",
            "from lookup import label_for\n\n\n"
            "def test_label_for():\n"
            '    assert label_for({"title": "a"}) == "a"\n\n\n'
            "def test_label_for_without_a_title():\n"
            '    assert label_for({}) == "untitled"\n',
            '    return record["title"]',
            '    return record.get("title", "untitled")',
            "The tests in this project fail. Read the source, work out what is wrong with "
            "label_for(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: a record without a title raises instead of using a default.",
        ),
        _task(
            "heldout-22-double-negative",
            "held-out",
            "failing-test repair",
            "temperatures.py",
            '"""Temperatures."""\n\n\n'
            "def is_freezing(celsius):\n"
            '    """Return whether the temperature is below zero."""\n'
            "    return celsius <= 0\n",
            "test_temperatures",
            "from temperatures import is_freezing\n\n\n"
            "def test_is_freezing():\n"
            "    assert is_freezing(-1) is True\n\n\n"
            "def test_zero_is_not_freezing():\n"
            "    assert is_freezing(0) is False\n",
            "    return celsius <= 0",
            "    return celsius < 0",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "is_freezing(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the freezing point is handled wrongly.",
        ),
        _task(
            "heldout-23-swap",
            "held-out",
            "failing-test repair",
            "move.py",
            '"""Moving."""\n\n\n'
            "def move(values, source, target):\n"
            '    """Move the value at the source index to the target index."""\n'
            "    values[source] = None\n"
            "    return values\n",
            "test_move",
            "from move import move\n\n\n"
            "def test_move():\n"
            "    assert move([1, 2, 3], 0, 2) == [None, 2, 1]\n",
            "    values[source] = None\n    return values",
            "    values[target] = values[source]\n"
            "    values[source] = None\n    return values",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "move(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the two values are not swapped.",
        ),
        _task(
            "heldout-24-sort-stable",
            "held-out",
            "failing-test repair",
            "ordered.py",
            '"""Ordering."""\n\n\n'
            "def by_length(words):\n"
            '    """Return the words ordered by their length."""\n'
            "    return sorted(words, key=len, reverse=True)\n",
            "test_ordered",
            "from ordered import by_length\n\n\n"
            "def test_by_length():\n"
            '    assert by_length(["aa", "b", "ccc"]) == ["b", "aa", "ccc"]\n',
            "    return sorted(words, key=len, reverse=True)",
            "    return sorted(words, key=len)",
            "The tests in this project fail. Read the source, work out what is wrong with "
            "by_length(), and fix it so that every test passes. Do not modify the tests.",
            "Failing-test repair: the words are ordered the wrong way round.",
        ),
    ]
    return tasks


def write_task(task: Archetype) -> Path:
    """Write one task into the repository."""
    base = ROOT / task["task_id"]
    repo = base / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (repo / "conftest.py").write_text(CONFTEST, encoding="utf-8")

    module_path = repo / task["module"]
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text(task["source"], encoding="utf-8")
    (tests / (task["test_module"] + ".py")).write_text(
        task["test_source"], encoding="utf-8"
    )

    (base / "task.json").write_text(
        json.dumps(
            {
                "task_id": task["task_id"],
                "split": task["split"],
                "category": task["category"],
                "description": task["description"],
                "prompt": task["prompt"],
                "test_command": ["{python}", "-m", "pytest", "-q"],
                "forbidden_paths": task["forbidden"],
                "max_steps": 12,
                "max_wall_seconds": 300,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (base / "reference_fix.json").write_text(
        json.dumps(
            {
                "file": task["module"],
                "old": task["old"],
                "new": task["new"],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return base


def main() -> int:
    written = 0
    for task in build_archetypes():
        path = write_task(task)
        written += 1
        print(f"wrote {path.name}")
    print(f"{written} tasks written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
