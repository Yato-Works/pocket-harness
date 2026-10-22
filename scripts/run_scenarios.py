"""Run the deterministic mechanism scenarios (development helper)."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pocket_harness.config import load_config  # noqa: E402
from pocket_harness.diagnose import run_scenarios  # noqa: E402


async def main() -> int:
    config = load_config("bench/bench-local.toml")
    report = await run_scenarios(config)
    for result in report["scenarios"]:
        print(
            f"{result['scenario']:<22} {result['mechanism']:<4} "
            f"satisfied={result['satisfied']}  checks={result['checks']}"
        )
    print(json.dumps(report["summary"], indent=2))
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
