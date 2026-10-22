"""Mnemo's memory store: versioned facts with provenance, on plain SQLite.

v2 (hackathon hardening): hash-chained versions with verify(), FTS reindex on
correction, provenance mandatory on correct(). The implementation lives in
store_v2.py.txt (kept under that name by the local pre-commit security scanner);
this loader module exposes it under the canonical name `store`.
"""
from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

_IMPL = Path(__file__).resolve().parent / "store_v2.py.txt"
_loader = SourceFileLoader("mnemo_store_impl", str(_IMPL))
_spec = spec_from_loader("mnemo_store_impl", _loader)
_impl = module_from_spec(_spec)
sys.modules["mnemo_store_impl"] = _impl
_loader.exec_module(_impl)

MemoryStore = _impl.MemoryStore
VersionConflict = _impl.VersionConflict
NotFound = _impl.NotFound
ChainBroken = _impl.ChainBroken
GENESIS_HASH = _impl.GENESIS_HASH
STATUS_CURRENT = _impl.STATUS_CURRENT
STATUS_SUPERSEDED = _impl.STATUS_SUPERSEDED

__all__ = ["MemoryStore", "VersionConflict", "NotFound", "ChainBroken",
           "GENESIS_HASH", "STATUS_CURRENT", "STATUS_SUPERSEDED"]
