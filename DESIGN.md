# Agent Hub - Technical Design

> Claude Code 会话统一管理平台

> **状态机相关问题,DESIGN.md 的 13-17 号踩坑和 3.3 的新图是最直接的参考。**

## 1. 项目目标

在多台设备（通过 Tailscale 组网）上运行的多个 Claude Code CLI 会话，需要一个中心化的管理平台来：

- **发现** — 知道当前有哪些会话在运行、分布在哪些机器上
- **观察** — 实时了解每个会话正在做什么任务、用了什么工具、进展如何
- **回溯** — 查看历史会话的活动时间线
- **统筹** — 通过 MCP Server 接口，让任一 Claude Code 会话能查询和管理其他会话

## 2. 系统架构

### 2.1 双 Hub 拓扑

两台主力计算机各运行一个独立的 Hub Server，其他设备选择性接入其中一个 Hub。两个 Hub 之间可定期或手动同步数据。

```
Tailscale Network (100.x.x.x)
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│       Main Machine A                    Main  Machine B              |
│  ┌──────────────────────┐           ┌──────────────────────┐         │
│  │   Agent Hub A:7800   │◀─ sync ──▶│   Agent Hub B:7800   │         │
│  │   ┌────┐ ┌───┐ ┌───┐ │           │┌────┐ ┌───┐ ┌───┐    │         │
│  │   │API │ │WS │ │MCP│ │           ││API │ │WS │ │MCP│    │         │
│  │   └────┘ └───┘ └───┘ │           │└────┘ └───┘ └───┘    │         │
│  │         │            │           │        │             │         │
│  │    ┌────┴────┐       │           │   ┌────┴────┐        │         │
│  │    │ hub_a.db│       │           │   │ hub_b.db│        │         │
│  │    └─────────┘       │           │   └─────────┘        │         │
│  └──────────────────────┘           └──────────────────────┘         │
│       ▲           ▲                       ▲          ▲               │
│   HTTP Hook   HTTP Hook              HTTP Hook   HTTP Hook           │
│  ┌───────┐  ┌───────┐              ┌───────┐   ┌───────┐            │
│  │CC #1  │  │CC #2  │              │CC #3  │   │CC #4  │            │
│  │proj-a │  │proj-b │              │proj-c │   │proj-d │            │
│  └───────┘  └───────┘              └───────┘   └───────┘            │
│                                                                      │
│  Machine C (辅助机)                  Machine D (辅助机)               │
│  ┌───────┐                          ┌───────┐                        │
│  │CC #5  │── HTTP Hook ──▶ Hub A    │CC #6  │── HTTP Hook ──▶ Hub B │
│  │proj-e │                          │proj-f │                        │
│  └───────┘                          └───────┘                        │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 核心原则

- **Push 模式** — Claude Code 通过原生 HTTP Hook 主动上报事件，Hub 不需要轮询
- **零侵入** — 只需在 `~/.claude/settings.json` 中配置 hooks，不修改 Claude Code 本身
- **单进程** — Hub Server、Web UI、MCP Server 运行在同一个 Python 进程中
- **Tailscale 内网** — 所有通信走 Tailscale 内网，无需公网暴露或额外鉴权
- **独立运行，按需同步** — 每个 Hub 独立工作，网络故障不影响各自的事件收集；同步是附加能力而非依赖

## 3. 数据模型

### 3.1 核心实体

```sql
-- 会话：一次 Claude Code CLI 的启动到退出
CREATE TABLE sessions (
    session_id    TEXT PRIMARY KEY,     -- Claude Code 分配的 session_id
    hub_id        TEXT NOT NULL,        -- 首次接收该会话的 Hub 标识
    hostname      TEXT NOT NULL,        -- 机器名
    cwd           TEXT NOT NULL,        -- 工作目录
    model         TEXT,                 -- 使用的模型
    status        TEXT NOT NULL DEFAULT 'active',  -- active / idle / stopped
    started_at    DATETIME NOT NULL,
    last_seen_at  DATETIME NOT NULL,    -- 最后一次收到事件的时间
    stopped_at    DATETIME,
    metadata      JSON                  -- 额外信息 (permission_mode 等)
);

-- 事件：会话中发生的每一个 hook 事件
CREATE TABLE events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid     TEXT NOT NULL UNIQUE,  -- 全局唯一 ID (hub_id + 本地 id), 用于去重同步
    hub_id        TEXT NOT NULL,         -- 产生该事件的 Hub
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    event_type    TEXT NOT NULL,         -- SessionStart / PreToolUse / PostToolUse / ...
    tool_name     TEXT,                  -- Bash / Write / Read / Edit / ...
    summary       TEXT,                  -- 人类可读的一行摘要
    payload       JSON,                  -- 完整的 hook payload（脱敏后）
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 同步水位线：记录从每个 peer hub 拉取到哪个位置
CREATE TABLE sync_watermarks (
    peer_hub_id   TEXT PRIMARY KEY,      -- 对端 Hub 标识
    last_event_uid TEXT NOT NULL,        -- 上次同步到的最后一条 event_uid
    synced_at     DATETIME NOT NULL
);

-- 索引
CREATE INDEX idx_events_session ON events(session_id, created_at DESC);
CREATE INDEX idx_events_type ON events(event_type, created_at DESC);
CREATE INDEX idx_events_hub ON events(hub_id, id);  -- 同步查询用
CREATE INDEX idx_sessions_status ON sessions(status);
```

> **实际 schema 的权威定义见 `src/agent_hub/db.py:init_db`**。运行时已通过自动迁移补充了以下列 (未完整列在上方骨架中): `tmux_session`、`transferred`、`pending_tool`、`pending_detail`、`input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_create_tokens`、`transcript_path`。

### 3.2 事件摘要生成规则

收到原始 hook payload 后，Hub 提取出一行人类可读的摘要存入 `events.summary`：

| event_type             | summary 示例                                   |
| ---------------------- | -------------------------------------------- |
| `SessionStart`         | `Session started (model: claude-sonnet-4-6)` |
| `PreToolUse` + `Bash`  | `$ npm test`                                 |
| `PreToolUse` + `Write` | `Write /src/app.js`                          |
| `PreToolUse` + `Read`  | `Read /src/app.js`                           |
| `PreToolUse` + `Edit`  | `Edit /src/app.js`                           |
| `PreToolUse` + `Grep`  | `Grep "pattern" in **/*.ts`                  |
| `PostToolUse` + `Bash` | `$ npm test (exit 0)`                        |
| `PostToolUseFailure`   | `FAIL: Bash "npm test" — exit 1`             |
| `UserPromptSubmit`     | `User: "fix the login bug"` (截断到 80 字符)      |
| `Stop`                 | `Session idle`                               |

### 3.3 会话状态机

```
SessionStart ──▶ active
                   │
            ToolUse / UserPrompt (刷新 last_seen_at)
                   │
         Stop event, 或 10min 无事件且 pane 不在 "esc to interrupt" ──▶ idle
                   │
                   │  (idle 是终态性持久状态 — 只要 tmux 还活着, 无限期保留)
                   ▼
         tmux session 真死 (背景 _sweep_dead_tmux 每 60s 扫一次) ──▶ stopped
                   │
         新的 SessionStart (相同 session_id, source=resume) ──▶ active
```

**设计原则**:
- 时间不是 stopped 的触发条件。早上睡醒之前的 idle session 全部保留,可以直接继续干活
- stopped 的唯一入口是 tmux 死亡,通过 `tmux ls` 和 DB 做集合差实现
- `Running` (长工具在执行) 和 `Waiting` (等审批) 是覆盖在 active 之上的派生状态,且互斥:审批阻塞期工具没在跑,不会同时出现两种徽章
- tmux 名字复用时 `ensure_session` 自动把同名的旧 session 退休为 stopped,避免累积重复

## 4. 双 Hub 同步机制

### 4.1 设计原则

- **各自独立** — 每个 Hub 独立接收和存储事件，不依赖对端在线
- **Append-only** — 事件只增不改，无写入冲突，同步本质上是日志复制
- **幂等同步** — 通过 `event_uid` 去重，重复同步不会产生重复数据
- **按需触发** — 支持手动触发和定时触发，不做实时双向复制

### 4.2 Hub 身份

每个 Hub 启动时需要配置一个唯一的 `hub_id`（如 `hub-a`、`hub-b`），写入配置文件：

```yaml
# config.yaml 或环境变量
hub_id: "hub-a"
peers:
  - id: "hub-b"
    url: "http://100.x.x.x:7800"
```

所有本地产生的事件 `event_uid` 格式为 `{hub_id}:{local_id}`，例如 `hub-a:1042`。

### 4.3 同步 API

```
# 供对端 Hub 拉取数据
GET /api/sync/events?after_uid={event_uid}&limit=500
  → 返回该 Hub 上 event_uid 之后的事件列表（含关联的 session 信息）

# 供对端 Hub 推送数据（或本地拉取后写入）
POST /api/sync/push
  Body: { "events": [...], "sessions": [...] }
  → 合并写入，按 event_uid 去重，按 session_id 合并（取较新的 last_seen_at）

# 查看同步状态
GET /api/sync/status
  → 返回各 peer 的水位线和最后同步时间
```

### 4.4 同步流程

```
Hub A                                    Hub B
  │                                        │
  │  GET /api/sync/events?after_uid=...    │
  │ ─────────────────────────────────────▶ │
  │                                        │
  │  { events: [...], sessions: [...] }    │
  │ ◀───────────────────────────────────── │
  │                                        │
  │  本地合并写入 (去重)                     │
  │  更新 sync_watermarks                   │
  │                                        │
```

双向同步：Hub A 拉 Hub B 的新数据，Hub B 也拉 Hub A 的新数据。两个方向独立执行。

### 4.5 触发方式

```bash
# 手动触发同步
agent-hub sync

# 定时同步（每 30 分钟，通过 cron 或 systemd timer）
*/30 * * * * cd ~/Git/agent-hub && uv run agent-hub sync
```

### 4.6 冲突处理

由于数据模型是 append-only，唯一可能的"冲突"是 session 状态：

- 同一个 `session_id` 不会出现在两个 Hub 上（一个 Claude Code 实例只连一个 Hub）
- 同步时 session 记录按 `last_seen_at` 取较新值合并
- 如果同一会话的 `status` 不一致，以 `last_seen_at` 更晚的为准

## 5. Hub Server API

### 5.1 事件接收（供 Claude Code Hook 调用）

```
POST /api/events
Content-Type: application/json

Body: Claude Code hook 原始 payload
```

这是唯一需要对外暴露的写入接口。Hub 根据 `hook_event_name` 字段决定如何处理：

- `SessionStart` → 创建或更新 session 记录，标记为 active
- `PreToolUse` / `PostToolUse` / `PostToolUseFailure` → 插入 event，更新 session.last_seen_at
- `UserPromptSubmit` → 插入 event（prompt 截断存储，不存完整内容）
- `Stop` → 插入 event，标记 session 为 idle

响应：

```json
{ "ok": true }
```

响应必须尽快返回（< 100ms），不能阻塞 Claude Code 的工作流。

### 5.2 查询与管理 API

```
GET  /api/sessions                     -- 列出所有会话 (支持 ?status=active|idle|stopped 过滤)
GET  /api/sessions/:id                 -- 会话详情 (SessionResponse 含 pending_tool / pending_detail)
GET  /api/sessions/:id/events          -- 会话事件时间线 (分页)
GET  /api/sessions/:id/events/latest   -- 最近 N 条事件
POST /api/sessions/:id/approve?always= -- 远程审批 pending tool (tmux send-keys)
DELETE /api/sessions/:id               -- 删除会话记录 (拒绝 active; idle 会连带 kill tmux; stopped 直接删)
POST /api/sessions/clear-stopped       -- 一键清空所有 stopped 会话
GET  /api/stats                        -- 全局统计 (active/idle/stopped/waiting 数量、今日事件数)
POST /api/notify                       -- 从 Claude session 往 Telegram 推自定义消息

-- Tmux Hub APIs:
GET  /api/tmux/list                    -- 未被 active/idle Claude session 占用的裸 tmux
POST /api/tmux/new                     -- 创建 detached tmux, body {name?, cwd, command?}
                                          command 支持 "claude"/"codex", 落地启动对应进程
POST /api/tmux/kill                    -- 按名字 kill tmux session
GET  /api/browse?path=                 -- 目录选择器的子目录列表
```

### 5.3 WebSocket 实时推送

```
WS /ws

-- 服务端推送格式：
{
  "type": "event",
  "session_id": "abc123",
  "event_type": "PreToolUse",
  "summary": "$ npm test",
  "timestamp": "2026-04-10T15:30:00Z"
}

{
  "type": "session_update",
  "session_id": "abc123",
  "status": "active",
  "hostname": "desktop",
  "cwd": "/home/user/project"
}
```

## 6. MCP Server

Hub 同时作为 MCP Server 运行，使用 SSE transport（便于远程连接）。

### 6.1 Tools

```yaml
list_sessions:
  description: "列出所有 Claude Code 会话"
  params:
    status: string? # active / idle / stopped / all (默认 active)
  returns: 会话列表，含 session_id, hostname, cwd, status, last_seen_at

get_session:
  description: "获取会话详情和最近活动"
  params:
    session_id: string
    event_limit: int? # 最近 N 条事件，默认 20
  returns: 会话信息 + 事件时间线

search_events:
  description: "搜索事件（按工具名、关键词）"
  params:
    query: string?     # 模糊搜索 summary
    tool_name: string? # 精确匹配工具名
    session_id: string? # 限定会话
    limit: int?        # 默认 50
  returns: 匹配的事件列表

get_dashboard:
  description: "获取全局仪表盘概览"
  returns: |
    - 各状态会话数量
    - 按机器分组的活跃会话
    - 最近 10 条事件
    - 今日统计

sync_now:
  description: "立即触发与所有 peer Hub 的数据同步"
  returns: 同步结果（每个 peer 拉取/推送了多少条记录）

sync_status:
  description: "查看各 peer Hub 的同步状态"
  returns: 各 peer 的水位线、最后同步时间、是否可达
```

### 6.2 MCP 配置（客户端侧）

在需要接入管理能力的 Claude Code 实例中，添加 MCP Server 配置：

```json
// ~/.claude/settings.json
{
  "mcpServers": {
    "agent-hub": {
      "type": "sse",
      "url": "http://100.x.x.x:7801/sse"
    }
  }
}
```

## 7. Claude Code Hook 配置

### 7.1 全局配置

在每台设备的 `~/.claude/settings.json` 中添加。

**重要约束：**
- Claude Code HTTP hook 仅允许 loopback (`127.0.0.1`)，Tailscale 私有 IP 会被拒绝
- SessionStart 不支持 HTTP hook（会导致 headless 模式死锁），必须使用 command hook
- 本机部署时所有 hook 统一使用 `http://127.0.0.1:7800`

```jsonc
{
  "hooks": {
    // SessionStart 必须用 command hook — HTTP hook 被 Claude Code 阻止
    "SessionStart": [
      {
        "type": "command",
        "command": "bash -c 'cat | curl -s -X POST \"http://127.0.0.1:7800/api/events?host=$(hostname)\" -H \"Content-Type: application/json\" -d @-'"
      }
    ],
    // 其余事件类型使用 HTTP hook
    "PreToolUse": [
      { "type": "http", "url": "http://127.0.0.1:7800/api/events?host=HOSTNAME" }
    ],
    "PostToolUse": [
      { "type": "http", "url": "http://127.0.0.1:7800/api/events?host=HOSTNAME" }
    ],
    "PostToolUseFailure": [
      { "type": "http", "url": "http://127.0.0.1:7800/api/events?host=HOSTNAME" }
    ],
    "UserPromptSubmit": [
      { "type": "http", "url": "http://127.0.0.1:7800/api/events?host=HOSTNAME" }
    ],
    "Stop": [
      { "type": "http", "url": "http://127.0.0.1:7800/api/events?host=HOSTNAME" }
    ]
  }
}
```

替换 `HOSTNAME` 为机器名。SessionStart 的 command hook 会自动获取 hostname。

### 7.2 设计要点

- **统一 endpoint** — 所有事件发送到同一个 `/api/events`，由 Hub 根据 `hook_event_name` 分发处理
- **loopback only** — HTTP hook 必须使用 `127.0.0.1`，Claude Code 阻止私有/非 loopback 地址
- **SessionStart command hook** — 通过 `cat | curl` 管道将 stdin payload 转发到 Hub API
- **无需 auth** — 本机 loopback 无需鉴权；跨机器访问通过 Tailscale 内网保护
- **hostname via query param** — `?host=HOSTNAME` 传递机器名，因为 hook payload 不含 hostname

### 7.3 hostname 识别

Hook payload 中不包含 hostname。Hub 通过以下方式获取：

1. 从 HTTP 请求的 `X-Forwarded-For` 或 peer IP 解析 Tailscale hostname
2. 或者在 hook URL 中加入 query param：`/api/events?host=desktop`
3. 推荐方案 2，因为更可靠：

```json
"url": "http://<hub-ip>:7800/api/events?host=my-desktop"
```

## 8. Web Dashboard

### 8.1 页面结构

```
/                          -- 主 Dashboard: 只展示活跃和 waiting 的 session
  ├── 顶部 stats 卡片: Active | Waiting | Idle (→/idle) | Stopped (→/stopped)
  ├── + New 按钮: 弹出目录选择器 → POST /api/tmux/new command=claude → 新标签页打开 Web Terminal
  ├── 活跃会话卡片列表 (Sessions / From Tmux 双 tab)
  │     每张卡片显示:
  │     - hostname + cwd (项目名)
  │     - 当前状态 (active, 可能带 waiting/running 徽章)
  │     - 最后活动时间
  │     - 最近一条事件摘要
  │     - 使用的模型
  │     - [Terminal] [Approve? (waiting 时)]
  ├── 实时事件流 (WebSocket)
  └── 统计摘要

/idle                       -- Idle session 列表 (点 Idle 卡片进入)
  └── 每张卡片: [Terminal] [Delete] — Delete 会 kill tmux 并清除 DB 行

/stopped                    -- Stopped session 列表 (点 Stopped 卡片进入)
  ├── 右上角 [Clear All] — 一键清空所有 stopped
  └── 每张卡片: [Delete]

/tmux                       -- Tmux Hub: 裸 tmux 的列表/创建/kill
/sessions/:id               -- 会话详情页
  ├── 会话元信息
  ├── 事件时间线 (无限滚动)
  └── 工具使用统计
```

### 8.2 技术实现

- **服务端渲染 + htmx** — 无需独立前端项目，HTML 模板内嵌在 Python 包中
- **WebSocket** — 实时事件推送，前端自动刷新卡片状态
- **Tailwind CSS (CDN)** — 简洁的样式，不需要构建步骤
- **暗色主题** — 默认暗色，适配终端用户习惯

## 9. 项目结构

```
agent-hub/
├── pyproject.toml              # 项目配置 (uv)
├── DESIGN.md                   # 本文档
│
├── src/
│   └── agent_hub/
│       ├── __init__.py
│       ├── main.py             # 入口：启动 FastAPI + MCP
│       ├── config.py           # 配置（hub_id、端口、peers、数据库路径）
│       ├── db.py               # SQLite 数据库操作
│       ├── models.py           # Pydantic 数据模型
│       │
│       ├── api/
│       │   ├── __init__.py
│       │   ├── events.py       # POST /api/events (hook 接收)
│       │   ├── sessions.py     # GET /api/sessions (查询)
│       │   ├── sync.py         # GET/POST /api/sync/* (Hub 间同步)
│       │   └── ws.py           # WebSocket /ws
│       │
│       ├── mcp/
│       │   ├── __init__.py
│       │   └── server.py       # MCP Server (FastMCP)
│       │
│       ├── services/
│       │   ├── __init__.py
│       │   ├── event_processor.py  # 事件处理 + 摘要生成
│       │   ├── session_manager.py  # 会话状态管理
│       │   └── sync_service.py     # Hub 间同步逻辑
│       │
│       └── web/
│           ├── __init__.py
│           ├── routes.py       # Web 页面路由
│           └── templates/
│               ├── base.html
│               ├── dashboard.html
│               └── session.html
│
└── tests/
    ├── test_api.py
    ├── test_event_processor.py
    ├── test_session_manager.py
    └── test_sync.py
```

## 10. 技术栈

| 组件        | 选择           | 版本     | 理由                           |
| --------- | ------------ | ------ | ---------------------------- |
| 包管理       | uv           | latest | 已有工具链，快速                     |
| Web 框架    | FastAPI      | 0.115+ | 原生 async、WebSocket、自动 API 文档 |
| MCP SDK   | fastmcp      | latest | Python MCP 官方推荐              |
| 数据库       | SQLite       | 内置     | 零运维、aiosqlite 支持 async       |
| SQLite 驱动 | aiosqlite    | latest | 配合 FastAPI async             |
| 模板        | Jinja2       | latest | FastAPI 内置支持                 |
| 前端交互      | htmx         | 2.0    | 最小化 JS，服务端驱动                 |
| 样式        | Tailwind CSS | CDN    | 无构建步骤                        |
| 进程管理      | uvicorn      | latest | ASGI 服务器                     |

## 11. 部署与运行

### 11.1 启动 Hub

```bash
cd ~/Git/agent-hub

# 主力机 A
uv run agent-hub serve --hub-id hub-a --host 0.0.0.0 --port 7800

# 主力机 B
uv run agent-hub serve --hub-id hub-b --host 0.0.0.0 --port 7800
```

Hub 监听 `0.0.0.0:7800`，Tailscale 网络内的所有设备均可访问。

### 11.2 配置 Claude Code

在每台设备上运行部署脚本（或手动编辑 `~/.claude/settings.json`）：

```bash
uv run agent-hub install --hub-url http://<tailscale-ip>:7800 --hostname my-machine
```

该命令自动将 hook 配置和 MCP Server 配置写入 `~/.claude/settings.json`（合并现有配置）。

### 11.3 验证连通性

```bash
# 从远程设备测试
curl http://<tailscale-ip>:7800/api/stats

# 模拟一个 hook 事件
curl -X POST http://<tailscale-ip>:7800/api/events \
  -H "Content-Type: application/json" \
  -d '{"session_id":"test-123","hook_event_name":"SessionStart","cwd":"/tmp","source":"startup"}'
```

### 11.4 同步两个 Hub

```bash
# 手动触发
uv run agent-hub sync

# 或加入 crontab 定时执行（每 30 分钟）
*/30 * * * * cd ~/Git/agent-hub && uv run agent-hub sync 2>&1 | logger -t agent-hub
```

## 12. 数据保留与隐私

### 12.1 脱敏策略

- **tool_input** — Bash 命令完整保留；Write/Edit 的文件内容不存储，仅保留文件路径
- **UserPromptSubmit** — prompt 截断至 200 字符，仅用于概览，不存完整对话
- **payload** — 完整 payload 存入 JSON 字段，但 30 天后自动清理 payload 只保留摘要

### 12.2 数据保留

- events 记录保留 90 天，之后自动清理
- sessions 记录永久保留（数据量很小）
- 可通过 `DELETE /api/sessions/:id` 手动清理

## 13. 实施计划

### Phase 1: 核心骨架 ✅

- [x] 项目初始化（pyproject.toml, 目录结构）
- [x] SQLite 数据库初始化（WAL 模式 + 自动迁移）
- [x] `POST /api/events` 端点 — 接收并存储 hook 事件
- [x] 事件处理器 — payload 解析 + 摘要生成 + 脱敏存储
- [x] 会话状态管理 — active → idle (Stop) → stopped (30min sweep)
- [x] 基本查询 API（sessions, events, stats）

### Phase 2: 实时 + 可视化 ✅

- [x] WebSocket 实时事件推送（broadcaster 模式）
- [x] Web Dashboard — 会话卡片 + 实时事件流
- [x] 会话详情页 — 事件时间线 + 分页加载
- [x] Token usage tracking — 解析 transcript .jsonl 聚合 input/output/cache 用量
- [x] Model auto-detection — 从 transcript 读取最新模型，支持 mid-session 切换
- [x] Waiting-for-authorization 检测 — 后台每 3s 检查 transcript 末尾的 pending tool_use
- [x] Mobile-friendly 响应式布局 — 两行事件卡片、紧凑 header、粉蓝/黄色状态方案
- [x] SessionStart command hook — 绕过 Claude Code 对 SessionStart HTTP hook 的限制
- [x] systemd user service — 开机自启，`systemctl --user restart agent-hub`

**实现过程中发现的约束和踩坑记录（供 hub-b 部署参考）：**

1. **HTTP hook 仅允许 loopback** — Claude Code 会检查 hook URL 的解析 IP，Tailscale 私有地址 (100.x.x.x) 被拒绝。所有 HTTP hook 必须用 `http://127.0.0.1:7800`。跨机器的 hook 无法使用 HTTP type，只能用 command type + curl。

2. **SessionStart 不支持 HTTP hook** — 源码 `hooks.ts:1850-1864`，headless 模式会死锁。必须使用 command hook：`bash -c 'cat | curl ... -d @-'`。

3. **SessionStart hook 中检测 tmux** — 命令中加 `$(tmux display-message -p "#{session_name}" 2>/dev/null || echo "")` 传给 Hub。不在 tmux 中时返回空字符串，不影响功能。

4. **MCP server 注册** — 必须用 `claude mcp add --transport sse --scope user agent-hub URL`。手动创建 `~/.claude/.mcp.json` 无效！配置写入 `~/.claude.json`。

5. **Terminal URL 不能写死 localhost** — 手机通过 Tailscale 访问时，嵌入页面的 terminal 链接也得是 Tailscale IP。解决：从 HTTP request 的 Host header 动态提取 hostname，只配端口号。

6. **Web Terminal 端口** — 从 7683 改为 7700。Web Terminal 和 Hub 是独立服务，分别用 systemd user service 管理。

7. **tmux session 生命周期** — Hub DB 中存的 `tmux_session` 名可能过期（tmux session 被 kill 但 DB 未清理）。Dashboard 的 attach 链接会失败。Web Terminal 端会返回 4404 错误码。

8. **Waiting 检测** — 依赖读取本地 transcript 文件（每 3s 检查尾部）。远程 session（transcript 不在本机）无法检测 waiting 状态。

9. **Model 检测** — SessionStart resume payload 不含 model 字段。解决：从 transcript 的最后一条 assistant message 的 `message.model` 字段读取。

10. **approve API** — Hub 内部通过 `http://127.0.0.1:{terminal_port}/api/terminals/{name}/send` 调用 Web Terminal。Web Terminal 如果配了 auth，Hub 也需要传 auth header（当前未实现，假设 loopback 免 auth）。

11. **5h/weekly 限额** — 不在 hook payload 或 transcript 中，无法追踪。

12. **前端 JS event 作用域** — `onclick="fn()"` 中的 `event` 对象在 async callback（fetch .then）中不可用。按钮操作需要直接传 DOM 元素：`onclick="fn(id, this)"`。

### Phase 3: MCP Server + 服务协同

**MCP Server ✅ — 让任意 Claude Code 会话查询和管理其他会话**

- [x] MCP Server 集成（FastMCP v3, SSE transport, 挂载在 Hub `/mcp` 路径下）
- [x] `list_sessions` tool — 列出会话，显示 waiting/pending 状态和 token 用量
- [x] `get_session` tool — 会话详情 + 最近 N 条事件，支持 partial ID 前缀匹配
- [x] `search_events` tool — 按 summary 关键词、tool_name、session_id 搜索
- [x] `get_dashboard` tool — 全局概览（各状态数量、活跃会话、最近事件）
- [x] `get_transcript_summary` tool — 读取 transcript 尾部，提取最近 user prompts / tool calls / responses
- [x] Claude Code MCP 注册 — `claude mcp add --transport sse --scope user agent-hub http://127.0.0.1:7800/mcp/sse`

**注意：** MCP server 配置必须通过 `claude mcp add` 命令注册，写入 `~/.claude.json`。手动创建 `.mcp.json` 文件无效。

**Tmux Hub + 状态机简化 + UI 流程 ✅**

- [x] Tmux Hub 页面 (`/tmux`) — 裸 tmux 列表 / 创建 (带目录选择器) / kill / dead-pane 检测
- [x] 自动转移检测 — 在预先存在的 tmux 里起 claude 会自动标记为 From Tmux
- [x] Dashboard `+ New` 按钮 — 复用目录选择器,POST `/api/tmux/new` 带 `command=claude`,一键起 tmux+claude
- [x] Hub-launched 会话不会被误判为 transferred — 用进程内 TTL set 记录,绕过 `_detect_transferred` 的时间阈值
- [x] Dead-tmux sweep — `_sweep_dead_tmux` 每 60s 跑一次 `tmux ls`,跟 DB 做集合差,tmux 死了立刻 (<60s) 翻到 stopped
- [x] 状态机彻底去时间化 — 删掉 `idle_timeout_minutes` 配置、`get_stale_sessions` 函数、`sweep_stale_sessions` 里的时间 cutoff 分支。idle 永久保留直到 tmux 死,实现"睡一觉醒来老会话还在"
- [x] 分离 Idle / Stopped 页面 — 主 Dashboard 只展示 active (+ waiting);Idle/Stopped stat 卡片点击进入独立页面
- [x] Delete 按钮 — 每张 idle/stopped 卡片一个;idle delete 会先 `tmux kill-session` 再清 DB 行 (避免"点了 Delete 但会话因 Claude 下次发事件又回来"的悬浮 bug)
- [x] `POST /api/sessions/clear-stopped` + Stopped 页面 Clear All 按钮 — 一键清空历史
- [x] 审批解析器升级 — 从硬依赖箱体边框 `│` 改成锚定 `❯ 1. Yes` pattern,既支持 boxed 又支持 unboxed 渲染;向上扫描窗口从 12 行扩到 25 行,并识别 `Bash command` / `Edit file` 等 header,工具名识别更准
- [x] `SessionResponse` 补全 `pending_detail` 字段 — 之前 pydantic model 漏了,API 只返 `pending_tool`
- [x] Running/Waiting 互斥 — `_enrich_running` 发现有 `pending_tool` 时 early-return,避免出现"Running Bash"和"waiting: Bash"同时显示
- [x] Tmux 名字复用自动退休 — `ensure_session` 在创建新 session row 前,把同名 `tmux_session` 的旧 active/idle 行批量标 stopped。防止 tmux 名字复用导致的孤儿行累积

**新增的踩坑 (接续 Phase 2 的 12 条)：**

13. **Running 与 Waiting 语义冲突** — 当 `_enrich_running` 只看 `last_event == PreToolUse` + elapsed > 30s,会把等审批的会话(PreToolUse 已触发,用户没批)误标为 Running。修复:把 pending_tool 作为 early-return 条件,两者互斥。

14. **审批提示的无框渲染** — Claude Code 的审批 UI 并非总是用 `│` 边框,Bash with shell-metachar warning 等场景会是纯文本。原解析器要求选择器行同时含 `│` 和 `❯`,会整体漏判。锚定 `❯ 1. Yes` 比箱体更可靠,同时兼容两种样式。

15. **Tmux 名字复用产生幽灵 idle 行** — 昨天的 `claude-proj-1` 退出 → tmux 可能死了或被重建 → 今天又起了一个 `claude-proj-1`,新 claude 是不同的 session_id,DB 增加一行。老的 `_sweep_stale_sessions` 用 tmux 名字匹配活 tmux 就 `touch_session`,把昨天的老行 last_seen_at 一直续命。修:`ensure_session` 在创建新行时直接退休同名旧行。

16. **Hub-launched claude 会被 `_detect_transferred` 误判** — `tmux new-session -d claude` 到 SessionStart 中间有 claude 的 workspace trust prompt 卡住,实际延迟可能远超 5s 阈值。加一个 `_HUB_LAUNCHED_TMUX` TTL set (120s),`_detect_transferred` 开头查一次就直接 return 0。

17. **idle→stopped 不应由时间驱动** — 30 分钟 cutoff 本质上是 "最近没动就判死",但一个用户睡一觉醒来想继续的 idle session 根本没死,只是没动。正确的语义是:idle 只要 tmux 还活着就保留,stopped 只由 tmux 死亡触发。这是对 Phase 1 状态机的根本性修正。

**Web Terminal 集成 — tmux 持久化 + Dashboard 联动**

现有 Web Terminal (Tornado + xterm.js) 运行在 `localhost:7683`，支持手机虚拟键盘。
核心改造：用 tmux 替代裸 PTY 作为后端，实现会话持久化和跨终端 attach。

选择 tmux 而非 zellij 的原因：
- `tmux send-keys` 可以从程序发送按键（实现一键审批）
- `tmux list-sessions` / `tmux capture-pane` 完整的 session 枚举和输出读取
- scripting API 成熟，被广泛用作 web terminal 后端（ttyd、gotty、wetty）
- zellij 的优势在 UI 和手动使用，但程序化控制远不如 tmux

#### 3.1 Web Terminal 改造（独立项目，`/home/user/Git/web-terminal`）

**URL 参数 API：**

```
http://localhost:7683/?name=SESSION_NAME&cwd=PATH&cmd=COMMAND&attach=TMUX_SESSION
```

| 参数 | 说明 | 示例 |
|------|------|------|
| `name` | 命名 terminal session，断开后可重连 | `?name=proj-a` |
| `cwd` | 初始工作目录 | `?cwd=/home/user/project` |
| `cmd` | 连接后自动执行命令 | `?cmd=claude+--resume+abc` |
| `attach` | 直接 attach 到已有 tmux session | `?attach=claude-1` |

**tmux 后端行为：**

```
连接时：
  if ?attach=SESSION:
    tmux attach-session -t SESSION          # attach 到已有 session
  elif ?name=NAME:
    tmux new-session -A -s NAME             # 创建或 reattach（-A 是关键）
    if ?cwd: tmux send-keys "cd PATH" Enter
    if ?cmd: tmux send-keys "CMD" Enter
  else:
    tmux new-session -s auto-XXXX           # 匿名 session

断开时：
  tmux session 继续存活（这是核心价值）
  重新连接同一 ?name= 自动 reattach

手机场景：
  切换应用 → WebSocket 断开 → tmux session 存活
  回到浏览器 → 自动 reattach → 终端状态完整保留
```

**Terminal 管理 API：**

```
GET  /api/terminals              → 列出所有活跃 tmux sessions
POST /api/terminals/:name/send   → 向指定 session 发送按键
     Body: {"keys": "y\n"}         （实现一键审批）
DELETE /api/terminals/:name      → 关闭指定 tmux session
```

#### 3.2 Hub Dashboard 集成（Agent Hub 端）

**Session card 添加操作按钮：**

```
┌─────────────────────────────────────────┐
│ ● amd-tr7975wx                          │
│ /home/user/project-a                    │
│ claude-opus-4-6        last: 14:30:25   │
│                                         │
│ [Terminal]  [waiting: Bash → Approve]   │
└─────────────────────────────────────────┘
```

- **Terminal 按钮** → `http://localhost:7683/?name=hub-SESSION_ID_PREFIX&cwd=SESSION_CWD`
  - 打开持久化终端，自动 cd 到会话目录
  - 再次点击 reattach 到同一终端（不会创建新的）

- **Approve 按钮**（仅 waiting 状态显示）→ 两种实现路径：
  1. 简单方案：跳转 Web Terminal attach 到 Claude Code 所在的 tmux session
  2. 高级方案：Hub 直接调用 `POST /api/terminals/:name/send {"keys": "y\n"}`
     - 前提：Claude Code 跑在 tmux 里，且 Hub 知道 tmux session 名

**配置：** Hub 需要知道 Web Terminal 的地址：

```python
# config.py 或环境变量
terminal_url = "http://localhost:7683"
```

#### 3.3 Claude Code 运行在 tmux 中的工作流

为了让 Hub 能 attach/approve Claude Code 会话，推荐在 tmux 中启动 Claude Code：

```bash
# 启动 Claude Code 时创建命名 tmux session
tmux new-session -d -s "claude-proj-a" -c "/home/user/project-a" "claude"

# Hub 检测到 waiting 时，可以直接发送审批
tmux send-keys -t "claude-proj-a" "y" Enter

# 也可以通过 Web Terminal attach 到这个 session
http://localhost:7683/?attach=claude-proj-a
```

未来可通过 Hub MCP tool 自动化这个流程：
```
> 在 /home/user/project-a 启动一个新的 Claude Code 会话
→ MCP tool: create_claude_session(cwd="/home/user/project-a")
→ Hub 创建 tmux session + 启动 claude
→ Dashboard 自动显示新会话
```

#### 3.4 统一服务门户

- [ ] Dashboard 顶部导航添加服务快捷链接（Terminal、OpenClaw 等）
- [ ] 手机只需收藏 Hub Dashboard 一个 URL 即可访问所有服务
- [ ] Terminal 管理页面 — 列出所有 tmux sessions，提供 attach/kill 操作

### Phase 4: 双 Hub 同步

- [ ] 同步 API（`/api/sync/events`, `/api/sync/push`, `/api/sync/status`）
- [ ] sync_service — 拉取 + 合并 + 水位线管理
- [ ] `agent-hub sync` CLI 命令
- [ ] MCP tool: `sync_now` / `sync_status`

### Phase 5: 增强

- [ ] 过期数据自动清理（events 90 天，payload 30 天后仅保留摘要）
- [ ] 按项目/机器/工具维度的统计分析
- [ ] 告警机制（会话长时间无响应等）
- [ ] 支持标注/备注会话（手动添加任务描述）
- [ ] Session 详情页内嵌 Terminal iframe — 同一页面查看事件 + 操作终端
- [ ] Cost tracking — 基于 token 用量和模型定价估算 API 费用
- [ ] `create_claude_session` MCP tool — 通过 Hub 在 tmux 中启动新 Claude Code 会话

## 14. 服务拓扑

当前单机部署的服务全景：

```
┌─── AMD-7975WX (Tailscale) ──────────────────────────────────────┐
│                                                                  │
│  Agent Hub (:7800)              Web Terminal (:7683)             │
│  ┌──────────────────┐           ┌──────────────────┐            │
│  │ Dashboard        │──[link]──▶│ xterm.js + tmux  │            │
│  │ REST API         │           │ 虚拟键盘 (mobile) │            │
│  │ WebSocket        │◀──[api]──▶│ /api/terminals   │            │
│  │ MCP Server (/mcp)│           └────────┬─────────┘            │
│  └──────┬───────────┘                    │                      │
│         │ HTTP hooks              tmux attach/send-keys          │
│  ┌──────┴────────────────────────────────┴──────────┐           │
│  │ tmux sessions                                     │           │
│  │  ├── claude-proj-a  (Claude Code #1)             │           │
│  │  ├── claude-proj-b  (Claude Code #2)             │           │
│  │  └── hub-XXXX       (ad-hoc terminals)           │           │
│  │                                                   │           │
│  │ Claude Code ← MCP client → Agent Hub             │           │
│  └──────────────────────────────────────────────────┘           │
│                                                                  │
│  OpenClaw (:443 via Tailscale serve)                            │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
         │
    手机 (Tailscale)
    └── 浏览器 → Hub Dashboard
         ├── 查看所有 session 状态和 token 用量
         ├── [Terminal] 按钮 → Web Terminal (tmux reattach)
         └── [Approve] 按钮 → tmux send-keys "y" (一键审批)
```

## 15. 未来扩展

暂不实现，但架构上预留扩展空间：

- **其他 CLI 接入** — Codex CLI / Crush CLI / Copilot CLI 通过 wrapper 脚本调用 `POST /api/events`
- **远程指令** — 通过 Hub 向指定会话发送消息（需要 Claude Code 支持双向通信）
- **任务分配** — 在 Hub 上创建任务，指定某个会话执行
- **多用户** — 加入认证，支持团队使用
