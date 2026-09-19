"""Thread-safe in-memory policy store."""
from __future__ import annotations

import threading
from typing import Any


class PolicyStore:
    def __init__(self) -> None:
        self._policies: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, policy: dict[str, Any]) -> bool:
        """Store a new policy. Returns False if the id already exists."""
        with self._lock:
            policy_id = policy["policy_id"]
            if policy_id in self._policies:
                return False
            self._policies[policy_id] = policy
            return True

    def replace(self, policy_id: str, policy: dict[str, Any]) -> bool:
        """Replace an existing policy. Returns False if it does not exist."""
        with self._lock:
            if policy_id not in self._policies:
                return False
            self._policies[policy_id] = policy
            return True

    def get(self, policy_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._policies.get(policy_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._policies.values())

    def delete(self, policy_id: str) -> bool:
        with self._lock:
            return self._policies.pop(policy_id, None) is not None

    def count(self) -> int:
        with self._lock:
            return len(self._policies)
