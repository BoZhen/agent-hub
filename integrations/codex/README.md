# Agent Hub — Codex CLI 集成

这个目录把 Agent Hub 暴露的 MCP server 接入到 [Codex CLI](https://github.com/openai/codex)，让 Codex 可以通过 MCP tool 查询 Hub 里的会话、事件和 transcript。

## 暴露的工具

连上之后 Codex 会看到 `mcp__agent_hub__*` 下的 5 个工具：

| 工具 | 作用 |
|------|------|
| `list_sessions` | 列出 Hub 当前追踪的 Claude / Codex 会话 |
| `get_session` | 单会话详情 + 最近事件时间线 |
| `get_transcript_summary` | 读取本地 transcript 文件并摘要 |
| `search_events` | 按关键字 / tool 名 / session id 查事件 |
| `get_dashboard` | 全局 overview：状态计数 + 活跃会话 + 最近事件 |

## 前置条件

1. Agent Hub 正在运行（默认 `http://localhost:7800`）  
   通常通过 systemd user service 启动：`systemctl --user start agent-hub.service`，或直接 `./start.sh`。
2. Codex CLI 已安装且能读取 `~/.codex/config.toml`。

## 两种安装方式

### 方式 A：脚本一键安装（推荐）

```bash
./install.sh
```

脚本做三件事：
1. 检查 `[mcp_servers.agent_hub]` 是否已经存在，存在就直接退出（幂等）
2. 给原 `config.toml` 打带时间戳的备份
3. 通过 `tmp + mv` 原子地追加 `config-snippet.toml` 的内容

环境变量 `CODEX_CONFIG` 可以覆盖默认路径，例如：
```bash
CODEX_CONFIG=/path/to/alt-config.toml ./install.sh
```

### 方式 B：手动粘贴

把 `config-snippet.toml` 的内容追加到 `~/.codex/config.toml` 末尾：

```toml
[mcp_servers.agent_hub]
url = "http://localhost:7800/mcp-http/mcp"
```

> Hub 同时暴露一个老式 SSE 端点 `http://localhost:7800/mcp/sse`（给 Claude Code 用），**Codex 不要指到那个**。Codex 0.120+ 把 URL-based MCP server 都当成 Streamable HTTP 协议处理，只有 `/mcp-http/mcp` 能通。

## 验证

```bash
codex mcp list
```

应该能在输出里看到 `agent_hub`。然后在 Codex 会话里随便调一个工具，比如让它 "list the sessions tracked by agent_hub"，应该能拿到 `list_sessions` 的返回。

## 端口不是 7800 怎么办？

修改 `config-snippet.toml`（或 `~/.codex/config.toml` 里已安装的 block），把 URL 改成你实际的 `http://<host>:<port>/mcp/sse`，然后重启 Codex 会话。

## 卸载

从 `~/.codex/config.toml` 里删掉 `[mcp_servers.agent_hub]` block 即可。备份文件（`config.toml.bak-YYYYMMDD-HHMMSS`）仍保留在原目录，需要时可以手动恢复。
