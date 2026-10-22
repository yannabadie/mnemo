#!/usr/bin/env python3
"""Read-only verifier of local receipt consistency for a Mnemo database.

Checks chain links, metadata, contiguous versions and stored head commitments.
This cannot detect an operator rewriting the database and all its hashes.
Verification never migrates, rehashes or repairs the database.

Usage:
    python verify.py --db mnemo.db
"""
from __future__ import annotations

import argparse
import sqlite3
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
    try:
        store = MemoryStore(args.db, read_only=True)
    except (sqlite3.Error, ValueError) as exc:
        print(f"cannot inspect database: {exc}")
        return 2
    try:
        result = store.verify(args.memory)
    finally:
        store.close()
    if result["ok"]:
        print(f"OK — local receipt consistency ({result['checked']} entries checked)")
        print("No external trust anchor: this is not protection against an operator rewriting all hashes.")
        return 0
    broken = result["broken_at"]
    print(f"BROKEN — memory {broken['memory_id']}, version {broken['version']}")
    print(result.get("note", ""))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
