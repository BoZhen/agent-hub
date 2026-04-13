# Agent Hub — 部署与配置指南

## 架构概览

```
每台计算机需要配置:              Hub 主机额外需要:
├── Claude Code hooks            ├── agent-hub server (systemd)
├── Codex + omx hooks [可选]     ├── web-terminal server (systemd)
├── MCP server 注册              └── SQLite 数据库
├── ai-tmux wrapper
└── fish/bash function
```

**角色定义**:
- **服务端(Hub 主机 / main machine)** —— 跑 `agent-hub` systemd 服务 + web terminal + SQLite DB。通常只有一台。
- **客户端(辅助计算机 / auxiliary machines)** —— 跑 Claude Code 或 Codex,把 hook 事件通过 Tailscale 推到服务端。每台机器都按 Part 2 配置一遍,包括 Hub 主机本身(因为主机上一般也直接用 Claude/Codex)。

---

## Part 1: Hub 服务端部署（仅主机）

### 1.1 安装依赖

```bash
cd ~/Git/agent-home
uv sync
```

### 1.2 启动服务

```bash
# 首次测试
uv run agent-hub serve --hub-id hub-a

# 注册为 systemd 用户服务
cat > ~/.config/systemd/user/agent-hub.service << 'EOF'
[Unit]
Description=Agent Hub — Claude Code session management
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/USER/Git/agent-home
ExecStart=/home/USER/.local/bin/uv run agent-hub serve --hub-id HUB_ID
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

# 替换 USER 和 HUB_ID，然后启动
systemctl --user daemon-reload
systemctl --user enable --now agent-hub

# 确保开机启动（用户未登录时也运行）
loginctl enable-linger $USER
```

### 1.3 Web Terminal（tmux 后端）

Web Terminal 是独立项目（`~/Git/web-terminal`），提供 tmux 持久化终端访问。

```bash
# 注册为 systemd 用户服务，端口 7700
systemctl --user enable --now webterminal
```

关键配置：
- 环境变量 `TMUX_TMPDIR=$HOME/.tmux` — 确保和用户 shell 使用同一 tmux socket
- 端口 7700

### 1.4 验证

```bash
curl http://127.0.0.1:7800/api/stats       # Hub API
curl http://127.0.0.1:7800/mcp/sse          # MCP SSE endpoint
curl http://127.0.0.1:7700/api/terminals    # Web Terminal API
```

---

## Part 2: 客户端配置（每台计算机）

以下配置在**所有运行 Claude Code 的机器**上执行，包括 Hub 主机本身。

### 2.1 Hook 配置

编辑 `~/.claude/settings.json`，在 `"hooks"` 字段中添加：

```jsonc
{
  "hooks": {
    // SessionStart 必须用 command hook（HTTP hook 被 Claude Code 阻止）
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "bash -c 'TS=$(tmux display-message -p \"#{session_name}\" 2>/dev/null || echo \"\"); cat | curl -s -X POST \"http://HUB_IP:7800/api/events?host=$(hostname)&tmux_session=$TS\" -H \"Content-Type: application/json\" -d @-'",
            "timeout": 10
          }
        ]
      }
    ],
    // 其余事件用 HTTP hook
    "PreToolUse": [
      { "matcher": "*", "hooks": [{ "type": "http", "url": "http://HUB_IP:7800/api/events?host=HOSTNAME" }] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [{ "type": "http", "url": "http://HUB_IP:7800/api/events?host=HOSTNAME" }] }
    ],
    "PostToolUseFailure": [
      { "matcher": "*", "hooks": [{ "type": "http", "url": "http://HUB_IP:7800/api/events?host=HOSTNAME" }] }
    ],
    "UserPromptSubmit": [
      { "matcher": "*", "hooks": [{ "type": "http", "url": "http://HUB_IP:7800/api/events?host=HOSTNAME" }] }
    ],
    "Stop": [
      { "matcher": "*", "hooks": [{ "type": "http", "url": "http://HUB_IP:7800/api/events?host=HOSTNAME" }] }
    ]
  }
}
```

**替换：**
- `HUB_IP` — Hub 本机用 `127.0.0.1`；辅助机用 Hub 的 Tailscale IP
- `HOSTNAME` — 本机名称（用于 Dashboard 显示）

**踩坑：**
- HTTP hook **仅允许 loopback (127.0.0.1)**。辅助机无法直接用 Tailscale IP 作为 HTTP hook URL！
- 辅助机的解决方案：所有 hook 都改为 command type + curl（和 SessionStart 一样）

### 2.2 辅助机 Hook（非 Hub 本机）

辅助机由于 loopback 限制，**所有 hook 都必须用 command type**：

```jsonc
{
  "hooks": {
    "SessionStart": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "bash -c 'TS=$(tmux display-message -p \"#{session_name}\" 2>/dev/null || echo \"\"); cat | curl -s -X POST \"http://TAILSCALE_IP:7800/api/events?host=$(hostname)&tmux_session=$TS\" -H \"Content-Type: application/json\" -d @-'", "timeout": 10 }] }
    ],
    "PreToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "bash -c 'cat | curl -s -X POST \"http://TAILSCALE_IP:7800/api/events?host=$(hostname)\" -H \"Content-Type: application/json\" -d @-'", "timeout": 5 }] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "bash -c 'cat | curl -s -X POST \"http://TAILSCALE_IP:7800/api/events?host=$(hostname)\" -H \"Content-Type: application/json\" -d @-'", "timeout": 5 }] }
    ],
    "PostToolUseFailure": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "bash -c 'cat | curl -s -X POST \"http://TAILSCALE_IP:7800/api/events?host=$(hostname)\" -H \"Content-Type: application/json\" -d @-'", "timeout": 5 }] }
    ],
    "UserPromptSubmit": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "bash -c 'cat | curl -s -X POST \"http://TAILSCALE_IP:7800/api/events?host=$(hostname)\" -H \"Content-Type: application/json\" -d @-'", "timeout": 5 }] }
    ],
    "Stop": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "bash -c 'cat | curl -s -X POST \"http://TAILSCALE_IP:7800/api/events?host=$(hostname)\" -H \"Content-Type: application/json\" -d @-'", "timeout": 5 }] }
    ]
  }
}
```

替换 `TAILSCALE_IP` 为 Hub 主机的 Tailscale IP（如 `100.64.0.1`）。

### 2.3 MCP Server 注册

```bash
# Hub 本机
claude mcp add --transport sse --scope user agent-hub http://127.0.0.1:7800/mcp/sse

# 辅助机（通过 Tailscale）
claude mcp add --transport sse --scope user agent-hub http://TAILSCALE_IP:7800/mcp/sse
```

**踩坑：**
- 必须用 `claude mcp add` 命令。手动创建 `.mcp.json` 文件**无效**。
- 配置写入 `~/.claude.json`，重启 Claude Code session 后生效。
- 用 `claude mcp list` 验证连接状态。

### 2.4 ai-tmux Wrapper

确保 `tmux` 已安装，然后配置 shell function：

**Fish (`~/.config/fish/config.fish`):**
```fish
# ai-tmux wrapper — auto-wrap CLI tools in tmux
function claude
    ~/Git/agent-home/scripts/ai-tmux claude $argv
end
function codex
    ~/Git/agent-home/scripts/ai-tmux codex $argv
end
```

**Bash/Zsh (`~/.bashrc` 或 `~/.zshrc`):**
```bash
claude() { ~/Git/agent-home/scripts/ai-tmux claude "$@"; }
codex() { ~/Git/agent-home/scripts/ai-tmux codex "$@"; }
```

**tmux socket 配置（确保 systemd 服务和 shell 共享同一 socket）:**
```fish
# Fish:
set -gx TMUX_TMPDIR "$HOME/.tmux"
if not test -d "$TMUX_TMPDIR"; mkdir -p "$TMUX_TMPDIR"; chmod 700 "$TMUX_TMPDIR"; end
```

**踩坑：**
- 不要用 symlink 替换 `~/.local/bin/claude` — Claude 自动更新会覆盖 symlink。
- Shell function 优先级高于 PATH 中的二进制，不会被覆盖。
- 辅助机也需要 `ai-tmux` 脚本：可以从 Hub 仓库复制，或通过 git clone。

### 2.5 验证

```bash
# 1. 新终端中启动 claude
claude

# 2. 检查是否在 tmux 中
tmux ls
# 应看到 claude-<dirname>-1

# 3. 检查 Hub 是否收到事件
curl http://HUB_IP:7800/api/sessions

# 4. 检查 tmux session 是否被检测
curl http://HUB_IP:7800/api/sessions | python3 -c "
import sys, json
for s in json.load(sys.stdin):
    if s['status'] == 'active':
        print(f\"{s['hostname']}  tmux={s.get('tmux_session')}\")"

# 5. MCP 连接
claude mcp list
# 应看到 agent-hub: ✓ Connected
```

### 2.6 Codex hook 配置（via omx,可选）

如果这台机器跑 Codex CLI(不只是 Claude Code),可以通过 [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex) 让 codex 事件也推送到 Hub,享受和 Claude 一致的 event feed、running state、以及 MCP 和 Bash 审批推送。

**不装 omx 也能工作**:Hub 会 fall back 到 pane-scan 轮询(每 3 秒扫 tmux 找 codex TUI),session discovery + 远程审批仍然可用,只是 dashboard 上 codex 卡片的 "latest 2 events" 会一直空着 —— 因为 pull 路径不生成 events 表行。

#### 2.6.1 服务端(Hub 主机)

Hub 本身**不需要**任何 codex 特定配置。`scripts/codex-hub-hook.sh` 作为 bridge 脚本已经跟着 repo 一起 —— `/api/events` endpoint 在 Commit `2fcf1c4` 之后就接受 `?tool=codex` query param,`session_manager` 会自动给 codex payload 打标签。

服务端唯一需要关心的是:**当 Hub 主机自己也跑 codex 时,按 2.6.2 客户端流程配置一遍**(和 Claude hook 一样,Hub 机既是服务端又是客户端)。

#### 2.6.2 客户端(每台跑 codex 的机器)

**前置**:机器上已经 clone `~/Git/agent-home`(Part 2.4 的 ai-tmux wrapper 就依赖这个)。

**Step 1 —— 安装 omx**:

```bash
npm install -g oh-my-codex
omx setup           # 会生成 ~/.codex/hooks.json 和 ~/.codex/config.toml
# 确认 ~/.codex/config.toml 里有:
#   [features]
#   codex_hooks = true
```

**Step 2 —— 追加 agent-hub bridge hook** 到 `~/.codex/hooks.json`。`omx setup` 生成的版本每个 event 下只有 omx 自己的 hook group,需要追加一个**第二个** hook group 指向 repo 的 bridge 脚本(不要动 omx 原有的 entry):

```jsonc
{
  "hooks": {
    "SessionStart": [
      { /* existing omx entry with matcher "startup|resume" — don't touch */ },
      {
        "hooks": [
          { "type": "command", "command": "/home/USER/Git/agent-home/scripts/codex-hub-hook.sh SessionStart" }
        ]
      }
    ],
    "UserPromptSubmit": [
      { /* existing omx entry */ },
      {
        "hooks": [
          { "type": "command", "command": "/home/USER/Git/agent-home/scripts/codex-hub-hook.sh UserPromptSubmit" }
        ]
      }
    ],
    "PreToolUse": [
      { /* existing omx entry with matcher "Bash" */ },
      {
        "hooks": [
          { "type": "command", "command": "/home/USER/Git/agent-home/scripts/codex-hub-hook.sh PreToolUse" }
        ]
      }
    ],
    "PostToolUse": [
      { /* existing omx entry */ },
      {
        "hooks": [
          { "type": "command", "command": "/home/USER/Git/agent-home/scripts/codex-hub-hook.sh PostToolUse" }
        ]
      }
    ],
    "Stop": [
      { /* existing omx entry with timeout 30 */ },
      {
        "hooks": [
          { "type": "command", "command": "/home/USER/Git/agent-home/scripts/codex-hub-hook.sh Stop" }
        ]
      }
    ]
  }
}
```

替换 `USER` 为你的实际用户名。`codex-hub-hook.sh` 必须 `chmod +x`(repo 里已经是)。

**Step 3 —— 指定 Hub 地址(仅辅助机)**:通过环境变量 `AGENT_HUB_URL` 告诉 wrapper hook 要 POST 到哪台机器:

```bash
# Hub 主机:默认 http://127.0.0.1:7800,无需任何配置
# 辅助机 (.bashrc / .zshrc / fish config.fish):
export AGENT_HUB_URL="http://100.64.0.1:7800"   # 替换为 Hub 主机的 Tailscale IP
```

**关键区别**:和 Claude Code 的 HTTP hook 不同,codex hook **全部是 command 类型**(bash + curl),所以**没有 loopback 127.0.0.1 限制** —— 辅助机可以直接让 wrapper curl 到 Tailscale IP,不需要像 Claude 辅助机那样额外搭一套全 command-type 的 hook。

**Step 4 —— 验证**:

1. 重启 codex session(hooks.json 在 codex 启动时加载一次):
   ```bash
   tmux new-session -d -s codex-test -c ~
   tmux send-keys -t codex-test: "omx --tmux" Enter
   ```

2. **发一条 prompt**(重要,见 2.6.3 第一条),然后查 session 列表:
   ```bash
   curl -s "$AGENT_HUB_URL/api/sessions?status=active" | \
     python3 -c "import sys,json; [print(s['session_id'], s['tool']) for s in json.load(sys.stdin) if s['tool']=='codex']"
   ```
   期望:`session_id` 是 UUID 格式(如 `019d85a5-7035-...`),`tool=codex`。如果看到 `codex-<tmux>-<ts>` 格式那是 pane-scan 的 placeholder,说明 hook 没 fire 到 Hub(检查 `AGENT_HUB_URL` 和 hooks.json)。

3. 查看 event feed:
   ```bash
   curl -s "$AGENT_HUB_URL/api/sessions/<uuid>/events" | python3 -m json.tool
   ```
   期望至少看到 `SessionStart` 和 `UserPromptSubmit` 两个事件。

#### 2.6.3 已知行为(不是 bug,但得心里有数)

- **codex 0.120.0 把 SessionStart hook 延迟到第一个 turn 开始**,不是进程启动。所以你刚起 `omx --tmux` 时 dashboard 卡片可能先是 pane-scan 创建的 placeholder;发第一条 prompt 后 hook 接管,placeholder 被自动 retire 掉,真 UUID session 显示出来。两条路径的 hybrid push + pull 就是为这种情况设计的。

- **MCP tool 调用不触发 PreToolUse hook**(验证过 `omx_state.*`、`omx_memory.*` 等)。Codex 给 MCP 工具配了独立的 4 选项审批 UI(`› 1. Allow`),Hub 的 pane-scan parser 识别这个 UI,dashboard 仍然显示 waiting 徽章 + Approve/Always 按钮,但这条路径靠 pane-scan 而不是 hook push。MCP 审批的 detail 格式是 `<server>: <tool>`(如 `omx_state: state_get_status`)。

- **PreToolUse 每次 tool call 可能 fire 两次**(dry-run + real),两次 `tool_use_id` 不同,第一次的常常没有对应的 PostToolUse。这是 codex 内部行为,对 `_enrich_running`(基于最后一条 event 的启发式)没有影响,可以忽略。

---

## Part 3: 踩坑总结

| 问题 | 症状 | 解决方案 |
|------|------|----------|
| HTTP hook 拒绝 | `HTTP hook blocked: resolves to private address` | Hub 本机用 127.0.0.1；辅助机改用 command hook |
| SessionStart HTTP hook 死锁 | hook 不触发，无错误 | 改用 command hook (`bash -c 'cat \| curl ...'`) |
| MCP server 不出现 | `/mcp` 列表中没有 agent-hub | 用 `claude mcp add` 注册，非手动编辑文件 |
| Terminal 按钮 localhost 不可达 | 手机通过 Tailscale 访问时报错 | Hub 已修复：自动从 request host 派生 URL |
| claude wrapper 被覆盖 | 自动更新后又变回裸启动 | 用 shell function 而非 symlink |
| tmux 双横杠命名 | `claude-dir--2` | 用 `printf` 代替 `echo`，避免换行符被转义 |
| approve 弹 JS error | `cannot read properties of undefined` | 传 `this` 而非依赖 `event` |
| tmux attach 失败 | Web Terminal 报 4404 | tmux session 已被 kill，DB 中的记录过期 |
| Waiting 检测不到 | 远程 session 无 waiting 显示 | 依赖读取本地 transcript 文件，远程机器不支持 |
| Model 显示 unknown | SessionStart resume 无 model | Hub 从 transcript 最后一条 assistant message 读取 |
| codex 卡片先是 placeholder | session_id 格式 `codex-<tmux>-<ts>`，发完第一条 prompt 才变 UUID | codex 0.120.0 的 SessionStart hook 延迟到首个 turn;pane-scan 先创建 placeholder,hook 到了自动 retire(这是设计) |
| codex TUI 报 `hook returned invalid JSON output` | 控制台出现 red error 但功能正常 | wrapper 脚本必须 `echo "{}"` 作为 stdout;检查 `scripts/codex-hub-hook.sh` 最后一行 |
| Hub 收不到 codex 事件 | dashboard 有卡但 event feed 空，session_id 是 placeholder 格式 | 检查 `~/.codex/hooks.json` 第二个 hook group 是否指向 `codex-hub-hook.sh`;辅助机检查 `AGENT_HUB_URL`;确认 `scripts/codex-hub-hook.sh` 有 `chmod +x` |
| codex MCP 审批无 waiting 徽章 | codex 卡住等 MCP tool，dashboard 无反应 | MCP 调用不走 hook，只靠 pane-scan 3s tick —— 等一个 tick;检查 API 返回的 `pending_tool=MCP` 字段是否出现 |
| `/clear` 后卡片跳到 From Tmux | 原 Sessions 标签里的会话在 `/clear` 后错误地变成 "From Tmux" | `ensure_session` 顺序反了 —— `_detect_transferred`(5s 启发式)先跑,看到 tmux 已老就返回 1。修复 `acd87b6`:orphan 查询提前,有 orphan 就继承它的 `transferred`,没有再跑启发式 |
| 打断工具后 Running 状态卡死 | Esc/Ctrl-C 中断工具后 dashboard 一直显示 `Running Bash (7m 33s)`,永不清除 | `_enrich_running` 只看 "last event=PreToolUse + elapsed>30s",中断后没 PostToolUse 收尾,elapsed 无界。修复 `2f163d1`:在条件满足后加 `_pane_shows_working` ground truth 校验,pane 不再显示 `(N s · esc to interrupt)` 就不标 running |

---

## Part 4: 服务管理速查

```bash
# Hub 服务
systemctl --user restart agent-hub
systemctl --user status agent-hub
journalctl --user -u agent-hub -f

# Web Terminal
systemctl --user restart webterminal
systemctl --user status webterminal

# 查看所有 tmux sessions
tmux ls

# 手动清理过期 Hub sessions
curl -X DELETE http://127.0.0.1:7800/api/sessions/SESSION_ID
```
