# -*- coding: utf-8 -*-
"""Rate Limiter and Circuit Breaker for Agent Cloud Gateway."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class CircuitState(str, Enum):
    """Circuit breaker state."""

    CLOSED = "CLOSED"      # Normal operation
    OPEN = "OPEN"          # Tripped, blocking all traffic
    HALF_OPEN = "HALF_OPEN"  # Trial recovery mode


@dataclass
class CircuitBreakerStatus:
    """Internal status of a circuit breaker for a given target."""

    target: str
    failure_count: int = 0
    state: CircuitState = CircuitState.CLOSED
    last_failure_time: float = 0.0
    half_open_trial_pending: bool = False


class CircuitBreaker:
    """Circuit Breaker for isolating failing Agent backend instances."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 10.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._circuits: dict[str, CircuitBreakerStatus] = {}

    def get_status(self, target: str) -> CircuitBreakerStatus:
        if target not in self._circuits:
            self._circuits[target] = CircuitBreakerStatus(target=target)
        return self._circuits[target]

    def can_execute(self, target: str) -> bool:
        """Check if request to target is permitted through the circuit."""
        circuit = self.get_status(target)
        now = time.time()

        if circuit.state == CircuitState.CLOSED:
            return True

        if circuit.state == CircuitState.OPEN:
            # Check if recovery timeout elapsed
            if now - circuit.last_failure_time >= self.recovery_timeout:
                circuit.state = CircuitState.HALF_OPEN
                circuit.half_open_trial_pending = True
                return True
            return False

        if circuit.state == CircuitState.HALF_OPEN:
            # Allow one trial probe
            if circuit.half_open_trial_pending:
                circuit.half_open_trial_pending = False
                return True
            return False

        return True

    def record_success(self, target: str) -> None:
        """Record successful execution, resetting circuit if needed."""
        circuit = self.get_status(target)
        circuit.failure_count = 0
        circuit.state = CircuitState.CLOSED
        circuit.half_open_trial_pending = False

    def record_failure(self, target: str) -> None:
        """Record a failure (timeout, 5xx, connection error)."""
        circuit = self.get_status(target)
        circuit.failure_count += 1
        circuit.last_failure_time = time.time()

        if circuit.state == CircuitState.HALF_OPEN or circuit.failure_count >= self.failure_threshold:
            circuit.state = CircuitState.OPEN
            circuit.half_open_trial_pending = False


@dataclass
class TokenBucket:
    capacity: float
    refill_rate: float  # tokens per second
    tokens: float
    last_update: float = field(default_factory=time.time)


class RateLimiter:
    """Token Bucket Rate Limiter per Tenant / Client."""

    def __init__(self, requests_per_minute: int = 120, burst: int = 30) -> None:
        self.refill_rate = requests_per_minute / 60.0  # tokens / sec
        self.capacity = float(burst)
        self._buckets: dict[str, TokenBucket] = {}

    def acquire(self, key: str, cost: float = 1.0) -> bool:
        """Attempt to acquire tokens for the given key."""
        now = time.time()
        bucket = self._buckets.get(key)

        if bucket is None:
            bucket = TokenBucket(
                capacity=self.capacity,
                refill_rate=self.refill_rate,
                tokens=self.capacity,
                last_update=now,
            )
            self._buckets[key] = bucket

        # Refill tokens based on elapsed time
        elapsed = now - bucket.last_update
        bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.refill_rate)
        bucket.last_update = now

        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return True
        return False
