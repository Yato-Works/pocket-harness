import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_report_prints_the_total():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "report.py"), str(ROOT / "data.csv")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "42", result.stdout
