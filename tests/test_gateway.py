# -*- coding: utf-8 -*-
"""Unit tests for the independent Agent Cloud API Gateway and WebSocket Layer."""
import base64
import json
import pytest
from httpx import AsyncClient, ASGITransport
from starlette.testclient import TestClient

from agentscope.gateway import (
    GatewayConfig,
    ConsistentHashRouter,
    ServiceRegistry,
    RateLimiter,
    CircuitBreaker,
    create_gateway_app,
)


def test_consistent_hash_router_affinity():
    """Verify consistent hashing pins the same session_id to the same backend."""
    upstreams = [
        "http://agent-node-1:8000",
        "http://agent-node-2:8000",
        "http://agent-node-3:8000",
    ]
    router = ConsistentHashRouter(instances=upstreams)

    target_a1 = router.route_by_key("session_12345")
    target_a2 = router.route_by_key("session_12345")
    assert target_a1 in upstreams
    assert target_a1 == target_a2

    # Extract session_id
    assert router.extract_session_id("/sessions/session_abc/stream") == "session_abc"
    assert router.extract_session_id("/chat", query_string="session_id=session_xyz") == "session_xyz"
    assert router.extract_session_id("/chat", headers={"x-session-id": "session_hdr"}) == "session_hdr"


def test_service_discovery_and_dynamic_registration():
    """Verify ServiceRegistry allows dynamic discovery and multi-agent service registration."""
    registry = ServiceRegistry()

    # 1. Register Managed Agent instances
    registry.register("managed-agent", "http://node-managed-1:8000")
    registry.register("managed-agent", "http://node-managed-2:8000")

    # 2. Register Code Agent instances
    registry.register("code-agent", "http://node-code-1:8000")

    services = registry.list_services()
    assert "managed-agent" in services
    assert len(services["managed-agent"]) == 2
    assert "code-agent" in services
    assert len(services["code-agent"]) == 1

    # Routers per service
    router_managed = registry.get_router("managed-agent")
    assert router_managed is not None
    assert len(router_managed.instances) == 2

    # Deregister instance
    inst_id = services["managed-agent"][0]["instance_id"]
    registry.deregister("managed-agent", inst_id)
    assert len(registry.get_healthy_endpoints("managed-agent")) == 1


def test_rate_limiter_and_circuit_breaker():
    """Verify TokenBucket RateLimiter and CircuitBreaker state transitions."""
    # 1. Rate Limiter
    limiter = RateLimiter(requests_per_minute=60, burst=2)
    assert limiter.acquire("tenant_1") is True
    assert limiter.acquire("tenant_1") is True
    assert limiter.acquire("tenant_1") is False  # Burst exhausted

    # 2. Circuit Breaker
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=0.5)
    target = "http://bad-backend:8000"
    assert breaker.can_execute(target) is True

    breaker.record_failure(target)
    assert breaker.can_execute(target) is True  # Failure count = 1

    breaker.record_failure(target)  # Failure count = 2 -> Tripped to OPEN
    assert breaker.can_execute(target) is False
    assert breaker.get_status(target).state.value == "OPEN"


@pytest.mark.asyncio
async def test_gateway_health_endpoint():
    """Verify gateway health check returns status and service registry."""
    config = GatewayConfig(
        upstream_instances=["http://10.0.0.1:8000"],
        enable_sticky_sessions=True,
    )
    app = create_gateway_app(config)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "managed-agent" in data["services"]
        assert data["rate_limiting_enabled"] is True
        assert data["circuit_breaker_enabled"] is True


@pytest.mark.asyncio
async def test_gateway_jwt_injection_and_proxy_routing():
    """Verify gateway extracts JWT claims, applies rate limiting, and injects multi-tenant headers."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    mock_backend = FastAPI()

    @mock_backend.api_route("/{path:path}", methods=["GET", "POST"])
    async def echo_headers(request: Request, path: str):
        return JSONResponse(
            content={
                "received_path": path,
                "x_user_id": request.headers.get("x-user-id"),
                "x_tenant_id": request.headers.get("x-tenant-id"),
                "x_user_role": request.headers.get("x-user-role"),
                "x_agent_service": request.headers.get("x-agent-service"),
            },
        )

    gateway_app = create_gateway_app(
        GatewayConfig(upstream_instances=["http://mock-backend.test"]),
    )

    backend_transport = ASGITransport(app=mock_backend)
    mock_http_client = AsyncClient(transport=backend_transport, base_url="http://mock-backend.test")
    gateway_app.state.http_client = mock_http_client

    payload = {"sub": "user_finance_99", "tenant_id": "tenant_alipay", "role": "admin"}
    token = "hdr." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".sig"

    gateway_transport = ASGITransport(app=gateway_app)
    async with AsyncClient(transport=gateway_transport, base_url="http://gateway.test") as client:
        resp = await client.post(
            "/sessions/sess_999/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": "hello agent"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("X-Accel-Buffering") == "no"

        data = resp.json()
        assert data["x_user_id"] == "user_finance_99"
        assert data["x_tenant_id"] == "tenant_alipay"
        assert data["x_user_role"] == "admin"
        assert data["x_agent_service"] == "managed-agent"

    await mock_http_client.aclose()


def test_dedicated_websocket_gateway_layer():
    """Verify dedicated WebSocket gateway layer connects, sends tokens, and handles barge-in."""
    from fastapi import FastAPI, Request
    from fastapi.responses import PlainTextResponse

    mock_agent = FastAPI()

    @mock_agent.post("/sessions/{session_id}/messages")
    async def mock_agent_stream(session_id: str, request: Request):
        return PlainTextResponse("data: {\"token\": \"Hello\"}\n\ndata: {\"token\": \"World\"}\n\n")

    @mock_agent.post("/sessions/{session_id}/interrupt")
    async def mock_agent_interrupt(session_id: str):
        return {"status": "cancelled", "session_id": session_id}

    config = GatewayConfig(upstream_instances=["http://mock-agent.test"])
    gateway_app = create_gateway_app(config)

    # Attach mock HTTP client for the WS layer to talk to mock_agent
    backend_transport = ASGITransport(app=mock_agent)
    mock_client = AsyncClient(transport=backend_transport, base_url="http://mock-agent.test")
    gateway_app.state.http_client = mock_client

    # Connect WebSocket client
    with TestClient(gateway_app) as client:
        with client.websocket_connect("/ws/sessions/session_ws_101") as ws:
            # 1. Initial connect acknowledgement
            init_msg = ws.receive_json()
            assert init_msg["type"] == "connected"
            assert init_msg["session_id"] == "session_ws_101"

            # 2. Heartbeat ping
            ws.send_json({"type": "ping", "timestamp": 123456})
            pong_msg = ws.receive_json()
            assert pong_msg["type"] == "pong"

            # 3. Barge-in interruption
            ws.send_json({"type": "barge_in"})
            interrupted_msg = ws.receive_json()
            assert interrupted_msg["type"] == "interrupted"
