#!/usr/bin/env python3
"""One-shot maintenance for the feishu-bridge queues.

Marks every pre-existing inbox entry as processed and every pre-existing
outbox entry as sent, so that starting the bridge cannot deliver stale
messages that were queued months ago. New entries created after this run
are untouched.

Usage: python reset_bridge_queues.py [--dry-run]
"""
import json
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent

def load(path: Path):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[warn] cannot parse {path.name}: {exc}")
        return []

def save(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def main() -> None:
    dry = "--dry-run" in sys.argv
    inbox_p, outbox_p = BRIDGE / "inbox.json", BRIDGE / "outbox.json"

    inbox = load(inbox_p)
    n_in = 0
    for e in inbox:
        if not e.get("processed"):
            e["processed"] = True
            e["status_note"] = "marked processed by reset_bridge_queues.py (stale, pre-restart)"
            n_in += 1

    outbox = load(outbox_p)
    n_out = 0
    for e in outbox:
        if not e.get("sent"):
            e["sent"] = True
            e["status_note"] = "marked sent by reset_bridge_queues.py (stale, never delivered)"
            n_out += 1

    print(f"inbox: {len(inbox)} entries, {n_in} newly marked processed")
    print(f"outbox: {len(outbox)} entries, {n_out} newly marked sent")
    if dry:
        print("[dry-run] nothing written")
        return
    save(inbox_p, inbox)
    save(outbox_p, outbox)
    print("written")

if __name__ == "__main__":
    main()
