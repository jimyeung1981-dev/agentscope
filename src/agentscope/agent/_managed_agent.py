# -*- coding: utf-8 -*-
"""Unified Managed Agent Orchestrator in AgentScope.

Implements the "One Core, Four Modes" architecture:
1. Autonomous ReAct Mode (Free multi-turn reasoning and tool invocation)
2. Human-in-the-Loop Mode (HITL: high-risk tool interception, PAUSED checkpointing, approval resumption)
3. SOP Workflow Mode (Strict stage gating, dynamic tool scoping, stage_commit transitions)
4. A2A Multi-Agent Mode (Subagent delegation, child session dispatch, and result aggregation)
"""
import asyncio
from enum import Enum
import inspect
import time
import uuid
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Set, Union
from pydantic import BaseModel, Field


class AgentPersona(BaseModel):
    """Configuration for an Agent's Persona and behavioral instructions."""
    persona_id: str = Field(default_factory=lambda: f"persona_{uuid.uuid4().hex[:8]}")
    name: str = "Assistant"
    role: Optional[str] = None
    role_title: Optional[str] = None
    goal: Optional[str] = None
    system_prompt: str = "You are a helpful and precise AI assistant."
    tone: str = "professional"  # professional, friendly, concise, rigorous
    constraints: List[str] = Field(default_factory=list)
    few_shot_examples: List[Dict[str, str]] = Field(default_factory=list)
    custom_instructions: Optional[str] = None

    def model_post_init(self, __context: Any) -> None:
        if self.role and not self.role_title:
            self.role_title = self.role
        elif self.role_title and not self.role:
            self.role = self.role_title

    def compile_system_prompt(self, context_additions: Optional[str] = None) -> str:
        """Compile complete system prompt incorporating persona, constraints, and runtime context."""
        title = self.role or self.role_title or self.name
        parts = [
            f"# Role: {self.name} - {title}",
            self.system_prompt,
        ]
        if self.goal:
            parts.append(f"## Primary Goal: {self.goal}")
        if self.constraints:
            parts.append("\n## Behavioral Constraints:")
            for c in self.constraints:
                parts.append(f"- {c}")
        if self.few_shot_examples:
            parts.append("\n## Examples:")
            for ex in self.few_shot_examples:
                u = ex.get("user") or ex.get("input") or ""
                a = ex.get("assistant") or ex.get("output") or ""
                parts.append(f"User: {u}\nAssistant: {a}")
        if context_additions:
            parts.append(f"\n## Dynamic Context:\n{context_additions}")
        return "\n".join(parts)


class ExecutionMode(str, Enum):
    """The four operational facets of the Unified Managed Agent."""
    AUTONOMOUS = "autonomous"
    HITL = "hitl"
    WORKFLOW = "workflow"
    A2A = "a2a"


class AgentEventType(str, Enum):
    """Fine-grained streaming events matching enterprise governance and Gantt tracing."""
    START = "start"
    THOUGHT = "thought"
    TOKEN = "token"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REQUIRE_CONFIRM = "require_confirm"
    STAGE_TRANSITION = "stage_transition"
    A2A_DISPATCH = "a2a_dispatch"
    FINAL_ANSWER = "final_answer"
    ERROR = "error"
    END = "end"


class AgentEvent(BaseModel):
    """Structured event emitted during agent reasoning and action."""
    event_type: AgentEventType
    session_id: str
    data: Dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)


class SOPStage(BaseModel):
    """A defined stage in an SOP workflow with allowed tools and required outputs."""
    stage_id: str
    name: str
    description: Optional[str] = None
    allowed_tools: List[str] = Field(default_factory=list)
    required_outputs: List[str] = Field(default_factory=list)
    is_terminal: bool = False


class HITLConfirmRequest(BaseModel):
    """Payload generated when a high-risk tool is intercepted, pausing the agent."""
    confirm_id: str = Field(default_factory=lambda: f"hitl_{uuid.uuid4().hex[:8]}")
    session_id: str
    tool_name: str
    tool_args: Dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "HIGH"
    reason: str
    status: str = "PENDING"  # PENDING, APPROVED, REJECTED
    comment: Optional[str] = None
    created_at: float = Field(default_factory=time.time)


class A2ADelegationTask(BaseModel):
    """Subagent delegation task dispatched during A2A multi-agent collaboration."""
    delegation_id: str = Field(default_factory=lambda: f"a2a_{uuid.uuid4().hex[:8]}")
    parent_session_id: str
    child_session_id: str
    target_agent_id: str
    instruction: str
    status: str = "DISPATCHED"  # DISPATCHED, COMPLETED, FAILED
    result: Optional[str] = None
    created_at: float = Field(default_factory=time.time)


class AgentTask(BaseModel):
    """Enterprise agent execution task contract."""
    task_id: str
    tenant_id: str
    session_id: str = "default_session"
    prompt: Optional[str] = None
    goal: Optional[str] = None
    mode: ExecutionMode = ExecutionMode.AUTONOMOUS
    status: str = "QUEUED"  # QUEUED, CLAIMED, RUNNING, PAUSED, COMPLETED, FAILED
    result: Optional[str] = None
    hitl_request: Optional[HITLConfirmRequest] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class ManagedAgent:
    """Unified Enterprise Managed Agent in AgentScope.
    
    Serves as the cognitive calculation kernel with 4 switchable operating modes:
    - Autonomous ReAct
    - Human-in-the-Loop Interception
    - SOP Stage-Gated Workflow
    - Agent-to-Agent Multi-Agent Delegation
    """

    def __init__(
        self,
        agent_id: str = "managed_agent_v1",
        persona: Optional[AgentPersona] = None,
        session_service: Optional[Any] = None,
        vault: Optional[Any] = None,
        telemetry_service: Optional[Any] = None,
        tenant_id: Optional[str] = None,
        mode: ExecutionMode = ExecutionMode.AUTONOMOUS,
        llm_caller: Optional[Callable[..., Any]] = None,
    ):
        self.agent_id = agent_id
        self.tenant_id = tenant_id or "default"
        self.persona = persona
        self.session_service = session_service
        self.vault = vault
        self.telemetry_service = telemetry_service
        self.mode = mode
        self.llm_caller = llm_caller

        # Checkpoints per session: session_id -> list of snapshots
        self._checkpoints: Dict[str, List[Dict[str, Any]]] = {}

        # Tool registry: name -> callable
        self._tools: Dict[str, Callable[..., Any]] = {}

        # High-risk tools that trigger HITL interception
        self._high_risk_tools: Set[str] = {
            "transfer_funds",
            "transfer_money",
            "delete_database",
            "drop_table",
            "execute_shell_drop",
            "modify_production",
        }

        # SOP workflow state
        self._sop_stages: List[SOPStage] = []
        self._current_stage_idx: Dict[str, int] = {}  # session_id -> stage index
        self._stage_data: Dict[str, Dict[str, Any]] = {}  # session_id -> accumulated stage outputs

        # Subagent pool for A2A mode: agent_id -> ManagedAgent instance
        self._subagents: Dict[str, "ManagedAgent"] = {}

        # Pending HITL checkpoints awaiting human approval: session_id -> state dict
        self._pending_hitl: Dict[str, Dict[str, Any]] = {}

        # Register default internal meta-tools
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register built-in meta-tools for SOP workflow and A2A delegation."""
        def stage_commit(session_id: str, stage_id: str, stage_output: Dict[str, Any]) -> str:
            """Advance to next SOP workflow stage."""
            current_idx = self._current_stage_idx.get(session_id, 0)
            if self._sop_stages and current_idx < len(self._sop_stages):
                curr_stage = self._sop_stages[current_idx]
                if curr_stage.stage_id == stage_id:
                    self._current_stage_idx[session_id] = current_idx + 1
                    sess_data = self._stage_data.setdefault(session_id, {})
                    sess_data.update(stage_output)
                    next_name = (
                        self._sop_stages[current_idx + 1].name
                        if current_idx + 1 < len(self._sop_stages)
                        else "COMPLETED"
                    )
                    return f"Stage '{stage_id}' committed. Advanced to stage: '{next_name}'."
            return f"Stage {stage_id} committed."

        self.register_tool("stage_commit", stage_commit)

    # ------------------------------------------------------------------------
    # Tool, Stage, Subagent Configuration
    # ------------------------------------------------------------------------
    def register_tool(
        self,
        name: str,
        func: Callable[..., Any],
        is_high_risk: bool = False,
    ) -> None:
        """Register an executable tool with risk classification."""
        self._tools[name] = func
        if is_high_risk:
            self._high_risk_tools.add(name)

    def register_subagent(self, agent_id: str, subagent: "ManagedAgent") -> None:
        """Register a downstream subagent for A2A delegation."""
        self._subagents[agent_id] = subagent

    def set_workflow_stages(self, stages: List[SOPStage]) -> None:
        """Set the sequence of SOP stages for Workflow Mode."""
        self._sop_stages = stages

    def set_mode(self, mode: ExecutionMode) -> None:
        """Dynamically switch operating mode."""
        self.mode = mode

    # ------------------------------------------------------------------------
    # Checkpointing & Governance
    # ------------------------------------------------------------------------
    @property
    def checkpoints(self) -> List[Dict[str, Any]]:
        all_cp = []
        for cps in self._checkpoints.values():
            all_cp.extend(cps)
        return all_cp

    def save_checkpoint(
        self,
        session_id: str,
        step: int,
        state_data: Dict[str, Any],
        status: str = "RUNNING",
    ) -> None:
        if session_id not in self._checkpoints:
            self._checkpoints[session_id] = []
        record = {
            "step": step,
            "status": status,
            "timestamp": time.time(),
            "state": state_data,
        }
        self._checkpoints[session_id].append(record)

        if self.telemetry_service and hasattr(self.telemetry_service, "log_step"):
            try:
                self.telemetry_service.log_step(self.agent_id, session_id, step, state_data)
            except Exception:
                pass

    def get_latest_checkpoint(self, session_id: str) -> Optional[Dict[str, Any]]:
        steps = self._checkpoints.get(session_id, [])
        return steps[-1] if steps else None

    # ------------------------------------------------------------------------
    # Core Streaming ReAct Generator: reply_stream
    # ------------------------------------------------------------------------
    async def reply_stream(
        self,
        session_id: str,
        prompt: str,
        tenant_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        mode: Optional[ExecutionMode] = None,
        forced_tool_call: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Core streaming reasoning loop yielding fine-grained AgentEvents."""
        effective_mode = mode or self.mode
        tenant = tenant_id or self.tenant_id

        # 1. Emit START event
        yield AgentEvent(
            event_type=AgentEventType.START,
            session_id=session_id,
            data={"agent_id": self.agent_id, "mode": effective_mode.value, "tenant_id": tenant},
            message=f"Agent {self.agent_id} started execution in {effective_mode.value} mode.",
        )

        # 2. Check for pending HITL in this session
        if session_id in self._pending_hitl:
            pending = self._pending_hitl[session_id]
            yield AgentEvent(
                event_type=AgentEventType.REQUIRE_CONFIRM,
                session_id=session_id,
                data=pending["hitl_request"].model_dump(),
                message="Session is paused awaiting human approval.",
            )
            return

        # 3. Dynamic Tool Scoping based on Mode
        scoped_tools = dict(self._tools)
        if effective_mode == ExecutionMode.WORKFLOW and self._sop_stages:
            stage_idx = self._current_stage_idx.get(session_id, 0)
            if stage_idx < len(self._sop_stages):
                active_stage = self._sop_stages[stage_idx]
                allowed = set(active_stage.allowed_tools) | {"stage_commit"}
                scoped_tools = {k: v for k, v in self._tools.items() if k in allowed}
                yield AgentEvent(
                    event_type=AgentEventType.STAGE_TRANSITION,
                    session_id=session_id,
                    data={"stage_id": active_stage.stage_id, "stage_name": active_stage.name, "index": stage_idx},
                    message=f"Entered SOP Stage [{active_stage.stage_id}]: {active_stage.name}",
                )

        # 4. Step 1: Reasoning & Thought Checkpoint
        thought_msg = f"Analyzing goal for session {session_id} under {effective_mode.value} policy: '{prompt}'"
        self.save_checkpoint(session_id, step=1, state_data={"thought": thought_msg})
        yield AgentEvent(
            event_type=AgentEventType.THOUGHT,
            session_id=session_id,
            data={"thought": thought_msg},
            message=thought_msg,
        )

        # 5. Tool Selection & Interception Logic
        tool_call_to_make = forced_tool_call or self._detect_tool_call(prompt, scoped_tools, effective_mode)

        if tool_call_to_make:
            tool_name = tool_call_to_make["name"]
            tool_args = tool_call_to_make.get("args", {})

            # HITL Mode Interception Check
            if (
                effective_mode == ExecutionMode.HITL
                or tool_name in self._high_risk_tools
                or tool_call_to_make.get("is_high_risk", False)
            ) and tool_name in self._high_risk_tools:
                hitl_req = HITLConfirmRequest(
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    reason=f"High-risk action '{tool_name}' requires human confirmation.",
                )
                self._pending_hitl[session_id] = {
                    "hitl_request": hitl_req,
                    "prompt": prompt,
                    "context": context,
                    "effective_mode": effective_mode,
                    "scoped_tools": scoped_tools,
                }
                self.save_checkpoint(
                    session_id,
                    step=2,
                    state_data={"hitl_pending": hitl_req.model_dump()},
                    status="PAUSED",
                )
                yield AgentEvent(
                    event_type=AgentEventType.REQUIRE_CONFIRM,
                    session_id=session_id,
                    data=hitl_req.model_dump(),
                    message=f"PAUSED: Action '{tool_name}' blocked pending human approval.",
                )
                yield AgentEvent(
                    event_type=AgentEventType.END,
                    session_id=session_id,
                    data={"status": "PAUSED"},
                    message="Agent execution paused for HITL review.",
                )
                return

            # Execute Tool (Normal / A2A / Workflow)
            async for evt in self._execute_tool_and_stream(session_id, tool_name, tool_args, scoped_tools):
                yield evt

        # 6. Step 3: Synthesis & Final Answer
        persona_prefix = f"[{self.persona.role}] " if self.persona and self.persona.role else ""
        answer_text = f"{persona_prefix}Task processed successfully: {prompt}"
        if tool_call_to_make:
            answer_text += f" (invoked tool '{tool_call_to_make['name']}')"

        # Stream tokens
        tokens = answer_text.split(" ")
        for i, token in enumerate(tokens):
            chunk = token + (" " if i < len(tokens) - 1 else "")
            yield AgentEvent(
                event_type=AgentEventType.TOKEN,
                session_id=session_id,
                data={"delta": chunk},
                message=chunk,
            )

        # Final answer event & checkpoint
        self.save_checkpoint(session_id, step=3, state_data={"final_answer": answer_text})
        yield AgentEvent(
            event_type=AgentEventType.FINAL_ANSWER,
            session_id=session_id,
            data={"answer": answer_text},
            message=answer_text,
        )

        yield AgentEvent(
            event_type=AgentEventType.END,
            session_id=session_id,
            data={"status": "COMPLETED"},
            message="Execution completed.",
        )

    # ------------------------------------------------------------------------
    # HITL Resumption: resume_after_hitl
    # ------------------------------------------------------------------------
    async def resume_after_hitl(
        self,
        session_id: str,
        approved: bool,
        comment: Optional[str] = None,
        new_args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Resume a paused agent session after human review."""
        if session_id not in self._pending_hitl:
            yield AgentEvent(
                event_type=AgentEventType.ERROR,
                session_id=session_id,
                message=f"No pending HITL session found for session_id: {session_id}",
            )
            return

        pending = self._pending_hitl.pop(session_id)
        hitl_req: HITLConfirmRequest = pending["hitl_request"]
        tool_name = hitl_req.tool_name
        tool_args = new_args if new_args is not None else hitl_req.tool_args
        scoped_tools = pending["scoped_tools"]

        if not approved:
            hitl_req.status = "REJECTED"
            hitl_req.comment = comment or "Action rejected by human reviewer."
            reject_msg = f"Action '{tool_name}' was REJECTED by human supervisor. Reason: {hitl_req.comment}"
            self.save_checkpoint(session_id, step=3, state_data={"hitl_rejected": hitl_req.model_dump()})
            yield AgentEvent(
                event_type=AgentEventType.THOUGHT,
                session_id=session_id,
                data={"thought": reject_msg},
                message=reject_msg,
            )
            yield AgentEvent(
                event_type=AgentEventType.FINAL_ANSWER,
                session_id=session_id,
                data={"answer": reject_msg},
                message=reject_msg,
            )
            yield AgentEvent(
                event_type=AgentEventType.END,
                session_id=session_id,
                data={"status": "REJECTED"},
                message="Session terminated following human rejection.",
            )
            return

        hitl_req.status = "APPROVED"
        hitl_req.comment = comment or "Approved by supervisor."
        resume_thought = f"Human supervisor APPROVED action '{tool_name}'. Proceeding with execution."
        self.save_checkpoint(session_id, step=3, state_data={"hitl_approved": hitl_req.model_dump()})
        yield AgentEvent(
            event_type=AgentEventType.THOUGHT,
            session_id=session_id,
            data={"thought": resume_thought},
            message=resume_thought,
        )

        async for evt in self._execute_tool_and_stream(session_id, tool_name, tool_args, scoped_tools):
            yield evt

        final_text = f"Action '{tool_name}' successfully executed with human authorization."
        self.save_checkpoint(session_id, step=4, state_data={"final_answer": final_text})
        yield AgentEvent(
            event_type=AgentEventType.FINAL_ANSWER,
            session_id=session_id,
            data={"answer": final_text},
            message=final_text,
        )
        yield AgentEvent(
            event_type=AgentEventType.END,
            session_id=session_id,
            data={"status": "COMPLETED"},
            message="Execution completed post-HITL approval.",
        )

    # ------------------------------------------------------------------------
    # A2A Delegation Sub-routine
    # ------------------------------------------------------------------------
    async def dispatch_subagent(
        self,
        parent_session_id: str,
        target_agent_id: str,
        instruction: str,
    ) -> A2ADelegationTask:
        """Dispatch a delegated subtask to a registered downstream subagent."""
        child_session_id = f"{parent_session_id}_child_{uuid.uuid4().hex[:6]}"
        task = A2ADelegationTask(
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
            target_agent_id=target_agent_id,
            instruction=instruction,
        )
        subagent = self._subagents.get(target_agent_id)
        if not subagent:
            task.status = "FAILED"
            task.result = f"Subagent '{target_agent_id}' not found in registered pool."
            return task

        events = []
        async for evt in subagent.reply_stream(
            session_id=child_session_id,
            prompt=instruction,
            tenant_id=self.tenant_id,
        ):
            events.append(evt)
            if evt.event_type == AgentEventType.FINAL_ANSWER:
                task.result = evt.data.get("answer")

        task.status = "COMPLETED" if task.result else "FAILED"
        return task

    # ------------------------------------------------------------------------
    # Helper: Tool Execution Streamer
    # ------------------------------------------------------------------------
    async def _execute_tool_and_stream(
        self,
        session_id: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        scoped_tools: Dict[str, Callable[..., Any]],
    ) -> AsyncGenerator[AgentEvent, None]:
        """Execute tool call and emit TOOL_CALL and TOOL_RESULT events."""
        yield AgentEvent(
            event_type=AgentEventType.TOOL_CALL,
            session_id=session_id,
            data={"tool": tool_name, "args": tool_args},
            message=f"Executing tool '{tool_name}'",
        )

        result_val: Any = None
        if tool_name == "delegate_subtask":
            target_id = tool_args.get("subagent_id", "")
            sub_prompt = tool_args.get("instruction", "")
            yield AgentEvent(
                event_type=AgentEventType.A2A_DISPATCH,
                session_id=session_id,
                data={"target_agent_id": target_id, "instruction": sub_prompt},
                message=f"Delegating subtask to subagent '{target_id}'",
            )
            delegation = await self.dispatch_subagent(session_id, target_id, sub_prompt)
            result_val = delegation.result or delegation.status
        elif tool_name in scoped_tools:
            func = scoped_tools[tool_name]
            try:
                sig = inspect.signature(func)
                kwargs = dict(tool_args)
                if "session_id" in sig.parameters and "session_id" not in kwargs:
                    kwargs["session_id"] = session_id

                if inspect.iscoroutinefunction(func):
                    result_val = await func(**kwargs)
                else:
                    result_val = func(**kwargs)
            except Exception as exc:
                result_val = f"Tool execution error: {str(exc)}"
        else:
            result_val = f"Tool '{tool_name}' is not allowed or not registered."

        self.save_checkpoint(
            session_id,
            step=2,
            state_data={"tool_name": tool_name, "tool_result": str(result_val)},
        )
        yield AgentEvent(
            event_type=AgentEventType.TOOL_RESULT,
            session_id=session_id,
            data={"tool": tool_name, "result": result_val},
            message=f"Tool '{tool_name}' returned: {result_val}",
        )

    # ------------------------------------------------------------------------
    # Helper: Tool Call Detector
    # ------------------------------------------------------------------------
    def _detect_tool_call(
        self,
        prompt: str,
        scoped_tools: Dict[str, Callable[..., Any]],
        mode: ExecutionMode,
    ) -> Optional[Dict[str, Any]]:
        """Intelligently detects intended tool calls from prompt or simulation."""
        prompt_lower = prompt.lower()

        if mode == ExecutionMode.A2A or "delegate" in prompt_lower or "subtask" in prompt_lower:
            for sub_id in self._subagents.keys():
                if sub_id.lower() in prompt_lower:
                    return {
                        "name": "delegate_subtask",
                        "args": {"subagent_id": sub_id, "instruction": prompt},
                    }

        for hr_tool in self._high_risk_tools:
            if hr_tool.replace("_", " ") in prompt_lower or hr_tool in prompt_lower:
                return {
                    "name": hr_tool,
                    "args": {"amount": 10000, "target": "account_dest"},
                    "is_high_risk": True,
                }

        for tool_name in scoped_tools.keys():
            if tool_name.replace("_", " ") in prompt_lower or tool_name in prompt_lower:
                return {"name": tool_name, "args": {}}

        return None

    # ------------------------------------------------------------------------
    # Backward Compatibility APIs: execute_task & execute_react_turn
    # ------------------------------------------------------------------------
    async def execute_task(self, task: AgentTask) -> AgentTask:
        """Backward-compatible batch task executor."""
        prompt_text = task.prompt or task.goal or "Execute default task"
        events: List[AgentEvent] = []

        async for evt in self.reply_stream(
            session_id=task.session_id,
            prompt=prompt_text,
            tenant_id=task.tenant_id,
            mode=task.mode,
        ):
            events.append(evt)
            if evt.event_type == AgentEventType.REQUIRE_CONFIRM:
                task.status = "PAUSED"
                task.hitl_request = HITLConfirmRequest(**evt.data)
                return task
            if evt.event_type == AgentEventType.FINAL_ANSWER:
                task.result = evt.data.get("answer")

        task.status = "COMPLETED" if task.result else "FAILED"
        return task

    async def execute_react_turn(
        self,
        tenant_id: str,
        session_id: str,
        user_prompt: str,
        tools: Optional[List[Callable[..., Any]]] = None,
    ) -> str:
        """Backward-compatible ReAct turn executor."""
        if tools:
            for t in tools:
                t_name = getattr(t, "__name__", str(t))
                self.register_tool(t_name, t)

        final_answer = ""
        async for evt in self.reply_stream(
            session_id=session_id,
            prompt=user_prompt,
            tenant_id=tenant_id,
        ):
            if evt.event_type == AgentEventType.FINAL_ANSWER:
                final_answer = evt.data.get("answer", "")

        return final_answer or f"[ManagedAgent Response to: {user_prompt}]"
