# AgentScope 2.0 当前项目代码深度架构文档

## 一、 系统宏观定位与设计哲学

AgentScope (v2.0.12dev) 是一个面向企业级多智能体系统的工业级应用平台与运行时（Runtime）。它不仅提供了智能体逻辑抽象（Agent、ReAct Loop、Toolkit、Middleware），更内建了一个**基于 FastAPI 的现代化异步服务体系（App Server）**，实现了请求非阻塞接入、分布式事件总线、人机交互协同（HITL）、工作空间沙箱隔离、多通道对接（钉钉、飞书、Discord）以及知识库 RAG。

```
+----------------------------------------------------------------------------------------------------+
|                                    客户端接入层 (Client Ecosystem)                                  |
|   Web UI (Vue/React)  |  Mobile App  |  Channel Webhooks (DingTalk/Feishu)  |  A2A / AG-UI / SSE   |
+----------------------------------------------------------------------------------------------------+
                                                  │
                                                  ▼ (HTTP / SSE / WebRTC)
+----------------------------------------------------------------------------------------------------+
|                              AgentScope 2.0 API 接入层 (FastAPI Layer)                              |
|   /chat        - 非阻塞任务提交 (入 FIFO 队列)       /sessions   - 会话生命周期与 SSE 实时事件流       |
|   /agents      - 智能体配置与定义 CRUD             /workspace  - 文件管理与文件下载鉴权              |
|   /sops        - SOP 标准作业流定义与运行           /channels   - 跨平台通道接入与绑定                |
|   /knowledge   - 知识库与切片索引管理              /realtime   - WebRTC 实时音视频全双工交互          |
+----------------------------------------------------------------------------------------------------+
                                                  │
                                                  ▼
+----------------------------------------------------------------------------------------------------+
|                         核心编排与调度中枢 (Management & Service Core)                             |
|  ┌─────────────────────────┐  ┌──────────────────────────┐  ┌───────────────────────────────────┐  |
|  |     WakeupDispatcher    |  |     ChatRunRegistry      |  |      BackgroundTaskManager        |  |
|  | (消费 chat_inputs FIFO)  |  |   (防重入单飞会话注册表)    |  |       (耗时任务异步脱机执行)       |  |
|  └─────────────────────────┘  └──────────────────────────┘  └───────────────────────────────────┘  |
|                                                 │                                                  |
|  ┌──────────────────────────────────────────────▼───────────────────────────────────────────────┐  |
|  |                                          ChatService                                         |  |
|  |  • 动态组装 Agent、模型、工具与中间件        • 会话级分布式排他锁 (session_run)                |  |
|  |  • 驱动 agent.reply_stream() 生成推理事件    • 事件持久化与扇出 (publish_session_event)          |  |
|  |  • 会话自动语义命名 (_auto_name_session)     • HITL 中断挂起与事件投影 (Projectors)             |  |
|  └──────────────────────────────────────────────────────────────────────────────────────────────┘  |
+----------------------------------------------------------------------------------------------------+
                                                  │
                       ┌──────────────────────────┴──────────────────────────┐
                       ▼                                                     ▼
+---------------------------------------------+       +----------------------------------------------+
|     Agent 运行时引擎 (Agent Runtime Core)    |       |      工具、沙箱与扩展层 (Tools & Sandboxes)    |
| • Agent (ReAct 循环与推理状态机)            |       | • Toolkit (工具集管理与并发隔离)             |
| • ModelConfig (支持主模型 + Fallback 降级)   |       | • PermissionEngine (细粒度权限校验与危险阻断)|
| • Middleware 管道链:                        |       | • Workspaces (隔离执行环境):                 |
|   - InboxMiddleware (跨 Agent 收件箱)       |       |   Local, Docker, Bubblewrap, Daytona,        |
|   - ToolOffloadMiddleware (后台脱机执行)    |       |   E2B, Kubernetes, AppleContainer            |
|   - StateChangeMiddleware (状态追踪)        |       | • MCP Gateway (Model Context Protocol 客户端)|
|   - RAGMiddleware (异构知识库多路召回)      |       | • Skills (动态加载代码技能包)                |
|   - TTSMiddleware (语音合成流式输出)        |       +----------------------------------------------+
+---------------------------------------------+
                       │
                       ▼
+----------------------------------------------------------------------------------------------------+
|                                    数据持久化与事件总线层                                           |
|  ┌──────────────────────────────────────────┐     ┌─────────────────────────────────────────────┐  |
|  |         Storage (唯一事实源存储)          |     |           MessageBus (实时事件总线)         |  |
|  |  • AsyncSQLAlchemyStorage (PG/SQLite/MySQL)|   |  • RedisMessageBus (基于 Redis 流与 Hash)   |  |
|  |    - credentials, agents, sessions, sops |     |    - session_event 流发布订阅               |  |
|  |    - messages (复合主键持久化), teams    |     |    - chat_inputs FIFO 队列与排它锁          |  |
|  |  • RedisStorage (纯内存高速持久化方案)   |     |  • InMemoryMessageBus (单机测试/轻量运行)   |  |
|  └──────────────────────────────────────────┘     └─────────────────────────────────────────────┘  |
+----------------------------------------------------------------------------------------------------+
```

---

## 二、 系统核心模块划分与代码实现映射

### 1. 接入与路由层 (`src/agentscope/app/_router/`)
- **`_chat.py`**: 核心聊天请求接入端点。采用**非阻塞提交设计（Fire-and-Forget）**：
  - `POST /chat`: 客户端提交新消息或 HITL 确认结果。不直接等待 LLM 生成，而是将消息压入 `chat_inputs` FIFO 队列，立即返回 HTTP 200 `ChatTriggerResponse(status="queued")`。
  - `GET /chat/queue`: 获取当前会话正在排队的未处理消息。
  - `PATCH /chat/queue/order` 与 `DELETE /chat/queue/{item_id}`: 支持在 Agent 尚未开工前重排或撤销待办对话。
- **`_session.py`**: 会话状态与实时事件出口：
  - `GET /sessions/{session_id}/stream`: 基于 **HTTP SSE (Server-Sent Events)** 的长连接，前端通过该连接单向实时接收 Agent 产生的 Token 流、工具调用进度、HITL 确认卡片及运行完成状态。
- **`_workspace.py`**: 工作空间文件访问与安全令牌下载机制（`download_secret` 签发有时效的下载凭证，防止未授权访问）。
- **`_channel.py`**: 跨平台 Bot 网关，接收并调度来自钉钉、飞书、Discord 的 Webhook 触发。

### 2. 调度中枢与生命周期管理 (`src/agentscope/app/_manager/` & `_service/`)
- **`WakeupDispatcher` (`_wakeup_dispatcher.py`)**: 
  - 维持后台异步主协程循环，监听全局运行触发信号。
  - 负责从 `chat_inputs` FIFO 队列中按顺序弹出待执行轮次，避免同会话并发产生状态错乱，调用 `ChatService.run()`。
- **`ChatRunRegistry` (`_chat_run_registry.py`)**:
  - 进程级会话并发防重入注册表，保证同一会话在单实例内只有一个正在活跃执行的 asyncio 任务。
- **`ChatService` (`_service/_chat.py`)**:
  - 执行引擎的核心中枢。单次运行（`run()`）包含严密的 7 个阶段：
    1. **会话排它锁竞争**: 通过 `MessageBus.session_run` 获取分布式锁（防止集群多实例同时处理同会话）。
    2. **环境解析与装配**: 加载 AgentRecord、SessionRecord、Team 上下文、ModelConfig 与 Workspace 沙箱。
    3. **中间件流水线挂载**: 按序串联 `InboxMiddleware`、`StateChangeMiddleware`、`ToolOffloadMiddleware`、`RAGMiddleware`、`TTSMiddleware` 等。
    4. **工具箱（Toolkit）编译**: 将基础文件工具、沙箱命令、外部技能、MCP 工具及团队协作工具打包。
    5. **启动 Agent 思考循环**: 调用 `agent.reply_stream()`，以异步生成器模式逐帧捕获事件。
    6. **事件广播与持久化**: 每产生一个事件立即写入 `MessageBus`（供 SSE 消费），并在结束时将整条 `AssistantMsg` 与状态更新回 Storage。
    7. **自动命名触发**: 首轮对话完成后，调用小模型异步总结生成 Session 标题。

### 3. 智能体与执行引擎 (`src/agentscope/agent/`, `model/`, `tool/`)
- **`Agent` (`agent/_agent.py`)**: 经典的 ReAct 驱动循环。维护上下文消息列表、调用大模型做决策、解析函数调用（Function Calling）、分发给 Toolkit 执行，支持 `tool_stop` 与人工中断退出。
- **`Model` (`model/`)**: 适配主流商业与开源模型（Anthropic Claude, Google Gemini, OpenAI, DashScope, DeepSeek, Ollama 等），统一了消息格式、流式 Token 产生与结构化输出。
- **`WorkspaceManager` (`workspace_manager/`)**:
  - 实现了多种级别的环境隔离：`LocalWorkspaceManager`（本地进程文件空间）、`DockerWorkspaceManager`（容器级隔离）、`BubblewrapWorkspaceManager`（Linux 命名空间隔离）、`Daytona`、`E2B`（云端微虚拟机沙箱）、`Kubernetes` 等。

### 4. 存储与状态总线 (`src/agentscope/app/storage/` & `message_bus/`)
- **`AsyncSQLAlchemyStorage` (`storage/_sql/`)**:
  - 基于 SQLAlchemy 2.0 异步 ORM。将关键关系字段（`user_id`, `agent_id`, `created_at` 等）提升为索引列，其余可扩展数据序列化到 JSON 列 `payload`。
  - 核心表结构：`credentials`、`agents`、`sessions`、`messages`（以 `session_id + msg_id` 为联合主键）、`knowledge_bases`、`knowledge_documents`、`sops`、`sop_runs`、`teams`。
- **`MessageBus` (`message_bus/`)**:
  - 提供分布式信号协调：`session_event`（事件流发布/重放）、`chat_inputs`（对话排队）、`registry`（活跃状态注册）及分布式会话互斥锁。

---

## 三、 核心架构时序图 (Architectural Sequence Diagrams)

### 时序图 1：端到端聊天请求与异步流式执行时序 (End-to-End Chat Flow)

这是 AgentScope 中最核心的数据与控制流。它展现了用户发起一条聊天请求后，系统如何通过“非阻塞入队 -> 调度器唤醒 -> 分布式锁控制 -> Agent 推理与沙箱执行 -> SSE 流式推送 -> 数据库落地”完成闭环：

```mermaid
sequenceDiagram
    autonumber
    actor User as 客户端 (Web/App)
    participant Gateway as 路由接入层 (_router/_chat)
    participant Bus as 事件总线 (MessageBus)
    participant Dispatcher as 调度中心 (WakeupDispatcher)
    participant ChatSvc as 核心服务 (ChatService)
    participant Storage as 数据库 (AsyncSQLStorage)
    participant Agent as 智能体引擎 (Agent / ReAct)
    participant LLM as 大模型供应商 (Model API)
    participant Sandbox as 执行沙箱 (Workspace / Tools)
    participant Stream as SSE 流端点 (_router/_session)

    Note over User, Stream: 阶段 1：非阻塞请求接入与入队
    User->>Gateway: POST /chat (session_id, user_id, input Msg)
    Gateway->>Storage: _ensure_session(user_id, session_id)
    Storage-->>Gateway: SessionRecord Valid
    Gateway->>Bus: enqueue_chat_input(session_id, Msg)
    Bus-->>Gateway: 入队成功 (item_id)
    Gateway-->>User: HTTP 200 {status: "queued", item_id}
    Note over User: 客户端立即释放 HTTP 连接，转而通过 SSE 等待结果

    Note over Dispatcher, ChatSvc: 阶段 2：后台调度与锁竞争
    loop 异步轮询与消费
        Dispatcher->>Bus: claim_next_chat_input(session_id)
        Bus-->>Dispatcher: 返回待办 input Msg
    end
    Dispatcher->>ChatSvc: run(user_id, session_id, agent_id, input_msg)
    ChatSvc->>Bus: acquire_session_lock(session_id, ttl=30s)
    Bus-->>ChatSvc: 获得分布式排他锁

    Note over ChatSvc, Agent: 阶段 3：环境组装与 Agent 驱动
    ChatSvc->>Storage: 加载 AgentRecord, SessionRecord
    Storage-->>ChatSvc: Record 数据
    ChatSvc->>Storage: upsert_message(input_msg)
    ChatSvc->>Sandbox: get_workspace(session_id)
    ChatSvc->>Agent: 初始化 Agent 实例 (注入 Model, Toolkit, Middlewares)
    ChatSvc->>Agent: agent.reply_stream(inputs=input_msg)

    Note over Agent, Stream: 阶段 4：ReAct 推理流与工具执行
    Agent->>LLM: POST chat completions (Prompt + Context + Tools)
    loop 模型流式吐字
        LLM-->>Agent: Token Chunks / ToolCall Specification
        Agent->>ChatSvc: 产生 ReplyStartEvent / TokenEvent
        ChatSvc->>Bus: publish_session_event(session_id, Event)
        Bus-->>Stream: Fan-out 推送
        Stream-->>User: SSE Data: {"event": "token", "content": "..."}
    end

    alt 大模型决策调用工具
        Agent->>Sandbox: 执行工具命令 (e.g. bash, edit, search)
        Sandbox-->>Agent: 返回 stdout / stderr 结果
        Agent->>ChatSvc: 产生 ToolResultEvent
        ChatSvc->>Bus: publish_session_event(ToolResultEvent)
        Bus-->>Stream: SSE 推送工具执行状态
        Stream-->>User: SSE Data: {"event": "tool_result", ...}
        Agent->>LLM: 将工具结果带入上下文，再次请求决策
        LLM-->>Agent: 最终回复内容
    end

    Note over ChatSvc, Storage: 阶段 5：会话落盘与状态更新
    Agent-->>ChatSvc: ReplyEndEvent (FinishedReason="completed")
    ChatSvc->>Storage: upsert_message(AssistantMsg)
    ChatSvc->>Storage: update_session_state(session_id, updated_state)
    ChatSvc->>Bus: release_session_lock(session_id)
    ChatSvc-)ChatSvc: 触发异步 _auto_name_session (自动总结会话标题)
```

---

### 时序图 2：人机协同 (HITL) 权限中断与挂起恢复时序 (Human-in-the-Loop Resumption)

当 Agent 执行危险操作（如删除核心文件、运行高危脚本），PermissionEngine 会拦截并产生挂起事件，等待前端人工确认后断点续传：

```mermaid
sequenceDiagram
    autonumber
    actor User as 用户前端
    participant Gateway as 路由接入层
    participant ChatSvc as 核心执行层 (ChatService)
    participant PermEngine as 权限引擎 (PermissionEngine)
    participant Agent as 智能体 (Agent)
    participant Bus as 消息总线 (MessageBus)
    participant Storage as 数据库存储

    Note over Agent, PermEngine: 1. 危险工具拦截与挂起
    Agent->>PermEngine: check_permissions("delete_database", args)
    PermEngine-->>Agent: 判定结果: PermissionBehavior.ASK (需要人机确认)
    Agent->>ChatSvc: 抛出 UserConfirmRequestEvent (reply_id, tool_call_id, prompt)
    ChatSvc->>Storage: 暂存当前未完成的 reply 消息
    ChatSvc->>Bus: publish_session_event(UserConfirmRequestEvent)
    Bus-->>User: SSE 推送: 渲染交互式确认卡片 (Approve / Reject)
    ChatSvc->>Bus: release_session_lock(session_id)
    Note over ChatSvc: 当前 Chat 协程正常收工，Session 进入挂起状态 (Parked)

    Note over User, Gateway: 2. 用户人工点击与恢复
    User->>User: 用户审查命令并点击【批准执行 (Approve)】
    User->>Gateway: POST /chat (session_id, input=UserConfirmResultEvent{approved: True})
    Gateway->>Bus: enqueue_run_trigger(kind="resume", event=UserConfirmResultEvent)
    Gateway-->>User: HTTP 200 {status: "started"}

    Note over Gateway, Agent: 3. 断点恢复与继续执行
    Bus->>ChatSvc: WakeupDispatcher 捕获 resume 触发
    ChatSvc->>Bus: acquire_session_lock(session_id)
    ChatSvc->>Storage: get_message(reply_id) (读取挂起前的消息快照)
    ChatSvc->>Agent: 恢复 Agent 运行时，将 UserConfirmResult 喂入上下文
    Agent->>Agent: 恢复断点执行工具逻辑并继续 ReAct 循环
    Agent->>Bus: 继续发布后续 Token 及完成事件
    ChatSvc->>Storage: 保存最终完整 Assistant 消息
    ChatSvc->>Bus: release_session_lock(session_id)
```

---

### 时序图 3：多智能体团队协作与 SOP 阶段扭转时序 (Multi-Agent & SOP Workflow)

展示了 Team Leader 启动 Worker 子 Agent，通过收件箱（Inbox）异步消息交换，最终通过 SOP 阶段提交工具完成流程扭转：

```mermaid
sequenceDiagram
    autonumber
    participant Leader as 队长会话 (Leader Session)
    participant TeamTools as 团队工具集 (Team Tools)
    participant Bus as 消息总线 (Inbox / Events)
    participant Worker as 专家子智能体 (Worker Session)
    participant SOP as SOP 作业流服务 (SOPService)
    participant Storage as 数据库 (Storage)

    Note over Leader, TeamTools: 1. 队长拆解任务并分发
    Leader->>TeamTools: 调用 team_say(to_agent="coder", message="编写数据爬取脚本")
    TeamTools->>Storage: 查找或动态创建 coder 子会话
    TeamTools->>Bus: deliver_to_inbox(worker_session_id, task_msg)
    TeamTools->>Bus: enqueue_run_trigger(worker_session_id, kind="queued_message")
    TeamTools-->>Leader: 返回: "已派发给 coder，正在后台执行"

    Note over Bus, Worker: 2. 专家 Worker 独立执行任务
    Bus->>Worker: 唤醒 Worker 协程运行
    Worker->>Worker: 挂载 TeamMemberLoopMiddleware
    Worker->>Worker: 编写代码并使用沙箱运行测试
    Worker->>Bus: deliver_to_inbox(leader_session_id, result_msg)
    Worker-->>Storage: 保存 Worker 会话记录

    Note over Leader, SOP: 3. 汇总成果并提交 SOP 阶段
    Bus->>Leader: 唤醒 Leader 会话，从 Inbox 捞取 Worker 成果
    Leader->>Leader: 综合评审 Worker 的输出结果
    Leader->>TeamTools: 调用 sop_submit(stage="code_review", artifacts=[...])
    TeamTools->>SOP: submit_step(sop_run_id, step_index, payload)
    SOP->>Storage: 更新 SOP 运行状态 (Phase: InProgress -> Completed)
    SOP->>Bus: publish_session_event("sop_phase_changed")
    Leader-->>Leader: 本阶段结束，推进至下一作业流阶段
```

---

## 四、 当前项目架构的核心特征与潜在局限

1. **分布式组件强绑定 Redis**:
   - 当前集群环境强依赖 `RedisMessageBus` 进行排他锁竞争、会话输入队列（`chat_inputs`）和事件流的分发。如果移除 Redis，单机模式只能退化为不支持多进程并发的 `InMemoryMessageBus`。
2. **多租户（Multi-Tenancy）支持仅停留在 `user_id` 级别**:
   - `deps.py` 中虽然规划了 JWT，但目前完全由 `X-User-ID` 单一标识驱动。数据库表结构缺乏顶层 `tenant_id` 强隔离，无行级安全策略（RLS），也未对多企业租户的 Token 配额、私有知识库进行硬性隔离。
3. **文件流中转消耗服务器网络与内存**:
   - 知识库与文件上传目前依赖服务端 FastAPI 中转存储到本地/BlobStore，缺乏直接生成云对象存储（S3/MinIO）预签名直传（Presigned URL）的非阻塞架构。
4. **工具参数防幻觉依赖基础机制**:
   - 工具基类（`ToolBase`）通过 JSONSchema 或静态检查进行参数检验，但在遇到复杂的大模型生成参数幻觉时，缺乏一层前置的、标准化的 Pydantic 运行时严格结构体“防弹衣”防御与结构化自愈机制。
