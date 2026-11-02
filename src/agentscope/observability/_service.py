# -*- coding: utf-8 -*-
"""Telemetry and Observability Ingestion Service for Agent Cloud."""
import time
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from .._utils._common import _generate_id


class TelemetrySource(str, Enum):
    """Source component emitting telemetry data."""

    GATEWAY = "gateway"  # Network edge, TTFT, RTT, Barge-in, 429/503
    AGENT = "agent"  # ReAct steps, Token usage, Model latency, Tool Spans
    CONNECTOR = "connector"  # Data source queries, AST checks, row counts


class TelemetrySpan(BaseModel):
    """Distributed tracing span payload submitted via Ingestion API."""

    span_id: str = Field(default_factory=lambda: f"span_{_generate_id()}")
    trace_id: str
    parent_span_id: Optional[str] = None
    source: TelemetrySource
    name: str  # e.g., "gateway_http_proxy", "agent_react_loop", "tool_execution"
    tenant_id: str
    user_id: Optional[str] = None
    agent_service: Optional[str] = None
    session_id: Optional[str] = None
    start_time: float = Field(default_factory=time.time)
    duration_ms: float = 0.0
    status: str = "ok"  # "ok", "error"
    attributes: Dict[str, Any] = Field(default_factory=dict)


class TelemetryAuditRecord(BaseModel):
    """Structured token consumption and billing audit record."""

    audit_id: str = Field(default_factory=lambda: f"audit_{_generate_id()}")
    trace_id: Optional[str] = None
    source: TelemetrySource
    tenant_id: str
    user_id: Optional[str] = None
    agent_service: Optional[str] = None
    session_id: Optional[str] = None
    model_name: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    latency_ms: float = 0.0
    status: str = "success"
    timestamp: float = Field(default_factory=time.time)


class TelemetryIngestionService:
    """Agent Cloud Platform Observability Ingestion API Service.

    Dual-Producer Architecture:
    1. Gateway Layer: Ingests boundary/network spans (HTTP latency, TTFT,
       WebSocket heartbeats, 429 rate limit triggers, Barge-in interruptions).
    2. Agent Compute Layer: Ingests internal reasoning spans (ReAct thinking steps,
       LLM token usage, tool invocations, parameter self-correction).
    3. Trace Stitching: Seamlessly merges spans from Gateway and Agent under the
       same `trace_id` (W3C TraceContext) to produce a unified waterfall trace.
    """

    def __init__(self) -> None:
        # In-memory storage for spans and audits (can persist to PostgreSQL/OTel backend)
        self._spans: List[TelemetrySpan] = []
        self._audits: List[TelemetryAuditRecord] = []

    def ingest_span(self, span: TelemetrySpan) -> str:
        """Ingest a distributed tracing span from either Gateway or Agent."""
        if not span.tenant_id:
            raise ValueError("tenant_id is required for observability isolation")
        if not span.trace_id:
            raise ValueError("trace_id is required for distributed trace correlation")
        self._spans.append(span)
        return span.span_id

    def ingest_audit(self, audit: TelemetryAuditRecord) -> str:
        """Ingest a billing and token usage audit record from either Gateway or Agent."""
        if not audit.tenant_id:
            raise ValueError("tenant_id is required for audit isolation")
        if audit.total_tokens == 0:
            audit.total_tokens = audit.prompt_tokens + audit.completion_tokens
        self._audits.append(audit)
        return audit.audit_id

    def get_trace_waterfall(
        self,
        tenant_id: str,
        trace_id: str,
    ) -> List[Dict[str, Any]]:
        """Retrieve stitched end-to-end trace spans ordered by execution start time."""
        matched = [
            s.model_dump()
            for s in self._spans
            if s.tenant_id == tenant_id and s.trace_id == trace_id
        ]
        matched.sort(key=lambda x: x["start_time"])
        return matched

    def list_audits(
        self,
        tenant_id: str,
        session_id: Optional[str] = None,
        source: Optional[TelemetrySource] = None,
    ) -> List[Dict[str, Any]]:
        """List tenant billing and audit records with optional filters."""
        results = []
        for a in self._audits:
            if a.tenant_id == tenant_id:
                if session_id and a.session_id != session_id:
                    continue
                if source and a.source != source:
                    continue
                results.append(a.model_dump())
        return results
