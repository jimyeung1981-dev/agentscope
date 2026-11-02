# -*- coding: utf-8 -*-
"""Unit tests for ManagedAgent in AgentScope."""
import pytest
from agentscope.agent import (
    A2ADelegationTask,
    AgentEvent,
    AgentEventType,
    AgentPersona,
    AgentTask,
    ExecutionMode,
    HITLConfirmRequest,
    ManagedAgent,
    SOPStage,
)


@pytest.mark.asyncio
async def test_managed_agent_persona():
    persona = AgentPersona(
        name="FinAdvisor",
        role="Senior Financial Analyst",
        goal="Provide risk assessment",
        constraints=["Never give direct financial advice", "Always cite sources"],
    )
    prompt = persona.compile_system_prompt()
    assert "FinAdvisor" in prompt
    assert "Senior Financial Analyst" in prompt
    assert "Never give direct financial advice" in prompt


@pytest.mark.asyncio
async def test_managed_agent_autonomous_stream():
    agent = ManagedAgent(agent_id="test_autonomous_v1")

    def calc_tax(income: int = 10000) -> float:
        return income * 0.2

    agent.register_tool("calc_tax", calc_tax)

    events = []
    async for evt in agent.reply_stream(
        session_id="sess_auton_1",
        prompt="Please calculate calc tax for me",
        forced_tool_call={"name": "calc_tax", "args": {"income": 50000}},
    ):
        events.append(evt)

    event_types = [e.event_type for e in events]
    assert AgentEventType.START in event_types
    assert AgentEventType.THOUGHT in event_types
    assert AgentEventType.TOOL_CALL in event_types
    assert AgentEventType.TOOL_RESULT in event_types
    assert AgentEventType.FINAL_ANSWER in event_types
    assert AgentEventType.END in event_types

    # Verify tool execution result
    tool_res_evt = next(e for e in events if e.event_type == AgentEventType.TOOL_RESULT)
    assert tool_res_evt.data["result"] == 10000.0

    # Verify checkpoint
    latest_cp = agent.get_latest_checkpoint("sess_auton_1")
    assert latest_cp is not None
    assert "final_answer" in latest_cp["state"]


@pytest.mark.asyncio
async def test_managed_agent_hitl_interception_and_resume():
    agent = ManagedAgent(agent_id="test_hitl_v1")

    def transfer_funds(amount: int, target: str) -> str:
        return f"Transferred ${amount} to {target}"

    agent.register_tool("transfer_funds", transfer_funds, is_high_risk=True)

    # 1. Trigger high-risk tool -> verify interception and PAUSED
    events = []
    async for evt in agent.reply_stream(
        session_id="sess_hitl_1",
        prompt="Please transfer funds now",
        mode=ExecutionMode.HITL,
        forced_tool_call={"name": "transfer_funds", "args": {"amount": 5000, "target": "acc_999"}},
    ):
        events.append(evt)

    confirm_evts = [e for e in events if e.event_type == AgentEventType.REQUIRE_CONFIRM]
    assert len(confirm_evts) == 1
    assert confirm_evts[0].data["tool_name"] == "transfer_funds"

    # Verify checkpoint status is PAUSED
    latest_cp = agent.get_latest_checkpoint("sess_hitl_1")
    assert latest_cp["status"] == "PAUSED"

    # 2. Resume after human approval
    resume_events = []
    async for evt in agent.resume_after_hitl(
        session_id="sess_hitl_1",
        approved=True,
        comment="Approved by manager",
    ):
        resume_events.append(evt)

    resume_types = [e.event_type for e in resume_events]
    assert AgentEventType.TOOL_CALL in resume_types
    assert AgentEventType.TOOL_RESULT in resume_types
    assert AgentEventType.FINAL_ANSWER in resume_types
    assert AgentEventType.END in resume_types

    tool_result = next(e for e in resume_events if e.event_type == AgentEventType.TOOL_RESULT)
    assert "Transferred $5000 to acc_999" in str(tool_result.data["result"])


@pytest.mark.asyncio
async def test_managed_agent_workflow_sop():
    agent = ManagedAgent(agent_id="test_sop_v1")
    agent.set_mode(ExecutionMode.WORKFLOW)

    stages = [
        SOPStage(
            stage_id="stage_research",
            name="Research Phase",
            allowed_tools=["search_web"],
        ),
        SOPStage(
            stage_id="stage_write",
            name="Writing Phase",
            allowed_tools=["write_report"],
        ),
    ]
    agent.set_workflow_stages(stages)
    agent.register_tool("search_web", lambda q="": f"results for {q}")
    agent.register_tool("write_report", lambda c="": f"report: {c}")

    events = []
    async for evt in agent.reply_stream(
        session_id="sess_sop_1",
        prompt="Gather initial research data",
    ):
        events.append(evt)

    stage_evts = [e for e in events if e.event_type == AgentEventType.STAGE_TRANSITION]
    assert len(stage_evts) == 1
    assert stage_evts[0].data["stage_id"] == "stage_research"


@pytest.mark.asyncio
async def test_managed_agent_a2a_delegation():
    parent_agent = ManagedAgent(agent_id="parent_orchestrator")
    child_agent = ManagedAgent(agent_id="code_specialist")

    parent_agent.register_subagent("code_specialist", child_agent)

    delegation = await parent_agent.dispatch_subagent(
        parent_session_id="parent_sess_1",
        target_agent_id="code_specialist",
        instruction="Generate quicksort algorithm",
    )

    assert delegation.status == "COMPLETED"
    assert "Task processed successfully" in delegation.result
