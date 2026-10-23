# Agent Hub

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Agent Hub 是一个面向 **Claude Code** 与 **Codex CLI** 的会话管理服务，用于集中查看会话状态、事件时间线、审批状态与 tmux 终端入口。

## 界面预览

<p align="center">
  <img src="figs/modern-desktop.png" alt="桌面端 Agent Hub 工作台" width="90%">
</p>
<p align="center"><em>桌面端：待处理队列 + 按项目分组的 session 导航 + 工作集标签 + 原生 TUI 主画布</em></p>

<p align="center">
  <img src="figs/modern-phone-sessions.png" alt="移动端 Session 导航" width="28%">
  <img src="figs/modern-phone-terminal.png" alt="移动端原生 TUI" width="28%">
</p>
<p align="center"><em>手机页面 <code>/m</code>：需要你处理的 session 排在最前；点进去是原生 TUI，底部用手机键盘回复，Esc 打断这一轮</em></p>

## 核心能力

- 统一管理 Claude Code 与 Codex CLI 会话
- 记录会话事件并在 Dashboard 中实时展示
- 基于 tmux 进行远程审批与终端接入
- 按项目分组的 session 导航（活跃 / 空闲 / 已结束同组展示）、待处理队列、浏览器工作集标签、session 重命名与详情抽屉
- 单一样式表（无 Tailwind / 无前端构建），6 套暗色 + 6 套亮色主题，并与 Web Terminal 同步
- 手机端独立 Session/Terminal 模式
- 终端里的文件路径可以点击，在右侧面板（手机上全屏）预览图片、PDF、Markdown 与代码（见「文件预览」）
- 运行中的 session 可一键重启 CLI，升级 claude / codex 后用上新版本（见「恢复会话」）
- 通过 MCP SSE 暴露查询接口
- 通过 `GET /api/stream`（Server-Sent Events）向手机推送需要你处理的 session：一轮结束后还没看过的，以及等待审批的（见「手机接口」）

## 快速开始

```bash
uv sync
./start.sh
# 或
uv run agent-hub serve --hub-id <hub-id>
```

默认情况下：

- Dashboard: `http://localhost:7800`
- API docs: `http://localhost:7800/api/docs`
- MCP SSE: `http://localhost:7800/mcp/sse`

## 远程访问（Tailscale）

Hub 监听 `0.0.0.0`，但无需把端口暴露到公网。推荐用 [Tailscale](https://tailscale.com/) 组建私有网络，在任意设备上通过 Hub 主机的 Tailscale IP 直接访问：

- 手机上打开 `http://<TAILSCALE_IP>:7800` 即可随时查看会话、远程审批
- 嵌入式终端（端口 7700）同样经 Tailscale 访问，可直接在手机上接入 tmux
- 跨机器访问：辅助机设置 `export AGENT_HUB_URL="http://<TAILSCALE_IP>:7800"` 后即可把事件推送到 Hub
- 全程走 Tailscale 加密隧道，不开放任何公网端口，比端口转发更安全

> 如果你有自己的云服务器，也可以用 [Nebula](https://github.com/slackhq/nebula) 自建 lighthouse 作为替代。中转节点掌握在自己手里，延迟更可控、通常更快。Hub 监听 `0.0.0.0`，对 Tailscale / Nebula 等任意私有网段都是开箱即用的。

详细配置见 `SETUP.md`。

## Web Terminal（嵌入式终端）

Dashboard 右侧的嵌入式终端由独立的伴生服务 **[web-terminal](https://github.com/BoZhen/webterminal)** 提供（默认端口 7700），负责把 tmux 会话以持久化终端的形式渲染到浏览器中。Hub 通过 iframe 嵌入它，用于终端预览与远程审批。

现代工作台通过 `embed=1`、`theme=<id>` 与受限的父子页面生命周期消息协调 Web Terminal。主题切换、连接状态与移动端可见性会同步，但终端输入仍只走 Web Terminal 自己的 PTY/WebSocket 通道。

该服务需单独部署，安装方式见其仓库说明与本仓库 `SETUP.md` 的「Web Terminal」一节。

### 文件预览

终端里出现的文件路径（`/a/b/report.pdf`、`~/notes.md`）以及程序发出的 `file://` 超链接（OSC 8，Claude Code 会发）都可以点击：桌面端在终端右侧的面板里打开，手机页面全屏打开。点面板以外的任意位置（包括终端）或按 Esc 收起，点另一条路径则换成那个文件。文件内容由伴生服务 **[web-file-manager](https://github.com/BoZhen/web-file-manager)** 显示，图片、PDF、Markdown（含公式）与代码都能直接看。

- web-file-manager 的地址默认是打开 Hub 时所用主机的 7701 端口，可用环境变量 `AGENT_HUB_WEBFM_URL` 改（例如 `http://<TAILSCALE_IP>:7701`）。
- web-file-manager 需要登录。面板里出现登录框或 401 时，点面板右上角的 ↗ 在新标签页登录一次，之后面板直接显示。
- tmux 只把超链接转给声明支持它的终端：在 `~/.tmux.conf` 的 `terminal-features` 里给 `xterm*` 加上 `hyperlinks`（或执行 `tmux set -as terminal-features ',xterm*:hyperlinks'`），已打开的终端重连后生效。普通的文件路径不需要这一项。
- 终端自动换行的长路径整条可点；由 TUI 自己断开的长路径只能识别其中一段。

### Claude Code

Claude 通过本地 hooks 将事件推送到 Hub。推荐按 `SETUP.md` 或本文仓库中的配置示例完成接入。

### Codex CLI

支持两种方式：

1. **推荐：Codex 原生 hook 模式**  
   Codex 自带 hook 机制（`~/.codex/config.toml` 的 `[features] hooks = true` + `~/.codex/hooks.json`）。把 `scripts/codex-hub-hook.sh` 注册进去，Codex 事件就会桥接到 Hub，不需要 oh-my-codex 之类的包装层。配置步骤见 `SETUP.md` 2.6。

2. **回退：tmux 扫描模式**  
   未配置 hook 时，Hub 仍可通过 tmux pane 扫描发现 Codex 会话并提供基础管理能力，但会话只有合成 id，没有事件时间线。

注意：Codex 在第一条 prompt 提交时才触发 SessionStart hook。刚启动、还没说话的 Codex 会话在 Hub 里先以扫描得到的合成 id 出现，第一条 prompt 之后自动换成 Codex 自己的会话 id。

## 会话跟踪范围（仅 tmux）

Agent Hub 以 tmux 作为会话持久化、远程审批与终端接入的载体，因此**只跟踪运行在 tmux 中的会话**。

在 tmux 之外启动的 CLI 会话——例如通过 Claude Agent SDK / headless 模式把 `claude` / `codex` 当子进程运行的 GUI 控制台——会被 Hub 主动忽略：它们没有可供 attach / 审批 / 状态探测的 tmux pane，记录下来也只会变成无法被清理的 `idle` 僵尸。这类会话由各自的控制台自行管理，与 Agent Hub 并行共存、互不干扰。

> 即便全局 hooks 会对机器上所有 CLI 会话触发，Hub 也会在事件入口直接丢弃这些没有 tmux 的会话，因此无需为它们单独关闭 hook，也不会把数据库越喂越大。

## 历史会话自动清理

`stopped`（已结束）的会话——其 tmux 已不存在——可以随时从侧栏或抽屉点 Resume 恢复（见下），但留着不管只是历史杂物。Hub 在每次 sweep 时会**自动删除停止时间超过保留期的 stopped 会话及其事件**：

- 由环境变量 `AGENT_HUB_STOPPED_RETENTION_DAYS` 控制，默认 **14** 天；设为 `0` 则永久保留、不自动清理。
- 删除之后 Hub 就不再记得这个 session 的 id 和目录，Resume 按钮也随之消失；需要的话仍可在终端里手动 `claude --resume <id>`。
- 删除的只是 Hub 的记录；Claude / Codex 的 transcript 仍在本地磁盘，日后 `--resume` 不受影响。

> 想立刻整理已堆积的旧记录，也可以在 Dashboard 的 `/stopped` 页一键 Clear。

## 恢复会话（升级 CLI、重启机器之后）

Hub 记着每个 session 的 tmux 名、目录和启动参数（`tool`），恢复就是用这些重建 tmux，并在里面运行 CLI 自己的恢复命令：Claude 是 `claude --resume <id>`，Codex 是 `codex resume <id>`（有真实 UUID 时；只有扫描得到的合成 id 时退回选择器）。审批与沙箱跟随你自己的 Claude / Codex 配置，Hub 不加参数；旧版本启动的行（`claude-yolo` / `codex-yolo` / `omx-yolo`）恢复时仍按当时的参数补上。TUI 由 CLI 自己从 transcript 重画，tmux 的滚屏缓冲不会保留。

- **运行中的 session（升级 claude / codex 之后）**：侧栏行 hover、抽屉或手机页 `/m` 里的 **Restart**。CLI 退出后在同一个 tmux、同一个 pane 里运行上面的恢复命令，用上刚装的版本；打开着的终端不断开。正在跑一轮的 session 要再确认一次，因为重启会中断这一轮。右上角菜单里的 **Restart Claude sessions** / **Restart Codex sessions** 一次重启某个工具的全部运行中 session，正在跑一轮或等待审批的跳过并列出。接口：`POST /api/sessions/{id}/restart`（`?force=true` 跳过“正在跑一轮”的检查），`POST /api/restart-sessions`，body `{"tool": "claude"}` 或 `{"tool": "codex"}`。
- **已结束的 session**：侧栏行 hover 或抽屉里的 **Resume**，恢复时就用上当前安装的版本。CLI 退出或恢复失败时窗格退回一个交互 shell，tmux 和记录都还在。
- **整机重启**：Hub 启动 2 秒后自动把 tmux 已消失的 active/idle session 逐个恢复（间隔 0.5 s）。`AGENT_HUB_RESTORE_MODE=shell` 可改为只重建 tmux、打印命令、由你手动决定。
- Hub 服务自身重启不影响任何 session（tmux 都还在）。
- 抽屉里的 "Resume with" 一行显示将要运行的命令；`GET /api/sessions/{id}` 的 `resume_command` 字段是同一个值。
- 第二个 Claude 账号：把它的 `CLAUDE_CONFIG_DIR` 目录写在 `AGENT_HUB_CLAUDE2_CONFIG_DIR`（默认 `~/.claude-alt`）。目录存在时 New session 多出 "Claude Code (account 2)"，这类 session 的行带 `account 2` 标记，恢复和 fork 都会带上同一个变量。该目录里的 `settings.json` 也要配好 Hub 的 hook，否则会话不会出现在 Hub 里。

## 手机接口

手机不在电脑前，只需要知道哪个 session 需要你，并能直接回复。下面这组接口为此而设。

- `GET /api/stream`：Server-Sent Events。连接后先发 `snapshot`（接口版本 `api`、所有运行中的 tmux session 的状态，以及一小时内的 notice），之后只在某个状态变化时发 `session`，session 结束或被删除时发 `removed`，`POST /api/notify` 的消息作为 `notice` 发出；每 20 秒发一行注释，两端都能发现断开的连接。`GET /api/attention` 以一次 JSON 返回同样的快照。
- 状态里的 `unseen` 表示上一轮结束之后还没人看过，`needs_you` 再加上等待审批的。一轮结束由 `Stop` hook 记录，hook 自带的 `last_assistant_message`（Claude 与 Codex 都有）就是 `last_message`。子代理和不在 tmux 里的 session 不在其中。
- 以下情况算看过：手机打开这个 session（`POST /api/sessions/{id}/seen`）、从手机发出消息，或这个 session 的 tmux 终端在这一轮结束后收到输入。人在电脑前操作这个终端，手机不会被打扰；人走开了就没有输入，手机照常收到通知。
- `POST /api/sessions/{id}/send`，body `{"text": "...", "submit": true}`：把文字以 bracketed paste 一次粘贴进 agent 所在的 pane，再按 Enter，多行文字是一条 prompt。正在等待审批（菜单里粘贴的字母可能选中选项），或 tmux 里的 agent 已经退出、只剩 shell（文字会被当成命令执行）时，返回 409。
- `POST /api/sessions/{id}/interrupt`：在 agent 的 pane 里按 Esc，停止正在运行的这一轮，或拒绝正在等待的审批。2 秒内的第二次会被忽略，因为 Claude Code 连按两次 Esc 会打开回到之前消息的菜单。
- `GET /api/sessions/{id}/messages?limit=30`：从 Claude / Codex 自己的 transcript 读出最近的消息（用户、助手、一行一个的工具调用）。
- `POST /api/notify`，body `{"message": "...", "title": "..."}`：脚本给手机发一条消息，例如长任务跑完时。
- 接口版本：`snapshot` 里的 `api`，现在是 `1`。只有会让已有客户端出错的改动才升版本；新增字段或新增接口不升。手机 App 据此判断这个 Hub 能不能用。

## 常用页面

- `/`：工作台。侧栏按项目分组，每个项目下依次列出等待审批、活跃、空闲、已结束的 session 以及该目录下未被跟踪的 tmux；右侧是工作集标签与嵌入式终端，每个打开的标签各自保持一条终端连接，切换标签不重连；session 详情与重命名在右侧抽屉里
- `/?session={id}`：直接打开某个 session 的终端（手机上进入全屏 Terminal 模式）
- `/m`：手机页面。上面是需要你处理的 session（等待审批的、一轮结束后还没看过的）。点进去默认是 Terminal：原生的 Claude Code / Codex TUI；可切换到 Text，以文字查看最近一轮的消息。两种视图下都能用手机键盘回复（文字粘贴进 TUI）、审批或拒绝。`/m#/s/{id}` 直接打开某个 session
- `/sessions/{id}`：单会话完整页（抽屉的降级页，含完整事件列表）
- `/agent`：Monitor 只读助手
- `/idle`、`/stopped`、`/tmux`：保留为跳转，回到工作台

## 常用接口

- `POST /api/events`
- `GET /api/sessions`
- `GET /api/sessions/{id}`
- `POST /api/sessions/{id}/approve`
- `POST /api/sessions/{id}/send`
- `GET /api/stats`
- `GET /api/stream`
- `WS /ws`
- `GET /mcp/sse`

## 文档导航

- `DESIGN.md`：系统框架、运行拓扑、接口分层与核心设计
- `SETUP.md`：安装、启动与 hook 配置
- `ROADMAP.md`：开发路线记录

## 开发说明

项目主代码位于 `src/agent_hub/`，测试位于 `tests/`。

如需理解系统架构、模块分层、tmux 设计、状态机与接口关系，请直接阅读 `DESIGN.md`。

## License

[MIT](LICENSE)
