# -*- coding: utf-8 -*-
"""Unit tests verifying the 4-layer Managed Agent architectural upgrades."""
import asyncio
import base64
import json
import pytest
from pydantic import BaseModel, Field

from agentscope.app.access import TenantIdentity, get_current_tenant, set_current_tenant
from agentscope.app.deps import get_current_identity, _parse_jwt_payload
from agentscope.app.storage._sql._storage import AsyncSQLAlchemyStorage
from agentscope.tool import ToolBase, ToolChunk, PydanticValidationMiddleware
from agentscope.message import ToolResultState


from agentscope.permission import PermissionBehavior, PermissionDecision


class DummyWeatherInput(BaseModel):
    city: str
    unit: str = Field(default="celsius")


class DummyWeatherTool(ToolBase):
    name = "get_weather"
    description = "Fetch weather"
    is_concurrency_safe = True
    is_read_only = True
    params_model = DummyWeatherInput

    async def call(self, city: str, unit: str = "celsius") -> ToolChunk:
        return ToolChunk(content=[], state=ToolResultState.SUCCESS)

    async def check_permissions(self, tool_input, context):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW)


@pytest.mark.asyncio
async def test_tenant_identity_and_context():
    """Verify TenantIdentity creation and context variable management."""
    identity = TenantIdentity(
        tenant_id="tenant_alipay",
        user_id="user_jack",
        role="admin",
    )
    assert identity.is_admin is True
    assert identity.tenant_scoped_path("reports/q3.pdf") == "tenants/tenant_alipay/reports/q3.pdf"

    set_current_tenant(identity)
    assert get_current_tenant() == identity
    set_current_tenant(None)
    assert get_current_tenant() is None


@pytest.mark.asyncio
async def test_jwt_and_identity_parsing():
    """Verify JWT payload parsing and identity resolution."""
    raw_payload = {"sub": "user_456", "tenant_id": "tenant_enterprise", "role": "admin"}
    token_str = "header." + base64.urlsafe_b64encode(json.dumps(raw_payload).encode()).decode().rstrip("=") + ".sig"
    parsed = _parse_jwt_payload(token_str)
    assert parsed.get("sub") == "user_456"
    assert parsed.get("tenant_id") == "tenant_enterprise"

    # Test get_current_identity with Bearer JWT
    identity = await get_current_identity(
        request=None,
        authorization=f"Bearer {token_str}",
    )
    assert identity.user_id == "user_456"
    assert identity.tenant_id == "tenant_enterprise"
    assert identity.role == "admin"


@pytest.mark.asyncio
async def test_pydantic_tool_validation_defense():
    """Verify PydanticValidationMiddleware intercepts parameter hallucinations."""
    tool = DummyWeatherTool(middlewares=[PydanticValidationMiddleware()])

    # 1. Valid arguments pass through successfully
    gen = await tool(city="Hangzhou", unit="celsius")
    valid_chunks = [chunk async for chunk in gen]
    assert len(valid_chunks) == 1
    assert valid_chunks[0].state == ToolResultState.SUCCESS

    # 2. Invalid/missing arguments are intercepted and returned as structured guidance
    err_gen = await tool(invalid_param=123)
    invalid_chunks = [chunk async for chunk in err_gen]
    assert len(invalid_chunks) == 1
    assert invalid_chunks[0].state == ToolResultState.ERROR
    error_text = invalid_chunks[0].content[0].text
    assert "Parameter validation failed" in error_text
    assert "city" in error_text


@pytest.mark.asyncio
async def test_sql_task_queue_and_audit_logging():
    """Verify lockless task queue and structured audit log persistence."""
    storage = AsyncSQLAlchemyStorage(url="sqlite+aiosqlite:///:memory:", create_tables=True)
    async with storage:
        # Task Queue tests
        await storage.enqueue_task(
            task_id="task_1",
            user_id="user_1",
            session_id="session_1",
            agent_id="agent_1",
            payload={"action": "run_analysis"},
            tenant_id="tenant_alipay",
        )

        claimed = await storage.claim_next_task(worker_id="worker_node_1", tenant_id="tenant_alipay")
        assert claimed is not None
        assert claimed["id"] == "task_1"
        assert claimed["tenant_id"] == "tenant_alipay"

        # Claiming again returns None because it is already claimed
        none_claimed = await storage.claim_next_task(worker_id="worker_node_2", tenant_id="tenant_alipay")
        assert none_claimed is None

        await storage.update_task_status("task_1", status="completed")

        # Audit Log tests
        await storage.record_audit_log(
            log_id="audit_1",
            user_id="user_1",
            session_id="session_1",
            agent_id="agent_1",
            event_type="chat_run",
            model_name="claude-3-5-sonnet",
            prompt_tokens=1500,
            completion_tokens=300,
            latency_ms=1200.5,
            cost=0.015,
            status="success",
            details={"tools_called": ["bash"]},
            tenant_id="tenant_alipay",
        )

        logs = await storage.list_audit_logs(tenant_id="tenant_alipay")
        assert len(logs) == 1
        assert logs[0]["id"] == "audit_1"
        assert logs[0]["total_tokens"] == 1800
        assert logs[0]["cost"] == 0.015


@pytest.mark.asyncio
async def test_platform_credential_vault_isolation_and_zero_exposure():
    """Verify PlatformCredentialVault multi-tenant isolation, masking, and runtime injection."""
    from agentscope.credential import PlatformCredentialVault

    audit_events = []

    def audit_hook(event):
        audit_events.append(event)

    vault = PlatformCredentialVault(audit_callback=audit_hook)

    # 1. Register BYOK credentials for Tenant A and Tenant B
    summary_a = vault.register_credential(
        tenant_id="tenant_alipay",
        name="Alipay OpenAI Production",
        provider="openai",
        secret_value="sk-proj-supersecretkey1234567890alipay",
        is_default=True,
    )
    assert summary_a.tenant_id == "tenant_alipay"
    # Zero-prompt exposure: masked representation hides secret
    assert summary_a.masked_value.startswith("sk-p...")
    assert "supersecret" not in summary_a.masked_value

    summary_b = vault.register_credential(
        tenant_id="tenant_bytedance",
        name="ByteDance Claude Production",
        provider="anthropic",
        secret_value="sk-ant-supersecretkey1234567890bytedance",
    )

    # 2. List credentials for Tenant A returns only Tenant A's masked keys
    tenant_a_creds = vault.list_credentials(tenant_id="tenant_alipay")
    assert len(tenant_a_creds) == 1
    assert tenant_a_creds[0].credential_id == summary_a.credential_id
    assert "supersecret" not in tenant_a_creds[0].masked_value

    # 3. Runtime Transport Layer Injection: Authorized resolution decrypts secret
    decrypted_a = vault.resolve_secret(
        tenant_id="tenant_alipay",
        credential_id=summary_a.credential_id,
        user_id="user_jack",
        agent_service="managed_agent",
        session_id="sess_001",
    )
    assert decrypted_a == "sk-proj-supersecretkey1234567890alipay"
    assert len(audit_events) == 1
    assert audit_events[0]["tenant_id"] == "tenant_alipay"
    assert audit_events[0]["agent_service"] == "managed_agent"

    # 4. Multi-Tenant Zero Trust Breach Prevention:
    # Tenant B tries to decrypt Tenant A's credential -> PermissionError
    with pytest.raises(PermissionError) as exc_info:
        vault.resolve_secret(
            tenant_id="tenant_bytedance",
            credential_id=summary_a.credential_id,
        )
    assert "Security breach" in str(exc_info.value)

    # 5. Revocation Lifecycle
    assert vault.revoke_credential("tenant_alipay", summary_a.credential_id) is True
    with pytest.raises(ValueError) as exc_info:
        vault.resolve_secret("tenant_alipay", summary_a.credential_id)
    assert "revoked" in str(exc_info.value)


@pytest.mark.asyncio
async def test_datasource_registry_and_managed_connection_service():
    """Verify Data Source Registration and Managed Connection Service with Vault Injection."""
    from agentscope.credential import PlatformCredentialVault
    from agentscope.connector import (
        DataSourceRegistry,
        DataSourceType,
        ManagedConnectionService,
    )

    audit_events = []

    def audit_hook(event):
        audit_events.append(event)

    vault = PlatformCredentialVault(audit_callback=audit_hook)
    registry = DataSourceRegistry()
    conn_service = ManagedConnectionService(
        registry=registry,
        vault=vault,
        audit_callback=audit_hook,
    )

    # 1. Tenant registers database credentials in Vault (BYOD/BYOK)
    db_cred = vault.register_credential(
        tenant_id="tenant_alipay",
        name="Finance Production DB Credential",
        provider="postgresql",
        secret_value="postgresql://admin:super_secret_db_pass_123@10.0.2.100:5432/finance_db",
    )

    # 2. Register Data Source into Platform Registry
    ds_meta = registry.register_datasource(
        tenant_id="tenant_alipay",
        datasource_id="ds_finance_oltp",
        name="Alipay Finance Data Source",
        type=DataSourceType.SQL,
        credential_id=db_cred.credential_id,
        description="Core financial transactions ledger",
        schema_metadata={
            "tables": {
                "transactions": ["id", "amount", "sender_id", "created_at"],
                "accounts": ["account_id", "balance", "currency"],
            },
        },
        read_only=True,
    )
    assert ds_meta.datasource_id == "ds_finance_oltp"

    # 3. LLM Prompt Isolation Verification:
    # Large Language Model only gets high-level schema metadata.
    # It NEVER sees host, port, username, password, or connection string!
    safe_schema = registry.get_safe_schema_for_prompt("tenant_alipay", "ds_finance_oltp")
    assert "transactions" in safe_schema["schema"]["tables"]
    assert "super_secret_db_pass" not in str(safe_schema)
    assert "10.0.2.100" not in str(safe_schema)

    # 4. Agent queries via Managed Connection Service (Zero Config in Agent)
    # The Agent code holds NO connection configs, only datasource_id
    res = conn_service.execute_query(
        tenant_id="tenant_alipay",
        datasource_id="ds_finance_oltp",
        query={"sql": "SELECT * FROM transactions WHERE amount > 100", "limit": 50},
        user_id="user_jack",
        agent_service="managed_agent",
        session_id="sess_888",
    )
    assert res["status"] == "success"
    assert res["datasource_id"] == "ds_finance_oltp"

    # 5. Safety Guardrails: Read-only protection blocks destructive SQL
    with pytest.raises(PermissionError) as exc_info:
        conn_service.execute_query(
            tenant_id="tenant_alipay",
            datasource_id="ds_finance_oltp",
            query={"sql": "DROP TABLE transactions"},
        )
    assert "DROP" in str(exc_info.value)
    assert "prohibited" in str(exc_info.value)

    # 6. Multi-Tenant Zero Trust Breach Prevention:
    # Tenant B tries to query Tenant A's data source -> PermissionError
    with pytest.raises(PermissionError) as exc_info:
        conn_service.execute_query(
            tenant_id="tenant_bytedance",
            datasource_id="ds_finance_oltp",
            query={"sql": "SELECT * FROM accounts"},
        )
    assert "cannot access data source" in str(exc_info.value)


@pytest.mark.asyncio
async def test_telemetry_ingestion_api_dual_producers():
    """Verify Observability Ingestion API with dual reporters: Gateway and Agent."""
    import time
    from agentscope.observability import (
        TelemetrySource,
        TelemetrySpan,
        TelemetryAuditRecord,
        TelemetryIngestionService,
    )

    service = TelemetryIngestionService()
    shared_trace_id = "trace_001_abc"

    # 1. Gateway layer reports boundary network metrics (Root Span)
    gw_span = TelemetrySpan(
        trace_id=shared_trace_id,
        source=TelemetrySource.GATEWAY,
        name="gateway_ws_request",
        tenant_id="tenant_alipay",
        user_id="user_jack",
        agent_service="managed_agent",
        session_id="sess_101",
        start_time=time.time(),
        duration_ms=45.2,
        attributes={"client_ip": "1.2.3.4", "ttft_ms": 120.0, "status_code": 200},
    )
    gw_span_id = service.ingest_span(gw_span)
    assert gw_span_id is not None

    # 2. Agent layer reports internal reasoning metrics (Child Span under same trace_id)
    agent_span = TelemetrySpan(
        trace_id=shared_trace_id,
        parent_span_id=gw_span_id,
        source=TelemetrySource.AGENT,
        name="agent_react_loop",
        tenant_id="tenant_alipay",
        user_id="user_jack",
        agent_service="managed_agent",
        session_id="sess_101",
        start_time=time.time() + 0.05,
        duration_ms=850.5,
        attributes={"react_steps": 3, "tools_called": ["run_backtest"]},
    )
    agent_span_id = service.ingest_span(agent_span)
    assert agent_span_id is not None

    # 3. Agent reports structured Token consumption & billing audit
    audit_rec = TelemetryAuditRecord(
        trace_id=shared_trace_id,
        source=TelemetrySource.AGENT,
        tenant_id="tenant_alipay",
        user_id="user_jack",
        agent_service="managed_agent",
        session_id="sess_101",
        model_name="claude-3-5-sonnet",
        prompt_tokens=1500,
        completion_tokens=350,
        cost=0.0185,
        latency_ms=850.5,
    )
    audit_id = service.ingest_audit(audit_rec)
    assert audit_id is not None

    # 4. Observability stitches the complete trace waterfall from Gateway to Agent
    waterfall = service.get_trace_waterfall("tenant_alipay", shared_trace_id)
    assert len(waterfall) == 2
    assert waterfall[0]["source"] == TelemetrySource.GATEWAY.value
    assert waterfall[0]["name"] == "gateway_ws_request"
    assert waterfall[1]["source"] == TelemetrySource.AGENT.value
    assert waterfall[1]["name"] == "agent_react_loop"
    assert waterfall[1]["parent_span_id"] == gw_span_id

    # 5. Multi-tenant isolation: Tenant B cannot access Tenant A's trace
    empty_waterfall = service.get_trace_waterfall("tenant_bytedance", shared_trace_id)
    assert len(empty_waterfall) == 0


@pytest.mark.asyncio
async def test_platform_artifact_service_s3_and_db_dual_storage():
    """Verify Artifact Management Service: DB stores S3 metadata/version, S3 stores raw payload."""
    from agentscope.artifact import PlatformArtifactService, ArtifactType, S3StorageAdapter

    s3_adapter = S3StorageAdapter(default_bucket="enterprise-artifacts-vault")
    audit_events = []
    service = PlatformArtifactService(
        s3_adapter=s3_adapter,
        default_bucket="enterprise-artifacts-vault",
        audit_callback=lambda ev: audit_events.append(ev),
    )

    # 1. Create artifact: code file
    code_content = "def calculate_risk(portfolio):\n    return sum(portfolio) * 0.05\n"
    record_v1 = await service.create_artifact(
        tenant_id="tenant_alipay",
        session_id="sess_algo_999",
        name="risk_model.py",
        content=code_content,
        type=ArtifactType.CODE,
        user_id="user_quant",
        metadata={"domain": "finance", "framework": "pandas"},
    )

    assert record_v1.artifact_id.startswith("art_")
    assert record_v1.version == 1
    assert record_v1.tenant_id == "tenant_alipay"
    assert record_v1.name == "risk_model.py"
    assert record_v1.size_bytes == len(code_content.encode("utf-8"))
    assert record_v1.s3_uri.startswith("s3://enterprise-artifacts-vault/tenants/tenant_alipay/sessions/sess_algo_999/artifacts/")

    # 2. Verify physical file is indeed in S3, and DB has metadata
    meta, raw_bytes = await service.get_artifact_content("tenant_alipay", record_v1.artifact_id)
    assert meta.artifact_id == record_v1.artifact_id
    assert raw_bytes.decode("utf-8") == code_content

    # 3. Update artifact -> increments version, creates new version in S3, updates DB index
    updated_code = "def calculate_risk(portfolio):\n    # v2 with VaR support\n    return sum(portfolio) * 0.03\n"
    record_v2 = await service.update_artifact(
        tenant_id="tenant_alipay",
        artifact_id=record_v1.artifact_id,
        content=updated_code,
        metadata_update={"model_type": "VaR"},
    )
    assert record_v2.version == 2
    assert "_v2_" in record_v2.s3_key
    assert record_v2.metadata["model_type"] == "VaR"
    assert record_v2.metadata["domain"] == "finance"

    # Verify S3 content reflects v2
    _, v2_bytes = await service.get_artifact_content("tenant_alipay", record_v1.artifact_id)
    assert "v2 with VaR support" in v2_bytes.decode("utf-8")

    # 4. Presigned URL generation
    download_url = await service.get_download_url("tenant_alipay", record_v1.artifact_id)
    assert "enterprise-artifacts-vault.s3.amazonaws.com" in download_url
    assert "_v2_risk_model.py" in download_url

    # 5. List artifacts by session & tenant
    items = await service.list_artifacts("tenant_alipay", session_id="sess_algo_999")
    assert len(items) == 1
    assert items[0].artifact_id == record_v1.artifact_id

    # 6. Multi-Tenant isolation: Tenant B cannot access or update Tenant A's artifact
    with pytest.raises(KeyError):
        await service.get_artifact_content("tenant_bytedance", record_v1.artifact_id)

    with pytest.raises(KeyError):
        await service.update_artifact("tenant_bytedance", record_v1.artifact_id, "evil_code")


@pytest.mark.asyncio
async def test_managed_agent_one_core_four_modes_integration():
    """Verify ManagedAgent in Layer 2: unified ReAct streaming, checkpoints, and execution."""
    from agentscope.agent import ManagedAgent, ExecutionMode, AgentEventType

    agent = ManagedAgent(
        agent_id="managed_orchestrator_l2",
        mode=ExecutionMode.AUTONOMOUS,
    )
    agent.register_tool("calc_risk", lambda score=10: score * 1.5)

    events = []
    async for evt in agent.reply_stream(
        session_id="sess_upgrade_001",
        prompt="Assess risk score",
        forced_tool_call={"name": "calc_risk", "args": {"score": 20}},
    ):
        events.append(evt)

    types = [e.event_type for e in events]
    assert AgentEventType.START in types
    assert AgentEventType.TOOL_CALL in types
    assert AgentEventType.TOOL_RESULT in types
    assert AgentEventType.FINAL_ANSWER in types
    assert AgentEventType.END in types

    tool_res = next(e for e in events if e.event_type == AgentEventType.TOOL_RESULT)
    assert tool_res.data["result"] == 30.0

