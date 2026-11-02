# -*- coding: utf-8 -*-
"""Shared FastAPI dependencies for the agentscope app."""
from fastapi import Depends, Header, HTTPException, Request, status

from .workspace_manager import WorkspaceManagerBase
from .channel import (
    ChannelClients,
    ChannelTypeRegistry,
)
from ._manager import (
    BackgroundTaskManager,
    ChatRunRegistry,
    SchedulerManager,
)
from ._service import (
    ChannelService,
    CredentialBindingService,
    ChatService,
    SOPService,
    KnowledgeBaseService,
    RealtimeService,
    ResourceAccessService,
    SessionService,
    WorkspaceService,
)
from ._types import AgentMiddlewareFactory, AgentToolFactory
from .hub import MCPHubBase, SkillHubBase
from .message_bus import MessageBus
from .rag.blob_store import BlobStoreBase
import base64
import json
from .access import TenantIdentity, set_current_tenant
from .rag.knowledge_base_manager import KnowledgeBaseManagerBase
from .storage import StorageBase
from ..rag import ChunkerBase, ParserBase


def _parse_jwt_payload(token: str) -> dict:
    """Best-effort decode of JWT payload without verifying signature."""
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            decoded = base64.urlsafe_b64decode(padded)
            return json.loads(decoded)
    except Exception:
        pass
    return {}


async def get_current_identity(
    request: Request,
    x_user_id: str | None = Header(None, description="Caller's user ID"),
    x_tenant_id: str | None = Header(None, description="Caller's tenant ID"),
    authorization: str | None = Header(None, description="Bearer JWT token"),
) -> TenantIdentity:
    """Resolve full multi-tenant identity from JWT or request headers.

    Extracts `user_id` (or `sub`), `tenant_id`, and `role`. Sets the active
    tenant context variable so downstream services and sandboxes can access it.
    """
    user_id = x_user_id if isinstance(x_user_id, str) else None
    tenant_id = x_tenant_id if isinstance(x_tenant_id, str) else None
    role = "member"

    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        claims = _parse_jwt_payload(token)
        if claims:
            user_id = user_id or claims.get("sub") or claims.get("user_id")
            tenant_id = tenant_id or claims.get("tenant_id") or claims.get("org_id")
            role = claims.get("role", role)

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User identity is required (via X-User-ID header or Bearer JWT).",
        )

    resolved_tenant_id = tenant_id or "default"
    identity = TenantIdentity(
        tenant_id=resolved_tenant_id,
        user_id=user_id,
        role=role,
    )
    set_current_tenant(identity)
    return identity


async def get_current_tenant_id(
    identity: TenantIdentity = Depends(get_current_identity),
) -> str:
    """Return the caller's tenant ID."""
    return identity.tenant_id


async def get_current_user_id(
    identity: TenantIdentity = Depends(get_current_identity),
) -> str:
    """Return the caller's user ID, preserving backwards-compatibility."""
    return identity.user_id


async def get_storage(request: Request) -> StorageBase:
    """Return the application-wide storage backend.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `StorageBase`: The storage instance stored in ``app.state``.
    """
    return request.app.state.storage


async def get_message_bus(request: Request) -> MessageBus:
    """Return the application-wide message bus.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `MessageBus`: The message bus instance stored in ``app.state``.
    """
    return request.app.state.message_bus


async def get_chat_service(request: Request) -> ChatService:
    """Return the application-wide chat service.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `ChatService`: The chat service instance stored in ``app.state``.
    """
    return request.app.state.chat_service


async def get_realtime_service(request: Request) -> RealtimeService:
    """Return the application-wide realtime agent assembly service."""
    return request.app.state.realtime_service


async def get_resource_access_service(
    request: Request,
) -> ResourceAccessService:
    """Return the application-wide resource access service.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `ResourceAccessService`:
            The access service stored in ``app.state`` — the single
            entry point routers should use to resolve
            credential / agent / knowledge base records.
    """
    return request.app.state.resource_access_service


async def get_session_service(request: Request) -> SessionService:
    """Return the application-wide session service.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `SessionService`: The session service instance stored in
        ``app.state``.
    """
    return request.app.state.session_service


async def get_workspace_service(request: Request) -> WorkspaceService:
    """Return the application-wide workspace service.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `WorkspaceService`: The instance stored in ``app.state``.
    """
    return request.app.state.workspace_service


async def get_chat_run_registry(request: Request) -> ChatRunRegistry:
    """Return the per-process chat-run registry.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `ChatRunRegistry`: The registry stored in ``app.state``.
    """
    return request.app.state.chat_run_registry


async def get_scheduler_manager(request: Request) -> SchedulerManager:
    """Return the application-wide scheduler manager.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `SchedulerManager`: The scheduler manager stored in ``app.state``.
    """
    return request.app.state.scheduler_manager


async def get_background_task_manager(
    request: Request,
) -> BackgroundTaskManager:
    """Return the application-wide background task manager.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `BackgroundTaskManager`: The background task manager stored in
        ``app.state``.
    """
    return request.app.state.background_task_manager


async def get_sop_service(request: Request) -> SOPService:
    """Return the application-wide SOP service.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `SOPService`: The SOP service instance stored in ``app.state``.
    """
    return request.app.state.sop_service


async def get_workspace_manager(request: Request) -> WorkspaceManagerBase:
    """Return the application-wide workspace manager.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `WorkspaceManagerBase`: The workspace manager stored in ``app.state``.
    """
    return request.app.state.workspace_manager


async def get_download_secret(request: Request) -> str:
    """Return the secret that signs file-download tokens.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `str`: The signing secret stored in ``app.state``.
    """
    return request.app.state.download_secret


async def get_extra_agent_middlewares(
    request: Request,
) -> AgentMiddlewareFactory | None:
    """Return the caller-supplied agent middleware factory, if any.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `AgentMiddlewareFactory | None`: The factory passed to
        :func:`~agentscope.app.create_app`, or ``None`` if not configured.
    """
    return request.app.state.extra_agent_middlewares


async def get_extra_agent_tools(
    request: Request,
) -> AgentToolFactory | None:
    """Return the caller-supplied agent tool factory, if any.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `AgentToolFactory | None`: The factory passed to
        :func:`~agentscope.app.create_app`, or ``None`` if not configured.
    """
    return request.app.state.extra_agent_tools


async def get_knowledge_base_service(
    request: Request,
) -> KnowledgeBaseService:
    """Return the application-wide knowledge base service.

    Args:
        request (`Request`):
            The incoming FastAPI request.

    Returns:
        `KnowledgeBaseService`:
            The service stored in ``app.state``.

    Raises:
        `HTTPException`:
            ``503`` when the app was created without a
            ``knowledge_base_manager`` and therefore exposes no
            knowledge base endpoints.
    """
    service = getattr(request.app.state, "knowledge_base_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Knowledge base feature is disabled — pass a "
                "knowledge_base_manager to create_app() to enable it."
            ),
        )
    return service


async def get_knowledge_base_manager(
    request: Request,
) -> KnowledgeBaseManagerBase:
    """Return the application-wide knowledge base manager.

    Args:
        request (`Request`):
            The incoming FastAPI request.

    Returns:
        `KnowledgeBaseManagerBase`:
            The manager stored in ``app.state``.

    Raises:
        `HTTPException`:
            ``503`` when the app was created without a
            ``knowledge_base_manager``.
    """
    manager = getattr(request.app.state, "knowledge_base_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Knowledge base feature is disabled — pass a "
                "knowledge_base_manager to create_app() to enable it."
            ),
        )
    return manager


async def get_blob_store(request: Request) -> BlobStoreBase:
    """Return the application-wide blob store.

    Args:
        request (`Request`):
            The incoming FastAPI request.

    Returns:
        `BlobStoreBase`:
            The blob store instance stored in ``app.state``.

    Raises:
        `HTTPException`:
            ``503`` when no blob store is configured (e.g. the KB
            feature was disabled at app-creation time).
    """
    blob_store = getattr(request.app.state, "blob_store", None)
    if blob_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Blob store is not configured — pass a "
                "knowledge_base_manager (and optionally a blob_store) "
                "to create_app() to enable knowledge base features."
            ),
        )
    return blob_store


async def get_knowledge_parsers(
    request: Request,
) -> list[ParserBase] | dict[str, ParserBase]:
    """Return the parser registry configured on the app.

    Args:
        request (`Request`):
            The incoming FastAPI request.

    Returns:
        `list[ParserBase] | dict[str, ParserBase]`:
            The parser registry stored in ``app.state.knowledge_parsers``
            — the same value the index worker uses to dispatch uploads.

    Raises:
        `HTTPException`:
            ``503`` when the KB feature is disabled (no parsers
            configured).
    """
    parsers = getattr(request.app.state, "knowledge_parsers", None)
    if not parsers:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Knowledge base feature is disabled — pass a "
                "knowledge_base_manager to create_app() to enable it."
            ),
        )
    return parsers


async def get_knowledge_chunkers(
    request: Request,
) -> list[type[ChunkerBase]]:
    """Return the chunker classes configured on the app.

    Args:
        request (`Request`):
            The incoming FastAPI request.

    Returns:
        `list[type[ChunkerBase]]`:
            The chunker classes stored in ``app.state.knowledge_chunkers``
            — the same value the index worker uses to rebuild chunkers.

    Raises:
        `HTTPException`:
            ``503`` when the KB feature is disabled (no chunkers
            configured).
    """
    chunkers = getattr(request.app.state, "knowledge_chunkers", None)
    if not chunkers:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Knowledge base feature is disabled — pass a "
                "knowledge_base_manager to create_app() to enable it."
            ),
        )
    return chunkers


async def get_mcp_hubs(request: Request) -> dict[str, MCPHubBase]:
    """Return the registered MCP hubs, keyed by hub id.

    Args:
        request (`Request`):
            The incoming FastAPI request.

    Returns:
        `dict[str, MCPHubBase]`:
            The hubs stored in ``app.state.mcp_hubs``, empty when none
            were passed to ``create_app``.
    """
    return getattr(request.app.state, "mcp_hubs", {})


async def get_skill_hubs(request: Request) -> dict[str, SkillHubBase]:
    """Return the registered skill hubs, keyed by hub id.

    Args:
        request (`Request`):
            The incoming FastAPI request.

    Returns:
        `dict[str, SkillHubBase]`:
            The hubs stored in ``app.state.skill_hubs``, empty when none
            were passed to ``create_app``.
    """
    return getattr(request.app.state, "skill_hubs", {})


async def get_channel_service(request: Request) -> ChannelService:
    """Return the application-wide channel CRUD service.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `ChannelService`: The service stored in ``app.state``.
    """
    return request.app.state.channel_service


async def get_credential_binding_service(
    request: Request,
) -> CredentialBindingService:
    """Return the application-wide credential-binding service.

    Present in every process: a binding session lives in the bus, so any
    replica can serve any step of it.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `CredentialBindingService`: The service stored in ``app.state``.
    """
    return request.app.state.credential_binding_service


async def get_channel_clients(
    request: Request,
) -> ChannelClients:
    """Return the factory for unconnected channel instances.

    Present in every process, whether or not this one holds the
    channels' long connections.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `ChannelClients`: The factory stored in ``app.state``.
    """
    return request.app.state.channel_clients


async def get_channel_type_registry(
    request: Request,
) -> ChannelTypeRegistry:
    """Return the registry of channel types allowed by this service.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `ChannelTypeRegistry`: The registry built in ``create_app`` from
        the ``channels`` list (empty when none were passed).
    """
    return request.app.state.channel_type_registry
