# -*- coding: utf-8 -*-
"""Artifact models for the Platform Deliverables and Artifacts Management Service."""
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class ArtifactType(str, Enum):
    """Classification of deliverables and artifacts produced by agents."""

    CODE = "code"          # Source code files (.py, .ts, .sh, .sql)
    DOCUMENT = "document"  # Markdown reports, PDFs, Word docs
    IMAGE = "image"        # PNG, SVG, charts, diagrams
    DATA = "data"          # CSV, Parquet, JSON data files
    DIFF = "diff"          # Git patches, unified diffs
    OTHER = "other"        # Miscellaneous binary assets


class ArtifactRecord(BaseModel):
    """Pydantic model representing artifact metadata stored in the database."""

    artifact_id: str = Field(description="Unique identifier of the artifact")
    tenant_id: str = Field(description="Tenant ID for strict multi-tenant isolation")
    user_id: Optional[str] = Field(default=None, description="User ID who owns or triggered creation")
    session_id: Optional[str] = Field(default=None, description="Session ID where artifact was produced")
    name: str = Field(description="Human-readable filename (e.g., 'backtest_strategy.py')")
    type: ArtifactType = Field(default=ArtifactType.CODE, description="Artifact classification")
    version: int = Field(default=1, description="Version increment number (1, 2, 3...)")
    s3_bucket: str = Field(description="S3 bucket where the raw payload is stored")
    s3_key: str = Field(description="S3 object key path")
    size_bytes: int = Field(default=0, description="Payload size in bytes")
    content_type: str = Field(default="text/plain", description="MIME content type")
    checksum: Optional[str] = Field(default=None, description="SHA-256 hash of the payload")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Custom metadata, e.g. language, agent name")
    created_at: float = Field(description="Creation unix timestamp")
    updated_at: float = Field(description="Last updated unix timestamp")

    @property
    def s3_uri(self) -> str:
        """Return the standard s3:// URI."""
        return f"s3://{self.s3_bucket}/{self.s3_key}"
