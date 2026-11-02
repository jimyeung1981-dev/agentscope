# -*- coding: utf-8 -*-
"""Data Source Registration and Metadata Schema for Agent Cloud."""
import time
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from .._utils._common import _generate_id


class DataSourceType(str, Enum):
    """Supported heterogeneous enterprise data source types."""

    SQL = "sql"  # PostgreSQL, MySQL, ClickHouse, Oracle
    VECTOR = "vector"  # pgvector, Milvus, Qdrant, Pinecone
    BLOB = "blob"  # S3, MinIO, OSS
    REST_API = "rest_api"  # OpenAPI, GraphQL, SaaS Webhooks


class DataSourceMetadata(BaseModel):
    """Registered data source specification.

    Crucially, this contains NO raw passwords or direct connection strings.
    Physical connection details are decoupled and managed via `credential_id`
    in the Platform Credential Vault.
    """

    datasource_id: str = Field(default_factory=lambda: f"ds_{_generate_id()}")
    tenant_id: str
    name: str
    type: DataSourceType
    description: str = ""
    credential_id: str  # Reference to PlatformCredentialVault
    schema_metadata: Dict[str, Any] = Field(default_factory=dict)
    read_only: bool = True
    max_rows: int = 1000
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class DataSourceRegistry:
    """Agent Cloud Platform Data Source Registration Service.

    Enterprises bring their own heterogeneous data assets (BYOD) and register
    them into this registry. Agents reference only the `datasource_id` without
    holding or persisting any connection configs.
    """

    def __init__(self) -> None:
        # Keyed by (tenant_id, datasource_id)
        self._sources: Dict[str, DataSourceMetadata] = {}

    def register_datasource(
        self,
        tenant_id: str,
        name: str,
        type: DataSourceType,
        credential_id: str,
        description: str = "",
        schema_metadata: Optional[Dict[str, Any]] = None,
        read_only: bool = True,
        max_rows: int = 1000,
        datasource_id: Optional[str] = None,
    ) -> DataSourceMetadata:
        """Register an enterprise data source into the platform."""
        if not tenant_id:
            raise ValueError("tenant_id is required for data source isolation")
        if not credential_id:
            raise ValueError(
                "credential_id pointing to Credential Vault is required",
            )

        ds_id = datasource_id or f"ds_{_generate_id()}"
        meta = DataSourceMetadata(
            datasource_id=ds_id,
            tenant_id=tenant_id,
            name=name,
            type=type,
            description=description,
            credential_id=credential_id,
            schema_metadata=schema_metadata or {},
            read_only=read_only,
            max_rows=max_rows,
        )
        self._sources[ds_id] = meta
        return meta

    def get_datasource(
        self,
        tenant_id: str,
        datasource_id: str,
    ) -> DataSourceMetadata:
        """Retrieve registered data source enforcing multi-tenant boundaries."""
        meta = self._sources.get(datasource_id)
        if not meta:
            raise KeyError(f"Data source '{datasource_id}' not found")
        if meta.tenant_id != tenant_id:
            raise PermissionError(
                f"Tenant '{tenant_id}' cannot access data source '{datasource_id}' "
                f"belonging to tenant '{meta.tenant_id}'",
            )
        return meta

    def list_datasources(
        self,
        tenant_id: str,
        type: Optional[DataSourceType] = None,
    ) -> List[DataSourceMetadata]:
        """List registered data sources for a tenant."""
        results: List[DataSourceMetadata] = []
        for meta in self._sources.values():
            if meta.tenant_id == tenant_id:
                if type and meta.type != type:
                    continue
                results.append(meta)
        return results

    def get_safe_schema_for_prompt(
        self,
        tenant_id: str,
        datasource_id: str,
    ) -> Dict[str, Any]:
        """Export safe schema representation for Agent and LLM Prompts.

        Guarantees ZERO physical leakage:
        Contains only table schemas, column names, and field descriptions.
        Completely strips host, port, credentials, and connection strings.
        """
        meta = self.get_datasource(tenant_id, datasource_id)
        return {
            "datasource_id": meta.datasource_id,
            "name": meta.name,
            "type": meta.type.value,
            "description": meta.description,
            "read_only": meta.read_only,
            "schema": meta.schema_metadata,
        }
