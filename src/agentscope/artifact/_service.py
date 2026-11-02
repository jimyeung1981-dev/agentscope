# -*- coding: utf-8 -*-
"""Platform Deliverables and Artifacts Management Service.

Provides dual-storage persistence:
- Metadata & S3 pointers stored in database;
- Actual physical file payloads stored in S3/Object Storage.
Supports REST-like API operations: create, update, get (metadata or download), list.
"""
import hashlib
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from ._models import ArtifactRecord, ArtifactType


class S3StorageAdapter:
    """Pluggable S3 adapter. Can wrap real aioboto3/S3BlobStore or simulated in-memory S3."""

    def __init__(self, default_bucket: str = "agentcloud-artifacts"):
        self.default_bucket = default_bucket
        self._objects: Dict[str, bytes] = {}  # key: f"{bucket}/{key}" -> bytes

    async def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload raw bytes to S3."""
        self._objects[f"{bucket}/{key}"] = data

    async def get_object(self, bucket: str, key: str) -> bytes:
        """Download raw bytes from S3."""
        full_key = f"{bucket}/{key}"
        if full_key not in self._objects:
            raise FileNotFoundError(f"S3 object s3://{bucket}/{key} not found.")
        return self._objects[full_key]

    async def delete_object(self, bucket: str, key: str) -> None:
        """Delete object from S3."""
        self._objects.pop(f"{bucket}/{key}", None)

    async def generate_presigned_url(
        self,
        bucket: str,
        key: str,
        expires_in: int = 3600,
    ) -> str:
        """Generate presigned download URL."""
        return f"https://{bucket}.s3.amazonaws.com/{key}?expires={expires_in}&sig=mock_presigned_sig"


class PlatformArtifactService:
    """Unified Platform Service for Managing Agent Deliverables and Artifacts.

    - Dual storage: Database stores S3 metadata/versioning; S3 stores physical files.
    - Multi-tenant isolation: every operation strictly checks tenant_id.
    - Versioned updates: updating an artifact increments version and preserves history.
    """

    def __init__(
        self,
        s3_adapter: Optional[S3StorageAdapter] = None,
        default_bucket: str = "agentcloud-artifacts",
        audit_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.s3 = s3_adapter or S3StorageAdapter(default_bucket=default_bucket)
        self.default_bucket = default_bucket
        self.audit_callback = audit_callback
        # In-memory DB representation: (tenant_id, artifact_id) -> ArtifactRecord
        self._db_records: Dict[Tuple[str, str], ArtifactRecord] = {}

    def _compute_checksum(self, data: bytes) -> str:
        """Compute SHA-256 checksum."""
        return hashlib.sha256(data).hexdigest()

    async def create_artifact(
        self,
        tenant_id: str,
        name: str,
        content: Union[bytes, str],
        type: ArtifactType = ArtifactType.CODE,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRecord:
        """Write a new deliverable/artifact to platform.

        Physical file is uploaded to S3; metadata and S3 index are written to DB.
        """
        payload_bytes = content.encode("utf-8") if isinstance(content, str) else content
        artifact_id = f"art_{uuid.uuid4().hex[:12]}"
        now = time.time()
        checksum = self._compute_checksum(payload_bytes)

        if not content_type:
            if type == ArtifactType.CODE:
                content_type = "text/x-python" if name.endswith(".py") else "text/plain"
            elif type == ArtifactType.DOCUMENT:
                content_type = "text/markdown" if name.endswith(".md") else "application/pdf"
            elif type == ArtifactType.DATA:
                content_type = "application/json" if name.endswith(".json") else "text/csv"
            else:
                content_type = "application/octet-stream"

        # Multi-tenant partitioned S3 Key
        safe_session = session_id or "global"
        s3_key = f"tenants/{tenant_id}/sessions/{safe_session}/artifacts/{artifact_id}_v1_{name}"

        # 1. Physical upload to S3
        await self.s3.put_object(
            bucket=self.default_bucket,
            key=s3_key,
            data=payload_bytes,
            content_type=content_type,
        )

        # 2. Write metadata and S3 index to Database
        record = ArtifactRecord(
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            name=name,
            type=type,
            version=1,
            s3_bucket=self.default_bucket,
            s3_key=s3_key,
            size_bytes=len(payload_bytes),
            content_type=content_type,
            checksum=checksum,
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )
        self._db_records[(tenant_id, artifact_id)] = record

        if self.audit_callback:
            self.audit_callback({
                "action": "create_artifact",
                "tenant_id": tenant_id,
                "artifact_id": artifact_id,
                "name": name,
                "size_bytes": len(payload_bytes),
                "s3_uri": record.s3_uri,
            })

        return record

    async def update_artifact(
        self,
        tenant_id: str,
        artifact_id: str,
        content: Union[bytes, str],
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRecord:
        """Update an existing artifact.

        Increments version number, writes new version file to S3, updates DB index.
        """
        existing = await self.get_artifact_metadata(tenant_id, artifact_id)
        if not existing:
            raise KeyError(f"Artifact {artifact_id} not found in tenant {tenant_id}.")

        payload_bytes = content.encode("utf-8") if isinstance(content, str) else content
        new_version = existing.version + 1
        now = time.time()
        checksum = self._compute_checksum(payload_bytes)

        safe_session = existing.session_id or "global"
        new_s3_key = f"tenants/{tenant_id}/sessions/{safe_session}/artifacts/{artifact_id}_v{new_version}_{existing.name}"

        # 1. Upload new version to S3
        await self.s3.put_object(
            bucket=existing.s3_bucket,
            key=new_s3_key,
            data=payload_bytes,
            content_type=existing.content_type,
        )

        # 2. Update DB metadata
        merged_metadata = dict(existing.metadata)
        if metadata_update:
            merged_metadata.update(metadata_update)

        updated_record = ArtifactRecord(
            artifact_id=existing.artifact_id,
            tenant_id=tenant_id,
            user_id=existing.user_id,
            session_id=existing.session_id,
            name=existing.name,
            type=existing.type,
            version=new_version,
            s3_bucket=existing.s3_bucket,
            s3_key=new_s3_key,
            size_bytes=len(payload_bytes),
            content_type=existing.content_type,
            checksum=checksum,
            metadata=merged_metadata,
            created_at=existing.created_at,
            updated_at=now,
        )
        self._db_records[(tenant_id, artifact_id)] = updated_record

        if self.audit_callback:
            self.audit_callback({
                "action": "update_artifact",
                "tenant_id": tenant_id,
                "artifact_id": artifact_id,
                "version": new_version,
                "size_bytes": len(payload_bytes),
                "s3_uri": updated_record.s3_uri,
            })

        return updated_record

    async def get_artifact_metadata(
        self,
        tenant_id: str,
        artifact_id: str,
    ) -> Optional[ArtifactRecord]:
        """Read artifact metadata from DB (strictly enforcing tenant isolation)."""
        record = self._db_records.get((tenant_id, artifact_id))
        return record

    async def get_artifact_content(
        self,
        tenant_id: str,
        artifact_id: str,
    ) -> Tuple[ArtifactRecord, bytes]:
        """Fetch both metadata from DB and actual payload bytes from S3."""
        record = await self.get_artifact_metadata(tenant_id, artifact_id)
        if not record:
            raise KeyError(f"Artifact {artifact_id} not found in tenant {tenant_id}.")
        content = await self.s3.get_object(record.s3_bucket, record.s3_key)
        return record, content

    async def list_artifacts(
        self,
        tenant_id: str,
        session_id: Optional[str] = None,
        artifact_type: Optional[ArtifactType] = None,
    ) -> List[ArtifactRecord]:
        """Query artifacts from DB filtered by tenant, session, and type."""
        results = []
        for (t_id, _), record in self._db_records.items():
            if t_id != tenant_id:
                continue
            if session_id is not None and record.session_id != session_id:
                continue
            if artifact_type is not None and record.type != artifact_type:
                continue
            results.append(record)
        return sorted(results, key=lambda x: x.created_at, reverse=True)

    async def get_download_url(
        self,
        tenant_id: str,
        artifact_id: str,
        expires_in: int = 3600,
    ) -> str:
        """Generate presigned S3 download URL for an artifact."""
        record = await self.get_artifact_metadata(tenant_id, artifact_id)
        if not record:
            raise KeyError(f"Artifact {artifact_id} not found in tenant {tenant_id}.")
        return await self.s3.generate_presigned_url(record.s3_bucket, record.s3_key, expires_in=expires_in)
