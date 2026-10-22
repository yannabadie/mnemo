#!/usr/bin/env python3
"""Offline receipt-chain verifier for a Mnemo database.

Recomputes the hash chain of every memory from genesis and reports the exact
first broken version, or OK with the number of entries checked.

Usage:
    python verify.py --db mnemo.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from store import MemoryStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(Path(__file__).resolve().parent / "mnemo.db"))
    parser.add_argument("--memory", default=None, help="verify a single memory id")
    args = parser.parse_args()
    if not Path(args.db).exists():
        print(f"no database at {args.db}")
        return 2
    result = MemoryStore(args.db).verify(args.memory)
    if result["ok"]:
        print(f"OK — receipt chain intact ({result['checked']} entries verified from genesis)")
        return 0
    broken = result["broken_at"]
    print(f"BROKEN — chain fails at memory {broken['memory_id']} version {broken['version']}")
    print(result.get("note", ""))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
