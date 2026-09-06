"""Terminal chat client against the running engine.

python scripts/chat.py <public_key>
"""

from __future__ import annotations

import json
import sys
import uuid

import httpx

API = "http://localhost:8000"


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    public_key = sys.argv[1]
    session_id = uuid.uuid4().hex

    with httpx.Client(timeout=120) as client:
        boot = client.get(f"{API}/v1/chat/bootstrap", params={"public_key": public_key})
        if boot.status_code != 200:
            print("bootstrap failed:", boot.text)
            raise SystemExit(1)
        info = boot.json()
        print(f"\n[{info['agent_name']}] {info['greeting']}\n")

        while True:
            try:
                text = input("you > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if text.lower() in {"quit", "exit"}:
                return
            if not text:
                continue

            print("bot > ", end="", flush=True)
            with client.stream(
                "POST",
                f"{API}/v1/chat/stream",
                json={"public_key": public_key, "session_id": session_id, "text": text},
            ) as response:
                event, printed = "", False
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        payload = json.loads(line.split(":", 1)[1].strip())
                        if event == "token":
                            print(payload["text"], end="", flush=True)
                            printed = True
                        elif event == "node":
                            pass
                        elif event == "done":
                            if not printed:
                                print(payload.get("reply", ""), end="")
                            usage = payload.get("usage", {})
                            print(
                                f"\n      [{' -> '.join(payload.get('trace', []))}]"
                                f"  {payload.get('latency_ms', 0)}ms"
                                f"  in={usage.get('input', 0)} out={usage.get('output', 0)}"
                                + ("  BLOCKED" if payload.get("blocked") else "")
                                + ("  ESCALATED" if payload.get("escalated") else "")
                            )
            print()


if __name__ == "__main__":
    main()
