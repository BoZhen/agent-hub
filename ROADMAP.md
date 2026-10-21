# Agent Hub — 系统路线图

> 从"会话监控"进化为"AI 协作编排平台"

## 1. 现状

```
观察层:  Agent Hub (:7800) — 实时监控所有 Claude Code 会话、token 用量、waiting 状态
执行层:  Claude Code / Codex — 在本机执行代码、研究、写作
知识层:  Paper Reader (MCP), Obsidian (笔记), Web Search
通信层:  Zulip (Tailscale 内网), 手机 App (GET /api/stream)
接入层:  Web Terminal (:7683) + tmux (手机操作, 改造中)
```

**核心缺口：系统是被动的。**

Agent Hub 能观察，但不能主动推送通知、不能分配任务、不能异步对话。用户和 agent 之间的交互是同步的——必须盯着终端。

## 2. 目标架构

```
用户 ──(手机 App / Dashboard / Zulip)──▶ 创建任务 / 下发指令
                                              │
                                         Agent Hub
                                        (Task Router)
                                              │
                     ┌───────────────────────┼───────────────────────┐
                     ▼                       ▼                       ▼
             tmux session #1          tmux session #2          tmux session #3
             Claude: 读论文           Codex: 复现代码          Claude: 搜补充文献
                     │                       │                       │
                     └────── progress → Zulip (结构化记录) ──────────┘
                     └────── stuck/complete → 手机通知 → 用户回复 → 继续 ─┘
```

## 3. 服务集成优先级

| 集成 | 价值 | 优先级 | 理由 |
|------|------|--------|------|
| **手机 App** | 双向异步通道 | P0 | 解决"不在电脑前"的通知+指令问题，手机随时可达 |
| **Zulip 深度集成** | 结构化研究日志 | P1 | 已有 MCP，作为 agent 工作过程的持久化记录 |
| **Paper Reader** | 知识输入 | P1 | 文献阅读工作流的核心依赖 |
| **Web Terminal (tmux)** | 直接操作 | P1 | 另一个 Claude 已在改造中 |
| Slack | 移动通知 | P2 | 和手机 App 重叠，除非有团队协作需求 |
| OpenClaw | 待定 | P3 | 取决于其在工作流中的角色 |

## 4. 手机 App — 双向异步通道

Hub 端的接口（见 README「手机接口」）：

- `GET /api/stream`：Server-Sent Events，只推送 session 状态的变化。一轮结束后还没看过的、等待审批的 session 标为 `needs_you`。
- `POST /api/sessions/{id}/send`、`/interrupt`、`/seen` 与 `GET /api/sessions/{id}/messages`：在手机上回复、停止、标记已看、读最近的消息。
- `POST /api/notify`：脚本给手机发消息。

手机 App 是独立的项目：WebView 加载 Hub 提供的手机页面，后台服务保持与 `GET /api/stream` 的连接并发出系统通知，在通知上可以直接回复或审批。

## 5. 文献阅读工作流

### 5.1 问题定义

让 Claude/Codex 深度阅读论文，不是"读一遍就回复"，而是：
1. **精读** — 逐段理解，提取核心主张和方法
2. **验证** — 通过代码复现验证自己对方法的理解
3. **困惑消解** — 遇到不理解的概念，搜索补充文献学习
4. **综合产出** — 核心创新点 + 理解报告 + 困惑点标注

### 5.2 多阶段结构

```
阶段 1: 获取论文
  ├── Paper Reader MCP 获取论文内容
  ├── 识别论文结构（abstract, method, experiments, results）
  └── 输出：论文骨架 + 初步关键词

阶段 2: 精读理解
  ├── 逐节阅读，提取核心主张
  ├── 对每个关键公式/算法，用自己的话解释
  ├── 标记困惑点（不理解的概念/符号/假设）
  └── 输出：逐节理解笔记 + 困惑清单

阶段 3: 困惑消解
  ├── 对每个困惑点，搜索相关文献/教程
  ├── 阅读补充材料直到理解
  ├── 更新理解笔记
  └── 输出：困惑消解记录（原困惑 → 查了什么 → 现在的理解）

阶段 4: 复现验证
  ├── 选择论文中的核心方法/算法
  ├── 从零实现（不看参考代码）
  ├── 与论文结果对比
  ├── 如果偏差大 → 回到阶段 2 重新理解
  └── 输出：复现代码 + 结果对比 + 偏差分析

阶段 5: 综合产出
  ├── 核心创新点总结
  ├── 与现有方法的对比定位
  ├── 局限性和可能的改进方向
  ├── 对自己研究的启发
  └── 输出：结构化阅读报告（发到 Zulip）
```

### 5.3 实现方式

- **Skill 文件** (`~/.claude/skills/read-paper.md`)：定义工作流模板、检查点、输出格式
- **Zulip 日志**：每个阶段完成后自动发消息到对应 stream/topic
- **Agent Hub 监控**：跟踪任务进度，检测卡住
- **手机通知**：关键节点推送（复现失败、需要确认理解、任务完成）
- **多 Session 可能**：阶段 3（搜索文献）和阶段 4（复现代码）可以并行

### 5.4 需要的工具链

| 工具 | 来源 | 用途 |
|------|------|------|
| `search_papers` | Paper Reader / Zulip CLI | 搜索和获取论文 |
| `import_paper` | Zulip CLI | 导入新发现的参考文献 |
| `upload_note` | Zulip CLI | 上传阅读笔记 |
| `send_message` | Zulip CLI | 发布进展到指定 stream |
| Web Search | 内置 | 搜索补充资料 |
| Bash/Write | 内置 | 复现代码 |
| `get_transcript_summary` | Agent Hub MCP | 其他 session 查看进度 |

## 6. Zulip 集成深化

当前已有 Zulip MCP（通过 CLI fallback），但只用于手动交互。深化方向：

### 6.1 Agent 自动发帖

Agent 在工作过程中自动将进展发到 Zulip：

```
Stream: #research
  Topic: "Paper: Attention Is All You Need"
    ├── [Bot] 阶段 1 完成：论文骨架已提取
    ├── [Bot] 阶段 2 进行中：Section 3 Multi-Head Attention 有困惑
    ├── [User] 这里的 scaled dot-product 是为了防止梯度消失
    ├── [Bot] 感谢，已理解。更新笔记。
    ├── [Bot] 阶段 4：复现 self-attention，结果匹配 ✓
    └── [Bot] 任务完成：阅读报告已上传
```

### 6.2 Zulip 作为异步指令通道

用户在 Zulip 中回复 → agent 读取 → 继续工作。
（需要长文本交互时的备选方案）

## 7. Hub 进化路径：Task Router

### 7.1 Task 系统

```sql
CREATE TABLE tasks (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT,
    status        TEXT DEFAULT 'pending',  -- pending/running/blocked/completed/failed
    session_id    TEXT REFERENCES sessions(session_id),
    tmux_session  TEXT,                    -- 对应的 tmux session 名
    created_at    DATETIME,
    completed_at  DATETIME,
    result        TEXT                     -- 任务结果/产出链接
);
```

### 7.2 工作流

```
1. 用户创建任务（Dashboard / 手机 App / Zulip）
2. Hub 分配任务：
   - 创建 tmux session
   - 启动 Claude Code + 加载对应 Skill
   - 注入任务参数
3. Agent 自主工作：
   - 按 Skill 定义的阶段推进
   - 进度自动发到 Zulip
   - 遇到阻塞 → 通知用户（手机通知）
4. 用户干预（可选）：
   - 审批 tool calls
   - 回答 agent 的问题
   - 调整方向
5. 任务完成：
   - 产出物存入指定位置
   - 通知用户
   - 更新 task 状态
```

## 8. 实施顺序

### 立即做
- [x] **Hub 手机接口** — `GET /api/stream` 推送需要处理的 session，回复、停止、已看、最近消息的接口
- [ ] **手机 App** — 系统通知，在通知上回复与审批（独立项目）

### 短期（1-2 周）
- [ ] **Paper Reading Skill** — 写 `~/.claude/skills/read-paper.md` 定义结构化流程
- [ ] **Zulip 自动发帖** — Skill 中嵌入 Zulip 发消息步骤
- [ ] **Web Terminal tmux 改造** — 另一个 Claude 已在进行

### 中期（2-4 周）
- [ ] **Task 系统** — Hub 增加 tasks 表 + API + Dashboard 展示
- [ ] **从手机创建任务** — 例如 `read-paper arxiv:2401.xxxxx`
- [ ] **Multi-session 任务** — 一个 task 拆分给多个 agent 并行处理

### 远期
- [ ] Agent 间通信 — 通过 Hub MCP 互相查询和传递信息
- [ ] 自动复现流水线 — 给定论文自动完成全流程无需人工干预
- [ ] OpenClaw / Slack 按需接入

## 9. 设计原则

- **渐进式**：每个集成独立可用，不依赖其他集成全部完成
- **异步优先**：agent 不应该阻塞等待用户，用户不应该盯着终端
- **结构化记录**：所有工作产出都有持久化存储（Zulip topic / Obsidian note / Hub DB）
- **Mobile-first**：关键操作都能在手机上完成（手机 App + Dashboard + Web Terminal）
- **MCP 统一接口**：所有服务通过 MCP tools 暴露给 Claude，形成工具生态
