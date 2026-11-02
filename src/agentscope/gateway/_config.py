# -*- coding: utf-8 -*-
"""Configuration for the independent Agent Cloud API Gateway."""
from dataclasses import dataclass, field
from typing import List, Dict


@dataclass
class GatewayConfig:
    """Settings governing the independent Agent Cloud API Gateway."""

    upstream_instances: List[str] = field(
        default_factory=lambda: ["http://127.0.0.1:8000"],
    )
    """Default fallback upstream instances (for backward compatibility)."""

    default_service_name: str = "managed-agent"
    """Default service name when not specified in route."""

    services: Dict[str, List[str]] = field(
        default_factory=lambda: {
            "managed-agent": ["http://127.0.0.1:8000"],
        },
    )
    """Initial pre-configured services and their endpoints in the service registry."""

    enable_sticky_sessions: bool = True
    """Whether to route requests with the same session_id to the same backend."""

    enable_rate_limiting: bool = True
    """Whether to enforce token-bucket rate limiting per tenant/client."""

    rate_limit_per_minute: int = 120
    """Max requests per minute per tenant/IP."""

    rate_limit_burst: int = 30
    """Burst token capacity."""

    enable_circuit_breaker: bool = True
    """Whether to enable circuit breaker fault tolerance on failing backends."""

    circuit_breaker_failure_threshold: int = 3
    """Number of consecutive failures to trip circuit breaker."""

    circuit_breaker_recovery_timeout: float = 10.0
    """Seconds to wait before trial recovery (HALF_OPEN) of a tripped circuit."""

    timeout_seconds: float = 300.0
    """HTTP client read timeout in seconds, accommodating long Agent reasoning."""

    jwt_secret: str | None = None
    """Optional JWT signing secret for signature verification."""

    cors_origins: List[str] = field(
        default_factory=lambda: ["*"],
    )
    """Allowed CORS origins for external web/mobile clients."""

    title: str = "Agent Cloud API Gateway"
    """OpenAPI title shown in docs."""
