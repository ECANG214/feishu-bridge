# Feishu Bridge — 飞书 ⇆ Claude Code 双向桥接

把飞书群聊变成 Claude Code 的执行端，实现「飞书 Bot ↔ Claude Code（终端）」两个执行端共享任务板、双向通信的多 Agent 协作架构。

> 这是「飞书多 Agent 协作系统」的基础设施层：飞书侧的 Bot 与 Claude Code 通过**文件队列协议**互相通信，交叉确认方向与产出质量。

## 核心思路

不依赖 Webhook 回传，而是用**基于文件的收件箱/发件箱协议**解耦两端：

```
飞书群聊 ──WebSocket──▶ bridge.py ──写入──▶ inbox.json
                                              │  (Claude Code 轮询读取)
                                              ▼
                                     Claude Code (终端) 处理任务
                                              │
                                              ▼
                                          outbox.json
                                              │  (bridge.py 每 1s 轮询读取)
飞书群聊 ◀──飞书 API── bridge.py ◀────发送────┘
```

- **收件箱（inbox.json）**：飞书消息进入后，Claude Code 轮询处理
- **发件箱（outbox.json）**：Claude Code 写入回复，bridge.py 轮询后经飞书 API 发送
- **共享任务板（task-board.md）**：两个执行端都可读写，任务在「进行中 / 排队中 / 已完成」三列间流转
- **消息落盘可追溯**：每条消息带 `id` / `timestamp`，发送状态由 outbox 的 `sent` / `sent_at` 标记，可审计排查

## 为什么用文件队列而不是直连

| 方案 | 问题 |
|------|------|
| Webhook 直连 | 需要公网域名/内网穿透，本地开发环境不稳定 |
| 文件队列 | 两端零耦合、消息落盘断电不丢、无需公网；断线重连由飞书 SDK 的 WebSocket 客户端负责 |

## 技术栈

- **Python 3.10+** + `lark-oapi`（飞书开放平台官方 SDK）
- **WebSocket 长连接** 接收消息（`p2_im_message_receive_v1` 事件）
- **飞书开放 API** 发送消息（`im.v1.message.create` / `reply`）
- **原子化文件读写**（先写 `.tmp` 再 `replace`，避免半写状态）
- **去重机制**：`bridge_state.json` 记录已处理的 `message_id`（保留最近 2048 条）

## 消息处理能力

- **多类型支持**：`text` / `post` / `image` / `file` / `audio` / `media` / `sticker`，非文本消息会替换为占位描述（如 `[图片: ...]`）
- **防回显**：自动过滤机器人自身及 `sender_type` 含 `bot` 的消息
- **线程/回复**：`reply_to` 由 `parent_id / thread_id / root_id` 推导，发送时走回复 API
- **收件箱上限**：`MAX_INBOX_SIZE = 500`，超出时丢弃最旧条目
- **一次性模式**：`python bridge.py --once` 仅发送 outbox 待发消息后退出（可配合定时任务）

## 多 Agent 协作：Claude 应答器（responder.py）

`bridge.py` 只做消息搬运，不产生回复；`responder.py` 是这条链路的另一半——它让本机的 Claude Code 以「Claude」的身份在群里作为团队成员工作：被 @ 时读文件回答问题，把结论写回发件箱。

**架构链路：**

```
飞书 ──▶ bridge.py ──▶ inbox.json ──▶ responder.py ──▶ claude -p
                                          │                  │
飞书 ◀── bridge.py ◀── outbox.json ◀──────┴──────────────────┘
```

即：飞书 → `bridge.py` → `inbox.json` → `responder.py` → `claude -p` → `outbox.json` → `bridge.py` → 飞书。

responder 实际执行的是：

```bash
claude -p "<包装后的群消息>" --output-format json --max-turns 8 \
       --allowedTools Read,Grep,Glob --append-system-prompt "<团队上下文>" \
       [--model <模型>] [--resume <session_id>]
```

`--output-format json` 的返回里取 `result` 作为回复正文、`session_id` 用于续接会话。

### 护栏（逐条写在代码里，不是可选项）

| 护栏 | 行为 |
|------|------|
| 只在被 @ 时响应 | 群消息（`chat_type == "group"`）必须命中 `mentions` 中的名字才处理；名字按子串、不区分大小写匹配，默认 `claude`。私聊（非 group）一律处理 |
| 拒绝机器人消息 | `sender_type` 为非空且不是 `user` 时跳过（空值按真人处理），防止两个 bot 互相 @ 刷成死循环。bridge 侧也会先滤掉 `sender_type` 含 `bot` 的消息，这里是不依赖 bridge 的第二道闸 |
| 单线程、按到达顺序 | 一个 `while` 循环顺序遍历 inbox，同一时刻只有一个 `claude` 子进程，无并发 |
| STOP 文件可急停 | 在 `responder.py` 同目录建一个名为 `STOP` 的文件（无扩展名），下一轮轮询即干净退出并先落盘会话状态。启动时若已存在 STOP 会先删除，避免刚重启就退出 |
| 工具默认只读 | `--allowedTools` 默认 `Read,Grep,Glob`。放开写/执行必须显式改 `BRIDGE_CLAUDE_TOOLS`——**群里任何人届时都能驱动这台机器** |
| 可选发送者白名单 | `BRIDGE_ALLOWED_SENDERS` 为逗号分隔的 open_id，留空表示不限制（任何真人）。注意 **open_id 按应用隔离**：同一个人在不同自建应用下的 open_id 不同 |
| 按 chat 续接会话上下文 | 以 `chat_id` 为键把 `session_id` 存入 `responder_state.json`，下一条消息用 `--resume` 接上；`BRIDGE_KEEP_CONTEXT=0` 可关闭 |

补充：

- **失败不静默**：`claude` 超时、找不到可执行文件、返回空结果时，responder 仍会往 outbox 写一条 `（Claude 这边出问题了：<原因>）`，让群里看得见。
- **单条消息炸不掉进程**：每条消息独立 try/except，跳过与异常都记入 `logs/responder.log`。
- **回复走回复接口**：写入 outbox 的条目带 `reply_to`（原 inbox 条目的 `id`），bridge 发送时用飞书的 reply API，所以群里的回复是挂在原消息下的。

### 可选：黑板集成（通过 MCP）

responder 自身不写任何文件。若要让 Claude 能读写团队的共享交接记录，需在本机 Claude Code 里配置一个名为 `blackboard` 的 MCP server（该 server 的具体配置不在本仓库，**见代码 / 你的 MCP 配置**），再把它的工具名追加进 `BRIDGE_CLAUDE_TOOLS`：

```bash
BRIDGE_CLAUDE_TOOLS=Read,Grep,Glob,mcp__blackboard__blackboard_read,mcp__blackboard__blackboard_append,mcp__blackboard__blackboard_log
```

只授予「读取 + 追加 + 记日志」三类工具，**刻意不给整文件覆盖**——群消息能驱动这台机器，覆盖式写入的风险过高，交接记录只允许追加。

### 怎么运行（Windows）

1. 先起 `bridge.py`（负责搬运消息）；或双击 `start-bridge.bat`。
2. 再起 responder。Windows 下推荐用 `start-responder-hidden.vbs`：它以**隐藏窗口、脱离控制台**的方式拉起 `responder.py`，关掉终端不会把它带走。也可以直接 `python responder.py` 前台运行（便于看日志）。
3. **部署前必改**：
   - `start-responder-hidden.vbs` 里的变量值全是**本机占位**（`claude.exe` 全路径、`python.exe` 全路径、工作目录、白名单 open_id），改成你自己的。
   - `responder.py` 的 `TEAM_CONTEXT` 里含有一份团队资料的**本机绝对路径**（形如 `C:\Users\...\team-agent\AGENT-BRIEFING.md`）。**这是部署相关路径，使用前请改成你自己的**；不改也能跑，只是 Claude 查不到那份资料。
4. 重启前建议先排空陈旧队列，否则新起的 responder 会把历史消息当新消息回一遍：

   ```bash
   python reset_bridge_queues.py --dry-run   # 先看要改多少
   python reset_bridge_queues.py             # 把所有已存在条目标为已处理/已发送
   ```

### 环境变量

responder 的全部配置来自**环境变量**（它不读 `config.yaml`）；下表默认值以 `responder.py` 代码为准。

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `BRIDGE_POLL_SECONDS` | 轮询 `inbox.json` 的间隔（秒） | `3` |
| `BRIDGE_CLAUDE_BIN` | `claude` 可执行文件；Windows 下必须填 `.exe` 全路径（`CreateProcess` 解析不了 npm 的 `claude.cmd`） | `claude` |
| `BRIDGE_CLAUDE_MODEL` | 传给 `--model`；置空则不带该参数 | `haiku` |
| `BRIDGE_CLAUDE_TOOLS` | 传给 `--allowedTools` 的工具列表 | `Read,Grep,Glob` |
| `BRIDGE_CLAUDE_MAX_TURNS` | 传给 `--max-turns` | `8` |
| `BRIDGE_CLAUDE_TIMEOUT` | 单次 `claude` 调用的超时（秒） | `240` |
| `BRIDGE_CLAUDE_CWD` | `claude` 子进程的工作目录，决定它能读到哪个项目的文件 | `<work-dir>`（本机路径，部署时必改） |
| `BRIDGE_MENTION_NAMES` | 群内可触发应答的 @ 名，逗号分隔，子串匹配、不区分大小写 | `claude` |
| `BRIDGE_ALLOWED_SENDERS` | 发送者 open_id 白名单，逗号分隔；空 = 不限制 | 空 |
| `BRIDGE_KEEP_CONTEXT` | 是否用 `--resume` 续接同一 chat 的会话；`0` / `false` / `no` 关闭 | `1`（开启） |

## 快速开始

```bash
cd feishu-bridge
pip install -r requirements.txt

# 1. 准备配置
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入飞书自建应用的 app_id / app_secret
#   - 在 https://open.feishu.cn 创建「自建应用」，于「凭证与基础信息」页获取
#   - 若开启了事件订阅的加密策略，还需填 encrypt_key / verification_token

# 2. 启动
python bridge.py
# 或 Windows 下双击 start-bridge.bat（已适配任意 clone 位置，日志输出到 logs/）
```

收件箱 / 发件箱 / 去重状态文件在首次写入时自动生成。`logs/` 目录则**需要已存在**：`start-bridge.bat` 会先创建它，`responder.py` 也会自建；若直接 `python bridge.py`，请先手动 `mkdir logs`，否则启动时打开日志文件会失败。

要让本机的 Claude Code 以「Claude」身份在群里应答，还需要另起 `responder.py`，见上一节「多 Agent 协作」。

## 目录结构

```
feishu-bridge/
├── bridge.py                   # 主程序：WebSocket 收消息 + 轮询发消息
├── responder.py                # Claude 应答器：inbox → claude -p → outbox
├── config.example.yaml         # 配置模板（复制为 config.yaml 填入真实值）
├── requirements.txt
├── task-board.md               # 与 Claude Code 共享的任务板
├── start-bridge.bat            # Windows 一键启动 bridge
├── start-responder-hidden.vbs  # Windows 隐藏窗口启动 responder
├── reset_bridge_queues.py      # 重启前排空陈旧队列（一次性维护脚本）
├── LICENSE
└── README.md
```

运行时自动生成（均被 `.gitignore` 忽略，不入库）：

```
config.yaml            # 真实飞书凭据（敏感）
inbox.json             # 收件箱（飞书 → Claude Code）
outbox.json            # 发件箱（Claude Code → 飞书）
bridge_state.json      # 去重状态（已处理的 message_id）
responder_state.json   # 会话状态（chat_id → session_id）
logs/bridge.log        # bridge 运行日志
logs/responder.log     # responder 运行日志
```

`STOP` 不是自动生成的：需要急停 responder 时，手动在 `responder.py` 同目录建一个名为 `STOP` 的空文件即可。

## 在「飞书多 Agent 协作系统」中的位置

本 bridge 是飞书侧执行端（Claude 执行端）的底座，与 Claude Code 共享 `task-board.md`：

| 角色 | 职责 |
|------|------|
| 主 Bot / 调度中枢 | 总调度、消息路由与任务分发 |
| 知识库策划 | 知识整理与结构化输出 |
| 资源整理 | 资源管理与归类 |
| Claude 执行端 | 任务执行，与本 bridge 协同；由 `responder.py` 以「Claude」身份在群里应答，默认只有只读文件权限 |
| 学习伙伴 | 学习陪伴与进度跟进 |

## License

MIT
