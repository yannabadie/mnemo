"""Bounded, transient plan receipts owned by one MCP connection.

The registry contains immutable JSON snapshots, never a pre-rendered export.
Opaque IDs are meaningful only in their owning registry. Repeated identical
preparations reuse an ID while retained; LRU eviction and restart invalidate it.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import re
import secrets
import threading

from store import NotFound


@dataclass(frozen=True)
class PlanReceipt:
    plan_id: str
    memory_id: str
    version: int
    entry_hash: str
    snapshot_json: str
    dedup_key: str
    size_bytes: int


class PlanReceiptRegistry:
    """Per-connection LRU: at most 500 receipts and 8 MiB by default."""

    def __init__(self, max_entries: int = 500, max_bytes: int = 8 * 1024 * 1024):
        if type(max_entries) is not int or not 1 <= max_entries <= 500:
            raise ValueError("Receipt limit must be an integer from 1 to 500")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("Receipt byte limit must be a positive integer")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries = OrderedDict()
        self._dedup = {}
        self._bytes = 0
        self._lock = threading.RLock()

    def register(self, plan: dict) -> str:
        if plan.get("status") != "ready":
            raise ValueError("Only a ready plan can receive an export receipt")
        if "export_text" in plan:
            raise ValueError("A plan receipt must not contain a pre-rendered export")
        snapshot = {key: value for key, value in plan.items() if key != "plan_id"}
        encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
        if size > self.max_bytes:
            raise ValueError("The plan exceeds the session receipt byte limit")
        dedup_key = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        brief = snapshot["brief"]
        with self._lock:
            existing = self._dedup.get(dedup_key)
            if existing is not None:
                self._entries.move_to_end(existing)
                return existing
            plan_id = "plan_" + secrets.token_hex(16)
            while plan_id in self._entries:
                plan_id = "plan_" + secrets.token_hex(16)
            receipt = PlanReceipt(plan_id, brief["memory_id"], brief["version"],
                                  brief["entry_hash"], encoded, dedup_key, size)
            self._entries[plan_id] = receipt
            self._dedup[dedup_key] = plan_id
            self._bytes += size
            while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
                _, old = self._entries.popitem(last=False)
                self._dedup.pop(old.dedup_key, None)
                self._bytes -= old.size_bytes
            return plan_id

    def get(self, plan_id: str) -> PlanReceipt:
        if not isinstance(plan_id, str) or not re.fullmatch(r"plan_[0-9a-f]{32}", plan_id):
            raise ValueError("plan_id must be an opaque plan receipt returned by prepare_workshop")
        with self._lock:
            receipt = self._entries.get(plan_id)
            if receipt is None:
                raise NotFound("No plan receipt in this session; prepare a new plan")
            self._entries.move_to_end(plan_id)
            return receipt

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._dedup.clear()
            self._bytes = 0

    def __len__(self):
        with self._lock:
            return len(self._entries)

    @property
    def total_bytes(self):
        with self._lock:
            return self._bytes
