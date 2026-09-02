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

> 本节记录当前已经实现的 Web 界面。下一版产品定位、信息架构、视觉语言与交互约束，
> 以本文末尾的“Agent Hub 产品与界面设计契约”为准；两者冲突时，后者优先。

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

---

# Agent Hub 产品与界面设计契约

## Source of truth

- Status: Active；现有界面尚未全部实现本契约。
- Last refreshed: 2026-09-02
- Primary product surfaces: 会话导航、Needs Action 队列、工作集标签、原生 Terminal 工作区、Session Inspector、归档与 tmux 管理、移动端 Session/Terminal 双模式。
- Canonical rule: 本节是产品与 UI/UX 决策的主依据；前文技术架构继续有效，`UI_REDESIGN_COLOR_HIERARCHY.md` 作为配色与层级的实现参考。
- Product priority: 先定稿视觉设计与交互逻辑，再进入实现稳定化；当前服务只运行在可信内网，统一认证、TLS 与公网防护不是首期设计或实现的阻塞项。
- Evidence reviewed:
  - 当前 Agent Hub：`README.md`、`src/agent_hub/web/templates/`、`src/agent_hub/web/routes.py`、`figs/computer-view.png`、`figs/phone-view.jpg`、`UI_REDESIGN_COLOR_HIERARCHY.md`
  - Claude Console：同级仓库的 `DESIGN.md`、`README.zh-CN.md`、`claude_console.py` 与 `figs/` 截图
  - Codex Console：同级仓库的 `DESIGN.md`、`README.zh-CN.md`、`codex_console.py` 与 `docs/codex-console-overview.png`
  - Web Terminal：同级仓库的 `README.md`、`webterminald.py` 与 `figs/view.jpg`

Observed facts:

- Claude Console 与 Codex Console 都以“项目侧栏 + Session 工作集标签 + 主内容区 + 紧凑状态”为核心外壳，并把对话、工具、Plan 与变更重新渲染为浏览器原生组件。
- Agent Hub 当前采用左侧卡片列表、右侧 Web Terminal iframe 的 `1/3 : 2/3` 布局，已经保留 Claude Code/Codex CLI 原生 TUI；这是区别于两个 Console 的核心能力。
- Web Terminal 当前通过 `?attach=<tmux>` 建立独立 WebSocket/PTY/tmux client，支持移动端虚拟键盘，但没有 `postMessage`、嵌入模式或主题同步协议。

Design inference:

- Agent Hub 不应复制两个 Console 的聊天渲染层。应借用它们成熟的工作台外壳和状态语义，把原生 TUI 提升为主画布，把 Hub 定位为跨 CLI、跨项目、跨主机的会话控制平面。

## Brand

- Personality: 安静、技术化、运维感、低噪音；像可靠的工作台，不像营销 Dashboard。
- Trust signals: 当前选中的 session 清晰可见；状态与连接性明确；原生 TUI 始终可达；关闭视图与终止进程严格区分；危险操作有明确边界。
- Signature: **Native TUI, modern workspace**。现代化发生在终端周围，不覆盖、仿制或弱化 Claude Code/Codex CLI 自己的界面。
- Avoid: 紫色大底、金属渐变按钮、持续发光的大面积卡片、全局等宽字体、装饰性图表、把终端降级成详情弹窗，以及重新实现一套聊天 Console。

## Product goals

- Goals:
  - 在修改主界面实现前，先形成可评审、可截图验证的桌面/平板/手机视觉基线和完整交互状态表。
  - 在一个浏览器工作台中发现、筛选、切换和控制 Claude Code、Codex CLI 与普通 tmux 会话。
  - 让用户在桌面和移动端都能以最少步骤进入原生 TUI，并完成审批、查看状态和恢复工作。
  - 吸收两个 Console 的项目分组、工作集标签、状态语义和响应式导航，但保留 tmux/Web Terminal 作为交互真相来源。
  - 把 Needs Action 与异常连接置于普通 activity 之上，降低同时运行多个 agent 时的注意力成本。
- Non-goals:
  - 不解析并重新渲染完整对话、Markdown、Plan、diff 或工具卡片。
  - 不替代 Claude Console、Codex Console 或 CLI 自身的会话管理能力。
  - 不在本轮现代化中引入 SPA、前端构建链或新的 UI 框架。
  - 不把 Agent Hub 扩展成公网多租户平台或通用任务编排器。
  - 不把统一认证、TLS、跨公网访问和企业级安全治理作为首期 UI 定稿条件；部署边界仍限定为可信内网。
- Success signals:
  - 桌面端从任意 waiting session 到可操作 TUI 不超过一次选择或一次主操作。
  - Session 切换、关闭工作集标签和浏览器断线都不会结束底层 tmux/CLI 进程。
  - 常见桌面尺寸下一屏可扫描至少 10 个 session 行，waiting 项不会落到首屏之外。
  - 390px 宽手机上，用户可以在 Session 列表与全屏 Terminal 之间明确切换，Terminal 不与缩小的桌面侧栏争夺空间。
  - 状态不只依赖颜色；键盘、触控和屏幕阅读器均能识别当前 session、连接状态与危险操作。

## Personas and jobs

- Primary personas: 同时运行多个 Claude/Codex 任务的开发者、研究者和单人运维者。
- User jobs:
  - 快速回答“哪个 agent 需要我、哪个仍在工作、哪个已经停了”。
  - 按项目找到 session，并把常用 session 放进当前工作集。
  - 不离开浏览器就进入原生 TUI，继续使用 CLI 的全部键盘能力和视觉反馈。
  - 在手机上完成查看、审批、短输入和紧急接管。
  - 在需要排障时查看事件、模型、cwd、host、tmux 与连接信息。
- Key contexts of use: 桌面长期工作台；平板横屏监控；手机纵屏审批与短时终端接管；Tailscale/Nebula 等可信私网访问。

## Information architecture

- Primary navigation:
  - `Needs Action`：全局置顶，只收 waiting、连接失败和需要恢复的 session。
  - `Projects`：按项目组织 live、idle/pinned 与历史入口，host/hub 作为次级 badge，而不是第一层导航。
  - `Open sessions`：主工作区上方的浏览器工作集标签；标签关闭只关闭视图，不终止 session。
  - `Utilities`：Stopped、raw tmux、设置与诊断进入低权重区域。
- Core routes/screens:
  - `/`：统一工作台，承担导航、注意力队列、工作集和 Terminal。
  - `/sessions/{id}`：深链接与 Inspector 的完整降级页，不再是日常切换的必经路径。
  - `/tmux`：底层 tmux 管理页，保持辅助工具定位。
  - `/idle`、`/stopped`：短期保留兼容；长期可收敛为导航筛选或归档视图。
- Content hierarchy: `Needs Action > selected native TUI > running/live evidence > idle/pinned > stopped/raw tmux`。

Desktop target:

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Agent Hub   host/connection     search              utilities       │
├──────────────────────┬───────────────────────────────────────────────┤
│ Needs Action         │ [session A] [session B] [session C]           │
│ ───────────────────  ├───────────────────────────────────────────────┤
│ Projects             │ selected session / status / cwd       actions│
│  project A           ├───────────────────────────────────────────────┤
│   ● session A        │                                               │
│   ◐ session B        │            Web Terminal iframe                │
│  project B           │          native Claude/Codex TUI              │
│   ○ session C        │                                               │
│                      │                                               │
│ Archive / raw tmux   │                                               │
└──────────────────────┴───────────────────────────────────────────────┘
```

- Content density: 左侧使用 40–52px 的 session row，而不是重复的大卡片；详细事件进入 hover/展开摘要或 Inspector。
- Selection model: session row 负责选择；行内主按钮只在 Needs Action 时出现。普通状态下，整行进入 Terminal，危险操作进入菜单。

## Design principles

- Native TUI is the canvas: Terminal 是主工作区和最终真相来源，不是附件、预览或详情按钮。
- Fleet navigation, not chat reconstruction: Hub 负责跨 session 的发现、状态、切换和控制；对话重排交给两个 Console。
- Separate view from process: “Close tab”“Detach view”“End session”“Kill tmux”必须使用不同文案、位置和危险等级。
- Attention over activity: waiting、恢复失败和断线可抢占视觉注意力；普通 active/busy 只提供稳定状态，不持续闪烁。
- Progressive disclosure: session 行先回答身份、状态和当前动作；模型、cwd、事件、token 与底层信息进入 Inspector。
- Keyboard ownership is explicit: Terminal 聚焦后，按键默认属于原生 TUI；Hub 全局快捷键不得截获常见 CLI/tmux 组合。
- Design the experience before hardening it: 先通过视觉基线、交互原型和状态表确定产品形态，再处理重连、嵌入协议与部署加固；稳定性工作不得反过来主导或稀释 UI 方向。
- Preserve native behavior during implementation: 视觉升级不能以丢失 tmux 状态、误杀 session、破坏移动端键盘或降低终端可读性为代价。
- Tradeoffs: 第一阶段接受切换标签时重新连接 Web Terminal；在 Web Terminal 提供安全的 suspend/resume 协议前，不用多个隐藏 iframe 冒险改变 tmux 尺寸或持有过多 client。

## Visual language

- Color:
  - 以 `UI_REDESIGN_COLOR_HIERARCHY.md` 的 Graphite Ops Console 色板为基础。
  - 中性 canvas/panel 构成层级，`#5CC8FF` 只用于品牌、选择与 focus。
  - green/blue/amber/gray/red 只表达 active/running/waiting/idle/danger；waiting 允许轻量动画，但必须支持 reduced motion。
  - 主题建立在同一套语义 token 上，交付五套暗色与五套亮色：Graphite、Midnight、Nord、Tokyo Night、Mocha；Paper、Solarized、One Light、Rosé Dawn、Latte。
  - Graphite 是默认主题和批准视觉参考；用户选择持久化到浏览器，并同步给嵌入式 Web Terminal。
- Typography: UI chrome 使用系统 sans-serif；路径、命令、session id 与技术 metadata 使用 monospace；Terminal 字体由 Web Terminal 自己管理。
- Spacing/layout rhythm: 4px 基础单位，常用间距 8/12/16px；列表紧凑，主画布尽量无内层装饰边距。
- Shape/radius/elevation: 6–10px 圆角，边框和背景亮度优先于阴影；Terminal 容器只做必要边界，不做悬浮卡片。
- Motion: 仅用于新状态、抽屉与连接反馈；无无限循环 glow；遵循 `prefers-reduced-motion`。
- Imagery/iconography: 复用工具 logo 或简洁线性图标；状态必须同时有文字/形状，不用 logo 大小承担层级。

## Components

- Existing components to reuse:
  - FastAPI/Jinja2 页面与 WebSocket 增量事件。
  - Web Terminal 的 `?attach=<tmux>` 嵌入和移动端虚拟键盘。
  - 现有 session/tmux API、审批、restore、fork、pin、统计与事件摘要。
  - 当前 Dashboard 的左右分屏和 iframe 选择逻辑作为迁移基础。
- New/changed components:
  - `AppShell`：统一 top bar、连接状态、全局搜索/过滤和 utility 入口。
  - `AttentionQueue`：waiting、orphan、terminal disconnected 的紧凑置顶列表。
  - `ProjectSessionNavigator`：项目分组、host badge、收藏与 dense session rows。
  - `WorkspaceTabs`：当前浏览器的 session 工作集；切换与关闭不改变底层进程。
  - `TerminalStage`：明确的 connecting/connected/disconnected/ended/attach-failed 状态，以及 standalone fallback。
  - `SessionInspector`：metadata、最近事件和次级操作的右侧抽屉或窄屏 sheet。
  - `MobileModeSwitch`：Sessions 与 Terminal 两个全屏模式，而不是缩小桌面双栏。
  - `ThemePicker`：按 Dark/Light 分类展示十套主题的名称与色板缩略图，切换后即时更新 Hub chrome 与 Web Terminal。
- Variants and states:
  - Session: needs-action、running、ready/idle、unread、disconnected、ended、selected。
  - Terminal: empty、connecting、connected、reconnecting、attach-failed、session-ended。
  - Tab: selected、background-running、background-needs-action、stale。
- Token/component ownership:
  - 在 Agent Hub 内使用语义 CSS custom properties 与语义组件类，逐步退出直接颜色类和金属渐变。
  - 第一阶段允许与现有 Tailwind CDN 并存，但不新增前端依赖；稳定后再决定是否移除 CDN。
  - Web Terminal 继续拥有 xterm、移动键盘、字体与终端内部主题；Hub 不通过 CSS 假装控制跨源 iframe 内容。

## Accessibility

- Target standard: 实用的 WCAG 2.1 AA，优先覆盖导航、审批、状态和危险操作。
- Keyboard/focus behavior:
  - Workspace tabs 使用 `tablist/tab` 语义、方向键导航、`aria-selected` 与可见 focus。
  - Session row、Approve、Inspector 与菜单可通过键盘到达。
  - iframe 有明确 `title`；从导航进入 Terminal 与返回导航都有显式动作。
  - Terminal 聚焦时不截获 CLI 常用组合；Hub 快捷键只在父页面聚焦时生效。
- Contrast/readability: 所有状态以文字/图形和颜色共同表达；正文与交互控件满足 AA 对比度；不以低对比灰字承载当前动作。
- Screen-reader semantics: live 状态更新使用节制的 `aria-live`；waiting 通知优先，普通事件流不连续播报。
- Reduced motion and sensory considerations: waiting pulse、进入动画和抽屉动画均可关闭；不得依靠闪烁表达唯一信息。

## Responsive behavior

- Supported breakpoints/devices:
  - Desktop `>= 1024px`：固定/可调宽导航 + Terminal 主画布。
  - Tablet `700–1023px`：抽屉式导航 + Terminal，横屏保留可选窄导航。
  - Phone `< 700px`：Sessions 与 Terminal 分屏切换；选中 session 后 Terminal 全屏。
- Layout adaptations:
  - Terminal 高度使用动态 viewport 与 safe-area；不通过全局增大 `html` 字号放大所有控件。
  - 手机上详情进入 bottom sheet，危险操作不与主 Terminal 键盘栏争夺底部空间。
  - 返回 Session 列表时不结束 tmux；重新进入 Terminal 时明确显示 reconnecting。
- Touch/hover differences: 主触控目标至少约 44px；hover 信息在触控端必须有 Inspector 或菜单替代；Web Terminal 的虚拟键盘保持独立控制权。

## Interaction states

- Loading: 首屏先渲染 shell 与 session rows；Terminal 以明确的 connecting 状态等待 iframe，不用空白黑框。
- Empty: 没有 live session 时解释如何创建/导入 tmux；没有选中 session 时显示轻量快捷入口，不展示装饰性空面板。
- Error: attach 失败显示原因、Retry、Open standalone 和返回 Sessions；不得静默关闭工作集标签。
- Success: 审批发送后立即进入“sent/waiting for CLI”过渡态，以 pane/WebSocket 真值确认后再清除。
- Disabled: ended/stopped session 不可输入；恢复、删除和 kill 使用不同按钮与文案。
- Offline/slow network: 保留导航、工作集与最后状态；WebSocket 和 Terminal 连接分别显示，不把 Hub 数据连接成功误报为 Terminal 已连接。

## Content voice

- Tone: 简短、操作化、可验证；优先回答“现在是谁、正在做什么、是否需要我”。
- Terminology:
  - `Session`：一个 Claude/Codex 工作会话。
  - `Terminal`：该 session 的原生 TUI 视图。
  - `Open/Close tab`：只改变浏览器工作集。
  - `End session`：结束 CLI 会话。
  - `Kill tmux`：不可恢复地终止底层 tmux；仅在底层管理上下文使用。
  - `Needs Action`：需要用户审批、恢复或处理连接异常。
- Microcopy rules: 不把 `Preview` 用作主 Terminal 入口；不使用含糊的 `Delete` 同时表示删记录与杀进程；状态文案描述当前事实，不描述系统猜测。

## Implementation constraints

- Framework/styling system: 保持 FastAPI + Jinja2 + vanilla JavaScript；不引入 SPA 或前端构建步骤。采用语义 CSS tokens 和小型可复用模板 partial。
- Design-token constraints: Agent Hub chrome 与 Web Terminal 各自拥有主题；跨端口 iframe 不能靠父页面 CSS 同步。主题同步必须通过明确的 Web Terminal URL/`postMessage` 协议实现。
- Performance constraints:
  - Session 更新继续使用 WebSocket 局部更新，不因单条事件重载列表或 Terminal。
  - 第一阶段只维持一个 active Web Terminal iframe；工作集标签保存浏览器导航状态。
  - 多 iframe keep-alive 必须等待 Web Terminal 支持 suspend/visibility/resize 协议，并验证不会因隐藏尺寸或多个 tmux client 改变 TUI 布局。
- Compatibility constraints:
  - 保持 `?attach=<tmux>`、移动虚拟键盘、tmux persistence、approve/always、restore、fork 和现有深链接可用。
  - 关闭标签、切换过滤器、收起导航和浏览器断线均不得 kill/detach 底层 tmux。
  - Web Terminal 当前无嵌入通信协议；Hub 第一阶段只能把 iframe load/error 与 Hub WebSocket 状态作为外部信号，不能宣称知道 xterm 内部 ready 状态。
  - 当前发布假设是个人使用的可信内网，首期沿用现有访问边界，不要求先完成统一认证或 TLS 改造。
  - 如果未来改变为公网、共享网络或多用户访问，统一认证、受控 origin、TLS 与访问审计立即升级为发布阻塞项。
- Test/screenshot expectations:
  - 保留现有 Python 回归测试，并补充 session selection、tab close vs process end、waiting priority、iframe URL、WebSocket reconnect 的前端行为测试。
  - 每个 UI 迭代至少验证 1440px desktop、1024px tablet、390×844 phone，以及键盘-only/reduced-motion。
  - Terminal 验收必须包含 Claude Code TUI、Codex CLI TUI、tmux 多窗口、移动软键盘和虚拟键盘，不只验证空 shell。

Design-freeze gate:

- 至少形成一套 1440px desktop、1024px tablet 和 390×844 phone 的高保真基线；三者必须来自同一套 token 与组件系统。
- 明确 session 选择、打开/关闭标签、waiting 审批、Terminal 聚焦、返回列表、重连、结束 session 和 kill tmux 的逐步交互。
- 为 Session、Tab、Terminal、Needs Action、Inspector 和移动 mode switch 列出默认、hover/focus、selected、loading、empty、error、disabled 状态。
- 确认信息密度、导航宽度、Terminal 占比、字体层级、状态色、按钮优先级和手机触控尺寸，不把这些留给实现阶段临场决定。
- 使用真实 Claude Code/Codex CLI TUI 截图验证 Terminal 主画布，而不是用空黑框或 lorem ipsum 代替。
- 所有会改变页面骨架的 open question 已解决；只影响后期能力的 Web Terminal 协议与安全增强可以延后。

Recommended delivery sequence:

1. **Phase 0 — Visual and interaction freeze**：先制作桌面/平板/手机高保真参考与关键流程原型，评审信息架构、视觉层级、状态和文案；不修改生产 UI。
2. **Phase 1 — Hub UI implementation**：按已批准基线实现语义 tokens、dense navigator、Needs Action、WorkspaceTabs、单 iframe TerminalStage、Inspector 和手机双模式；继续复用现有 API 与 Web Terminal。
3. **Phase 2 — Interaction stability**：补齐刷新恢复、WebSocket/iframe 重连、焦点与快捷键边界、状态一致性和视觉回归；只在确有体验收益时扩展 Web Terminal embed/visibility 协议。
4. **Phase 3 — Deployment hardening when needed**：仅在访问边界超出可信内网时实施统一认证、TLS、受控 origin、审计与速率限制。

Phase 1 implementation acceptance gate:

- 原生 Claude/Codex TUI 在桌面和手机均完整可操作。
- Session 标签只管理视图，任何关闭/切换动作都不结束进程。
- waiting session 始终优先可见且能一次进入审批上下文。
- 没有新增运行时依赖或前端构建链。
- 当前 API、MCP、Telegram、restore/fork 与 WebSocket 行为没有回归。

## Open questions

- [x] 首期导航默认采用 `project → session`，host/hub 作为 badge；未来可以增加按 host/time/status 切换分组，不阻塞页面骨架。
- [ ] 是否需要修改 `web-terminal` 建立 embed/主题/连接状态协议；owner: product + web-terminal；impact: TerminalStage 精确状态与 tab keep-alive；不阻塞 Phase 0/1。
- [x] 统一 Basic Auth/反向代理入口延后；当前可信内网假设下不阻塞视觉与交互改版，访问边界变化时重新评审。
- [x] 首版交付五暗五亮十套主题；Graphite 为默认与视觉验收基线，其他主题共享相同组件语义并纳入对比度抽检。
- [ ] `/agent` Monitor 页面在现代化后保留、降级为诊断页或移除；owner: product；impact: 顶部导航与产品边界。
