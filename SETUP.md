# Agent Hub — 部署与配置指南

## 架构概览

```
每台计算机需要配置:              Hub 主机额外需要:
├── Claude Code hooks            ├── agent-hub server (systemd)
├── MCP server 注册              ├── web-terminal server (systemd)
├── ai-tmux wrapper              └── SQLite 数据库
└── fish/bash function
```

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
