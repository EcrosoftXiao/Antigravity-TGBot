"""Telegram command and message handlers for Antigravity Agent bridge."""

import asyncio
import logging
import os
import re
from typing import List, Optional, Set
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from antigravity_bridge.core.agent_cli import AgentCliBridge
from antigravity_bridge.core.models import (
    ContentEvent,
    ErrorEvent,
    ModelTier,
    AVAILABLE_MODELS,
    get_model_by_identifier,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnCompleteEvent,
)
from antigravity_bridge.core.session_manager import SessionManager
from antigravity_bridge.core.transcript_monitor import TranscriptMonitor
from .formatter import ThrottledEditor, split_message

logger = logging.getLogger(__name__)


class TelegramHandlers:
    """Encapsulates all command and message handlers for Telegram."""

    def __init__(
        self,
        agent_cli: AgentCliBridge,
        monitor: TranscriptMonitor,
        session_mgr: SessionManager,
        allowed_users: Optional[Set[int]] = None,
        default_model: str = "flash",
        default_workspace: Optional[str] = None,
    ):
        self.agent_cli = agent_cli
        self.monitor = monitor
        self.session_mgr = session_mgr
        self.allowed_users = allowed_users or set()
        self.default_model = default_model
        self.default_workspace = default_workspace or os.getcwd()

    def is_authorized(self, update: Update) -> bool:
        """Verify if the sender is authorized to control the machine."""
        if not self.allowed_users:
            return True
        user = update.effective_user
        if not user or user.id not in self.allowed_users:
            logger.warning(
                f"Unauthorized access attempt from user: {user.id if user else 'Unknown'} "
                f"(@{user.username if user else 'none'})"
            )
            return False
        return True

    async def _check_auth(self, update: Update) -> bool:
        if not self.is_authorized(update):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "⛔ *访问被拒绝*：您尚未被授权控制此 Antigravity 本地代理。\n"
                    f"您的 Telegram 用户 ID 为：`{update.effective_user.id}`\n\n"
                    "💡 若需授权，请在服务端的 `.env` 文件中将此 ID 添加到 `ALLOWED_USERS` 配置项中。",
                    parse_mode=ParseMode.MARKDOWN,
                )
            return False
        return True

    # ------------------------------------------------------------------
    # Command: /start & /help
    # ------------------------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)
        if not session.workspace:
            session.workspace = self.default_workspace
            session.model = self.default_model
            self.session_mgr.save()

        welcome_text = (
            "🛸 *欢迎使用 Antigravity 远程遥控器*\n\n"
            "本机器人直接桥接至你本机运行的 **Antigravity Agent** 核心环境。\n"
            "无需配置云端 API Key — 所有操作均通过你本机的原生 Agent 执行！\n\n"
            "📌 *常用快捷指令*：\n"
            "• `/new` — 开启新的本地会话\n"
            "• `/sessions` — 列出本地历史会话\n"
            "• `/session <ID>` — 绑定当前聊天至指定会话\n"
            "• `/status` — 查看当前会话、模型与工作区状态\n"
            "• `/models` — 查看所有可用模型列表及序号\n"
            "• `/model <序号|名称>` — 切换模型（如 `/model 1` 或 `/model pro`）\n"
            "• `/workspace <路径>` — 查看或切换本地工程工作区目录\n"
            "• `/batch` — 开启批量消息暂存模式\n"
            "• `/help` — 查看完整的机器人指令手册\n\n"
            "💡 *提示：直接发送任意文本即可与你的本地 Antigravity Agent 对话！*"
        )
        await update.effective_message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        help_text = (
            "🛠 *Antigravity 远程遥控指令手册*（对标 dsh-im）：\n\n"
            "🚀 *会话管理*\n"
            "• `/new` : 开启全新会话（重置会话绑定，下次发送消息将在新会话中执行）\n"
            "• `/new <序号|模型名称>` : 切换模型并开启全新会话\n"
            "• `/session <ID>` (或 `/s`) : 绑定当前聊天至指定的已有会话\n"
            "• `/sessions` (或 `/list`) : 列出本地最近的 Antigravity 会话列表\n"
            "• `/history [条数]` : 查看当前活动会话最近的历史交互记录\n"
            "• `/status` : 查看当前会话、模型级别、工作区与运行状态\n\n"
            "🧠 *模型与工作区*\n"
            "• `/models` : 查看所有支持的模型列表与对应序号\n"
            "• `/model` : 查看当前正在使用的模型信息\n"
            "• `/model <序号|名称>` : 按序号（如 `/model 1`）或名称（如 `/model sonnet`）切换模型\n"
            "• `/workspace` (或 `/ws`) : 查看当前操作的本地工程目录\n"
            "• `/workspace <路径>` : 切换本地工程工作区绝对路径\n\n"
            "📦 *批量任务提交*\n"
            "• `/batch` : 进入批量模式，后续输入的消息将暂存入缓冲区\n"
            "• `/send` : 一次性将缓冲区内所有消息合并提交给 Agent 执行\n"
            "• `/cancel` : 清空缓冲区并退出批量模式\n\n"
            "🛑 *任务控制*\n"
            "• `/stop` : 中断或请求停止当前正在执行的任务\n"
        )
        await update.effective_message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

    # ------------------------------------------------------------------
    # Command: /new [model]
    # ------------------------------------------------------------------
    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)

        model = session.model or self.default_model

        if context.args:
            target_arg = context.args[0].strip().lower()
            opt = get_model_by_identifier(target_arg)
            if opt:
                model = opt.id
                self.session_mgr.set_model(chat_id, model)

        # Clear active conversation binding locally
        self.session_mgr.clear_conversation(chat_id)

        opt = get_model_by_identifier(model)
        model_display = opt.display_name if opt else model
        current_ws = session.workspace or self.default_workspace

        reply = (
            f"✨ *已就绪！下次发送消息将开启全新会话*\n\n"
            f"• *当前使用模型*：*{model_display}*\n"
            f"• *当前工程工作区*：`{current_ws}`\n\n"
            f"💡 请直接发送你的指令或问题，Agent 将在此工作区中创建全新会话并开始工作。"
        )
        await update.effective_message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

    # ------------------------------------------------------------------
    # Command: /session <id> & /sessions
    # ------------------------------------------------------------------
    async def cmd_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        if not context.args:
            session = self.session_mgr.get_session(chat_id)
            curr = session.active_conversation_id
            if curr:
                await update.effective_message.reply_text(
                    f"当前绑定的会话：`{curr}`\n使用 `/session <会话ID>` 切换其他会话。",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await update.effective_message.reply_text(
                    "当前未绑定任何会话。请使用 `/new` 创建新会话或 `/session <会话ID>` 进行绑定。",
                    parse_mode=ParseMode.MARKDOWN,
                )
            return

        target_id = context.args[0].strip()
        try:
            # Validate conversation exists
            await self.agent_cli.get_metadata(target_id)
            self.session_mgr.bind_conversation(chat_id, target_id)
            await update.effective_message.reply_text(
                f"🔗 已成功绑定到会话：`{target_id}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            await update.effective_message.reply_text(
                f"❌ 绑定会话 `{target_id}` 失败：{exc}",
                parse_mode=ParseMode.MARKDOWN,
            )

    async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        limit = 10
        if context.args and context.args[0].isdigit():
            limit = min(int(context.args[0]), 30)

        status_msg = await update.effective_message.reply_text("🔍 正在扫描本地 Antigravity 会话记录...")
        try:
            convs = await self.agent_cli.list_conversations(limit=limit)
            if not convs:
                await status_msg.edit_text("未在 `~/.gemini/antigravity/brain` 中找到现有会话记录。")
                return

            chat_id = update.effective_chat.id
            active_id = self.session_mgr.get_session(chat_id).active_conversation_id

            lines = ["📋 *最近的 Antigravity 本地会话列表：*\n"]
            for i, c in enumerate(convs, 1):
                marker = "⭐ " if c.conversation_id == active_id else "• "
                time_str = c.created_at.replace("T", " ")[:19] if c.created_at else ""
                lines.append(
                    f"{marker}*#{i}* `{c.conversation_id}`\n"
                    f"   🕒 _{time_str}_ | 💬 {c.title}\n"
                )

            lines.append("发送 `/session <会话ID>` 即可快速绑定。")
            msg_text = "\n".join(lines)
            await status_msg.edit_text(msg_text, parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            logger.exception("Error listing sessions")
            await status_msg.edit_text(f"❌ 获取会话列表失败：`{exc}`", parse_mode=ParseMode.MARKDOWN)

    # ------------------------------------------------------------------
    # Command: /history [limit]
    # ------------------------------------------------------------------
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)
        conv_id = session.active_conversation_id

        if not conv_id:
            await update.effective_message.reply_text(
                "❌ 当前未绑定任何活动会话。\n"
                "• 发送 `/sessions` 可查看并绑定历史会话\n"
                "• 或直接发送任意文本即可开启全新会话",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        limit = 3
        if context.args and context.args[0].isdigit():
            limit = min(max(int(context.args[0]), 1), 10)

        status_msg = await update.effective_message.reply_text("🔍 正在读取当前会话历史记录...")

        try:
            history = await self.agent_cli.get_conversation_history(conv_id, limit=limit)
            if not history:
                await status_msg.edit_text(
                    f"ℹ️ 会话 `{conv_id}` 暂无交互记录（或尚未生成有效回复）。",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            lines = [f"📜 *会话历史交互记录* (`{conv_id[:8]}...`，最近 {len(history)} 轮)：\n"]
            for i, (user_req, agent_resp) in enumerate(history, 1):
                clean_user = user_req.strip().replace("\n", " ")
                if len(clean_user) > 120:
                    clean_user = clean_user[:120] + "..."
                clean_resp = agent_resp.strip().replace("\n", " ")
                if len(clean_resp) > 200:
                    clean_resp = clean_resp[:200] + "..."

                lines.append(f"*#{i}* 👤 *用户*：\n{clean_user}\n🤖 *Agent*：\n{clean_resp}\n")

            lines.append("💡 发送 `/history <条数>`（如 `/history 5`）可查看更多历史轮次。")
            await status_msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            logger.exception("Failed to fetch conversation history")
            await status_msg.edit_text(f"❌ 读取会话历史记录失败：`{exc}`", parse_mode=ParseMode.MARKDOWN)

    # ------------------------------------------------------------------
    # Command: /status
    # ------------------------------------------------------------------
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)

        conv_id = session.active_conversation_id or "_(暂无 - 发送任意消息可自动创建)_"
        model = session.model or self.default_model
        ws = session.workspace or self.default_workspace
        batch = f"已开启 (已暂存 {len(session.batch_buffer)} 条)" if session.batch_mode else "未开启"

        opt = get_model_by_identifier(session.model or self.default_model)
        model_display = f"#{opt.index} {opt.display_name} [{opt.badge}]" if opt else (session.model or self.default_model)
        text = (
            "📊 *Antigravity 远程遥控桥接状态*\n\n"
            f"• *当前活动会话*：`{conv_id}`\n"
            f"• *当前使用模型*：*{model_display}*\n"
            f"• *本地工程目录*：`{ws}`\n"
            f"• *批量暂存模式*：{batch}\n"
            f"• *底层执行程序*：`{self.agent_cli.agentapi_cmd[0]}`"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    # ------------------------------------------------------------------
    # Command: /models & /model [number|name]
    # ------------------------------------------------------------------
    async def cmd_models(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)
        current_model = session.model or self.default_model

        lines = ["🧠 *Antigravity Models (当前支持的所有模型):*\n"]
        for opt in AVAILABLE_MODELS:
            is_selected = (opt.id == current_model or opt.tier == current_model or str(opt.index) == current_model)
            prefix = "⭐ " if is_selected else "• "
            selected_tag = " *(当前已选中)*" if is_selected else ""
            lines.append(
                f"{prefix}*#{opt.index}* `{opt.id}` — *{opt.display_name}* `[{opt.badge}]`{selected_tag}\n"
                f"   _{opt.description}_\n"
            )

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(
            "💡 *选择模型指令:*\n"
            "• *按序号选择*：发送 `/model 1` 到 `/model 7`\n"
            "• *按名称选择*：发送 `/model sonnet`、`/model 3.8` 等"
        )
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)

        if not context.args:
            # When no arguments are passed, show available models and current selection
            await self.cmd_models(update, context)
            return

        target_arg = context.args[0].strip()
        matched_opt = get_model_by_identifier(target_arg)

        if not matched_opt:
            valid_list = ", ".join([f"`{m.index}` ({m.display_name})" for m in AVAILABLE_MODELS])
            await update.effective_message.reply_text(
                f"❌ 未知模型或序号: `{target_arg}`\n\n"
                f"可选序号范围: 1 ~ {len(AVAILABLE_MODELS)} ({valid_list})\n"
                "发送 `/models` 查看完整的模型列表与说明。",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        self.session_mgr.set_model(chat_id, matched_opt.id)
        reply = (
            f"✅ *已成功切换模型！*\n\n"
            f"• *序号*：`#{matched_opt.index}`\n"
            f"• *模型*：*{matched_opt.display_name}*\n"
            f"• *规格*：`{matched_opt.badge}` (映射底座：`{matched_opt.tier}`)\n"
            f"• *说明*：_{matched_opt.description}_\n\n"
            f"💡 新建会话（`/new`）将使用此模型进行驱动。"
        )
        await update.effective_message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

    # ------------------------------------------------------------------
    # Command: /workspace [path]
    # ------------------------------------------------------------------
    async def cmd_workspace(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)

        if not context.args:
            current = session.workspace or self.default_workspace
            active_conv = session.active_conversation_id or "_(暂无活动会话)_"
            await update.effective_message.reply_text(
                f"📂 *当前本地工程工作区*：`{current}`\n"
                f"🔗 *当前活动会话*：`{active_conv}`\n\n"
                "💡 *用法说明*：发送 `/workspace <本地绝对路径>` 可切换至其他工程目录。切换后发送消息将在新工作区开启新会话。",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        target_path = os.path.expanduser(context.args[0].strip())
        target_path = os.path.abspath(target_path)

        if not os.path.isdir(target_path):
            await update.effective_message.reply_text(
                f"❌ 目录不存在：`{target_path}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        self.session_mgr.set_workspace(chat_id, target_path)
        # Unbind old conversation when switching workspace
        self.session_mgr.clear_conversation(chat_id)

        reply = (
            f"📂 *本地工程工作区已切换！*\n\n"
            f"• *当前工程工作区*：`{target_path}`\n"
            f"• *会话状态*：已就绪（旧会话已解绑）\n\n"
            f"💡 请直接发送你的指令或问题，Agent 将在此工作区中创建全新会话并开始工作。"
        )
        await update.effective_message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

    # ------------------------------------------------------------------
    # Batch Mode: /batch, /send, /cancel
    # ------------------------------------------------------------------
    async def cmd_batch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        self.session_mgr.start_batch_mode(chat_id)
        await update.effective_message.reply_text(
            "📦 *批量暂存模式已开启！*\n\n"
            "接下来发送的所有消息都会先存入暂存区。\n"
            "准备好后，发送 `/send` 即可合并为一条任务派发给 Agent，或发送 `/cancel` 取消并退出。",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)

        if not session.batch_mode or not session.batch_buffer:
            await update.effective_message.reply_text(
                "⚠️ 批量暂存区为空。请先发送要暂存的内容或使用 `/batch`。",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        buffered_messages = self.session_mgr.flush_batch_mode(chat_id)
        combined_prompt = "\n\n".join(buffered_messages)

        await update.effective_message.reply_text(
            f"🚀 正在将暂存的 {len(buffered_messages)} 条消息合并为单一任务派发给 Agent...",
            parse_mode=ParseMode.MARKDOWN,
        )
        await self._dispatch_agent_prompt(update, combined_prompt)

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        count = self.session_mgr.cancel_batch_mode(chat_id)
        await update.effective_message.reply_text(
            f"🚫 批量模式已取消，已清空并丢弃 {count} 条暂存消息。",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        await update.effective_message.reply_text(
            "🛑 已请求停止任务，已向本地 Antigravity Agent 发送中断信号。",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ------------------------------------------------------------------
    # Message Dispatcher & Real-time Progress Streaming
    # ------------------------------------------------------------------
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        text = update.effective_message.text
        if not text:
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)

        # Handle batch collection mode
        if session.batch_mode:
            count = self.session_mgr.add_batch_message(chat_id, text)
            await update.effective_message.reply_text(
                f"📝 已暂存消息 #{count}。发送 `/send` 提交执行，或发送 `/cancel` 取消。",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await self._dispatch_agent_prompt(update, text)

    async def _dispatch_agent_prompt(self, update: Update, prompt: str) -> None:
        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)

        conv_id = session.active_conversation_id
        start_step = 0

        status_msg = await update.effective_message.reply_text(
            "⏳ *正在连接本地 Antigravity Agent...*",
            parse_mode=ParseMode.MARKDOWN,
        )
        editor = ThrottledEditor(status_msg, min_interval=1.2)

        try:
            # Auto-create conversation if none exists
            if not conv_id:
                await editor.edit("🔄 正在初始化新的 Antigravity 会话...")
                conv_id = await self.agent_cli.new_conversation(
                    prompt=prompt,
                    model=session.model or self.default_model,
                    cwd=session.workspace or self.default_workspace,
                )
                self.session_mgr.bind_conversation(chat_id, conv_id)
                start_step = 0
            else:
                # Existing conversation: record current max step
                start_step = self.monitor.get_current_max_step(conv_id) + 1
                await editor.edit(f"📤 正在派发任务至会话 `{conv_id[:8]}...`")
                await self.agent_cli.send_message(
                    conversation_id=conv_id,
                    content=prompt,
                )

            # Stream real-time events from transcript.jsonl
            final_response = ""
            current_status = "🤖 正在处理中..."

            async for event in self.monitor.stream_events(conv_id, start_step_index=start_step):
                if isinstance(event, ThinkingEvent):
                    clean_thought = re.sub(r"[*_`\[\]]", "", event.thought).strip().replace("\n", " ")[:80]
                    current_status = f"🧠 *思考中：* _{clean_thought}..._"
                    await editor.edit(current_status)

                elif isinstance(event, ToolCallEvent):
                    tool_name = event.tool_name or "tool"
                    action = event.tool_action or event.tool_summary or ""
                    detail = f" ({action})" if action else ""
                    current_status = f"⚙️ *正在执行工具：* `{tool_name}`{detail}..."
                    await editor.edit(current_status)

                elif isinstance(event, ToolResultEvent):
                    current_status = "🔄 正在处理工具执行结果..."
                    await editor.edit(current_status)

                elif isinstance(event, TurnCompleteEvent):
                    final_response = event.final_content
                    break

                elif isinstance(event, ContentEvent):
                    final_response = event.content

                elif isinstance(event, ErrorEvent):
                    await editor.edit(f"❌ *执行出错：* {event.error_message}", force=True)
                    return

            # Display final agent response
            if final_response:
                chunks = split_message(final_response, max_length=4000)
                # First chunk edits the status message
                success = await editor.edit(chunks[0], force=True)
                if not success:
                    # Fallback to direct reply if editing status message failed
                    try:
                        await update.effective_message.reply_text(
                            chunks[0], parse_mode=ParseMode.MARKDOWN
                        )
                    except Exception:
                        await update.effective_message.reply_text(chunks[0], parse_mode=None)

                # Subsequent chunks sent as new messages
                for chunk in chunks[1:]:
                    try:
                        await update.effective_message.reply_text(
                            chunk, parse_mode=ParseMode.MARKDOWN
                        )
                    except Exception:
                        await update.effective_message.reply_text(chunk, parse_mode=None)
            else:
                await editor.edit("✅ 任务已执行完成（无文本输出内容）。", force=True)

        except Exception as exc:
            logger.exception("Error executing prompt on Antigravity Agent")
            await editor.edit(f"❌ *执行失败：* `{exc}`", force=True)
