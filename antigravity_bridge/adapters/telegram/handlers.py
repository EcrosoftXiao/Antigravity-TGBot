"""Telegram command and message handlers for Antigravity Agent bridge."""

import asyncio
import html
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
        self.pending_questions: Dict[int, Dict[str, Any]] = {}

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
                    f"当前绑定的会话：`{curr}`\n使用 `/session <序号或会话ID>` 切换其他会话，或使用 `/sessions` 查看列表。",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await update.effective_message.reply_text(
                    "当前未绑定任何会话。请使用 `/new` 创建新会话或 `/session <序号或会话ID>` 进行绑定。",
                    parse_mode=ParseMode.MARKDOWN,
                )
            return

        target_arg = context.args[0].strip()
        convs = await self.agent_cli.list_conversations(limit=50)

        clean_num = target_arg.lstrip("#")
        target_id = None
        target_title = ""

        # 1. Check if argument is a numeric index (e.g. "1", "#1")
        if clean_num.isdigit():
            idx = int(clean_num)
            if 1 <= idx <= len(convs):
                chosen = convs[idx - 1]
                target_id = chosen.conversation_id
                target_title = chosen.title
            else:
                await update.effective_message.reply_text(
                    f"⚠️ 序号 `#{clean_num}` 超出范围：当前本地记录共有 {len(convs)} 个会话（请输入 1 ~ {len(convs)}）。\n"
                    f"请先发送 `/sessions` 查看列表。",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
        else:
            # 2. Check if argument matches full ID or unique prefix
            prefix = target_arg.lower()
            matches = [c for c in convs if c.conversation_id.lower().startswith(prefix)]
            if len(matches) == 1:
                target_id = matches[0].conversation_id
                target_title = matches[0].title
            elif len(matches) > 1:
                matched_lines = "\n".join([f"• `{c.conversation_id[:8]}` ({c.title})" for c in matches[:5]])
                await update.effective_message.reply_text(
                    f"⚠️ 匹配到多个以 `{target_arg}` 开头的会话：\n{matched_lines}\n请提供更多字符以精确定位。",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            else:
                target_id = target_arg

        try:
            # Validate conversation existence: check local directory or agentapi
            brain_dir = self.agent_cli.gemini_dir / "brain" / target_id
            if not brain_dir.is_dir():
                # Attempt metadata check through agentapi
                await self.agent_cli.get_metadata(target_id)

            self.session_mgr.bind_conversation(chat_id, target_id)
            title_desc = f"\n💬 _{target_title}_" if target_title else ""
            await update.effective_message.reply_text(
                f"🔗 *已成功绑定到会话：*\n`{target_id}`{title_desc}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            await update.effective_message.reply_text(
                f"❌ 绑定会话 `{target_id}` 失败：未找到该会话记录或会话已失效。\n`{exc}`",
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

            lines.append("💡 发送 `/session <序号>`（如 `/session 1`）或 `/session <会话ID>` 即可快速绑定。")
            msg_text = "\n".join(lines)
            await status_msg.edit_text(msg_text, parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            logger.exception("Error listing sessions")
            await status_msg.edit_text(f"❌ 获取会话列表失败：`{exc}`", parse_mode=ParseMode.MARKDOWN)

    # ------------------------------------------------------------------
    # Command: /history [limit or session_id] [limit]
    # ------------------------------------------------------------------
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)
        conv_id = session.active_conversation_id

        limit = 3
        target_conv_id = conv_id

        # Flexible argument handling: /history 5, /history #1, /history 1, /history #1 5, /history <UUID>
        if context.args:
            first_arg = context.args[0].strip()
            second_arg = context.args[1].strip() if len(context.args) > 1 else None

            # Case 1: Single numeric argument (e.g. /history 5)
            if first_arg.isdigit() and not second_arg:
                val = int(first_arg)
                if target_conv_id:
                    limit = min(max(val, 1), 20)
                else:
                    # If no active session, try resolving as session index
                    convs = await self.agent_cli.list_conversations(limit=30)
                    if 1 <= val <= len(convs):
                        target_conv_id = convs[val - 1].conversation_id
                    limit = 3
            # Case 2: Session index/prefix specified (e.g. /history #1, /history 1 5, /history <prefix>)
            else:
                convs = await self.agent_cli.list_conversations(limit=50)
                clean_num = first_arg.lstrip("#")
                if clean_num.isdigit():
                    idx = int(clean_num)
                    if 1 <= idx <= len(convs):
                        target_conv_id = convs[idx - 1].conversation_id
                else:
                    matches = [c for c in convs if c.conversation_id.lower().startswith(first_arg.lower())]
                    if len(matches) == 1:
                        target_conv_id = matches[0].conversation_id
                    else:
                        target_conv_id = first_arg

                if second_arg and second_arg.isdigit():
                    limit = min(max(int(second_arg), 1), 20)

        if not target_conv_id:
            await update.effective_message.reply_text(
                "❌ 当前未绑定任何活动会话。\n"
                "• 发送 `/sessions` 可查看并绑定历史会话\n"
                "• 发送 `/history <序号>`（如 `/history 1`）可直接查看指定会话历史\n"
                "• 或直接发送任意文本开启全新会话",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        status_msg = await update.effective_message.reply_text(
            f"🔍 正在读取会话 <code>{html.escape(target_conv_id[:8])}...</code> 历史记录...",
            parse_mode=ParseMode.HTML,
        )

        try:
            history = await self.agent_cli.get_conversation_history(target_conv_id, limit=limit)
            if not history:
                await status_msg.edit_text(
                    f"ℹ️ 会话 <code>{html.escape(target_conv_id)}</code> 暂无交互记录（或尚未生成有效回复）。",
                    parse_mode=ParseMode.HTML,
                )
                return

            lines = [
                f"📜 <b>会话历史交互记录</b> (<code>{html.escape(target_conv_id[:8])}...</code>，最近 {len(history)} 轮)：\n"
            ]

            for i, (user_req, agent_resp) in enumerate(history, 1):
                clean_user = re.sub(r"\s+", " ", user_req).strip()
                orig_user_len = len(clean_user)
                is_u_trunc = orig_user_len > 120
                if is_u_trunc:
                    clean_user = clean_user[:120].rstrip() + "..."

                clean_resp = re.sub(r"\s+", " ", agent_resp).strip()
                orig_resp_len = len(clean_resp)
                is_r_trunc = orig_resp_len > 250
                if is_r_trunc:
                    clean_resp = clean_resp[:250].rstrip() + "..."

                u_trunc_tag = f" <i>(共 {orig_user_len} 字，已截断)</i>" if is_u_trunc else ""
                r_trunc_tag = f" <i>(共 {orig_resp_len} 字，已截断)</i>" if is_r_trunc else ""

                lines.append(
                    f"<b>#{i} 👤 用户</b>{u_trunc_tag}：\n"
                    f"{html.escape(clean_user)}\n\n"
                    f"<b>🤖 Agent</b>{r_trunc_tag}：\n"
                    f"{html.escape(clean_resp)}\n"
                    f"────────────────────"
                )

            lines.append("💡 <i>提示：长对话内容已自动截断以保证清晰展示。发送 /history &lt;条数&gt;（如 /history 5）可查看更多轮次。</i>")
            msg_text = "\n".join(lines)

            # Cap message text to fit within Telegram limits
            if len(msg_text) > 3800:
                msg_text = msg_text[:3800] + "\n\n<i>...(历史轮次内容过长，已自动截断输出)...</i>"

            try:
                await status_msg.edit_text(msg_text, parse_mode=ParseMode.HTML)
            except Exception:
                # Fallback to plain text if HTML parsing fails
                plain_text = re.sub(r"<[^>]+>", "", msg_text)
                await status_msg.edit_text(plain_text, parse_mode=None)

        except Exception as exc:
            logger.exception("Failed to fetch conversation history")
            await status_msg.edit_text(f"❌ 读取会话历史记录失败：{exc}", parse_mode=None)

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
        self.pending_questions.pop(chat_id, None)
        count = self.session_mgr.cancel_batch_mode(chat_id)
        await update.effective_message.reply_text(
            f"🚫 批量模式已取消，已清空并丢弃 {count} 条暂存消息。",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        self.pending_questions.pop(chat_id, None)
        await update.effective_message.reply_text(
            "🛑 已请求停止任务，已向本地 Antigravity Agent 发送中断信号。",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ------------------------------------------------------------------
    # Question Interactive Selection Helpers & Callback Query Handler
    # ------------------------------------------------------------------
    def _render_question_content(self, pending: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
        """Format the question prompt text and generate Telegram InlineKeyboardMarkup."""
        questions = pending.get("questions", [])
        selections = pending.get("selections", {})

        is_all_single = all(not q.get("is_multi_select", False) for q in questions)
        is_simple_single = (len(questions) == 1 and is_all_single)

        lines = ["❓ <b>Agent 正在等待你的选项确认：</b>\n"]
        keyboard: List[List[InlineKeyboardButton]] = []

        for q_idx, q in enumerate(questions):
            q_text = q.get("question", "")
            is_multi = q.get("is_multi_select", False)
            type_str = "多选" if is_multi else "单选"
            prefix = (
                f"📌 <b>{html.escape(q_text)}</b> <i>({type_str})</i>"
                if len(questions) == 1
                else f"📌 <b>Q{q_idx+1}: {html.escape(q_text)}</b> <i>({type_str})</i>"
            )
            lines.append(prefix)

            opts = q.get("options", [])
            chosen_set = selections.get(q_idx, set())

            for o_idx, opt in enumerate(opts):
                is_chosen = o_idx in chosen_set
                if is_simple_single:
                    icon = "🔘"
                else:
                    icon = "✅" if is_chosen else "⬜"

                opt_num = o_idx + 1
                opt_display = opt.strip()
                lines.append(f"   {icon} <b>{opt_num}.</b> {html.escape(opt_display)}")

                # Inline button label
                btn_label = f"{opt_num}. {opt_display}"
                if not is_simple_single:
                    btn_label = f"{icon} {btn_label}"

                max_btn_len = 38
                if len(btn_label) > max_btn_len:
                    btn_label = btn_label[: max_btn_len - 1] + "…"

                if is_simple_single:
                    cb_data = f"q_sel:{q_idx}:{o_idx}"
                else:
                    cb_data = f"q_tog:{q_idx}:{o_idx}"

                keyboard.append([InlineKeyboardButton(btn_label, callback_data=cb_data)])

            lines.append("")

        if is_simple_single:
            lines.append("💡 <i>点击下方按钮直接选择，或在聊天框发送序号（如 1）：</i>")
            keyboard.append([InlineKeyboardButton("⏭️ 跳过 (Skip)", callback_data="q_skp")])
        else:
            lines.append("💡 <i>点击选项切换勾选后点击【提交】，或直接输入序号（如 1, 2）：</i>")
            action_row = [
                InlineKeyboardButton("📤 提交选择 (Submit)", callback_data="q_sub"),
                InlineKeyboardButton("⏭️ 跳过 (Skip)", callback_data="q_skp"),
            ]
            keyboard.append(action_row)

        return "\n".join(lines).strip(), InlineKeyboardMarkup(keyboard)

    async def _submit_question_answer(
        self,
        chat_id: int,
        answer_text: str,
        summary_text: str,
    ) -> None:
        """Submit the selected answer to Antigravity and update the Telegram message."""
        pending = self.pending_questions.pop(chat_id, None)
        if not pending:
            return

        conv_id = pending["conv_id"]
        editor: Optional[ThrottledEditor] = pending.get("editor")

        if editor:
            try:
                await editor.edit(
                    f"✅ <b>已提交选择：</b>\n<blockquote>{html.escape(summary_text)}</blockquote>\n\n<i>⏳ Agent 正在继续执行...</i>",
                    force=True,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
            except Exception as exc:
                logger.warning(f"Failed to update status after submitting question: {exc}")

        try:
            await self.agent_cli.send_message(
                conversation_id=conv_id,
                content=answer_text,
            )
        except Exception as exc:
            logger.exception("Failed to send question answer to Antigravity")
            if editor:
                await editor.edit(f"❌ *发送选项失败：* `{exc}`", force=True)

    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline button clicks for question selections and toggles."""
        query = update.callback_query
        if not query:
            return

        await query.answer()

        if not self.is_authorized(update):
            return

        chat_id = update.effective_chat.id
        data = query.data or ""

        pending = self.pending_questions.get(chat_id)
        if not pending:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.answer("⚠️ 选项已失效或已处理完成", show_alert=True)
            return

        if data.startswith("q_sel:"):
            # Single select immediate submit: q_sel:<q_idx>:<o_idx>
            parts = data.split(":")
            q_idx, o_idx = int(parts[1]), int(parts[2])
            questions = pending.get("questions", [])
            if q_idx < len(questions):
                opts = questions[q_idx].get("options", [])
                if o_idx < len(opts):
                    opt_text = opts[o_idx]
                    ans = f"A{q_idx+1}: {opt_text}"
                    await self._submit_question_answer(chat_id, ans, opt_text)

        elif data.startswith("q_tog:"):
            # Toggle checkbox in multi-select: q_tog:<q_idx>:<o_idx>
            parts = data.split(":")
            q_idx, o_idx = int(parts[1]), int(parts[2])
            questions = pending.get("questions", [])
            if q_idx < len(questions):
                is_multi = questions[q_idx].get("is_multi_select", False)
                selections = pending.setdefault("selections", {})
                chosen = selections.setdefault(q_idx, set())
                if is_multi:
                    if o_idx in chosen:
                        chosen.remove(o_idx)
                    else:
                        chosen.add(o_idx)
                else:
                    if o_idx in chosen:
                        chosen.clear()
                    else:
                        chosen.clear()
                        chosen.add(o_idx)

                text, markup = self._render_question_content(pending)
                editor = pending.get("editor")
                if editor:
                    await editor.edit(text, force=True, parse_mode=ParseMode.HTML, reply_markup=markup)
                else:
                    try:
                        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
                    except Exception:
                        pass

        elif data == "q_sub":
            # Submit multi-select choices
            questions = pending.get("questions", [])
            selections = pending.get("selections", {})

            if not any(selections.values()):
                await query.answer("请先勾选至少一个选项，或点击【跳过】", show_alert=True)
                return

            ans_lines = []
            summary_lines = []
            for q_idx, q in enumerate(questions):
                chosen = sorted(list(selections.get(q_idx, set())))
                opts = q.get("options", [])
                if chosen:
                    chosen_texts = [opts[i] for i in chosen if i < len(opts)]
                    ans_lines.append(f"A{q_idx+1}: {', '.join(chosen_texts)}")
                    summary_lines.append(f"Q{q_idx+1}: {', '.join(chosen_texts)}")
                else:
                    ans_lines.append(f"A{q_idx+1}: (Skipped)")
                    summary_lines.append(f"Q{q_idx+1}: (跳过)")

            ans_text = "\n".join(ans_lines)
            summary_text = "\n".join(summary_lines)
            await self._submit_question_answer(chat_id, ans_text, summary_text)

        elif data == "q_skp":
            # Skip question
            await self._submit_question_answer(chat_id, "Skipped", "⏭️ 已跳过当前选项")

    async def _handle_pending_question_text(self, update: Update, text: str) -> bool:
        """Handle chat text replies while a question is pending."""
        chat_id = update.effective_chat.id
        pending = self.pending_questions.get(chat_id)
        if not pending:
            return False

        clean_text = text.strip()
        lower_text = clean_text.lower()

        # 1. Skip keywords
        if lower_text in ("skip", "跳过", "pass", "none", "取消"):
            await self._submit_question_answer(chat_id, "Skipped", "⏭️ 已跳过当前选项")
            return True

        questions = pending.get("questions", [])
        if not questions:
            return False

        # 2. Check for numeric tokens (e.g. "1", "1, 2", "1 2 3", "1、2", "1 2 3 4")
        tokens = [t for t in re.split(r"[\s,，、]+", clean_text) if t]
        if tokens and all(t.isdigit() for t in tokens):
            nums = [int(t) for t in tokens]
            if len(questions) == 1:
                opts = questions[0].get("options", [])
                valid_nums = [n for n in nums if 1 <= n <= len(opts)]
                if valid_nums:
                    chosen_texts = [opts[n - 1] for n in valid_nums]
                    ans = f"A1: {', '.join(chosen_texts)}"
                    summary = ", ".join(chosen_texts)
                    await self._submit_question_answer(chat_id, ans, summary)
                    return True
                else:
                    await update.effective_message.reply_text(
                        f"⚠️ 输入的序号超出范围（有效范围：1-{len(opts)}），请重新输入或点击选项按钮。",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return True

        # 3. Free-form text response (write-in response)
        ans = f"A1: {clean_text}"
        summary = f"自定义输入：{clean_text}"
        await self._submit_question_answer(chat_id, ans, summary)
        return True

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

        # Check if user is responding to a pending interactive question
        if chat_id in self.pending_questions:
            handled = await self._handle_pending_question_text(update, text)
            if handled:
                return

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
                    clean_thought = html.escape(event.thought.strip().replace("\n", " ")[:80])
                    current_status = f"🧠 <b>思考中：</b> <i>{clean_thought}...</i>"
                    await editor.edit(current_status, parse_mode=ParseMode.HTML)

                elif isinstance(event, ToolCallEvent):
                    tool_name = event.tool_name or "tool"
                    if tool_name == "ask_question":
                        questions_raw = event.arguments.get("questions", [])
                        if isinstance(questions_raw, str):
                            try:
                                questions = json.loads(questions_raw)
                            except Exception:
                                questions = []
                        elif isinstance(questions_raw, list):
                            questions = questions_raw
                        else:
                            questions = []

                        if questions:
                            self.pending_questions[chat_id] = {
                                "conv_id": conv_id,
                                "questions": questions,
                                "selections": {q_idx: set() for q_idx in range(len(questions))},
                                "editor": editor,
                                "status_msg_id": status_msg.message_id,
                            }
                            text, markup = self._render_question_content(self.pending_questions[chat_id])
                            await editor.edit(text, force=True, parse_mode=ParseMode.HTML, reply_markup=markup)
                            continue

                    action = event.tool_action or event.tool_summary or ""
                    clean_tool = html.escape(tool_name)
                    clean_detail = f" ({html.escape(action)})" if action else ""
                    current_status = f"⚙️ <b>正在执行工具：</b> <code>{clean_tool}</code>{clean_detail}..."
                    await editor.edit(current_status, parse_mode=ParseMode.HTML)

                elif isinstance(event, ToolResultEvent):
                    self.pending_questions.pop(chat_id, None)
                    current_status = "🔄 正在处理工具执行结果..."
                    await editor.edit(current_status, parse_mode=None, reply_markup=None)

                elif isinstance(event, TurnCompleteEvent):
                    self.pending_questions.pop(chat_id, None)
                    final_response = event.final_content
                    break

                elif isinstance(event, ContentEvent):
                    final_response = event.content

                elif isinstance(event, ErrorEvent):
                    self.pending_questions.pop(chat_id, None)
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
        finally:
            self.pending_questions.pop(chat_id, None)
