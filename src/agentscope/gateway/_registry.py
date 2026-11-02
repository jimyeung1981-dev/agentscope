# -*- coding: utf-8 -*-
"""Service Discovery and Registry for Agent Cloud Gateway."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from ._hash_ring import ConsistentHashRouter


@dataclass
class ServiceInstance:
    """A registered backend Agent service instance."""

    instance_id: str
    service_name: str
    endpoint: str  # e.g., "http://10.0.0.1:8000"
    metadata: dict[str, Any] = field(default_factory=dict)
    weight: int = 1
    healthy: bool = True
    last_heartbeat: float = field(default_factory=time.time)


class ServiceRegistry:
    """Agent Cloud Service Registry.

    Supports dynamic registration, deregistration, and heartbeat of heterogeneous
    Agent services (e.g. 'managed-agent', 'code-agent', 'workflow-agent').
    Maintains a ConsistentHashRouter per service for sticky session routing.
    """

    def __init__(self, heartbeat_timeout: float = 30.0) -> None:
        self.heartbeat_timeout = heartbeat_timeout
        # service_name -> {instance_id: ServiceInstance}
        self._services: dict[str, dict[str, ServiceInstance]] = {}
        # service_name -> ConsistentHashRouter
        self._routers: dict[str, ConsistentHashRouter] = {}

    def register(
        self,
        service_name: str,
        endpoint: str,
        instance_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ServiceInstance:
        """Register or refresh an Agent service instance."""
        inst_id = instance_id or f"{service_name}@{endpoint}"
        instance = ServiceInstance(
            instance_id=inst_id,
            service_name=service_name,
            endpoint=endpoint.rstrip("/"),
            metadata=metadata or {},
            healthy=True,
            last_heartbeat=time.time(),
        )

        if service_name not in self._services:
            self._services[service_name] = {}

        self._services[service_name][inst_id] = instance
        self._rebuild_router(service_name)
        return instance

    def deregister(self, service_name: str, instance_id: str) -> bool:
        """Deregister an instance from a service."""
        if service_name in self._services and instance_id in self._services[service_name]:
            del self._services[service_name][instance_id]
            self._rebuild_router(service_name)
            return True
        return False

    def heartbeat(self, service_name: str, instance_id: str) -> bool:
        """Update instance heartbeat."""
        if service_name in self._services and instance_id in self._services[service_name]:
            self._services[service_name][instance_id].last_heartbeat = time.time()
            self._services[service_name][instance_id].healthy = True
            return True
        return False

    def mark_unhealthy(self, service_name: str, endpoint: str) -> None:
        """Mark an instance unhealthy by endpoint."""
        if service_name in self._services:
            for inst in self._services[service_name].values():
                if inst.endpoint == endpoint:
                    inst.healthy = False
            self._rebuild_router(service_name)

    def get_router(self, service_name: str) -> ConsistentHashRouter | None:
        """Get the consistent hash router for a specific agent service."""
        self._evict_stale()
        return self._routers.get(service_name)

    def get_healthy_endpoints(self, service_name: str) -> list[str]:
        """Return list of active healthy endpoints for a given service."""
        self._evict_stale()
        instances = self._services.get(service_name, {})
        return [inst.endpoint for inst in instances.values() if inst.healthy]

    def list_services(self) -> dict[str, list[dict[str, Any]]]:
        """List all registered services and their instances."""
        self._evict_stale()
        result: dict[str, list[dict[str, Any]]] = {}
        for svc_name, instances in self._services.items():
            result[svc_name] = [
                {
                    "instance_id": inst.instance_id,
                    "endpoint": inst.endpoint,
                    "healthy": inst.healthy,
                    "metadata": inst.metadata,
                    "last_heartbeat": inst.last_heartbeat,
                }
                for inst in instances.values()
            ]
        return result

    def _rebuild_router(self, service_name: str) -> None:
        """Rebuild consistent hash ring for the service using active healthy endpoints."""
        endpoints = [
            inst.endpoint
            for inst in self._services.get(service_name, {}).values()
            if inst.healthy
        ]
        if endpoints:
            self._routers[service_name] = ConsistentHashRouter(instances=endpoints)
        elif service_name in self._routers:
            del self._routers[service_name]

    def _evict_stale(self) -> None:
        """Evict instances whose heartbeat has expired."""
        now = time.time()
        changed_services: set[str] = set()

        for svc_name, instances in list(self._services.items()):
            for inst_id, inst in list(instances.items()):
                if now - inst.last_heartbeat > self.heartbeat_timeout:
                    del instances[inst_id]
                    changed_services.add(svc_name)

        for svc_name in changed_services:
            self._rebuild_router(svc_name)
