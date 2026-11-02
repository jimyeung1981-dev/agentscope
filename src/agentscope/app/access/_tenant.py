# -*- coding: utf-8 -*-
"""Multi-tenant identity and execution context support.

Provides thread-local / async-context-safe tracking of the current tenant,
user, and role throughout the request lifecycle (Gateway -> Harness -> DB).
"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TenantIdentity:
    """Represents an authenticated multi-tenant identity."""

    tenant_id: str
    user_id: str
    role: str = "member"
    quotas: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        """Check if the identity holds admin privileges for the tenant."""
        return self.role in ("admin", "owner", "superadmin")

    def tenant_scoped_path(self, path: str) -> str:
        """Generate a tenant-partitioned resource path (e.g. for S3/Blob storage)."""
        clean_path = path.lstrip("/")
        return f"tenants/{self.tenant_id}/{clean_path}"


_current_tenant_identity: ContextVar[TenantIdentity | None] = ContextVar(
    "current_tenant_identity",
    default=None,
)


def get_current_tenant() -> TenantIdentity | None:
    """Return the active TenantIdentity from the current async context, if any."""
    return _current_tenant_identity.get()


def set_current_tenant(identity: TenantIdentity | None) -> None:
    """Set or clear the active TenantIdentity in the current async context."""
    _current_tenant_identity.set(identity)
