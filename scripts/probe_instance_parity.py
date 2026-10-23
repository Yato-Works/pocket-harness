"""Do the two upstream instances (11434 / 11435) answer identically?

Temperature 0 and a fixed seed, three rounds each: the instance-pair probe for
the independent-instance comparison (DECISION.md section 7.1 step 1).
"""

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

src = Path(__file__).with_name("probe_reproducibility.py").read_text(encoding="utf-8")
ns: dict = {}
exec(compile(src.split("def main")[0], "probe", "exec"), ns)  # noqa: S102 - local trusted file
CONVERSATION, TOOLS = ns["CONVERSATION"], ns["TOOLS"]
MODEL = sys.argv[3] if len(sys.argv) > 3 else "qwen2.5:7b-instruct"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
PORT_A = 11434
PORT_B = 11435


def fingerprint(port: int) -> list[str]:
    outputs = []
    with httpx.Client(timeout=300.0) as client:
        for _ in range(ROUNDS):
            response = client.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json={
                    "model": MODEL,
                    "messages": CONVERSATION,
                    "tools": TOOLS,
                    "temperature": 0.0,
                    "seed": 7,
                    "max_tokens": 1024,
                },
            )
            message = response.json()["choices"][0]["message"]
            calls = [
                (c["function"]["name"], c["function"]["arguments"])
                for c in message.get("tool_calls") or []
            ]
            outputs.append(json.dumps({"content": message.get("content"), "calls": calls}, sort_keys=True))
    return outputs


def main() -> int:
    a, b = fingerprint(PORT_A), fingerprint(PORT_B)
    print(f"model: {MODEL}  rounds: {ROUNDS}  temperature: 0  seed: 7")
    print(f"internal reproducibility A ({PORT_A}): {len(set(a)) == 1}")
    print(f"internal reproducibility B ({PORT_B}): {len(set(b)) == 1}")
    print(f"cross-instance agreement A==B: {a[0] == b[0]}")
    for index, (x, y) in enumerate(zip(a, b, strict=False)):
        print(f"  [{index}] A: {x[:140]}")
        print(f"        B: {y[:140]}")
    return 0


if __name__ == "__main__":
    import json

    raise SystemExit(main())
