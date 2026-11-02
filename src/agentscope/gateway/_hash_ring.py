# -*- coding: utf-8 -*-
"""Consistent hashing router for Gateway Sticky Sessions."""
import bisect
import hashlib
import re
from typing import List
from urllib.parse import parse_qs


_SESSION_PATH_REGEX = re.compile(
    r"/(?:sessions|chat|workspace)/([a-zA-Z0-9_\-]+)",
)


class ConsistentHashRouter:
    """Routes requests to upstream Managed Agent instances using consistent hashing.

    Ensures that turns belonging to the same session_id are consistently dispatched
    to the same backend worker replica, maximizing local cache and workspace hits.
    """

    def __init__(
        self,
        instances: List[str],
        replicas_per_node: int = 100,
    ) -> None:
        """Initialize the hash ring with upstream URLs."""
        if not instances:
            raise ValueError("At least one upstream instance is required.")
        self._instances = [inst.rstrip("/") for inst in instances]
        self._replicas = replicas_per_node
        self._ring: list[int] = []
        self._ring_map: dict[int, str] = {}
        self._rr_counter = 0

        self._build_ring()

    @property
    def instances(self) -> List[str]:
        """Return the list of upstream instance endpoints."""
        return list(self._instances)

    def _hash(self, key: str) -> int:
        """Generate a 32-bit integer hash for a given key."""
        digest = hashlib.md5(key.encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    def _build_ring(self) -> None:
        """Build the virtual node ring."""
        self._ring.clear()
        self._ring_map.clear()
        for inst in self._instances:
            for i in range(self._replicas):
                v_key = f"{inst}#virtual_{i}"
                h = self._hash(v_key)
                self._ring.append(h)
                self._ring_map[h] = inst
        self._ring.sort()

    def route_by_key(self, key: str) -> str:
        """Find the target upstream instance URL for a given affinity key."""
        if not self._ring:
            raise RuntimeError("Consistent hash ring is empty.")
        h = self._hash(key)
        idx = bisect.bisect_right(self._ring, h)
        if idx == len(self._ring):
            idx = 0
        return self._ring_map[self._ring[idx]]

    def route_round_robin(self) -> str:
        """Fallback round-robin dispatch for session-less requests."""
        inst = self._instances[self._rr_counter % len(self._instances)]
        self._rr_counter += 1
        return inst

    def extract_session_id(
        self,
        path: str,
        query_string: str = "",
        headers: dict | None = None,
    ) -> str | None:
        """Extract session_id from Header, Query, or Path."""
        headers = headers or {}
        # 1. Header takes precedence
        header_sid = headers.get("x-session-id") or headers.get("X-Session-ID")
        if header_sid:
            return header_sid

        # 2. Query param
        if query_string:
            qs = parse_qs(query_string)
            if "session_id" in qs and qs["session_id"]:
                return qs["session_id"][0]

        # 3. Path regex match
        m = _SESSION_PATH_REGEX.search(path)
        if m:
            matched = m.group(1)
            # Exclude sub-resource keywords
            if matched not in ("queue", "stream", "order", "files"):
                return matched

        return None

    def resolve_target(
        self,
        path: str,
        query_string: str = "",
        headers: dict | None = None,
        sticky: bool = True,
    ) -> str:
        """Determine target upstream URL based on session affinity."""
        if sticky:
            sid = self.extract_session_id(path, query_string, headers)
            if sid:
                return self.route_by_key(sid)

        return self.route_round_robin()
