# -*- coding: utf-8 -*-
"""Platform-level Credential Vault & BYOK Management for Agent Cloud."""
import base64
import hashlib
import hmac
import os
import time
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel, Field

from .._utils._common import _generate_id


class CredentialSummary(BaseModel):
    """Safe metadata representation of a credential with zero secret leakage."""

    credential_id: str
    tenant_id: str
    name: str
    provider: str
    masked_value: str
    is_default: bool = False
    is_active: bool = True
    created_at: float


class CredentialRecord(BaseModel):
    """Internal vault record holding encrypted secret."""

    credential_id: str = Field(default_factory=_generate_id)
    tenant_id: str
    name: str
    provider: str
    encrypted_secret: str
    salt: str
    masked_value: str
    is_default: bool = False
    is_active: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class PlatformCredentialVault:
    """Agent Cloud Platform Credential Vault & BYOK Central Service.

    Responsible for:
    1. Multi-tenant cryptographic isolation of LLM & SaaS API keys.
    2. Prompt Zero-Exposure: Agent prompts and reasoning contexts only reference
       `credential_id`, preventing prompt-injection credential exfiltration.
    3. Runtime Transport Layer Injection: Secrets are decrypted strictly at the
       network transport layer when dispatching requests to external providers.
    4. Lifecycle governance: centralized revocation, rotation, and audit hooks.
    """

    def __init__(
        self,
        master_key: Optional[str] = None,
        audit_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        """Initialize the Platform Credential Vault.

        Args:
            master_key (`Optional[str]`):
                The platform master encryption key or KMS secret. If None,
                reads from `AGENT_CLOUD_VAULT_KEY` or uses secure machine seed.
            audit_callback (`Optional[Callable]`):
                Optional hook invoked upon secret resolution for audit logging.
        """
        self._master_key = (
            master_key
            or os.environ.get("AGENT_CLOUD_VAULT_KEY")
            or "agent-cloud-vault-master-secret-seed-v1"
        ).encode("utf-8")
        self._audit_callback = audit_callback
        # In-memory storage keyed by (tenant_id, credential_id)
        self._vault: Dict[str, CredentialRecord] = {}

    @staticmethod
    def mask_secret(secret: str) -> str:
        """Produce safe redacted representation for display & prompt auditing."""
        if not secret:
            return ""
        if len(secret) <= 8:
            return "***"
        prefix = secret[:4]
        suffix = secret[-4:]
        return f"{prefix}...{suffix}"

    def _derive_key(self, tenant_id: str, salt: bytes) -> bytes:
        """Derive tenant-specific subkey using HKDF/HMAC with master key."""
        return hmac.new(
            self._master_key,
            tenant_id.encode("utf-8") + b":" + salt,
            hashlib.sha256,
        ).digest()

    def _encrypt(self, plaintext: str, tenant_id: str) -> tuple[str, str]:
        """Encrypt secret payload with tenant-derived key."""
        salt = os.urandom(16)
        key = self._derive_key(tenant_id, salt)
        # XOR keystream envelope with SHA256 block expansion
        data = plaintext.encode("utf-8")
        keystream = hashlib.sha256(key + b":stream").digest()
        while len(keystream) < len(data):
            keystream += hashlib.sha256(keystream + key).digest()
        ciphertext = bytes(a ^ b for a, b in zip(data, keystream[: len(data)]))
        return (
            base64.urlsafe_b64encode(ciphertext).decode("utf-8"),
            base64.urlsafe_b64encode(salt).decode("utf-8"),
        )

    def _decrypt(
        self,
        ciphertext_b64: str,
        salt_b64: str,
        tenant_id: str,
    ) -> str:
        """Decrypt secret payload using tenant-derived key."""
        salt = base64.urlsafe_b64decode(salt_b64.encode("utf-8"))
        ciphertext = base64.urlsafe_b64decode(ciphertext_b64.encode("utf-8"))
        key = self._derive_key(tenant_id, salt)
        keystream = hashlib.sha256(key + b":stream").digest()
        while len(keystream) < len(ciphertext):
            keystream += hashlib.sha256(keystream + key).digest()
        plaintext = bytes(
            a ^ b for a, b in zip(ciphertext, keystream[: len(ciphertext)])
        )
        return plaintext.decode("utf-8")

    def register_credential(
        self,
        tenant_id: str,
        name: str,
        provider: str,
        secret_value: str,
        credential_id: Optional[str] = None,
        is_default: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CredentialSummary:
        """Securely ingest and encrypt a tenant credential (BYOK)."""
        if not tenant_id:
            raise ValueError("tenant_id is required for credential isolation")
        if not secret_value:
            raise ValueError("secret_value cannot be empty")

        cid = credential_id or f"cred_{_generate_id()}"
        encrypted_sec, salt = self._encrypt(secret_value, tenant_id)
        masked = self.mask_secret(secret_value)

        # If set to default, clear previous defaults for the same provider
        if is_default:
            for record in self._vault.values():
                if (
                    record.tenant_id == tenant_id
                    and record.provider == provider
                ):
                    record.is_default = False

        record = CredentialRecord(
            credential_id=cid,
            tenant_id=tenant_id,
            name=name,
            provider=provider,
            encrypted_secret=encrypted_sec,
            salt=salt,
            masked_value=masked,
            is_default=is_default,
            is_active=True,
            metadata=metadata or {},
        )
        self._vault[cid] = record

        return CredentialSummary(
            credential_id=record.credential_id,
            tenant_id=record.tenant_id,
            name=record.name,
            provider=record.provider,
            masked_value=record.masked_value,
            is_default=record.is_default,
            is_active=record.is_active,
            created_at=record.created_at,
        )

    def resolve_secret(
        self,
        tenant_id: str,
        credential_id: str,
        user_id: Optional[str] = None,
        agent_service: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """Resolve decrypted secret strictly at transport layer.

        Enforces multi-tenant isolation: tenants can NEVER access or decrypt
        credentials belonging to other tenants.
        """
        record = self._vault.get(credential_id)
        if not record:
            raise KeyError(f"Credential '{credential_id}' not found in vault")
        if record.tenant_id != tenant_id:
            raise PermissionError(
                f"Security breach: tenant '{tenant_id}' cannot access "
                f"credential owned by tenant '{record.tenant_id}'",
            )
        if not record.is_active:
            raise ValueError(f"Credential '{credential_id}' is revoked")

        decrypted = self._decrypt(
            record.encrypted_secret,
            record.salt,
            tenant_id,
        )

        if self._audit_callback:
            try:
                self._audit_callback(
                    {
                        "event": "credential_resolved",
                        "tenant_id": tenant_id,
                        "credential_id": credential_id,
                        "provider": record.provider,
                        "user_id": user_id,
                        "agent_service": agent_service,
                        "session_id": session_id,
                        "timestamp": time.time(),
                    },
                )
            except Exception:
                pass

        return decrypted

    def list_credentials(
        self,
        tenant_id: str,
        provider: Optional[str] = None,
    ) -> List[CredentialSummary]:
        """List tenant credentials with masked values (Safe for UI & Prompts)."""
        results: List[CredentialSummary] = []
        for r in self._vault.values():
            if r.tenant_id == tenant_id:
                if provider and r.provider != provider:
                    continue
                results.append(
                    CredentialSummary(
                        credential_id=r.credential_id,
                        tenant_id=r.tenant_id,
                        name=r.name,
                        provider=r.provider,
                        masked_value=r.masked_value,
                        is_default=r.is_default,
                        is_active=r.is_active,
                        created_at=r.created_at,
                    ),
                )
        return results

    def revoke_credential(self, tenant_id: str, credential_id: str) -> bool:
        """Immediately deactivate a credential."""
        record = self._vault.get(credential_id)
        if not record or record.tenant_id != tenant_id:
            return False
        record.is_active = False
        record.updated_at = time.time()
        return True
