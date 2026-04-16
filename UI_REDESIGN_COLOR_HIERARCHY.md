# Agent Hub UI 改版方案

本文只覆盖两件事：配色系统与页面层级。

## 1. 配色方案

### 1.1 视觉方向

- 方向关键词：`Graphite Ops Console`
- 目标气质：克制、稳定、低噪音、高信噪比
- 设计原则：品牌色只做导航和选中态，状态色只表达状态，不再混用粉、紫、银、红多套视觉语言

### 1.2 色板

| Token | Hex | 用途 |
| --- | --- | --- |
| `bg-canvas` | `#0B0F14` | 页面总背景 |
| `bg-panel` | `#121821` | 主卡片、主面板 |
| `bg-panel-2` | `#18212B` | 次级面板、hover、preview header |
| `bg-panel-3` | `#1F2A36` | 强调区块、选中块底色 |
| `border-default` | `#273242` | 默认边框 |
| `border-strong` | `#344256` | 分组边框、选中边框底层 |
| `text-primary` | `#E6EDF5` | 主文本 |
| `text-secondary` | `#A5B3C2` | 次文本 |
| `text-muted` | `#6B7A8C` | 弱信息、说明、时间 |
| `brand` | `#5CC8FF` | 主导航、选中态、主 CTA |
| `brand-strong` | `#2EAADC` | hover / active 强化 |
| `state-active` | `#35C47C` | Active |
| `state-running` | `#67B7FF` | Running |
| `state-waiting` | `#F3B94E` | Waiting / pending approval |
| `state-idle` | `#7F93AB` | Idle |
| `state-stopped` | `#5B6572` | Stopped |
| `state-danger` | `#E06C75` | Delete / Kill / destructive |
| `state-approve-scope` | `#4EC98F` | Always / scoped allow |

### 1.3 用色规则

- 页面背景统一使用 `bg-canvas`，不再使用紫调大底。
- 所有卡片与面板统一落在 `bg-panel` / `bg-panel-2`，只靠亮度层级区分，不靠多颜色区分。
- `brand` 只用于：当前页面、当前 tab、当前选中 session、主按钮、focus ring。
- `state-active` / `state-running` / `state-waiting` / `state-idle` / `state-stopped` 只用于状态点、状态 badge、状态边框高亮。
- `state-danger` 只用于删除、清理、kill，不进入普通导航和主操作。
- `state-approve-scope` 只用于 `Always` 相关文案和按钮，不与普通成功态混用。
- 去掉现有粉色金属按钮、紫色主色、银色详情按钮、深红终端按钮并存的混搭风格，统一成一套中性底 + 单品牌色 + 状态色体系。

### 1.4 组件配色建议

- 主导航：`bg-canvas`，当前页文字或下边线使用 `brand`。
- 卡片默认态：`bg-panel + border-default`。
- 卡片 hover：`bg-panel-2`。
- 卡片选中态：`bg-panel-2 + border-strong + outer ring brand`。
- 主按钮：`brand` 实底。
- 次按钮：`bg-panel-2 + border-default`。
- 危险按钮：`state-danger` 弱实底。
- Waiting badge：`state-waiting` 弱底 + 强文字。
- Running badge：`state-running` 弱底 + 强文字。
- Always badge / label：`state-approve-scope` 弱底或弱文字。

## 2. 层级方案

### 2.1 全局层级

1. 全局导航与主操作
2. 当前最需要处理的内容
3. 当前正在发生什么
4. 可回来的内容
5. 历史与归档内容

对应到产品：

1. `/` 顶部导航、筛选、`+ New`
2. `Waiting` / `Needs Action`
3. `Active` / `Running` 的实时证据
4. `Pinned Idle`
5. `/idle`、`/stopped`、`/tmux`

### 2.2 Dashboard 层级

#### 左侧主列

1. 顶部控制条
2. 待处理区
3. 活跃证据区
4. 已固定 Idle 区

说明：

- 顶部控制条只放全局动作：导航、筛选、排序、`+ New`
- 待处理区单独成组，权重高于普通 active list
- 活跃证据区不只是列出 active session，还要让用户一眼看到“它刚刚做了什么”
- 已固定 idle 区放在 active 之后，不与 active 混排

#### 右侧主工作区

1. 当前选中 session 标题
2. 当前状态摘要
3. Terminal / Preview 主体

说明：

- 右侧是工作区，不是装饰区
- 一旦选中某个 session，右侧优先表达“你现在正在看谁”
- `name > status > cwd/model > terminal`

### 2.3 Session Card 层级

每张卡片按四层组织：

1. 身份层
2. 当前动作层
3. 证据层
4. 操作层

具体内容：

- 身份层：tool icon、session name、来源标记、pin、主状态点
- 当前动作层：`waiting detail`、`running detail`、当前命令、当前审批范围，这是卡片里最重要的一行
- 证据层：最近 2 到 4 条事件，不再只是弱化附属信息，而是卡片核心内容之一
- 操作层：按钮区

证据层强调规则：

- 证据层必须直接回答“这个 agent 刚刚做了什么”
- 优先显示具体动作和命令，而不是抽象状态词
- `Bash ls -la`、`Read config.py`、`MCP: state_get_status` 这类信息优先级高于时间戳
- 时间戳存在，但降级为辅助信息
- 活跃 session 的“最新一条事件”可以使用更高对比度文字
- 新事件进入时应有非常轻的更新反馈，让用户感知“它还活着”

按钮优先级：

1. `Approve`
2. `Always`
3. `Terminal`
4. `Detail`

规则：

- 当存在 pending approval 时，`Approve` 必须成为唯一主按钮
- `Always` 是次主按钮，但要紧跟 `Approve`
- `Terminal` 与 `Detail` 退到辅助层
- 没有 pending 时，`Terminal` 升为主按钮，`Detail` 为次按钮

额外规则：

- 不要把证据层压缩成一行灰字
- 卡片可以比现在略高一点，也要保证证据层至少有稳定的 2 行可视空间
- 如果一个 session 正在 running 或 waiting，证据层视觉权重应该高于按钮区

### 2.4 Session Detail 层级

1. Session 状态头
2. 当前动作与命令
3. 快捷操作
4. 关键元信息
5. 时间线

说明：

- 页面顶部先回答“这个 session 现在是什么状态”
- 然后立刻回答“它正在做什么”
- 再给操作：`Approve`、`Terminal`
- 再给 metadata：cwd、model、session id、hub
- 最后才是完整事件流

### 2.5 Tmux Hub 层级

1. 新建 tmux
2. 可直接接管的活 tmux
3. 已附着 / 空闲 tmux
4. dead tmux

规则：

- 活的、可操作的 tmux 永远在前
- dead tmux 永远在后，并使用危险色收口
- Tmux Hub 的信息层级要明显弱于 Dashboard，不抢主产品入口

## 3. 页面优先级落地

### `/`

- 主页面，承担实时监控与审批
- 颜色对比最强，层级最明确

### `/sessions/{id}`

- 次主页面，承担单会话排查
- 比 dashboard 更强调内容连续性，少强调全局导航

### `/tmux`

- 辅助页面，承担底层会话管理
- 视觉压强低于 dashboard

### `/idle` 与 `/stopped`

- 归档页面
- 降低颜色饱和度，只保留必要状态色和危险操作色

## 4. 一句话结论

这次 UI 改版应从“多风格功能页”收敛成“一套深色控制台系统”：底色中性化，品牌色单一化，状态色语义化，层级上始终把 `Needs Action > Live Evidence > Active > Idle > History` 放在最前。
