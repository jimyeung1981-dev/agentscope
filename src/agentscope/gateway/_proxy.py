# -*- coding: utf-8 -*-
"""Streaming reverse proxy handler for the Agent Cloud Gateway."""
from __future__ import annotations

import base64
import json
from typing import Any, AsyncIterator
import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from ._limiter import CircuitBreaker, RateLimiter
from ._registry import ServiceRegistry


# Hop-by-hop headers to strip during proxying
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _extract_jwt_claims(token: str) -> dict[str, Any]:
    """Extract claims from a Bearer JWT without signature check."""
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            decoded = base64.urlsafe_b64decode(padded)
            return json.loads(decoded)
    except Exception:
        pass
    return {}


async def proxy_request(
    request: Request,
    registry: ServiceRegistry,
    http_client: httpx.AsyncClient,
    rate_limiter: RateLimiter | None = None,
    circuit_breaker: CircuitBreaker | None = None,
    default_service: str = "managed-agent",
    sticky_sessions: bool = True,
) -> Response:
    """Proxy an inbound HTTP request to the target Agent service in Agent Cloud."""
    path = request.url.path
    query = request.url.query
    raw_headers = dict(request.headers)

    # 1. Determine target Agent service name and forward path
    # Paths like /agents/{service_name}/... route to specific Agent service
    target_service = default_service
    forward_path = path
    if path.startswith("/agents/"):
        parts = path.strip("/").split("/", 2)
        if len(parts) >= 2:
            target_service = parts[1]
            forward_path = f"/{parts[2]}" if len(parts) > 2 else "/"

    # 2. Multi-tenant JWT inspection
    tenant_id = "default"
    user_id = "anonymous"
    role = "member"

    auth = raw_headers.get("authorization") or raw_headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        token = auth[7:].strip()
        claims = _extract_jwt_claims(token)
        if claims:
            user_id = str(claims.get("sub") or claims.get("user_id") or "anonymous")
            tenant_id = str(claims.get("tenant_id") or claims.get("org_id") or "default")
            role = str(claims.get("role", "member"))

    # Also support explicit tenant headers if present
    if "x-tenant-id" in raw_headers:
        tenant_id = raw_headers["x-tenant-id"]
    if "x-user-id" in raw_headers:
        user_id = raw_headers["x-user-id"]

    # 3. Rate Limiting Check
    if rate_limiter:
        rate_key = tenant_id if tenant_id != "default" else (request.client.host if request.client else "unknown")
        if not rate_limiter.acquire(rate_key):
            return Response(
                content=json.dumps(
                    {
                        "detail": f"Rate limit exceeded for tenant '{tenant_id}'. Please slow down.",
                        "error_code": "RATE_LIMIT_EXCEEDED",
                    },
                ),
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": "60"},
            )

    # 4. Service Discovery & Consistent Hash Sticky Routing
    router = registry.get_router(target_service)
    if not router:
        return Response(
            content=json.dumps(
                {
                    "detail": f"Service '{target_service}' has no available healthy instances in Agent Cloud.",
                    "error_code": "SERVICE_UNAVAILABLE",
                },
            ),
            status_code=503,
            media_type="application/json",
        )

    target_base = router.resolve_target(
        path=forward_path,
        query_string=query,
        headers=raw_headers,
        sticky=sticky_sessions,
    )

    # 5. Circuit Breaker Check
    if circuit_breaker and not circuit_breaker.can_execute(target_base):
        return Response(
            content=json.dumps(
                {
                    "detail": f"Circuit breaker OPEN for target '{target_base}'. Service temporarily disabled.",
                    "error_code": "CIRCUIT_BREAKER_OPEN",
                },
            ),
            status_code=503,
            media_type="application/json",
        )

    target_url = f"{target_base}{forward_path}"
    if query:
        target_url = f"{target_url}?{query}"

    # 6. Filter hop-by-hop headers & Inject Multi-tenant headers
    forward_headers = {
        k: v for k, v in raw_headers.items() if k.lower() not in _HOP_BY_HOP
    }
    forward_headers["X-Tenant-ID"] = tenant_id
    forward_headers["X-User-ID"] = user_id
    forward_headers["X-User-Role"] = role
    forward_headers["X-Agent-Service"] = target_service

    if request.client:
        forward_headers["X-Forwarded-For"] = request.client.host

    body = await request.body()

    # 7. Forward request
    req = http_client.build_request(
        method=request.method,
        url=target_url,
        headers=forward_headers,
        content=body,
    )

    try:
        upstream_resp = await http_client.send(req, stream=True)
        if circuit_breaker:
            if upstream_resp.status_code >= 500:
                circuit_breaker.record_failure(target_base)
            else:
                circuit_breaker.record_success(target_base)
    except httpx.ConnectError as exc:
        if circuit_breaker:
            circuit_breaker.record_failure(target_base)
        return Response(
            content=json.dumps(
                {
                    "detail": f"Upstream Agent node {target_base} ({target_service}) is unavailable: {exc}",
                    "error_code": "UPSTREAM_CONNECT_ERROR",
                },
            ),
            status_code=503,
            media_type="application/json",
        )

    # 8. Stream response from target backend
    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length"
    }
    # Tell downstream reverse proxies (Nginx, Cloudflare) not to buffer streaming tokens
    resp_headers["X-Accel-Buffering"] = "no"
    resp_headers["X-Gateway-Target"] = target_base
    resp_headers["X-Agent-Service"] = target_service

    async def stream_content() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        stream_content(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
