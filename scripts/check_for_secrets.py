"""Check the files that would be committed for credential-shaped content."""

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-_]{20,}"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"),
]

out = subprocess.run(
    ["git", "status", "--porcelain=v1", "--untracked-files=all"],
    capture_output=True,
    text=True,
    check=True,
).stdout

files = [line[3:].strip() for line in out.splitlines() if line.startswith("??")]
scanned = 0
hits = []
for name in files:
    path = Path(name)
    if not path.is_file():
        continue
    scanned += 1
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern in PATTERNS:
            if pattern.search(line):
                hits.append((name, number, line.strip()[:120]))

print(f"scanned {scanned} files")
if hits:
    print(f"{len(hits)} potential credential pattern(s):")
    for name, number, line in hits:
        print(f"  {name}:{number}: {line}")
    sys.exit(1)
print("no credential-shaped content found")
