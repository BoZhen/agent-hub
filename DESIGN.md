# Agent Hub 技术设计文档

## 1. 文档目的

本文档描述 Agent Hub 的设计目标、系统边界、核心架构、关键数据模型与运行机制。其目标读者包括：

- 项目维护者
- 新加入的开发者
- 需要理解部署方式或集成方式的使用者

本文档聚焦**当前实现与稳定设计原则**，不记录临时调试过程、个人环境习惯或一次性实验配置。安装细节与操作示例请以 `README.md` 与 `SETUP.md` 为准。

---

## 2. 系统概览

Agent Hub 是一个面向 AI CLI 会话的统一管理服务，用于集中观察和控制多个 **Claude Code** 与 **Codex CLI** 会话。系统提供以下能力：

- 会话发现与注册
- 事件采集与时间线记录
- 实时状态展示
- 基于 tmux 的远程审批与会话操作
- 通过 WebSocket 向前端推送实时更新
- 通过 MCP SSE 暴露查询接口，供其他 Agent 或工具调用

当前实现是一个**单进程 Python 服务**，在同一应用内提供：

- HTTP API
- Web 页面
- WebSocket
- MCP Server
- Telegram 通知集成

---

## 3. 设计目标与非目标

### 3.1 设计目标

1. **统一观测**  
   用单一界面展示多台主机上的 AI CLI 会话状态、事件与运行上下文。

2. **低侵入接入**  
   尽量复用 Claude Code / Codex CLI 的现有能力，不要求修改 CLI 本体。

3. **实时反馈**  
   新事件、等待审批、状态切换应尽快反映到 Web UI 和通知通道。

4. **可恢复会话管理**  
   依赖 tmux 作为会话承载层，使会话能够脱离浏览器连接而持续存在。

5. **集成能力**  
   通过 MCP、API 与模块化服务层支持当前系统接入与自动化使用。

### 3.2 非目标

当前版本**不以以下内容为核心目标**：

- 分布式强一致同步
- 多租户权限系统
- 公网暴露场景下的复杂鉴权体系
- 通用任务编排平台

---

## 4. 系统上下文

系统目前支持三类事件来源：

1. **Claude Code（Hook Push）**  
   通过本地 hook 将事件推送到 Hub。

2. **Codex CLI + OMX（Hook Push）**  
   借助 oh-my-codex 提供的原生 hook，再由桥接脚本转发到 Hub。

3. **Codex CLI（tmux 扫描回退）**  
   未启用 OMX 时，Hub 通过周期性扫描 tmux pane 识别 Codex 会话并推断状态。

总体关系如下：

```text
Claude Code hooks  ───────┐
                          │
Codex + OMX hooks ────────┼──▶ Agent Hub (FastAPI)
                          │       ├─ REST API
Bare Codex tmux scan ─────┘       ├─ Web UI
                                  ├─ WebSocket
                                  ├─ MCP SSE
                                  ├─ Telegram integration
                                  └─ SQLite (WAL)
                                           │
                                           └─ Session / Event state

Web Dashboard  ◀──────────── WebSocket / HTTP
Web Terminal   ◀──────────── tmux attach / send-keys
Other Agents   ◀──────────── MCP SSE
```

---

## 5. 核心架构

### 5.1 应用入口

`src/agent_hub/main.py` 负责：

- 解析命令行参数
- 构建 `HubConfig`
- 初始化 SQLite 数据库
- 注册 API、Web 与 WebSocket 路由
- 挂载 MCP Server
- 启动后台任务：
  - `periodic_sweep`
  - `periodic_pending_check`
- 启动与停止 Telegram Bot

### 5.2 分层结构

项目采用较为清晰的分层设计：

#### API 层

位于 `src/agent_hub/api/`，负责暴露外部接口：

- `events.py`：接收 hook 事件
- `sessions.py`：查询、审批、删除、pin、通知
- `tmux.py`：tmux 会话管理
- `ws.py`：WebSocket 广播

#### 服务层

位于 `src/agent_hub/services/`，承载主要业务逻辑：

- `event_processor.py`：事件落库、摘要生成、广播
- `session_manager.py`：状态机、tmux 扫描、审批检测、生命周期维护
- `telegram_bot.py`：Telegram 通知
- `transcript_reader.py`：从 transcript 提取 token 与摘要信息

#### 数据层

`src/agent_hub/db.py` 定义数据库 schema、迁移与 CRUD。

#### Web 层

`src/agent_hub/web/` 提供 Jinja2 页面与路由。

#### MCP 层

`src/agent_hub/mcp/server.py` 暴露面向 Agent 的查询工具。

### 5.3 运行拓扑

当前系统的运行拓扑如下：

```text
Claude Code CLI       ──[HTTP / command hooks]────────┐
                                                      │
Codex CLI + OMX      ──[native hook -> bridge]────────┼──▶ Agent Hub
                                                      │      ├─ FastAPI API
Bare Codex CLI       ◀─[tmux capture-pane polling]────┘      ├─ Web UI
                                                             ├─ WebSocket
                                                             ├─ MCP SSE
                                                             ├─ Telegram Bot
                                                             └─ SQLite (WAL)

Web Dashboard         ◀──────────── HTTP / WebSocket
Web Terminal          ◀──────────── tmux attach / send-keys
Other MCP Clients     ◀──────────── /mcp/sse
```

该拓扑反映了三类输入路径：

- Claude 通过 hook 主动推送事件
- Codex + OMX 通过桥接脚本复用相同事件入口
- 未接入 hook 的 Codex 通过 tmux 扫描维持发现与状态判断

### 5.4 外部接口总览

系统当前暴露四类外部接口：

#### HTTP API

主要接口包括：

- `POST /api/events`
- `GET /api/sessions`
- `GET /api/sessions/{id}`
- `GET /api/sessions/{id}/events`
- `GET /api/sessions/{id}/events/latest`
- `POST /api/sessions/{id}/approve`
- `POST /api/sessions/{id}/pin`
- `DELETE /api/sessions/{id}`
- `POST /api/sessions/clear-stopped`
- `GET /api/stats`
- `GET /api/tmux/list`
- `POST /api/tmux/new`
- `POST /api/tmux/kill`
- `GET /api/browse`
- `POST /api/notify`

#### Web 页面

- `/`
- `/idle`
- `/stopped`
- `/tmux`
- `/sessions/{id}`

#### WebSocket

- `WS /ws`

用于推送事件、pending 状态变化和统计更新。

#### MCP

- `GET /mcp/sse`

用于向外部 Agent 暴露查询型工具。

---

## 6. 数据模型

### 6.1 核心表

系统以两个核心表为中心：

#### `sessions`

记录单个 AI CLI 会话的长期状态，包含：

- `session_id`
- `hub_id`
- `hostname`
- `cwd`
- `model`
- `status`
- `started_at`
- `last_seen_at`
- `stopped_at`
- `tool`
- `tmux_session`
- `pending_tool`
- `pending_detail`
- `pending_always_label`
- `pinned`
- transcript 与 token 统计相关字段

#### `events`

记录每一个已接收的 hook 事件，包含：

- `event_uid`
- `session_id`
- `event_type`
- `tool_name`
- `summary`
- `payload`
- `created_at`

### 6.2 设计原则

1. **会话与事件分离**  
   `sessions` 用于表达当前状态，`events` 用于表达历史过程。

2. **摘要与原始载荷分离**  
   `summary` 负责支撑 UI 与 MCP 中的快速阅读；`payload` 用于保留足够上下文。

3. **运行时字段直接入库**  
   `pending_tool`、`tmux_session`、`pinned` 等字段作为查询热点直接保存在 `sessions` 中，降低页面刷新与实时推送的计算成本。

4. **数据库定义以代码为准**  
   文档用于解释模型；真实 schema 与迁移逻辑以 `src/agent_hub/db.py` 为权威来源。

---

## 7. 事件模型与处理流程

### 7.1 接收入口

`POST /api/events` 是事件写入主入口。请求至少需要包含：

- `session_id`
- `hook_event_name`

Hub 同时通过 query 参数接收额外上下文，例如：

- `host`
- `tmux_session`
- `tool`

### 7.2 标准处理流程

事件进入系统后，处理步骤如下：

1. 校验最小字段集合
2. 将 `tmux_session` 与 CLI 类型注入 payload
3. 异步调用 `event_processor.process_event`
4. 调用 `session_manager.ensure_session` 确保会话存在
5. 生成人类可读摘要
6. 对 payload 做脱敏和裁剪
7. 将事件写入数据库
8. 根据事件类型更新会话状态
9. 如有 transcript，则补充 token 与模型信息
10. 通过 WebSocket 广播更新

### 7.3 摘要生成

系统对常见事件生成短摘要，供 Dashboard、会话详情页和 MCP 查询使用。例如：

- `SessionStart` → `Session started`
- `PreToolUse` + `Bash` → `$ <command>`
- `Read` / `Write` / `Edit` → `<tool> <path>`
- `UserPromptSubmit` → `User: "<prompt>"`
- `Stop` → `Session idle`

摘要的目标是**帮助快速扫描**，而不是完整复述 payload。

### 7.4 脱敏策略

为控制存储量和敏感信息暴露，系统会在入库前进行处理：

- 对 `Write` / `Edit` 去除大段文件内容
- 对部分 `tool_response` 进行裁剪
- 对用户 prompt 进行长度截断
- 不在事件 payload 中保留本地 `transcript_path`

---

## 8. 会话生命周期模型

### 8.1 主状态

会话主状态包括：

- `active`
- `idle`
- `stopped`

### 8.2 派生状态

在主状态之上，还有两个重要派生状态：

- `waiting`：当前存在待审批操作
- `running`：当前存在长时间运行中的工具调用

这两个派生状态是为 UI 表达服务的覆盖态，不替代主状态机。

### 8.3 状态转移原则

```text
SessionStart / activity  ─────────▶ active
Stop or soft-idle rule   ─────────▶ idle
tmux session disappears  ─────────▶ stopped
resume / new activity    ─────────▶ active
```

更具体地说：

1. `SessionStart` 或新的活跃事件会将会话置为 `active`
2. `Stop` 会将会话置为 `idle`
3. 长时间无活动且不处于“正在工作”界面时，可被后台任务软降级为 `idle`
4. 当底层 tmux session 消失时，会话被标记为 `stopped`

### 8.4 设计取舍

- `stopped` 的判断基于 **tmux 是否实际消失**，而不是单纯时间超时
- `idle` 会话可以长期保留，以支持次日继续恢复工作
- 同名 tmux 被复用时，旧会话会被自动退休，避免主面板出现重复活跃行

---

## 9. tmux 集成设计

tmux 是 Agent Hub 的关键基础设施，用于承载以下能力：

1. **会话持久化**  
   Web 页面断开后，CLI 会话仍可继续执行。

2. **远程审批**  
   Hub 可通过 `tmux send-keys` 向目标 pane 发送确认操作。

3. **状态探测**  
   Hub 可通过 `tmux capture-pane` 获取当前屏幕内容，以判断：
   - 是否等待审批
   - 是否仍在运行工具
   - 是否属于 Codex pane

4. **回退发现机制**  
   对未接入原生 hook 的 Codex 会话，tmux 扫描是发现与状态维持的基础。

### 9.1 远程审批策略

审批逻辑按 CLI 类型区分：

- **Claude**  
  - 默认批准路径通过 Web Terminal API 发送 `y + Enter`
  - "Always" 路径通过 `tmux send-keys Down Enter` 导航

- **Codex**
  - 默认路径通过 `Enter` 确认当前高亮选项（每种审批 UI 的 option 1 都是默认接受动作）
  - "Always" 路径根据 UI 类型决定 `Down` 次数：
    - **Bash 命令审批**（3 选项）：1 Down + Enter
    - **Edit / sandbox 重试审批**（3 选项）：1 Down + Enter
    - **MCP 工具审批**（4 选项）：2 Down + Enter
  - 判别字段是 `pending_tool`，由 parser 在识别 UI 变体时一并写入

### 9.2 审批检测原则

系统不只依赖历史事件，而是使用 pane 内容作为**地面真实状态**：

- 判断当前是否存在待审批提示
- 判断工具是否仍在执行
- 处理 UI 折行和不同渲染样式

这一设计可以减少由于 hook 延迟、遗漏或界面刷新带来的误判。

Codex 解析器把**检测**和**分类**解耦，以避免 Claude 解析器从未遇到的 UI 漂移问题：Codex 有多种审批 UI（Bash / MCP / Edit / 未来变体），而 Claude 只有一种。

**检测**采用纯结构信号，任一 UI 变体都不会改变这三条：

1. `› 1. (Yes|Allow)` 选择器在 pane 尾部窗口（最后 16 行）
2. `2. <text>` 选项行在 selector 下方 8 行内出现（确认是活的选项列表，排除单行残片）
3. Codex 底栏文案（`Press enter to confirm or esc to cancel` 或 `enter to submit | esc to cancel`）在同一尾部窗口

三条全部命中 → 认定存在待审批，**不依赖标题短语**。

**分类**是第二遍 best-effort 扫描，用于 badge 显示和 Always 按钮的导航键计数：

- **Bash** — 标题 `Would you like to run the following command?`，详情为 `$ <cmd>` 行
- **MCP** — 标题 `Allow the <server> MCP server to run tool <x>?`，详情为 `<server>: <tool>`
- **Edit / sandbox 重试** — 标题 `Would you like to make the following edits?`，详情取自 `Reason: ...` 子标题
- **Codex（generic fallback）** — 以上都未命中时仍然报告待审批，详情尽量从最近的 `$ `/`Reason:`/以 `?` 结尾的标题行回取

分类窗口向上扫描 80 行，且从 selector 就近遍历（closest title wins），这样即便历史里还残留已批准块也不会错配。未能分类的 generic Codex 会正常显示 Approve 按钮（Enter 在每种 Codex UI 上都能确认默认选项），只有 Always 按钮会在分类失败时静默隐藏（`_extract_codex_always_label` 对未知 tool_name 返回 `None`）。

这个设计的核心规律：**标题短语漂移或新增 UI 变体只会降级 badge 的精确度，不会把审批请求丢在地上**。要新增一个已知 UI 分类时，只需在 `_CODEX_QUESTION_PATTERNS` 注册短语并按需提供 detail/Always 提取器。

---

## 10. Codex 支持策略

Codex 支持分为两个层次：

### 10.1 首选路径：OMX Hook

在启用 oh-my-codex 后，Codex 可通过桥接脚本向 `/api/events?tool=codex` 推送原生事件。此路径具备：

- 完整事件时间线
- 更准确的工具名称与上下文
- 更及时的状态更新

### 10.2 回退路径：tmux Pane 扫描

未启用 OMX 时，系统通过周期性扫描 tmux pane：

- 识别 Codex pane
- 基于 pane 内容 hash 判断是否有活动
- 解析审批界面
- 更新活跃/空闲状态

该模式能够提供基础管理能力，但事件流不如 hook 模式完整。

---

## 11. Web 界面设计

### 11.1 页面组成

当前 Web 层包含以下主要页面：

- `/`：主 Dashboard
- `/idle`：空闲会话列表
- `/stopped`：已停止会话列表
- `/tmux`：tmux 管理页面
- `/sessions/{id}`：单会话详情页

主 Dashboard 与 `/tmux` 页采用同源的 **1/3 : 2/3 分屏布局**（`lg+` 断点）：

- **左列（1/3）** —— 页面级 scoped nav（`Agent Hub` 与 `Tmux Hub` 互跳，金属配色按钮）+ 紧凑统计条 + 标签 / 新建按钮 + 可滚动卡片列表
- **右列（2/3）** —— iframe 嵌入 Web Terminal（`:7700`），点击任意卡片空白处即将 iframe 切换到该会话的 `?attach=<tmux_name>`，header 同步显示名称与工作目录；窄屏自动收成单列并隐藏 iframe

`/tmux` 页相对 Dashboard 做了减法：

- 卡片仅保留 dot + 名称 + 状态 badge + cwd + Terminal/Kill 按钮，不渲染工具图标、事件流、token 与审批徽章
- 统计条只显示 `Total / Attached` 两项（裸 tmux 多数为 detached，单独统计意义不大）
- 不引入额外 tab；dead pane 通过红色 dot + Kill 按钮直接出现在主列表中
- 当前以 10 秒轮询 + `SessionStart` WebSocket 事件触发列表刷新，未启用按 ID 注入卡片的实时通道

### 11.2 交互原则

Dashboard 的设计目标是：

- 在单屏内快速扫描会话状态
- 让常用操作尽量接近会话卡片
- 将详情信息下沉到详情页，避免主页面信息过载

主页面重点展示：

- 状态
- 会话名 / tmux 名
- 主机名
- 最近事件
- 审批按钮
- 终端入口

### 11.3 实时更新

WebSocket 负责向前端推送：

- 新事件
- pending 状态变化
- waiting 统计变化

这使得页面在大多数场景下无需手动刷新。

---

## 12. MCP 设计

系统通过 `/mcp/sse` 暴露 FastMCP 服务，当前以**查询类工具**为主，主要包括：

- `list_sessions`
- `get_session`
- `search_events`
- `get_dashboard`
- `get_transcript_summary`

设计原则如下：

1. **优先返回人类可读文本**  
   工具面向 Agent 调用场景，强调快速理解。

2. **支持部分 session_id 匹配**  
   减少人工复制完整 ID 的负担。

3. **与 Dashboard 保持一致语义**  
   MCP 返回的状态字段与页面展示尽量一致，避免双重解释。

---

## 13. 后台任务

系统目前依赖两个常驻后台任务：

### 13.1 `periodic_sweep`

负责：

- 软降级长时间无活动的会话
- 发现底层已死亡的 tmux session
- 清理与退休过期状态

### 13.2 `periodic_pending_check`

负责：

- 周期性读取 tmux pane
- 判断是否出现待审批状态
- 解析审批详情与“Always”标签
- 将结果同步到数据库与前端广播
- 触发 Telegram 待审批通知

---

## 14. 配置与运行约定

### 14.1 启动方式

标准启动方式如下：

```bash
uv sync
uv run agent-hub serve --hub-id <hub-id>
```

默认配置由 `HubConfig` 定义，主要包括：

- `host`：默认 `0.0.0.0`
- `port`：默认 `7800`
- `db_path`：默认 `hub.db`
- `terminal_port`：默认 `7700`

### 14.2 环境变量

当前支持的外部配置主要包括：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 14.3 Hook 与桥接脚本

Hook 配置与脚本接入属于部署说明，不在本文档展开写入本机路径、个人目录或设备专属配置。实际配置方式请参阅：

- `README.md`
- `SETUP.md`
- `scripts/codex-hub-hook.sh`

---

## 15. 安全与隐私考虑

### 15.1 网络边界

当前设计默认 Hub 部署在受信任网络中使用，例如：

- 本机 loopback
- 私有局域网
- Tailscale 等私有网络

当前实现不以公网暴露为主要目标，因此不内建完整认证体系。若部署到更开放的网络环境，应在反向代理或网络层补充访问控制。

### 15.2 数据最小化

系统通过以下方式降低敏感信息风险：

- 事件摘要优先
- 大字段裁剪
- 不保存无必要的本地路径信息到事件 payload
- transcript 仅读取必要信息

### 15.3 远程控制边界

审批与会话控制能力本质上依赖 tmux 控制通道，因此应将 Hub 视为**高权限控制平面**。部署时应保证：

- Hub 所在主机可信
- tmux 会话访问受控
- Web Terminal 不向非授权用户开放

---

## 16. 测试与验证

当前仓库中已有基础回归测试，重点覆盖 Codex 审批解析逻辑：

- `tests/test_codex_parser.py`
- `tests/fixtures/`

---

## 17. 代码结构索引

```text
src/agent_hub/
├── api/
│   ├── events.py
│   ├── sessions.py
│   ├── tmux.py
│   └── ws.py
├── mcp/
│   └── server.py
├── services/
│   ├── event_processor.py
│   ├── session_manager.py
│   ├── telegram_bot.py
│   └── transcript_reader.py
├── web/
│   ├── routes.py
│   └── templates/
├── config.py
├── db.py
├── main.py
└── models.py
```

---

## 18. 总结

Agent Hub 的核心设计可以概括为：

- 以 **hook + tmux 扫描** 作为事件与状态来源
- 以 **SQLite** 作为单机状态存储
- 以 **FastAPI + WebSocket + Jinja2** 作为交互层
- 以 **tmux** 作为会话持久化与远程控制基础设施
- 以 **MCP** 作为对其他 Agent 的可编程接口

该设计优先服务于单机/私有网络中的高效会话管理场景，强调低侵入、可恢复与可观测。
