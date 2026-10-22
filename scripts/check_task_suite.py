"""Verify that every repository task is broken before the fix and solvable."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pocket_harness.repo_bench import check_task, load_repo_tasks  # noqa: E402

FIXES = {
    "dev-01-fix-add": ("calc.py", "return a - b", "return a + b"),
    "dev-02-config-timeout": ("settings.py", "REQUEST_TIMEOUT_MS = 30", "REQUEST_TIMEOUT_MS = 30000"),
    "dev-03-rename-symbol": ("orders.py", "compute_total", "compute_sum"),
    "heldout-01-add-palindrome": (
        "text_utils.py",
        None,
        '\n\ndef is_palindrome(text):\n    """Return True when the text is a palindrome."""\n'
        "    cleaned = text.strip().lower()\n    return cleaned == cleaned[::-1]\n",
    ),
    "heldout-02-fix-report-script": (
        "scripts/report.py",
        "running += int(parts[1])",
        "running += int(parts[1]) if parts[0] != 'name' else 0",
    ),
    "heldout-03-slice-off-by-one": ("sequence.py", "values[: count - 1]", "values[:count]"),
    "heldout-04-format-price": (
        "pricing.py",
        "{cents / 100:.0f}",
        "{cents / 100:.2f}",
    ),
    "heldout-05-dedupe": (
        "lists.py",
        None,
        '\n\ndef dedupe(values):\n    """Return the values without repetitions."""\n'
        "    result = []\n"
        "    for value in values:\n"
        "        if value not in result:\n"
        "            result.append(value)\n"
        "    return result\n",
    ),
    "heldout-06-leap-year": (
        "calendar.py",
        "return year % 4 == 0",
        "return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)",
    ),
    "heldout-07-clamp": (
        "bounds.py",
        "    if value < low:\n        return high\n    if value > high:\n        return low",
        "    if value < low:\n        return low\n    if value > high:\n        return high",
    ),
    "heldout-08-word-count": (
        "words.py",
        "return len(text.split()) + 1",
        "return len(text.split())",
    ),
    "heldout-09-merge-dicts": (
        "mappings.py",
        "    result = dict(other)\n"
        "    for key, value in other.items():\n"
        "        result[key] = value\n    return result",
        "    result = dict(base)\n    result.update(other)\n    return result",
    ),
}


def main() -> int:
    tasks = load_repo_tasks()
    failures = 0
    for task in tasks:
        work = Path(tempfile.mkdtemp(prefix="taskcheck-"))
        try:
            shutil.copytree(task.snapshot, work / "repo")
            repo = work / "repo"
            before = check_task(task, repo)
            name, old, new = FIXES[task.task_id]
            path = repo / name
            text = path.read_text(encoding="utf-8")
            if old is not None:
                assert old in text, f"{task.task_id}: expected {old!r} in {name}"
                path.write_text(text.replace(old, new), encoding="utf-8")
            else:
                path.write_text(text + new, encoding="utf-8")
            after = check_task(task, repo)
            status = "OK " if (not before["passed"] and after["passed"]) else "BAD"
            if status == "BAD":
                failures += 1
            print(
                f"{status} {task.task_id}: before exit={before['test_exit_code']} "
                f"passed={before['passed']} | after exit={after['test_exit_code']} "
                f"passed={after['passed']}"
            )
            if status == "BAD":
                print("   before:", before["test_output_tail"][-400:])
                print("   after :", after["test_output_tail"][-400:])
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print(f"\n{len(tasks) - failures}/{len(tasks)} tasks are broken-before / fixed-after")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

