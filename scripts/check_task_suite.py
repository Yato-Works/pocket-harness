"""Verify that every repository task is broken before the fix and solved after.

The reference edit comes from ``reference_fix.json`` next to the task (for the
generated tasks) or from the built-in table (for the hand written ones).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "bench" / "repos"

#: The reference edits of the hand written tasks.
FIXES = {
    "dev-01-fix-add": ("calc.py", "return a - b", "return a + b"),
    "dev-02-config-timeout": (
        "settings.py",
        "REQUEST_TIMEOUT_MS = 30",
        "REQUEST_TIMEOUT_MS = 30000",
    ),
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


def reference_fix(task_dir: Path) -> tuple[str, str | None, str]:
    """Return the (file, old, new) of the reference edit."""
    reference = task_dir / "reference_fix.json"
    if reference.is_file():
        data = json.loads(reference.read_text(encoding="utf-8"))
        return str(data["file"]), data.get("old"), str(data["new"])
    task_id = task_dir.name
    if task_id not in FIXES:
        raise KeyError(f"no reference fix for {task_id}")
    return FIXES[task_id]


def run_tests(repo: Path, test_command: list[str]) -> tuple[int, str]:
    """Run the task's test command and return (exit code, output tail)."""
    command = [sys.executable if part == "{python}" else part for part in test_command]
    try:
        completed = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return 124, "the test command timed out"
    return completed.returncode, ((completed.stdout or "") + (completed.stderr or ""))[-1500:]


def main() -> int:
    failures = 0
    checked = 0
    for task_dir in sorted(TASKS.iterdir()):
        if not task_dir.is_dir():
            continue
        task_file = task_dir / "task.json"
        if not task_file.is_file():
            continue
        task = json.loads(task_file.read_text(encoding="utf-8"))
        snapshot = task_dir / "repo"
        if not snapshot.is_dir():
            continue
        checked += 1
        work = Path(tempfile.mkdtemp(prefix="taskcheck-"))
        try:
            repo = work / "repo"
            shutil.copytree(snapshot, repo)
            before_code, before_out = run_tests(repo, task["test_command"])
            file_name, old, new = reference_fix(task_dir)
            path = repo / file_name
            text = path.read_text(encoding="utf-8")
            if old is not None:
                if old not in text:
                    print(f"BAD  {task['task_id']}: expected {old!r} in {file_name}")
                    failures += 1
                    continue
                path.write_text(text.replace(old, new), encoding="utf-8")
            else:
                path.write_text(text + new, encoding="utf-8")
            after_code, after_out = run_tests(repo, task["test_command"])
            ok = before_code != 0 and after_code == 0
            if not ok:
                failures += 1
                print(f"BAD  {task['task_id']}: before={before_code} after={after_code}")
                print("   before:", before_out[-400:])
                print("   after :", after_out[-400:])
            else:
                print(
                    f"OK   {task['task_id']:<28} [{task.get('split', '?'):<8}] "
                    f"before={before_code} after={after_code}"
                )
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print(f"\n{checked - failures}/{checked} tasks are broken-before / fixed-after")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
