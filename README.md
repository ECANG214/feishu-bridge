# Feishu Bridge — 飞书 ⇆ Claude Code 双向桥接

把飞书群聊变成 Claude Code 的执行端，实现「飞书 Bot ↔ Claude Code（终端）」两个执行端共享任务板、双向通信的多 Agent 协作架构。

> 这是「飞书多 Agent 协作系统」的基础设施层：飞书侧的 Bot（Agent 4「罅光」）与 Claude Code 通过**文件队列协议**互相通信，交叉确认方向与产出质量。

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
                                              │  (bridge.py 轮询读取)
飞书群聊 ◀──飞书 API── bridge.py ◀────发送────┘
```

- **收件箱（inbox.json）**：飞书消息进入后，Claude Code 轮询处理
- **发件箱（outbox.json）**：Claude Code 写入回复，bridge.py 轮询后经飞书 API 发送
- **共享任务板（task-board.md）**：两个执行端都可读写，任务在「进行中 / 排队中 / 已完成」三列间流转

## 为什么用文件队列而不是直连

| 方案 | 问题 |
|------|------|
| Webhook 直连 | 需要公网域名/内网穿透，本地开发环境不稳定 |
| 文件队列 | 两端零耦合、天然可断线重连、可审计（每个消息都有 `id`/`timestamp`/`processed` 标记）|

## 技术栈

- **Python 3.10+** + `lark-oapi`（飞书开放平台官方 SDK）
- **WebSocket 长连接** 接收消息（`p2_im_message_receive_v1` 事件）
- **飞书开放 API** 发送消息（`im.v1.message.create` / `reply`）
- **原子化文件读写**（tmp + replace，避免半写状态）
- **去重机制**：`bridge_state.json` 记录已处理的 `message_id`（保留最近 2048 条）

## 目录结构

```
feishu-bridge/
├── bridge.py            # 主程序：WebSocket 收消息 + 轮询发消息
├── config.example.yaml  # 配置模板（复制为 config.yaml 填入真实值）
├── requirements.txt
├── task-board.md        # 与 Claude Code 共享的任务板
└── start-bridge.bat     # Windows 一键启动脚本
```

运行时自动生成（均被 `.gitignore` 忽略，不入库）：

```
config.yaml         # 真实飞书凭据（敏感）
inbox.json          # 收件箱（飞书 → Claude Code）
outbox.json         # 发件箱（Claude Code → 飞书）
bridge_state.json   # 去重状态（已处理的 message_id）
logs/bridge.log     # 运行日志
```

## 快速开始

```bash
cd feishu-bridge
pip install -r requirements.txt

# 1. 准备配置
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入飞书自建应用的 app_id / app_secret

# 2. 启动
python bridge.py
# 或 Windows 下双击 start-bridge.bat
```

## 在「飞书多 Agent 协作系统」中的位置

本 bridge 是 4 号 Agent「罅光」（飞书执行端）的底座，与 Claude Code 共享 `task-board.md`：

| Agent | 角色 | 职责 |
|-------|------|------|
| 1 号「泉」 | 主 Bot / 调度中枢 | 总调度、消息路由与任务分发 |
| 2 号 | 知识库策划 | 知识整理与结构化输出 |
| 3 号 | 资源整理 | 资源管理与归类 |
| 4 号「罅光」 | Claude 执行端 | 任务执行，与本 bridge 协同 |
| 5 号 | 学习伙伴 | 学习陪伴与进度跟进 |

## License

MIT
