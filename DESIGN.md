# Agent Hub - Technical Design

> Claude Code 会话统一管理平台

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
            ToolUse/UserPrompt (刷新 last_seen_at)
                   │
              Stop ──▶ idle
                   │
         超过 30 分钟无事件 ──▶ stopped
                   │
         新的 SessionStart (相同 session_id, source=resume) ──▶ active
```

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

### 5.2 查询 API

```
GET  /api/sessions                  -- 列出所有会话（支持 ?status=active 过滤）
GET  /api/sessions/:id              -- 会话详情
GET  /api/sessions/:id/events       -- 会话事件时间线（支持分页）
GET  /api/sessions/:id/events/latest -- 最近 N 条事件
GET  /api/stats                     -- 全局统计（活跃数、今日事件数等）
DELETE /api/sessions/:id            -- 删除会话记录（仅清理已停止的）
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

在每台设备的 `~/.claude/settings.json` 中添加：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "http",
            "url": "http://<hub-tailscale-ip>:7800/api/events",
            "timeout": 5
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "http",
            "url": "http://<hub-tailscale-ip>:7800/api/events",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "http",
            "url": "http://<hub-tailscale-ip>:7800/api/events",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "http",
            "url": "http://<hub-tailscale-ip>:7800/api/events",
            "timeout": 5
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "http",
            "url": "http://<hub-tailscale-ip>:7800/api/events",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "http",
            "url": "http://<hub-tailscale-ip>:7800/api/events",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

### 7.2 设计要点

- **统一 endpoint** — 所有事件发送到同一个 `/api/events`，由 Hub 根据 `hook_event_name` 分发处理
- **timeout: 5** — 设为 5 秒，避免 Hub 不可达时阻塞 Claude Code
- **无需 auth** — Tailscale 内网本身提供了网络级鉴权（只有你的设备能访问）
- **无需 allowedEnvVars** — 不传递敏感环境变量
- **matcher: `*`** — 捕获所有工具事件；如果事件量过大，可后续按需过滤

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
/                          -- 仪表盘首页
  ├── 活跃会话卡片列表
  │     每张卡片显示：
  │     - hostname + cwd（项目名）
  │     - 当前状态 (active/idle)
  │     - 最后活动时间
  │     - 最近一条事件摘要
  │     - 使用的模型
  │
  ├── 实时事件流（WebSocket）
  │     最近 20 条事件，实时滚动
  │
  └── 统计摘要
        - 今日活跃会话数
        - 今日事件总数
        - 按工具分布饼图

/sessions/:id              -- 会话详情页
  ├── 会话元信息
  ├── 事件时间线（无限滚动）
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

### Phase 1: 核心骨架

- [ ] 项目初始化（pyproject.toml, 目录结构）
- [ ] SQLite 数据库初始化
- [ ] `POST /api/events` 端点 — 接收并存储 hook 事件
- [ ] 事件处理器 — payload 解析 + 摘要生成
- [ ] 会话状态管理 — 自动状态流转
- [ ] 基本查询 API（sessions, events）

### Phase 2: 实时 + 可视化

- [ ] WebSocket 实时事件推送
- [ ] Web Dashboard — 会话卡片 + 事件流
- [ ] 会话详情页 — 时间线视图

### Phase 3: MCP Server

- [ ] MCP Server 集成（SSE transport）
- [ ] 实现 list_sessions / get_session / search_events / get_dashboard tools
- [ ] 编写 install 命令 — 自动配置 Claude Code hooks + MCP

### Phase 4: 双 Hub 同步

- [ ] 同步 API（`/api/sync/events`, `/api/sync/push`, `/api/sync/status`）
- [ ] sync_service — 拉取 + 合并 + 水位线管理
- [ ] `agent-hub sync` CLI 命令
- [ ] MCP tool: `sync_now` / `sync_status`

### Phase 5: 增强

- [ ] 过期数据自动清理
- [ ] 按项目/机器/工具维度的统计分析
- [ ] 告警机制（会话长时间无响应等）
- [ ] 支持标注/备注会话（手动添加任务描述）

## 14. 未来扩展

暂不实现，但架构上预留扩展空间：

- **其他 CLI 接入** — Codex CLI / Crush CLI / Copilot CLI 通过 wrapper 脚本调用 `POST /api/events`
- **远程指令** — 通过 Hub 向指定会话发送消息（需要 Claude Code 支持双向通信）
- **任务分配** — 在 Hub 上创建任务，指定某个会话执行
- **多用户** — 加入认证，支持团队使用
