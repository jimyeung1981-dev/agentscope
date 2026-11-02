# -*- coding: utf-8 -*-
"""Independent WebSocket Gateway Layer for Agent Cloud.

Manages persistent client connections, heartbeats, multi-tenant session pinning,
full-duplex streaming, and real-time Barge-in (interruption) without coupling
with specific Agent implementations.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
import httpx
from fastapi import WebSocket, WebSocketDisconnect

from ._proxy import _extract_jwt_claims
from ._registry import ServiceRegistry

logger = logging.getLogger(__name__)


class WebSocketGatewayManager:
    """Manages full-duplex WebSocket connections and bidirectional Agent streaming."""

    def __init__(self, registry: ServiceRegistry) -> None:
        self.registry = registry
        # session_id -> WebSocket
        self.active_connections: dict[str, WebSocket] = {}
        # session_id -> asyncio.Task (active streaming task)
        self.active_tasks: dict[str, asyncio.Task[Any]] = {}

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        """Accept and register client WebSocket connection."""
        await websocket.accept()
        self.active_connections[session_id] = websocket

    def disconnect(self, session_id: str) -> None:
        """Clean up disconnected client."""
        if session_id in self.active_connections:
            del self.active_connections[session_id]
        if session_id in self.active_tasks:
            self.active_tasks[session_id].cancel()
            del self.active_tasks[session_id]

    async def handle_connection(
        self,
        websocket: WebSocket,
        session_id: str,
        service_name: str = "managed-agent",
        token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Main WebSocket loop for an authenticated client session."""
        await self.connect(session_id, websocket)

        # 1. Multi-tenant claims extraction from token or query
        claims: dict[str, Any] = {}
        if token:
            claims = _extract_jwt_claims(token)

        tenant_id = claims.get("tenant_id") or claims.get("org_id") or "default"
        user_id = claims.get("sub") or claims.get("user_id") or "anonymous"
        role = claims.get("role") or "member"

        # Acknowledge connection ready
        await websocket.send_json(
            {
                "type": "connected",
                "session_id": session_id,
                "service": service_name,
                "tenant_id": tenant_id,
                "user_id": user_id,
            },
        )

        client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(120.0))
        own_client = http_client is None

        try:
            while True:
                data = await websocket.receive_text()
                try:
                    payload = json.loads(data)
                except Exception:
                    payload = {"type": "chat", "content": data}

                msg_type = payload.get("type", "chat")

                # Handle Barge-in / User Interruption
                if msg_type in ("barge_in", "interrupt", "cancel"):
                    await self._handle_barge_in(session_id, service_name, client)
                    await websocket.send_json(
                        {
                            "type": "interrupted",
                            "session_id": session_id,
                            "detail": "Streaming generation cancelled by client barge-in.",
                        },
                    )
                    continue

                # Handle Ping Heartbeat
                if msg_type == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": payload.get("timestamp")})
                    continue

                # Handle Chat/Message Execution
                if msg_type == "chat":
                    # Cancel any prior ongoing generation before starting new one
                    if session_id in self.active_tasks and not self.active_tasks[session_id].done():
                        self.active_tasks[session_id].cancel()

                    # Spawn async task for streaming to avoid blocking the WS read loop
                    task = asyncio.create_task(
                        self._stream_agent_response(
                            websocket=websocket,
                            session_id=session_id,
                            service_name=service_name,
                            content=payload.get("content", ""),
                            tenant_id=tenant_id,
                            user_id=user_id,
                            role=role,
                            http_client=client,
                        ),
                    )
                    self.active_tasks[session_id] = task

        except WebSocketDisconnect:
            self.disconnect(session_id)
        except Exception as exc:
            logger.warning("WebSocket error on session %s: %s", session_id, exc)
            self.disconnect(session_id)
        finally:
            if own_client:
                await client.aclose()

    async def _handle_barge_in(
        self,
        session_id: str,
        service_name: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        """Handle immediate user interruption (barge-in)."""
        # 1. Cancel local streaming task
        if session_id in self.active_tasks:
            self.active_tasks[session_id].cancel()
            del self.active_tasks[session_id]

        # 2. Notify backend Agent replica to stop generation
        router = self.registry.get_router(service_name)
        if router:
            target_endpoint = router.route_by_key(session_id)
            try:
                await http_client.post(
                    f"{target_endpoint}/sessions/{session_id}/interrupt",
                    json={"session_id": session_id, "reason": "barge_in"},
                    timeout=2.0,
                )
            except Exception:
                pass

    async def _stream_agent_response(
        self,
        websocket: WebSocket,
        session_id: str,
        service_name: str,
        content: str,
        tenant_id: str,
        user_id: str,
        role: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        """Stream chunks from target Agent service back through WebSocket."""
        # Resolve target backend using Consistent Hash Sticky Ring
        router = self.registry.get_router(service_name)
        if not router:
            await websocket.send_json(
                {
                    "type": "error",
                    "detail": f"No active instances found for service '{service_name}'.",
                },
            )
            return

        target_base = router.route_by_key(session_id)
        url = f"{target_base}/sessions/{session_id}/messages"

        headers = {
            "Content-Type": "application/json",
            "X-Tenant-ID": tenant_id,
            "X-User-ID": user_id,
            "X-User-Role": role,
            "X-Session-ID": session_id,
            "Accept": "text/event-stream, application/json",
        }

        try:
            req = http_client.build_request(
                method="POST",
                url=url,
                headers=headers,
                json={"content": content, "session_id": session_id},
            )
            resp = await http_client.send(req, stream=True)

            if resp.status_code >= 400:
                await websocket.send_json(
                    {
                        "type": "error",
                        "status_code": resp.status_code,
                        "detail": f"Backend agent {target_base} returned error {resp.status_code}",
                    },
                )
                await resp.aclose()
                return

            # Read stream chunks and forward as WebSocket frames
            async for line in resp.aiter_lines():
                if not line:
                    continue
                # If SSE format
                if line.startswith("data:"):
                    raw_data = line[5:].strip()
                    try:
                        parsed = json.loads(raw_data)
                        await websocket.send_json({"type": "chunk", "data": parsed})
                    except Exception:
                        await websocket.send_json({"type": "chunk", "text": raw_data})
                else:
                    await websocket.send_json({"type": "chunk", "text": line})

            await resp.aclose()
            await websocket.send_json({"type": "done", "session_id": session_id})

        except asyncio.CancelledError:
            # Task was interrupted by barge-in
            raise
        except Exception as exc:
            await websocket.send_json(
                {
                    "type": "error",
                    "detail": f"Streaming failed: {exc}",
                },
            )
