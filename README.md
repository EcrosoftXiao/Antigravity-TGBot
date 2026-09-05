# Antigravity Telegram Remote Controller

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Antigravity](https://img.shields.io/badge/Antigravity-Native%20Local%20Bridge-brightgreen.svg)](https://antigravity.google)

将你本地运行的 **Antigravity Agent** 桥接至 **Telegram Bot**，让手机或远程设备成为你在外遥控本机 Agent 编写代码、执行终端指令与探索工程的遥控器。

---

## 核心特性

1. **纯本地桥接，零 Gemini API Key 鉴权：**
   - 直连本机运行的 Antigravity 宿主进程与内置 `agentapi` 命令行工具。
   - 自动复用本地 IDE 登录凭证与权限沙箱，**无需申请或配置任何 Gemini API Key**。
2. **对标 `dsh-im` 的完整机器人交互命令：**
   - 丰富会话管理（`/new`、`/session`、`/sessions`、`/status`、`/history`）。
   - 本地工作区与模型档位动态切换（`/workspace`、`/model`）。
   - 批量消息聚合模式（`/batch`、`/send`、`/cancel`）。
   - 任务中断与控制（`/stop`）。
3. **高可扩展的多平台适配器架构：**
   - 核心会话与进程监控（`core/`）与通讯平台（`adapters/`）彻底解耦。
   - 预留了统一的 `BaseBotAdapter` 抽象基类，未来可无缝接入 Discord、Slack、飞书、微信等。
4. **实时推理流与工具进度反馈：**
   - 动态监听 `transcript.jsonl` 日志，实时在 Telegram 中推送思考状态（`[THINKING]`）、工具调用状态（`[TOOL]`）及执行结果。
   - 内置智能节流器（ThrottledEditor），严格遵守 Telegram 消息编辑频率限制，避免触发 429 限流。
   - 超过 4096 字符的长消息自动按 Markdown 代码块安全切片分段发送。
5. **原生交互式选项确认与内联键盘 (Interactive Buttons)**：
   - 当 Agent 调用 `ask_question` 遇到分支决策时，在 Telegram 端实时弹出原生内联按钮（Inline Keyboard）。
   - 单选直接点击提交，多选支持勾选状态动态切换（`[ ]` / `[X]`）与批量提交，并提供跳过选项。
   - 兼容在聊天框直接输入数字序号（如 `1` 或 `1, 2`）或自定义文本回复，实现全功能远程闭环。
6. **严密的安全访问控制：**
   - 允许通过 `ALLOWED_USERS` 配置 Telegram 用户 ID 白名单，杜绝未授权人员远程操作你的计算机。

---

## 架构设计

```
/Users/evasi0nxiao/Antigravity-TG/
├── run.sh                         # 一键启停辅助脚本 (支持后台守护进程与日志流)
├── bot.py                         # 主程序启动入口
├── requirements.txt               # 基础运行依赖
├── .env.example                   # 环境变量配置模板
├── .gitignore                     # Git 忽略配置
└── antigravity_bridge/
    ├── core/                      # 核心 Agent 桥接引擎
    │   ├── agent_cli.py           # 原生 agentapi 进程异步调用封装
    │   ├── transcript_monitor.py  # 实时日志/思考/工具事件流监控器
    │   ├── session_manager.py     # 跨平台多会话映射与状态持久化
    │   └── models.py              # 数据结构与事件类型定义
    └── adapters/                  # 平台适配器抽象层 (可扩展多 IM)
        ├── base.py                # IM 平台统一接口定义 (BaseBotAdapter)
        └── telegram/              # Telegram 具体实现
            ├── bot.py             # python-telegram-bot 实例装配
            ├── handlers.py        # 命令与消息事件分发器
            └── formatter.py       # 文本切片、防抖节流与格式化工具
```

---

## 快速开始

### 1. 环境准备
- macOS 或 Linux 系统。
- 已安装并启动 [Antigravity IDE / App](https://antigravity.google)。
- Python 3.10 及以上。

### 2. 获取 Telegram Bot Token
1. 在 Telegram 中找到官方机器人 [@BotFather](https://t.me/BotFather)。
2. 发送 `/newbot`，根据提示创建一个新的机器人并获取 API Token。
3. （推荐）在 Telegram 中向 [@userinfobot](https://t.me/userinfobot) 发送消息，获取你的个人 Telegram User ID。

### 3. 配置环境变量
在项目根目录下复制 `.env.example` 为 `.env` 并填写配置：

```bash
cp .env.example .env
```

编辑 `.env`：
```env
# 1. 必填：你的 Telegram Bot Token
TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrSTUvwxyz"

# 2. 强烈建议：填写你的 Telegram 用户 ID (多个用逗号隔开)，只允许你自己远程控制！
ALLOWED_USERS="12345678"

# 3. 选填：新会话的默认模型等级 (flash_lite | flash | pro)
DEFAULT_MODEL="flash"

# 4. 选填：默认工作区目录 (留空默认为当前项目目录)
DEFAULT_WORKSPACE="/Users/evasi0nxiao/Antigravity-TG"
```

### 4. 启动机器人

#### 方式 A：前台直接运行（便于调试）
```bash
./run.sh
# 或
python3 bot.py
```

#### 方式 B：后台守护进程运行（推荐）
```bash
./run.sh start     # 启动后台守护进程
./run.sh status    # 查看运行状态
./run.sh logs      # 查看实时输出日志
./run.sh stop      # 停止后台服务
./run.sh restart   # 重启后台服务
```

---

## 指令大全 (Commands Reference)

| 命令 | 参数 | 说明 |
| :--- | :--- | :--- |
| `/start` | 无 | 显示欢迎信息与快速指引 |
| `/help` | 无 | 查看完整命令手册（对标 `dsh-im`） |
| `/new` | `[model] [title]` | 创建新的 Agent 会话并自动绑定到当前对话 |
| `/session` 或 `/s` | `<conversation_id>` | 绑定当前聊天到指定的已有会话 ID |
| `/sessions` 或 `/list` | `[limit]` | 列出本地最近的 Antigravity 会话列表 |
| `/status` | 无 | 查看当前绑定的会话 ID、模型级别与工作区路径 |
| `/models` 或 `/modellist` | 无 | 列出所有可选模型及其序号、说明与当前选择状态 |
| `/model` | `[序号\|名称]` | 切换模型，支持序号（如 `/model 1`）或名称（如 `/model pro`） |
| `/workspace` 或 `/ws` | `[path]` | 查看或切换当前会话所指向的本地文件夹绝对路径 |
| `/history` | `[limit]` | 查看当前会话最近的历史交互记录 |
| `/batch` | 无 | 进入批量聚合模式，后续发送的多条消息将暂存到缓冲区 |
| `/send` | 无 | 将缓冲区内所有消息合并为一个完整任务一次性派发给 Agent |
| `/cancel` | 无 | 退出批量模式并清空当前缓冲区 |
| `/stop` | 无 | 停止当前正在生成的回复或任务 |

### 可用模型清单 (Supported Models)

通过 `/models` 可以查看并随时通过 `/model <序号>` 切换当前支持的 7 款模型：

| 序号 | 模型标识 (ID) | 显示名称 | 规格标签 | 特点与适用场景 |
| :---: | :--- | :--- | :--- | :--- |
| **1** | `gemini-3.8-flash` | Gemini 3.8 Flash | `High / Fast` | 默认推荐，新一代多模态旗舰 Flash 模型，高智能、极速响应 |
| **2** | `gemini-3.7-flash` | Gemini 3.7 Flash | `Medium` | 经典稳定高效通用模型，各项综合指标优秀 |
| **3** | `gemini-3.6-flash` | Gemini 3.6 Flash | `Medium / Fast` | 极速轻量，开销最低，适合日常快速探索与问答 |
| **4** | `gemini-3.1-pro` | Gemini 3.1 Pro | `Low / Reasoning` | 专业级深层推理模型，适合大型架构与高难度逻辑任务 |
| **5** | `claude-sonnet-4.6` | Claude Sonnet 4.6 | `Thinking` | Anthropic 思考增强模型，超强代码生成与复杂算法推理 |
| **6** | `claude-opus-4.6` | Claude Opus 4.6 | `Thinking` | Anthropic 顶级超旗舰模型，处理极度复杂的工程任务 |
| **7** | `gpt-oss-120b` | GPT-OSS 120B | `Medium` | 开源大参数模型，兼具高容量与中等推理能力 |

> **日常对话**：在未输入斜杠指令时直接发送普通文本，Bot 会自动转发给当前绑定的 Antigravity 会话（若尚未绑定则自动开启新会话），并实时在 Telegram 中更新 Agent 的思考过程与工具调用！

---

## 扩展其他 IM 平台 (Extensibility)

如需增加其他平台支持（如 Discord、Slack、飞书）：
1. 继承 `antigravity_bridge/adapters/base.py` 中的 `BaseBotAdapter` 基类。
2. 实现 `start()`、`stop()` 及消息发送方法。
3. 平台接收到消息后，直接调用 `self.agent_cli.send_message()` 或 `self.agent_cli.new_conversation()`，并通过 `self.monitor.stream_events()` 获取实时状态。

---

## 安全提示

由于 Antigravity Agent 在本机具备强大的执行能力（读写文件、终端运行命令等），**请务必在 `.env` 中设置 `ALLOWED_USERS`**，切勿将未设权限过滤的 Bot Token 暴露在公开群组或开放给陌生人。
