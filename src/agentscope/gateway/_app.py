# -*- coding: utf-8 -*-
"""FastAPI application factory for the independent Agent Cloud API Gateway."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
import httpx
from fastapi import FastAPI, Request, Response, WebSocket, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ._config import GatewayConfig
from ._limiter import CircuitBreaker, RateLimiter
from ._proxy import proxy_request
from ._registry import ServiceRegistry
from ._websocket import WebSocketGatewayManager


class ServiceRegistrationRequest(BaseModel):
    """Payload to dynamically register an Agent service instance."""

    endpoint: str
    instance_id: str | None = None
    metadata: dict[str, Any] | None = None


def create_gateway_app(config: GatewayConfig | None = None) -> FastAPI:
    """Create and configure an independent Agent Cloud API Gateway application.

    Serves as the unified, decoupled entry point for the entire Agent Cloud platform:
    - Service Discovery & Dynamic Agent Registration
    - Dynamic Routing & Dispatching across heterogeneous Agent services
    - Multi-tenant JWT Authentication & Header Injection (Token Relay)
    - Rate Limiting (Token Bucket) & Circuit Breaker Fault Isolation
    - Session-affinity Consistent Hash Routing (Sticky Sessions)
    - Dedicated WebSocket Gateway Layer for real-time streaming & Barge-in
    """
    cfg = config or GatewayConfig()

    # 1. Initialize Service Discovery & Registry
    registry = ServiceRegistry()
    for svc_name, endpoints in cfg.services.items():
        for ep in endpoints:
            registry.register(service_name=svc_name, endpoint=ep)
    # Also support cfg.upstream_instances as default service
    for ep in cfg.upstream_instances:
        registry.register(service_name=cfg.default_service_name, endpoint=ep)

    # 2. Initialize Traffic Governance
    rate_limiter = (
        RateLimiter(
            requests_per_minute=cfg.rate_limit_per_minute,
            burst=cfg.rate_limit_burst,
        )
        if cfg.enable_rate_limiting
        else None
    )

    circuit_breaker = (
        CircuitBreaker(
            failure_threshold=cfg.circuit_breaker_failure_threshold,
            recovery_timeout=cfg.circuit_breaker_recovery_timeout,
        )
        if cfg.enable_circuit_breaker
        else None
    )

    # 3. Dedicated WebSocket Gateway Manager
    ws_manager = WebSocketGatewayManager(registry=registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=100, max_connections=500),
            follow_redirects=False,
        )
        app.state.http_client = client
        yield {
            "http_client": client,
            "registry": registry,
            "ws_manager": ws_manager,
            "rate_limiter": rate_limiter,
            "circuit_breaker": circuit_breaker,
        }
        await client.aclose()

    app = FastAPI(
        title=cfg.title,
        version="2.0.12dev",
        lifespan=lifespan,
    )

    # Populate app.state early for direct testing and lifespan compatibility
    app.state.config = cfg
    app.state.registry = registry
    app.state.ws_manager = ws_manager
    app.state.rate_limiter = rate_limiter
    app.state.circuit_breaker = circuit_breaker

    # CORS
    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ==================== Health & Service Registry APIs ====================

    @app.get("/health", summary="Gateway health check")
    async def health_check() -> dict[str, Any]:
        """Report gateway status, registered services and traffic governance."""
        return {
            "status": "healthy",
            "service": "agent-cloud-gateway",
            "services": registry.list_services(),
            "sticky_sessions_enabled": cfg.enable_sticky_sessions,
            "rate_limiting_enabled": cfg.enable_rate_limiting,
            "circuit_breaker_enabled": cfg.enable_circuit_breaker,
        }

    @app.get("/gateway/services", summary="List registered Agent services")
    async def list_services() -> dict[str, Any]:
        """List all discovered agent services and healthy instances."""
        return {"services": registry.list_services()}

    @app.post("/gateway/services/{service_name}/register", summary="Register agent instance")
    async def register_service_instance(
        service_name: str,
        payload: ServiceRegistrationRequest,
    ) -> dict[str, Any]:
        """Dynamically register a new Agent instance (e.g. managed-agent, code-agent)."""
        instance = registry.register(
            service_name=service_name,
            endpoint=payload.endpoint,
            instance_id=payload.instance_id,
            metadata=payload.metadata,
        )
        return {
            "status": "registered",
            "service_name": service_name,
            "instance_id": instance.instance_id,
            "endpoint": instance.endpoint,
        }

    @app.post("/gateway/services/{service_name}/deregister", summary="Deregister agent instance")
    async def deregister_service_instance(
        service_name: str,
        instance_id: str = Query(...),
    ) -> dict[str, Any]:
        """Remove an Agent instance from the service registry."""
        success = registry.deregister(service_name=service_name, instance_id=instance_id)
        return {"status": "deregistered" if success else "not_found", "instance_id": instance_id}

    # ==================== Dedicated WebSocket Gateway Layer ====================

    @app.websocket("/ws/{service_name}/sessions/{session_id}")
    async def websocket_service_session_endpoint(
        websocket: WebSocket,
        service_name: str,
        session_id: str,
        token: str | None = Query(default=None),
    ) -> None:
        """Dedicated WebSocket full-duplex session endpoint for a specific Agent service."""
        client = getattr(websocket.app.state, "http_client", None)
        await ws_manager.handle_connection(
            websocket=websocket,
            session_id=session_id,
            service_name=service_name,
            token=token,
            http_client=client,
        )

    @app.websocket("/ws/sessions/{session_id}")
    async def websocket_default_session_endpoint(
        websocket: WebSocket,
        session_id: str,
        token: str | None = Query(default=None),
    ) -> None:
        """Dedicated WebSocket full-duplex session endpoint for default Managed Agent."""
        client = getattr(websocket.app.state, "http_client", None)
        await ws_manager.handle_connection(
            websocket=websocket,
            session_id=session_id,
            service_name=cfg.default_service_name,
            token=token,
            http_client=client,
        )

    # ==================== HTTP Reverse Proxy ====================

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        summary="Reverse proxy traffic across Agent Cloud",
        include_in_schema=False,
    )
    async def reverse_proxy(request: Request, path: str) -> Response:
        """Route HTTP/SSE traffic to the target Agent service in Agent Cloud."""
        client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
        if client is None:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(cfg.timeout_seconds, connect=10.0),
                follow_redirects=False,
            )
            request.app.state.http_client = client

        cur_registry: ServiceRegistry = getattr(request.app.state, "registry", registry)
        cur_limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", rate_limiter)
        cur_breaker: CircuitBreaker | None = getattr(request.app.state, "circuit_breaker", circuit_breaker)

        return await proxy_request(
            request=request,
            registry=cur_registry,
            http_client=client,
            rate_limiter=cur_limiter,
            circuit_breaker=cur_breaker,
            default_service=cfg.default_service_name,
            sticky_sessions=cfg.enable_sticky_sessions,
        )

    return app
