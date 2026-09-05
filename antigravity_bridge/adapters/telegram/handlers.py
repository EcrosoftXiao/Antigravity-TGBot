"""Telegram command and message handlers for Antigravity Agent bridge."""

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from antigravity_bridge.core.agent_cli import AgentCliBridge
from antigravity_bridge.core.models import (
    ContentEvent,
    ErrorEvent,
    ModelOption,
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
        self.submitting_questions: Set[int] = set()
        self.last_submitted_time: Dict[int, float] = {}
        self.active_tasks: Dict[int, asyncio.Task] = {}
        self.active_editors: Dict[int, ThrottledEditor] = {}

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
                    "[DENIED] *访问被拒绝*：您尚未被授权控制此 Antigravity 本地代理。\n"
                    f"您的 Telegram 用户 ID 为：`{update.effective_user.id}`\n\n"
                    "[*] 若需授权，请在服务端的 `.env` 文件中将此 ID 添加到 `ALLOWED_USERS` 配置项中。",
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
            "[ANTIGRAVITY] *远程遥控终端*\n\n"
            "本机器人直接桥接至你本机运行的 **Antigravity Agent** 核心环境。\n"
            "无需配置云端 API Key — 所有操作均通过你本机的原生 Agent 执行！\n\n"
            "[COMMANDS] *常用快捷指令*：\n"
            "• `/new` — 开启新的本地会话\n"
            "• `/sessions` — 列出本地历史会话\n"
            "• `/session <ID>` — 绑定当前聊天至指定会话\n"
            "• `/status` — 查看当前会话、模型与工作区状态\n"
            "• `/models` — 查看所有可用模型列表及序号\n"
            "• `/model <序号|名称>` — 切换模型（如 `/model 1` 或 `/model pro`）\n"
            "• `/workspace <路径>` — 查看或切换本地工程工作区目录\n"
            "• `/batch` — 开启批量消息暂存模式\n"
            "• `/help` — 查看完整的机器人指令手册\n\n"
            "[*] *提示：直接发送任意文本即可与你的本地 Antigravity Agent 对话！*"
        )
        await update.effective_message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        help_text = (
            "[MANUAL] *Antigravity 远程遥控指令手册*（对标 dsh-im）：\n\n"
            "[SESSION] *会话管理*\n"
            "• `/new` : 开启全新会话（重置会话绑定，下次发送消息将在新会话中执行）\n"
            "• `/new <序号|模型名称>` : 切换模型并开启全新会话\n"
            "• `/session <ID>` (或 `/s`) : 绑定当前聊天至指定的已有会话\n"
            "• `/sessions` (或 `/list`) : 列出本地最近的 Antigravity 会话列表\n"
            "• `/history [条数]` : 查看当前活动会话最近的历史交互记录\n"
            "• `/status` : 查看当前会话、模型级别、工作区与运行状态\n\n"
            "[CONFIG] *模型与工作区*\n"
            "• `/models` : 查看所有支持的模型列表与对应序号\n"
            "• `/model` : 查看当前正在使用的模型信息\n"
            "• `/model <序号|名称>` : 按序号（如 `/model 1`）或名称（如 `/model sonnet`）切换模型\n"
            "• `/workspace` (或 `/ws`) : 查看当前操作的本地工程目录\n"
            "• `/workspace <路径>` : 切换本地工程工作区绝对路径\n\n"
            "[BATCH] *批量任务提交*\n"
            "• `/batch` : 进入批量模式，后续输入的消息将暂存入缓冲区\n"
            "• `/send` : 一次性将缓冲区内所有消息合并提交给 Agent 执行\n"
            "• `/cancel` : 清空缓冲区并退出批量模式\n\n"
            "[CONTROL] *任务控制*\n"
            "• `/stop` : 中断或请求停止当前正在执行的任务\n\n"
            "[IO] *文件与图片交互*\n"
            "• `/getfile <路径>` (或 `/get`) : 获取并下载工作区或指定路径的文件\n"
            "• 直接发送照片/截图：Bot 自动保存并派发给 Agent 查看与分析\n"
            "• 直接发送文件/文档：Bot 自动保存并派发给 Agent 解析\n"
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
            if not opt:
                try:
                    live_models = await self.agent_cli.get_available_models(force_refresh=True)
                    opt = get_model_by_identifier(target_arg, model_list=live_models)
                except Exception:
                    pass
            if opt:
                model = opt.id
                self.session_mgr.set_model(chat_id, model)

        # Clear active conversation binding locally
        self.session_mgr.clear_conversation(chat_id)

        opt = get_model_by_identifier(model)
        model_display = opt.display_name if opt else model
        current_ws = session.workspace or self.default_workspace

        reply = (
            f"[NEW] *已就绪！下次发送消息将开启全新会话*\n\n"
            f"• *当前使用模型*：*{model_display}*\n"
            f"• *当前工程工作区*：`{current_ws}`\n\n"
            f"[*] 请直接发送你的指令或问题，Agent 将在此工作区中创建全新会话并开始工作。"
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
                    f"[WARN] 序号 #{clean_num} 超出范围：当前本地记录共有 {len(convs)} 个会话（请输入 1 ~ {len(convs)}）。\n"
                    f"请先发送 /sessions 查看列表。",
                    parse_mode=None,
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
                matched_lines = "\n".join([f"• <code>{html.escape(c.conversation_id[:8])}</code> ({html.escape(c.title)})" for c in matches[:5]])
                await update.effective_message.reply_text(
                    f"[WARN] 匹配到多个以 <code>{html.escape(target_arg)}</code> 开头的会话：\n{matched_lines}\n请提供更多字符以精确定位。",
                    parse_mode=ParseMode.HTML,
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
            clean_t = re.sub(r"\s+", " ", target_title).strip()
            title_desc = f"\n> <i>{html.escape(clean_t)}</i>" if clean_t else ""
            await update.effective_message.reply_text(
                f"[OK] <b>已成功绑定到会话：</b>\n<code>{html.escape(target_id)}</code>{title_desc}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            await update.effective_message.reply_text(
                f"[ERROR] 绑定会话失败：未找到该会话记录或会话已失效。\n{exc}",
                parse_mode=None,
            )

    async def _render_sessions_view(self, chat_id: int, limit: int = 10) -> tuple[str, InlineKeyboardMarkup]:
        """Render recent session list text and inline keyboard markup for quick switching."""
        convs = await self.agent_cli.list_conversations(limit=limit)
        if not convs:
            empty_text = "未在 <code>~/.gemini/antigravity/brain</code> 中找到现有会话记录。"
            empty_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(text="➕ 开启全新会话", callback_data="s_act:new")]
            ])
            return empty_text, empty_markup

        active_id = self.session_mgr.get_session(chat_id).active_conversation_id

        lines = ["[SESSIONS] <b>最近的 Antigravity 本地会话列表：</b>\n"]
        for i, c in enumerate(convs, 1):
            marker = "[ACTIVE] " if c.conversation_id == active_id else "• "
            time_str = c.created_at.replace("T", " ")[:19] if c.created_at else ""
            clean_title = re.sub(r"\s+", " ", c.title).strip()
            if len(clean_title) > 50:
                clean_title = clean_title[:50].rstrip() + "..."
            escaped_title = html.escape(clean_title)
            escaped_id = html.escape(c.conversation_id)
            escaped_time = html.escape(time_str)

            lines.append(
                f"{marker}<b>#{i}</b> <code>{escaped_id}</code>\n"
                f"   <i>[{escaped_time}]</i> | {escaped_title}\n"
            )

        lines.append("────────────────────")
        lines.append("[*] 点击下方按钮可直接切换绑定会话，或发送 <code>/session &lt;序号&gt;</code>。")
        msg_text = "\n".join(lines)

        keyboard: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        for i, c in enumerate(convs[:6], 1):
            is_active = (c.conversation_id == active_id)
            prefix = "[✓] " if is_active else ""
            time_short = c.created_at[5:16].replace("T", " ") if c.created_at else f"#{i}"
            btn_text = f"{prefix}#{i} ({time_short})"
            row.append(InlineKeyboardButton(text=btn_text, callback_data=f"s_sel:{c.conversation_id}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        keyboard.append([
            InlineKeyboardButton(text="➕ 开启全新会话", callback_data="s_act:new"),
            InlineKeyboardButton(text="🔄 刷新列表", callback_data="s_act:refresh"),
        ])
        return msg_text, InlineKeyboardMarkup(keyboard)

    async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        limit = 10
        if context.args and context.args[0].isdigit():
            limit = min(int(context.args[0]), 30)

        status_msg = await update.effective_message.reply_text("[SCAN] 正在扫描本地 Antigravity 会话记录...")
        try:
            chat_id = update.effective_chat.id
            msg_text, markup = await self._render_sessions_view(chat_id, limit=limit)
            try:
                await status_msg.edit_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                plain_text = re.sub(r"<[^>]+>", "", msg_text)
                await status_msg.edit_text(plain_text, parse_mode=None, reply_markup=markup)
        except Exception as exc:
            logger.exception("Error listing sessions")
            await status_msg.edit_text(f"[ERROR] 获取会话列表失败：{exc}", parse_mode=None)

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
                "[WARN] 当前未绑定任何活动会话。\n"
                "• 发送 `/sessions` 可查看并绑定历史会话\n"
                "• 发送 `/history <序号>`（如 `/history 1`）可直接查看指定会话历史\n"
                "• 或直接发送任意文本开启全新会话",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        status_msg = await update.effective_message.reply_text(
            f"[READ] 正在读取会话 <code>{html.escape(target_conv_id[:8])}...</code> 历史记录...",
            parse_mode=ParseMode.HTML,
        )

        try:
            history = await self.agent_cli.get_conversation_history(target_conv_id, limit=limit)
            if not history:
                await status_msg.edit_text(
                    f"[INFO] 会话 <code>{html.escape(target_conv_id)}</code> 暂无交互记录（或尚未生成有效回复）。",
                    parse_mode=ParseMode.HTML,
                )
                return

            lines = [
                f"[HISTORY] <b>会话历史交互记录</b> (<code>{html.escape(target_conv_id[:8])}...</code>，最近 {len(history)} 轮)：\n"
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
                    f"<b>#{i} [USER]</b>{u_trunc_tag}：\n"
                    f"{html.escape(clean_user)}\n\n"
                    f"<b>[AGENT]</b>{r_trunc_tag}：\n"
                    f"{html.escape(clean_resp)}\n"
                    f"────────────────────"
                )

            lines.append("[*] <i>提示：长对话内容已自动截断以保证清晰展示。发送 /history &lt;条数&gt;（如 /history 5）可查看更多轮次。</i>")
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
            await status_msg.edit_text(f"[ERROR] 读取会话历史记录失败：{exc}", parse_mode=None)

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
        model_display = f"#{opt.code} {opt.display_name} [{opt.badge}]" if opt else (session.model or self.default_model)
        text = (
            "[STATUS] *Antigravity 远程遥控桥接状态*\n\n"
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
    async def _render_models_view(self, chat_id: int, show_all: bool = False, force_refresh: bool = False) -> tuple[str, InlineKeyboardMarkup]:
        """Render available models text and inline keyboard markup for quick switching."""
        session = self.session_mgr.get_session(chat_id)
        current_model = session.model or self.default_model

        try:
            live_models = await self.agent_cli.get_available_models(force_refresh=force_refresh)
        except Exception as exc:
            logger.warning(f"Failed to fetch live models: {exc}")
            live_models = AVAILABLE_MODELS

        display_models = live_models if show_all else [m for m in live_models if getattr(m, "is_recommended", True)]
        if not display_models:
            display_models = live_models

        def format_limit_remaining(quota_remaining: Optional[float], reset_time_str: Optional[str]) -> str:
            resets_in = ""
            if reset_time_str:
                try:
                    clean_iso = reset_time_str.replace("Z", "+00:00")
                    reset_dt = datetime.fromisoformat(clean_iso)
                    now_dt = datetime.now(timezone.utc)
                    secs = int((reset_dt - now_dt).total_seconds())
                    if secs <= 0:
                        resets_in = "Resets soon"
                    else:
                        hours = secs // 3600
                        mins = (secs % 3600) // 60
                        if hours > 0:
                            resets_in = f"Resets in {hours}h {mins}m"
                        else:
                            resets_in = f"Resets in {mins}m"
                except Exception:
                    pass

            pct_str = f"{int(round(quota_remaining * 100))}%" if quota_remaining is not None else ("0%" if reset_time_str else "100%")
            reset_suffix = f" ({resets_in})" if resets_in else ""
            return f"Five Hour Limit Remaining: {pct_str}{reset_suffix}"

        lines = ["[MODELS] <b>Antigravity 实时可用模型列表:</b>\n"]
        # Group models by major series (opt.index)
        groups: Dict[int, List[ModelOption]] = {}
        for opt in display_models:
            groups.setdefault(opt.index, []).append(opt)

        def get_series_title(m: ModelOption) -> str:
            disp = m.display_name
            cleaned = re.sub(r"\s*\((High|Medium|Low|Thinking)\)", "", disp, flags=re.IGNORECASE).strip()
            return cleaned

        def get_sub_level_label(m_id: str, badge: str, disp: str) -> str:
            m_level = re.search(r"-(high|medium|low|thinking|extra-low)$", m_id.lower())
            if m_level:
                lvl = m_level.group(1)
                if badge and badge.lower() != lvl:
                    return f"{lvl} [{badge}]"
                return lvl
            m_disp = re.search(r"\((High|Medium|Low|Thinking)\)", disp, flags=re.IGNORECASE)
            if m_disp:
                lvl = m_disp.group(1).lower()
                if badge and badge.lower() != lvl:
                    return f"{lvl} [{badge}]"
                return lvl
            return f"standard [{badge}]" if badge else "standard"

        for major_idx, opts in groups.items():
            first_opt = opts[0]
            series_name = html.escape(get_series_title(first_opt))
            escaped_desc = html.escape(first_opt.description)
            limit_str = format_limit_remaining(first_opt.quota_remaining, first_opt.reset_time)

            is_series_active = any(
                (opt.id == current_model or opt.code == current_model or opt.tier == current_model)
                for opt in opts
            )
            series_prefix = "[ACTIVE] " if is_series_active else "• "

            if len(opts) == 1:
                # Single item series: show directly on the series header line without sub-levels
                selected_tag = " <b>(当前已选中)</b>" if is_series_active else ""
                badge_tag = f" <code>[{first_opt.badge}]</code>" if first_opt.badge else ""
                lines.append(f"{series_prefix}<b>#{first_opt.code} {series_name}</b>{badge_tag}{selected_tag}")
                lines.append(f"  <i>{escaped_desc}</i>")
                if limit_str:
                    lines.append(f"  <code>[{limit_str}]</code>")
            else:
                # Multiple sub-items in series: render header and indented sub-levels
                lines.append(f"{series_prefix}<b>#{major_idx} {series_name}</b>")
                lines.append(f"  <i>{escaped_desc}</i>")
                if limit_str:
                    lines.append(f"  <code>[{limit_str}]</code>")

                for opt in opts:
                    is_opt_selected = (opt.id == current_model or opt.code == current_model or opt.tier == current_model)
                    sub_prefix = "    [ACTIVE] " if is_opt_selected else "  • "
                    selected_tag = " <b>(当前已选中)</b>" if is_opt_selected else ""
                    sub_label = html.escape(get_sub_level_label(opt.id, opt.badge, opt.display_name))

                    lines.append(f"{sub_prefix}<b>#{opt.code}</b> {sub_label}{selected_tag}")
            lines.append("")

        lines.append("────────────────────")
        lines.append(
            "<b>[*] 切换模型使用指南:</b>\n"
            "• <b>点击下方按钮</b>可直接秒级热切换模型（保留上下文）\n"
            "• <b>或发送指令</b>：<code>/model 1.1</code> (分级序号), <code>/model sonnet</code> (模型别名)\n"
            "• <b>带模型开启新会话</b>：<code>/new 1.1</code> 或 <code>/new sonnet</code>"
        )
        if not show_all and len(live_models) > len(display_models):
            lines.append(f"\n💡 当前展示核心推荐模型。发送 <code>/models all</code> 可查看全部 {len(live_models)} 款底层模型。")

        msg_text = "\n".join(lines)

        # Build inline keyboard buttons
        keyboard: List[List[InlineKeyboardButton]] = []
        for major_idx, opts in groups.items():
            if len(opts) == 1:
                opt = opts[0]
                is_active = (opt.id == current_model or opt.code == current_model or opt.tier == current_model)
                prefix = "[✓] " if is_active else ""
                s_name = get_series_title(opt)
                btn_text = f"{prefix}#{opt.code} {s_name}"
                if len(btn_text) > 24:
                    btn_text = btn_text[:22] + ".."
                keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"m_sel:{opt.id}")])
            else:
                row: List[InlineKeyboardButton] = []
                for opt in opts:
                    is_active = (opt.id == current_model or opt.code == current_model or opt.tier == current_model)
                    prefix = "[✓] " if is_active else ""
                    lvl_str = opt.badge or (opt.id.split("-")[-1].capitalize())
                    btn_text = f"{prefix}#{opt.code} {lvl_str}"
                    row.append(InlineKeyboardButton(text=btn_text, callback_data=f"m_sel:{opt.id}"))
                    if len(row) == 3:
                        keyboard.append(row)
                        row = []
                if row:
                    keyboard.append(row)

        keyboard.append([
            InlineKeyboardButton(text="🔄 刷新可用配额与状态", callback_data="m_act:refresh")
        ])

        return msg_text, InlineKeyboardMarkup(keyboard)

    async def cmd_models(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        show_all = bool(context.args and context.args[0].lower() in ("all", "full", "全部"))

        status_msg = await update.effective_message.reply_text("[MODELS] 正在从本地 Antigravity 获取实时模型列表...")
        try:
            msg_text, markup = await self._render_models_view(chat_id, show_all=show_all, force_refresh=True)
            try:
                await status_msg.edit_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                plain = re.sub(r"<[^>]+>", "", msg_text)
                await status_msg.edit_text(plain, parse_mode=None, reply_markup=markup)
        except Exception as exc:
            logger.exception("Failed to fetch live models")
            await status_msg.edit_text(f"[ERROR] 获取模型列表失败：{exc}", parse_mode=None)

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

        # If not matched, try force refreshing live models once
        if not matched_opt:
            try:
                live_models = await self.agent_cli.get_available_models(force_refresh=True)
                matched_opt = get_model_by_identifier(target_arg, model_list=live_models)
            except Exception:
                pass

        if not matched_opt:
            await update.effective_message.reply_text(
                f"[ERROR] 未知模型或序号: `{target_arg}`\n\n"
                "可选序号示例: `1.1` (3.8 High), `1.2` (Medium), `1.3` (Low), `4.1` (Pro High), `5.1` (Sonnet) ...\n"
                "发送 `/models` 查看实时的完整分级模型列表与说明。",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        self.session_mgr.set_model(chat_id, matched_opt.id)
        quota_info_text = ""
        resets_in = ""
        if matched_opt.reset_time:
            try:
                clean_iso = matched_opt.reset_time.replace("Z", "+00:00")
                reset_dt = datetime.datetime.fromisoformat(clean_iso)
                now_dt = datetime.datetime.now(datetime.timezone.utc)
                secs = int((reset_dt - now_dt).total_seconds())
                if secs <= 0:
                    resets_in = "Resets soon"
                else:
                    h = secs // 3600
                    m = (secs % 3600) // 60
                    resets_in = f"Resets in {h}h {m}m" if h > 0 else f"Resets in {m}m"
            except Exception:
                pass

        if matched_opt.quota_remaining is not None or matched_opt.reset_time:
            pct_str = f"{int(round(matched_opt.quota_remaining * 100))}%" if matched_opt.quota_remaining is not None else "0%"
            suffix = f" ({resets_in})" if resets_in else ""
            quota_info_text = f"\n• *可用额度*：`Five Hour Limit Remaining: {pct_str}{suffix}`"

        reply = (
            f"[OK] *已成功切换模型！*\n\n"
            f"• *序号*：`#{matched_opt.code}`\n"
            f"• *模型*：*{matched_opt.display_name}*\n"
            f"• *规格*：`{matched_opt.badge}` (映射底座：`{matched_opt.tier}`){quota_info_text}\n"
            f"• *说明*：_{matched_opt.description}_\n\n"
            f"[*] 新建会话（`/new`）将使用此模型进行驱动。"
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
                f"[WORKSPACE] *当前本地工程工作区*：`{current}`\n"
                f"• *当前活动会话*：`{active_conv}`\n\n"
                "[*] *用法说明*：发送 `/workspace <本地绝对路径>` 可切换至其他工程目录。切换后发送消息将在新工作区开启新会话。",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        target_path = os.path.expanduser(context.args[0].strip())
        target_path = os.path.abspath(target_path)

        if not os.path.isdir(target_path):
            await update.effective_message.reply_text(
                f"[ERROR] 目录不存在：`{target_path}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        self.session_mgr.set_workspace(chat_id, target_path)
        # Unbind old conversation when switching workspace
        self.session_mgr.clear_conversation(chat_id)

        reply = (
            f"[WORKSPACE] *本地工程工作区已切换！*\n\n"
            f"• *当前工程工作区*：`{target_path}`\n"
            f"• *会话状态*：已就绪（旧会话已解绑）\n\n"
            f"[*] 请直接发送你的指令或问题，Agent 将在此工作区中创建全新会话并开始工作。"
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
            "[BATCH] *批量暂存模式已开启！*\n\n"
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
                "[WARN] 批量暂存区为空。请先发送要暂存的内容或使用 `/batch`。",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        buffered_messages = self.session_mgr.flush_batch_mode(chat_id)
        combined_prompt = "\n\n".join(buffered_messages)

        await update.effective_message.reply_text(
            f"[DISPATCH] 正在将暂存的 {len(buffered_messages)} 条消息合并为单一任务派发给 Agent...",
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
            f"[CANCEL] 批量模式已取消，已清空并丢弃 {count} 条暂存消息。",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)
        conv_id = session.active_conversation_id

        # 1. Pop any pending question state
        self.pending_questions.pop(chat_id, None)

        # 2. Get active background stream task and editor
        running_task = self.active_tasks.pop(chat_id, None)
        active_editor = self.active_editors.pop(chat_id, None)

        # 3. Call Antigravity Language Server CancelCascadeInvocation RPC
        rpc_cancelled = False
        if conv_id:
            rpc_cancelled = await self.agent_cli.cancel_cascade(conv_id)

        # 4. Cancel the bot's background stream task
        if running_task and not running_task.done():
            running_task.cancel()

        # 5. Update status editor if it was running
        if active_editor:
            try:
                await active_editor.edit(
                    "[STOP] <b>任务已手动中断停止。</b>",
                    force=True,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
            except Exception:
                pass

        # 6. Inform user
        if running_task or rpc_cancelled:
            await update.effective_message.reply_text(
                "[STOP] *已成功停止会话任务*：已向本地 Antigravity Agent 发送中断信号，并终止了当前回复流。",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.effective_message.reply_text(
                "[INFO] 当前没有正在执行的 Agent 任务，会话处于空闲状态。",
                parse_mode=ParseMode.MARKDOWN,
            )

    # ------------------------------------------------------------------
    # Question Interactive Selection Helpers & Callback Query Handler
    # ------------------------------------------------------------------
    def _render_single_box_content(
        self, pending: Dict[str, Any], q_idx: int
    ) -> Tuple[str, InlineKeyboardMarkup]:
        """Format the question prompt text and generate Telegram InlineKeyboardMarkup for a single question box."""
        questions = pending.get("questions", [])
        if q_idx >= len(questions):
            return "", InlineKeyboardMarkup([])

        total_q = len(questions)
        q = questions[q_idx]
        q_text = q.get("question", "").strip()
        is_multi = q.get("is_multi_select", False)
        opts = q.get("options", [])

        boxes = pending.get("boxes", {})
        box_state = boxes.get(q_idx, {})
        chosen_set = box_state.get("selections", set())

        lines: List[str] = []
        if total_q > 1:
            type_str = "多选" if is_multi else "单选"
            lines.append(f"[QUESTION] <b>选项 ({q_idx+1}/{total_q})：{html.escape(q_text)}</b> <i>({type_str})</i>\n")
        else:
            type_str = "多选" if is_multi else "单选"
            lines.append(f"[QUESTION] <b>{html.escape(q_text)}</b> <i>({type_str})</i>\n")

        for o_idx, opt in enumerate(opts):
            opt_num = o_idx + 1
            opt_display = opt.strip() if isinstance(opt, str) else str(opt.get("text", "")).strip()
            is_chosen = o_idx in chosen_set
            if is_multi:
                icon = "[X]" if is_chosen else "[ ]"
            else:
                icon = "( )"
            lines.append(f"   {icon} <b>{opt_num}.</b> {html.escape(opt_display)}")

        lines.append("")
        if is_multi:
            lines.append("[*] <i>点击选项切换勾选后点击【确认】，或直接输入序号（如 1, 2）：</i>")
        else:
            lines.append("[*] <i>点击下方按钮直接选择，或在聊天框发送序号（如 1）：</i>")

        keyboard: List[List[InlineKeyboardButton]] = []
        for o_idx, opt in enumerate(opts):
            opt_num = o_idx + 1
            opt_display = opt.strip() if isinstance(opt, str) else str(opt.get("text", "")).strip()
            is_chosen = o_idx in chosen_set
            if is_multi:
                icon = "[X]" if is_chosen else "[ ]"
                btn_label = f"{icon} {opt_num}. {opt_display}"
                cb_data = f"q_tog:{q_idx}:{o_idx}"
            else:
                btn_label = f"{opt_num}. {opt_display}"
                cb_data = f"q_sel:{q_idx}:{o_idx}"

            max_btn_len = 38
            if len(btn_label) > max_btn_len:
                btn_label = btn_label[: max_btn_len - 1] + "…"

            keyboard.append([InlineKeyboardButton(btn_label, callback_data=cb_data)])

        if is_multi:
            sub_label = f"[SUBMIT] 确认第 {q_idx+1} 题" if total_q > 1 else "[SUBMIT] 确认提交"
            skp_label = f"[SKIP] 跳过第 {q_idx+1} 题" if total_q > 1 else "[SKIP] 跳过"
            action_row = [
                InlineKeyboardButton(sub_label, callback_data=f"q_sub:{q_idx}"),
                InlineKeyboardButton(skp_label, callback_data=f"q_skp:{q_idx}"),
            ]
            keyboard.append(action_row)
        else:
            skp_label = f"[SKIP] 跳过第 {q_idx+1} 题" if total_q > 1 else "[SKIP] 跳过"
            keyboard.append([InlineKeyboardButton(skp_label, callback_data=f"q_skp:{q_idx}")])

        return "\n".join(lines).strip(), InlineKeyboardMarkup(keyboard)

    def _render_question_content(self, pending: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
        """Backward-compatible renderer for single question dialog."""
        return self._render_single_box_content(pending, 0)

    async def _send_next_question_box(
        self,
        bot: Any,
        chat_id: int,
        pending: Dict[str, Any],
        next_q_idx: int,
    ) -> None:
        """Pop up the next sequential question box in Telegram."""
        questions = pending.get("questions", [])
        total_q = len(questions)
        if next_q_idx >= total_q:
            return

        box_state = pending.get("boxes", {}).setdefault(next_q_idx, {
            "msg_id": None,
            "editor": None,
            "selections": set(),
            "confirmed": False,
            "skipped": False,
            "write_in": "",
        })

        text, markup = self._render_single_box_content(pending, next_q_idx)
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            editor = ThrottledEditor(msg, min_interval=1.0)
            box_state["editor"] = editor
            box_state["msg_id"] = msg.message_id
            logger.info(
                f"Popped up sequential question box {next_q_idx+1}/{total_q} (msg {msg.message_id}) for chat {chat_id}"
            )
        except Exception as send_err:
            logger.warning(
                f"Failed to pop up question box {next_q_idx+1}/{total_q} to chat {chat_id}: {send_err}"
            )

    async def send_pending_questions(
        self,
        bot: Any,
        chat_id: int,
        conv_id: str,
        step_index: int,
        questions: List[Dict[str, Any]],
        existing_editor: Optional[ThrottledEditor] = None,
    ) -> None:
        """Initialize pending questions and pop up the first question box."""
        pending_data: Dict[str, Any] = {
            "conv_id": conv_id,
            "step_index": step_index,
            "questions": questions,
            "boxes": {},
            "editor": existing_editor,
        }
        self.pending_questions[chat_id] = pending_data

        total_q = len(questions)
        for q_idx in range(total_q):
            pending_data["boxes"][q_idx] = {
                "msg_id": None,
                "editor": None,
                "selections": set(),
                "confirmed": False,
                "skipped": False,
                "write_in": "",
            }

        # Pop up the first question box initially
        box_state = pending_data["boxes"][0]
        text, markup = self._render_single_box_content(pending_data, 0)

        if existing_editor:
            try:
                await existing_editor.edit(text, force=True, parse_mode=ParseMode.HTML, reply_markup=markup)
                box_state["editor"] = existing_editor
                box_state["msg_id"] = existing_editor.message.message_id if existing_editor.message else None
                logger.info(
                    f"Updated existing message into initial question box 1/{total_q} (step {step_index}) for chat {chat_id}"
                )
                return
            except Exception as e:
                logger.warning(f"Could not edit existing editor for box 0: {e}")

        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            editor = ThrottledEditor(msg, min_interval=1.0)
            box_state["editor"] = editor
            box_state["msg_id"] = msg.message_id
            logger.info(
                f"Sent initial question box 1/{total_q} (msg {msg.message_id}) for chat {chat_id}"
            )
        except Exception as send_err:
            logger.warning(
                f"Failed to send initial question box 1/{total_q} to chat {chat_id}: {send_err}"
            )

    async def cleanup_pending_questions(self, chat_id: int) -> None:
        """Mark all active question boxes as resolved externally."""
        pop_pending = self.pending_questions.pop(chat_id, None)
        if not pop_pending:
            return
        boxes = pop_pending.get("boxes", {})
        for q_idx, b in boxes.items():
            editor = b.get("editor")
            if editor and not b.get("confirmed"):
                try:
                    await editor.edit(
                        "ℹ️ <b>当前选项已在外部终端确认或跳过。</b>",
                        force=True,
                        parse_mode=ParseMode.HTML,
                        reply_markup=None,
                    )
                except Exception:
                    pass
        legacy_editor = pop_pending.get("editor")
        if legacy_editor and not boxes:
            try:
                await legacy_editor.edit(
                    "ℹ️ <b>当前选项已在外部终端确认或跳过。</b>",
                    force=True,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
            except Exception:
                pass

    async def _check_and_submit_all_boxes(
        self,
        chat_id: int,
        pending: Dict[str, Any],
        trigger_q_idx: int,
        query: Optional[Any] = None,
    ) -> None:
        """Check if current question is completed; if next question exists, pop it up; otherwise submit all answers."""
        questions = pending.get("questions", [])
        boxes = pending.get("boxes", {})
        total_q = len(questions)

        # Check if there is an unconfirmed question next in sequence
        next_unconfirmed = None
        for i in range(total_q):
            if not boxes.get(i, {}).get("confirmed", False):
                next_unconfirmed = i
                break

        if next_unconfirmed is not None:
            # If the next unconfirmed question hasn't been sent yet, pop it up!
            next_box = boxes.get(next_unconfirmed, {})
            if not next_box.get("msg_id"):
                bot = getattr(self, "bot", None)
                if query and query.message:
                    bot = bot or query.message.get_bot()
                if bot:
                    await self._send_next_question_box(bot, chat_id, pending, next_unconfirmed)

            if query:
                await self._safe_answer_query(
                    query, f"[OK] 已完成第 {trigger_q_idx+1} 题，已弹出第 {next_unconfirmed+1} 题"
                )
            return

        # All boxes confirmed or skipped!
        if query:
            await self._safe_answer_query(query, "[OK] 选项已全部确认，正在提交...")

        ans_lines = []
        summary_lines = []
        responses = []
        all_skipped = True

        for i, q in enumerate(questions):
            b = boxes.get(i, {})
            opts = q.get("options", [])
            is_multi = q.get("is_multi_select", False)

            if b.get("skipped", False):
                ans_lines.append(f"A{i+1}: (Skipped)")
                summary_lines.append(f"Q{i+1}: (跳过)")
                responses.append({
                    "question": q.get("question", ""),
                    "options": [
                        {"id": str(idx + 1), "text": o if isinstance(o, str) else o.get("text", "")}
                        for idx, o in enumerate(opts)
                    ],
                    "is_multi_select": is_multi,
                    "selected_option_ids": [],
                    "write_in_response": "",
                    "skipped": True,
                })
            else:
                all_skipped = False
                chosen = sorted(list(b.get("selections", set())))
                write_in = b.get("write_in", "")
                chosen_texts = [
                    opts[idx] if isinstance(opts[idx], str) else opts[idx].get("text", "")
                    for idx in chosen if idx < len(opts)
                ]
                if write_in:
                    chosen_texts.append(write_in)

                ans_lines.append(f"A{i+1}: {', '.join(chosen_texts)}")
                summary_lines.append(f"Q{i+1}: {', '.join(chosen_texts)}")
                responses.append({
                    "question": q.get("question", ""),
                    "options": [
                        {"id": str(idx + 1), "text": o if isinstance(o, str) else o.get("text", "")}
                        for idx, o in enumerate(opts)
                    ],
                    "is_multi_select": is_multi,
                    "selected_option_ids": [str(idx + 1) for idx in chosen],
                    "write_in_response": write_in,
                    "skipped": False,
                })

        last_editor = boxes.get(trigger_q_idx, {}).get("editor")
        await self._submit_question_answer(
            chat_id=chat_id,
            answer_text="\n".join(ans_lines),
            summary_text="\n".join(summary_lines),
            responses=responses,
            cancelled=all_skipped,
            final_editor=last_editor,
        )

    async def _submit_question_answer(
        self,
        chat_id: int,
        answer_text: str,
        summary_text: str,
        responses: Optional[List[Dict[str, Any]]] = None,
        cancelled: bool = False,
        final_editor: Optional[ThrottledEditor] = None,
    ) -> None:
        """Submit the selected answer to Antigravity and update the Telegram message."""
        pending = self.pending_questions.pop(chat_id, None)
        if not pending:
            return

        self.submitting_questions.add(chat_id)
        self.last_submitted_time[chat_id] = time.time()

        try:
            conv_id = pending["conv_id"]
            step_index = pending.get("step_index", -1)
            questions = pending.get("questions", [])

            editor = final_editor or pending.get("editor")
            bot = getattr(self, "bot", None) or (editor.message.get_bot() if editor and editor.message else None)

            if len(questions) > 1 and bot:
                try:
                    status_msg = await bot.send_message(
                        chat_id=chat_id,
                        text=f"[OK] <b>选项已全部确认：</b>\n<blockquote>{html.escape(summary_text)}</blockquote>\n\n<i>[RUNNING] Agent 正在继续执行...</i>",
                        parse_mode=ParseMode.HTML,
                    )
                    editor = ThrottledEditor(status_msg, min_interval=1.0)
                except Exception as exc:
                    logger.debug(f"Could not send multi-box summary notice: {exc}")
            elif editor:
                try:
                    await editor.edit(
                        f"[OK] <b>已提交选择：</b>\n<blockquote>{html.escape(summary_text)}</blockquote>\n\n<i>[RUNNING] Agent 正在继续执行...</i>",
                        force=True,
                        parse_mode=ParseMode.HTML,
                        reply_markup=None,
                    )
                except Exception as exc:
                    logger.warning(f"Failed to update status after submitting question: {exc}")

            # 1. Direct RPC via Language Server HandleCascadeUserInteraction
            rpc_success = False
            try:
                rpc_success = await self.agent_cli.handle_ask_question_interaction(
                    conversation_id=conv_id,
                    step_index=step_index,
                    responses=responses,
                    cancelled=cancelled,
                )
                logger.info(f"handle_ask_question_interaction for {conv_id[:8]} returned {rpc_success}")
            except Exception as exc:
                logger.warning(f"Failed calling handle_ask_question_interaction: {exc}")

            # 2. Fallback to CLI send-message if RPC was not successful
            if not rpc_success:
                try:
                    await self.agent_cli.send_message(
                        conversation_id=conv_id,
                        content=answer_text,
                    )
                    logger.info(f"Fallback send_message dispatched to {conv_id[:8]}")
                except Exception as exc:
                    logger.exception("Failed to send question answer via fallback send_message")
                    if editor:
                        await editor.edit(f"[ERROR] *发送选项失败：* `{exc}`", force=True)
                    return

            # 3. If no active background stream task is monitoring this turn, launch one
            if editor and (chat_id not in self.active_tasks or self.active_tasks[chat_id].done()):
                start_step = self.monitor.get_current_max_step(conv_id) + 1
                task = asyncio.create_task(
                    self._stream_turn_events(chat_id, conv_id, editor, start_step, editor.message)
                )
                self.active_tasks[chat_id] = task
        finally:
            self.submitting_questions.discard(chat_id)
            self.last_submitted_time[chat_id] = time.time()

    def _try_recover_pending_question(
        self, chat_id: int, message: Optional[Message] = None
    ) -> Optional[Dict[str, Any]]:
        """Attempt to restore pending question from active conversation transcript if memory was wiped."""
        if chat_id in self.pending_questions:
            return self.pending_questions[chat_id]

        session = self.session_mgr.get_session(chat_id)
        conv_id = session.active_conversation_id
        if not conv_id:
            return None

        pending_info = self.monitor.get_pending_question(conv_id)
        if not pending_info:
            return None

        step_idx, questions = pending_info
        if not questions:
            return None

        logger.info(
            f"Successfully recovered pending question (step {step_idx}) for chat {chat_id} from conversation {conv_id[:8]}"
        )
        editor = ThrottledEditor(message, min_interval=1.2) if message else None
        boxes = {}
        for q_idx in range(len(questions)):
            boxes[q_idx] = {
                "msg_id": message.message_id if message else None,
                "editor": editor,
                "selections": set(),
                "confirmed": False,
                "skipped": False,
                "write_in": "",
            }
        self.pending_questions[chat_id] = {
            "conv_id": conv_id,
            "step_index": step_idx,
            "questions": questions,
            "boxes": boxes,
            "editor": editor,
            "status_msg_id": message.message_id if message else None,
        }
        return self.pending_questions[chat_id]

    async def _safe_answer_query(
        self, query: Any, text: Optional[str] = None, show_alert: bool = False
    ) -> bool:
        """Safely acknowledge callback query without raising on expired queries."""
        try:
            if text:
                await query.answer(text=text, show_alert=show_alert)
            else:
                await query.answer()
            return True
        except Exception as exc:
            logger.debug(f"Telegram callback query acknowledgment ignored: {exc}")
            return False

    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline button clicks for question selections and toggles."""
        query = update.callback_query
        if not query:
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id if update.effective_user else None
        data = query.data or ""

        logger.info(f"Telegram CallbackQuery: chat_id={chat_id}, user_id={user_id}, data='{data}'")

        if not self.is_authorized(update):
            logger.warning(f"Unauthorized callback query from user {user_id}")
            await self._safe_answer_query(query, "[DENIED] 未授权用户，禁止操作", show_alert=True)
            return

        # --------------------------------------------------------------
        # Inline Model Switching & Actions
        # --------------------------------------------------------------
        if data.startswith("m_sel:"):
            model_id = data[6:]
            matched_opt = get_model_by_identifier(model_id)
            if not matched_opt:
                try:
                    live_models = await self.agent_cli.get_available_models(force_refresh=True)
                    matched_opt = get_model_by_identifier(model_id, model_list=live_models)
                except Exception:
                    pass

            if matched_opt:
                active_session = self.session_mgr.get_session(chat_id)
                if active_session and active_session.active_conversation_id:
                    self.session_mgr.continue_session_with_new_model(chat_id, matched_opt.id)
                else:
                    self.session_mgr.set_model(chat_id, matched_opt.id)

                await self._safe_answer_query(query, f"[OK] 已切换至 #{matched_opt.code} {matched_opt.display_name}")
                try:
                    text, markup = await self._render_models_view(chat_id)
                    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
                except Exception as exc:
                    logger.debug(f"Failed to update models markup after selection: {exc}")
            else:
                await self._safe_answer_query(query, "[ERROR] 未找到所选模型", show_alert=True)
            return

        elif data == "m_act:refresh":
            await self._safe_answer_query(query, "[OK] 正在刷新可用模型与配额...")
            try:
                text, markup = await self._render_models_view(chat_id, force_refresh=True)
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception as exc:
                logger.debug(f"Failed to refresh models view: {exc}")
            return

        # --------------------------------------------------------------
        # Inline Session Switching & Actions
        # --------------------------------------------------------------
        elif data.startswith("s_sel:"):
            target_conv_id = data[6:]
            try:
                self.session_mgr.bind_conversation(chat_id, target_conv_id)
                await self._safe_answer_query(query, f"[OK] 已成功绑定会话 {target_conv_id[:8]}...")
                text, markup = await self._render_sessions_view(chat_id)
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception as exc:
                await self._safe_answer_query(query, f"[ERROR] 绑定失败：{exc}", show_alert=True)
            return

        elif data == "s_act:new":
            self.session_mgr.new_session(chat_id)
            await self._safe_answer_query(query, "[NEW] 已开启新会话！下次发送消息将创建全新对话。")
            try:
                text, markup = await self._render_sessions_view(chat_id)
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception as exc:
                logger.debug(f"Failed to refresh sessions after new: {exc}")
            return

        elif data == "s_act:refresh":
            await self._safe_answer_query(query, "[OK] 正在刷新会话列表...")
            try:
                text, markup = await self._render_sessions_view(chat_id)
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception as exc:
                logger.debug(f"Failed to refresh sessions: {exc}")
            return

        pending = self.pending_questions.get(chat_id)
        if not pending:
            pending = self._try_recover_pending_question(chat_id, query.message)

        if not pending:
            logger.warning(f"Callback query '{data}' received for chat {chat_id} but no pending question active")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await self._safe_answer_query(
                query,
                "[WARN] 该选项已失效、已处理或机器人已重启，请在聊天框直接输入",
                show_alert=True,
            )
            return

        questions = pending.get("questions", [])
        total_q = len(questions)
        boxes = pending.setdefault("boxes", {})
        if not boxes:
            for i in range(total_q):
                boxes[i] = {
                    "msg_id": query.message.message_id if query.message else None,
                    "editor": pending.get("editor") or (ThrottledEditor(query.message, min_interval=1.0) if query.message else None),
                    "selections": set(),
                    "confirmed": False,
                    "skipped": False,
                    "write_in": "",
                }

        # Associate this message's editor with the matching box if message is known
        if query.message:
            for i, b in boxes.items():
                if b.get("msg_id") == query.message.message_id or (total_q == 1 and not b.get("editor")):
                    if not b.get("editor"):
                        b["editor"] = ThrottledEditor(query.message, min_interval=1.0)
                    b["msg_id"] = query.message.message_id

        if data.startswith("q_sel:"):
            # Single select immediate submit: q_sel:<q_idx>:<o_idx>
            parts = data.split(":")
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                q_idx, o_idx = int(parts[1]), int(parts[2])
                if q_idx < total_q:
                    opts = questions[q_idx].get("options", [])
                    if o_idx < len(opts):
                        opt_item = opts[o_idx]
                        opt_text = opt_item if isinstance(opt_item, str) else opt_item.get("text", "")
                        
                        b = boxes.setdefault(q_idx, {})
                        if b.get("confirmed"):
                            await self._safe_answer_query(query, "已完成选择，请勿重复点击")
                            return

                        b["selections"] = {o_idx}
                        b["confirmed"] = True
                        b["skipped"] = False
                        
                        # Immediately answer the callback query so the loading spinner stops instantly
                        await self._safe_answer_query(query, f"已选择：{opt_text}")

                        editor = b.get("editor") or (ThrottledEditor(query.message, min_interval=1.0) if query.message else None)
                        if editor:
                            box_title = f"选项 ({q_idx+1}/{total_q})" if total_q > 1 else "选项"
                            try:
                                await editor.edit(
                                    f"[OK] <b>{box_title} 已确认：</b>\n<blockquote>{html.escape(opt_text)}</blockquote>",
                                    force=True,
                                    parse_mode=ParseMode.HTML,
                                    reply_markup=None,
                                )
                            except Exception as exc:
                                logger.warning(f"Failed to edit confirmed box {q_idx}: {exc}")

                        await self._check_and_submit_all_boxes(chat_id, pending, trigger_q_idx=q_idx, query=query)
                        return

            await self._safe_answer_query(query, "选项无效", show_alert=True)

        elif data.startswith("q_tog:"):
            # Toggle checkbox in multi-select: q_tog:<q_idx>:<o_idx>
            parts = data.split(":")
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                q_idx, o_idx = int(parts[1]), int(parts[2])
                if q_idx < total_q:
                    b = boxes.setdefault(q_idx, {})
                    chosen = b.setdefault("selections", set())
                    if o_idx in chosen:
                        chosen.remove(o_idx)
                        toast_text = f"[ ] 已取消勾选第 {o_idx+1} 项"
                    else:
                        chosen.add(o_idx)
                        toast_text = f"[X] 已勾选第 {o_idx+1} 项"

                    await self._safe_answer_query(query, toast_text)

                    text, markup = self._render_single_box_content(pending, q_idx)
                    editor = b.get("editor") or (ThrottledEditor(query.message, min_interval=1.0) if query.message else None)
                    if editor:
                        try:
                            await editor.edit(text, force=True, parse_mode=ParseMode.HTML, reply_markup=markup)
                        except Exception:
                            try:
                                await query.edit_message_reply_markup(reply_markup=markup)
                            except Exception:
                                pass
                    return

            await self._safe_answer_query(query, "选项无效", show_alert=True)

        elif data.startswith("q_sub"):
            q_idx = int(data.split(":")[1]) if ":" in data and data.split(":")[1].isdigit() else 0
            if q_idx < total_q:
                b = boxes.setdefault(q_idx, {})
                if b.get("confirmed"):
                    await self._safe_answer_query(query, "已完成提交，请勿重复点击")
                    return

                chosen = b.get("selections", set())
                opts = questions[q_idx].get("options", [])
                if not chosen:
                    await self._safe_answer_query(query, "[WARN] 请先勾选至少一个选项，或点击【跳过】", show_alert=True)
                    return

                b["confirmed"] = True
                b["skipped"] = False
                chosen_texts = [
                    opts[i] if isinstance(opts[i], str) else opts[i].get("text", "")
                    for i in sorted(chosen) if i < len(opts)
                ]
                summary = ", ".join(chosen_texts)

                # Immediately answer callback query
                await self._safe_answer_query(query, "[OK] 已确认提交当前选项")

                editor = b.get("editor") or (ThrottledEditor(query.message, min_interval=1.0) if query.message else None)
                if editor:
                    box_title = f"选项 ({q_idx+1}/{total_q})" if total_q > 1 else "选项"
                    try:
                        await editor.edit(
                            f"[OK] <b>{box_title} 已确认：</b>\n<blockquote>{html.escape(summary)}</blockquote>",
                            force=True,
                            parse_mode=ParseMode.HTML,
                            reply_markup=None,
                        )
                    except Exception as exc:
                        logger.warning(f"Failed to edit submitted box {q_idx}: {exc}")

                await self._check_and_submit_all_boxes(chat_id, pending, trigger_q_idx=q_idx, query=query)
                return

        elif data.startswith("q_skp"):
            q_idx = int(data.split(":")[1]) if ":" in data and data.split(":")[1].isdigit() else 0
            if q_idx < total_q:
                b = boxes.setdefault(q_idx, {})
                if b.get("confirmed"):
                    await self._safe_answer_query(query, "已跳过，请勿重复点击")
                    return

                b["confirmed"] = True
                b["skipped"] = True
                b["selections"] = set()

                # Immediately answer callback query
                await self._safe_answer_query(query, "[SKIP] 已跳过当前选项")

                editor = b.get("editor") or (ThrottledEditor(query.message, min_interval=1.0) if query.message else None)
                if editor:
                    box_title = f"选项 ({q_idx+1}/{total_q})" if total_q > 1 else "选项"
                    try:
                        await editor.edit(
                            f"[SKIP] <b>{box_title} 已跳过</b>",
                            force=True,
                            parse_mode=ParseMode.HTML,
                            reply_markup=None,
                        )
                    except Exception as exc:
                        logger.warning(f"Failed to edit skipped box {q_idx}: {exc}")

                await self._check_and_submit_all_boxes(chat_id, pending, trigger_q_idx=q_idx, query=query)
                return

        else:
            await self._safe_answer_query(query)

    async def _handle_pending_question_text(self, update: Update, text: str) -> bool:
        """Handle chat text replies while a question is pending."""
        chat_id = update.effective_chat.id
        pending = self.pending_questions.get(chat_id)
        if not pending:
            return False

        clean_text = text.strip()
        lower_text = clean_text.lower()
        questions = pending.get("questions", [])
        if not questions:
            return False

        boxes = pending.setdefault("boxes", {})
        total_q = len(questions)

        target_q_idx = None
        for i in range(total_q):
            if not boxes.get(i, {}).get("confirmed", False):
                target_q_idx = i
                break

        if target_q_idx is None:
            return False

        b = boxes.setdefault(target_q_idx, {})
        q = questions[target_q_idx]
        opts = q.get("options", [])
        is_multi = q.get("is_multi_select", False)

        # 1. Skip keywords
        if lower_text in ("skip", "跳过", "pass", "none", "取消"):
            b["skipped"] = True
            b["confirmed"] = True
            b["selections"] = set()
            editor = b.get("editor")
            if editor:
                box_title = f"选项 ({target_q_idx+1}/{total_q})" if total_q > 1 else "选项"
                try:
                    await editor.edit(
                        f"[SKIP] <b>{box_title} 已跳过</b>",
                        force=True,
                        parse_mode=ParseMode.HTML,
                        reply_markup=None,
                    )
                except Exception:
                    pass

            await self._check_and_submit_all_boxes(chat_id, pending, trigger_q_idx=target_q_idx)
            return True

        # 2. Check for numeric tokens (e.g. "1", "1, 2", "1 2 3", "1、2")
        tokens = [t for t in re.split(r"[\s,，、]+", clean_text) if t]
        if tokens and all(t.isdigit() for t in tokens):
            nums = [int(t) for t in tokens]
            valid_nums = [n for n in nums if 1 <= n <= len(opts)]
            if valid_nums:
                b["selections"] = {n - 1 for n in valid_nums}
                b["confirmed"] = True
                b["skipped"] = False
                chosen_texts = [
                    opts[n - 1] if isinstance(opts[n - 1], str) else opts[n - 1].get("text", "")
                    for n in valid_nums
                ]
                summary = ", ".join(chosen_texts)
                editor = b.get("editor")
                if editor:
                    box_title = f"选项 ({target_q_idx+1}/{total_q})" if total_q > 1 else "选项"
                    try:
                        await editor.edit(
                            f"[OK] <b>{box_title} 已确认：</b>\n<blockquote>{html.escape(summary)}</blockquote>",
                            force=True,
                            parse_mode=ParseMode.HTML,
                            reply_markup=None,
                        )
                    except Exception:
                        pass

                await self._check_and_submit_all_boxes(chat_id, pending, trigger_q_idx=target_q_idx)
                return True
            else:
                await update.effective_message.reply_text(
                    f"[WARN] 输入的序号超出第 {target_q_idx+1} 题有效范围（1-{len(opts)}），请重新输入或点击选项按钮。",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return True

        # 3. Free-form text response (write-in response)
        b["write_in"] = clean_text
        b["confirmed"] = True
        b["skipped"] = False
        editor = b.get("editor")
        if editor:
            box_title = f"选项 ({target_q_idx+1}/{total_q})" if total_q > 1 else "选项"
            try:
                await editor.edit(
                    f"[OK] <b>{box_title} 已确认：</b>\n<blockquote>自定义输入：{html.escape(clean_text)}</blockquote>",
                    force=True,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
            except Exception:
                pass

        await self._check_and_submit_all_boxes(chat_id, pending, trigger_q_idx=target_q_idx)
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
        if chat_id not in self.pending_questions:
            self._try_recover_pending_question(chat_id, update.effective_message)

        if chat_id in self.pending_questions:
            handled = await self._handle_pending_question_text(update, text)
            if handled:
                return

        session = self.session_mgr.get_session(chat_id)

        # Handle batch collection mode
        if session.batch_mode:
            count = self.session_mgr.add_batch_message(chat_id, text)
            await update.effective_message.reply_text(
                f"[BUFFER] 已暂存消息 #{count}。发送 `/send` 提交执行，或发送 `/cancel` 取消。",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await self._dispatch_agent_prompt(update, text)

    def _get_user_upload_dir(self, conv_id: Optional[str]) -> Path:
        """Return Antigravity native .user_uploaded artifact directory for zero-permission access."""
        if conv_id:
            upload_dir = self.agent_cli.gemini_dir / "brain" / conv_id / ".user_uploaded"
        else:
            upload_dir = self.agent_cli.gemini_dir / "brain" / "_user_uploaded"
        upload_dir.mkdir(parents=True, exist_ok=True)
        return upload_dir

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming photos from user, save directly to Antigravity native artifacts, and dispatch to Agent."""
        if not await self._check_auth(update):
            return

        photos = update.effective_message.photo
        if not photos:
            return

        chat_id = update.effective_chat.id
        photo = photos[-1]  # Highest resolution
        session = self.session_mgr.get_session(chat_id)
        conv_id = session.active_conversation_id

        status_msg = await update.effective_message.reply_text(
            "[DOWNLOAD] <i>正在接收并下载图片...</i>",
            parse_mode=ParseMode.HTML,
        )
        editor = ThrottledEditor(status_msg, min_interval=1.2)

        timestamp_ms = int(time.time() * 1000)
        ext = ".jpg"

        # Storing directly inside ~/.gemini/antigravity/brain/<conv_id>/.user_uploaded
        # completely avoids Antigravity's "outside workspace" permission confirmation prompts.
        upload_dir = self._get_user_upload_dir(conv_id)
        target_path = upload_dir / f"media_{timestamp_ms}{ext}"

        try:
            tg_file = await photo.get_file()
            await tg_file.download_to_drive(custom_path=str(target_path))
        except Exception as e:
            logger.exception("Failed to download photo from Telegram")
            await editor.edit(f"[ERROR] 图片下载失败：{e}", force=True, parse_mode=None)
            return

        caption = (update.effective_message.caption or "").strip()
        user_text = caption or "请查看并分析这张图片。"
        prompt = (
            f"{user_text}\n\n"
            f"<ADDITIONAL_METADATA>\n"
            f"The user has uploaded 1 image(s):\n"
            f"- {target_path.resolve()}\n"
            f"You can embed this image in an artifact if you need the USER to review it.\n"
            f"</ADDITIONAL_METADATA>"
        )

        await self._dispatch_agent_prompt(update, prompt, editor=editor)

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming documents/files from user, save directly to Antigravity native artifacts, and dispatch to Agent."""
        if not await self._check_auth(update):
            return

        doc = update.effective_message.document
        if not doc:
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)
        conv_id = session.active_conversation_id

        raw_name = doc.file_name or f"file_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{doc.file_unique_id[:6]}"
        clean_name = re.sub(r'[/\\?%*:|"<>]', '_', raw_name)
        mime_type = doc.mime_type or "application/octet-stream"
        is_image = mime_type.startswith("image/") or clean_name.lower().endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
        )

        file_size_mb = (doc.file_size or 0) / (1024 * 1024)
        size_str = f"{file_size_mb:.2f} MB" if file_size_mb >= 1.0 else f"{(doc.file_size or 0) / 1024:.1f} KB"

        status_msg = await update.effective_message.reply_text(
            f"[DOWNLOAD] <i>正在下载文件：<b>{html.escape(clean_name)}</b> ({size_str})...</i>",
            parse_mode=ParseMode.HTML,
        )
        editor = ThrottledEditor(status_msg, min_interval=1.2)

        timestamp_ms = int(time.time() * 1000)
        ext = Path(clean_name).suffix or (".jpg" if is_image else "")

        upload_dir = self._get_user_upload_dir(conv_id)
        target_path = upload_dir / (f"media_{timestamp_ms}{ext}" if is_image else clean_name)

        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(custom_path=str(target_path))
        except Exception as e:
            logger.exception("Failed to download document from Telegram")
            await editor.edit(f"[ERROR] 文件下载失败：{e}", force=True, parse_mode=None)
            return

        caption = (update.effective_message.caption or "").strip()
        if is_image:
            user_text = caption or "请查看并分析这张图片。"
            prompt = (
                f"{user_text}\n\n"
                f"<ADDITIONAL_METADATA>\n"
                f"The user has uploaded 1 image(s):\n"
                f"- {target_path.resolve()}\n"
                f"You can embed this image in an artifact if you need the USER to review it.\n"
                f"</ADDITIONAL_METADATA>"
            )
        else:
            user_text = caption or "请读取并分析这个文件。"
            prompt = (
                f"{user_text}\n\n"
                f"<ADDITIONAL_METADATA>\n"
                f"The user has uploaded 1 file(s):\n"
                f"- {target_path.resolve()} (name: {clean_name}, type: {mime_type}, size: {size_str})\n"
                f"</ADDITIONAL_METADATA>"
            )

        await self._dispatch_agent_prompt(update, prompt, editor=editor)

    async def handle_voice_or_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming voice notes and audio clips from user, store in native artifacts, and dispatch to Agent."""
        if not await self._check_auth(update):
            return

        msg = update.effective_message
        if not msg:
            return

        voice = msg.voice
        audio = msg.audio
        if not voice and not audio:
            return

        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)
        conv_id = session.active_conversation_id

        media_type = "语音留言" if voice else "音频文件"
        status_msg = await msg.reply_text(
            f"[VOICE] <i>正在接收并下载{media_type}...</i>",
            parse_mode=ParseMode.HTML,
        )
        editor = ThrottledEditor(status_msg, min_interval=1.2)

        timestamp_ms = int(time.time() * 1000)
        if voice:
            ext = ".ogg"
            filename = f"voice_{timestamp_ms}{ext}"
            tg_media = voice
        else:
            orig_name = audio.file_name or "audio.mp3"
            ext = Path(orig_name).suffix or ".mp3"
            clean_name = re.sub(r"[^\w.-]", "_", Path(orig_name).stem)
            filename = f"{clean_name}_{timestamp_ms}{ext}"
            tg_media = audio

        upload_dir = self._get_user_upload_dir(conv_id)
        target_path = upload_dir / filename

        try:
            tg_file = await tg_media.get_file()
            await tg_file.download_to_drive(custom_path=str(target_path))
        except Exception as e:
            logger.exception(f"Failed to download {media_type} from Telegram")
            await editor.edit(f"[ERROR] {media_type}下载失败：{e}", force=True, parse_mode=None)
            return

        caption = (msg.caption or "").strip()
        user_text = caption or f"请听取并处理这段{media_type}内容。"
        prompt = (
            f"{user_text}\n\n"
            f"<ADDITIONAL_METADATA>\n"
            f"The user has sent a voice/audio message:\n"
            f"- {target_path.resolve()}\n"
            f"Listen to the audio, transcribe or analyze its content, and fulfill the user request.\n"
            f"</ADDITIONAL_METADATA>"
        )

        await self._dispatch_agent_prompt(update, prompt, editor=editor)

    async def cmd_getfile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Download and send a specified local file from workspace to Telegram."""
        if not await self._check_auth(update):
            return

        args = context.args or []
        if not args:
            await update.effective_message.reply_text(
                "[*] *用法*：`/getfile <文件路径>`\n例如：`/getfile README.md` 或绝对路径",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        req_path = " ".join(args).strip()
        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)
        target = Path(req_path)
        if not target.is_absolute():
            target = Path(session.workspace or self.default_workspace) / target

        target = target.resolve()
        if not target.is_file():
            await update.effective_message.reply_text(
                f"[ERROR] 未找到指定文件：`{target}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        bot = getattr(self, "bot", None) or (update.effective_message.get_bot() if update.effective_message else None)
        status_msg = await update.effective_message.reply_text("[UPLOAD] 正在上传发送文件...")

        ext = target.suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            success = await self._send_image_file(chat_id, str(target), caption=f"<code>{html.escape(target.name)}</code>", bot=bot)
        else:
            success = await self._send_document_file(chat_id, str(target), caption=f"<code>{html.escape(target.name)}</code>", bot=bot)

        if success:
            try:
                await status_msg.delete()
            except Exception:
                pass
        else:
            try:
                await status_msg.edit_text("[ERROR] 发送文件失败，请检查文件权限或大小（Telegram 单文件上限 50MB）。")
            except Exception:
                pass

    async def _send_image_file(
        self,
        chat_id: int,
        img_path: str,
        caption: Optional[str] = None,
        bot: Optional[Any] = None,
    ) -> bool:
        """Send a local image file to the user via Telegram send_photo."""
        if not os.path.isfile(img_path):
            return False
        bot = bot or getattr(self, "bot", None)
        if not bot:
            return False
        try:
            with open(img_path, "rb") as photo_f:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_f,
                    caption=caption or "",
                    parse_mode=ParseMode.HTML if caption else None,
                )
            logger.info(f"Successfully sent photo {img_path} to chat {chat_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to send photo {img_path} to chat {chat_id}: {e}")
            return False

    async def _send_document_file(
        self,
        chat_id: int,
        file_path: str,
        caption: Optional[str] = None,
        bot: Optional[Any] = None,
    ) -> bool:
        """Send a local document/file to the user via Telegram send_document."""
        if not os.path.isfile(file_path):
            return False
        bot = bot or getattr(self, "bot", None)
        if not bot:
            return False
        try:
            with open(file_path, "rb") as doc_f:
                await bot.send_document(
                    chat_id=chat_id,
                    document=doc_f,
                    filename=os.path.basename(file_path),
                    caption=caption or "",
                    parse_mode=ParseMode.HTML if caption else None,
                )
            logger.info(f"Successfully sent document {file_path} to chat {chat_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to send document {file_path} to chat {chat_id}: {e}")
            return False

    async def _stream_turn_events(
        self,
        chat_id: int,
        conv_id: str,
        editor: ThrottledEditor,
        start_step: int,
        reply_target_msg: Optional[Message] = None,
    ) -> None:
        """Stream real-time thinking, tool calls, and final response from transcript."""
        self.active_editors[chat_id] = editor
        sent_files: Set[str] = set()
        try:
            final_response = ""
            current_status = "[RUNNING] 正在处理中..."

            async for event in self.monitor.stream_events(conv_id, start_step_index=start_step):
                if isinstance(event, ThinkingEvent):
                    if chat_id in self.pending_questions:
                        continue
                    clean_thought = html.escape(event.thought.strip().replace("\n", " ")[:80])
                    current_status = f"[THINKING] <b>思考中：</b> <i>{clean_thought}...</i>"
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

                        if questions and chat_id not in self.pending_questions:
                            bot = editor.message.get_bot() if editor.message else getattr(self, "bot", None)
                            if bot:
                                await self.send_pending_questions(
                                    bot=bot,
                                    chat_id=chat_id,
                                    conv_id=conv_id,
                                    step_index=event.step_index,
                                    questions=questions,
                                    existing_editor=editor,
                                )
                                continue

                    if chat_id in self.pending_questions:
                        continue

                    action = event.tool_action or event.tool_summary or ""
                    clean_tool = html.escape(tool_name)
                    clean_detail = f" ({html.escape(action)})" if action else ""
                    current_status = f"[TOOL] <b>正在执行工具：</b> <code>{clean_tool}</code>{clean_detail}..."
                    await editor.edit(current_status, parse_mode=ParseMode.HTML)

                elif isinstance(event, ToolResultEvent):
                    self.pending_questions.pop(chat_id, None)
                    current_status = "[RESULT] 正在处理工具执行结果..."
                    await editor.edit(current_status, parse_mode=None, reply_markup=None)

                    # Auto-detect generated image and document paths and send to Telegram
                    out_text = str(event.raw_step.get("content", ""))
                    file_matches = re.findall(r"(/[\w./-]+\.(?:png|jpg|jpeg|webp|gif|pdf|csv|xlsx|zip|docx|tar\.gz))", out_text)
                    for match in file_matches:
                        if match not in sent_files and os.path.isfile(match):
                            sent_files.add(match)
                            bot = getattr(self, "bot", None) or (editor.message.get_bot() if editor and editor.message else None)
                            ext = Path(match).suffix.lower()
                            if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                                await self._send_image_file(chat_id, match, caption="[IMAGE] <b>Agent 已生成并发送图片</b>", bot=bot)
                            else:
                                await self._send_document_file(chat_id, match, caption=f"[FILE] <b>Agent 已生成并发送文件：</b> <code>{html.escape(os.path.basename(match))}</code>", bot=bot)

                elif isinstance(event, TurnCompleteEvent):
                    self.pending_questions.pop(chat_id, None)
                    final_response = event.final_content
                    break

                elif isinstance(event, ContentEvent):
                    final_response = event.content

                elif isinstance(event, ErrorEvent):
                    self.pending_questions.pop(chat_id, None)
                    await editor.edit(f"[ERROR] *执行出错：* {event.error_message}", force=True)
                    return

            # Display final agent response
            if final_response:
                # Auto-detect any markdown images in final response
                md_img_matches = re.findall(r"!\[(.*?)\]\((/[^\)]+\.(?:png|jpg|jpeg|webp|gif))\)", final_response)
                for caption, path in md_img_matches:
                    if path not in sent_files and os.path.isfile(path):
                        sent_files.add(path)
                        bot = getattr(self, "bot", None) or (editor.message.get_bot() if editor and editor.message else None)
                        await self._send_image_file(chat_id, path, caption=caption or None, bot=bot)

                # Auto-detect any markdown document download links in final response
                md_doc_matches = re.findall(r"\[(.*?)\]\((?:file://)?(/[^\)]+\.(?:pdf|csv|xlsx|zip|docx|tar\.gz))\)", final_response)
                for caption, path in md_doc_matches:
                    if path not in sent_files and os.path.isfile(path):
                        sent_files.add(path)
                        bot = getattr(self, "bot", None) or (editor.message.get_bot() if editor and editor.message else None)
                        await self._send_document_file(chat_id, path, caption=f"[DOWNLOAD] <b>文件下载：</b> <code>{html.escape(os.path.basename(path))}</code>", bot=bot)

                # Clean markdown image syntax ![[caption]](path) and document links [text](path) from final_response
                # to prevent ugly raw markdown tags showing up in Telegram
                cleaned_response = re.sub(r"!\[(.*?)\]\((/[^\)]+\.(?:png|jpg|jpeg|webp|gif))\)", "", final_response)
                cleaned_response = re.sub(r"\[(.*?)\]\((?:file://)?(/[^\)]+\.(?:pdf|csv|xlsx|zip|docx|tar\.gz))\)", r"\1", cleaned_response)
                cleaned_response = re.sub(r"\n{3,}", "\n\n", cleaned_response).strip()

                if cleaned_response:
                    chunks = split_message(cleaned_response, max_length=4000)
                    # First chunk edits the status message
                    success = await editor.edit(chunks[0], force=True)
                    if not success and reply_target_msg:
                        try:
                            await reply_target_msg.reply_text(
                                chunks[0], parse_mode=ParseMode.MARKDOWN
                            )
                        except Exception:
                            await reply_target_msg.reply_text(chunks[0], parse_mode=None)

                    # Subsequent chunks sent as new messages
                    for chunk in chunks[1:]:
                        target = reply_target_msg or editor.message
                        if target:
                            try:
                                await target.reply_text(
                                    chunk, parse_mode=ParseMode.MARKDOWN
                                )
                            except Exception:
                                await target.reply_text(chunk, parse_mode=None)
                elif sent_files:
                    # If all content was images/files that were already sent via send_photo/send_document, delete or complete status
                    try:
                        await editor.message.delete()
                    except Exception:
                        await editor.edit("[OK] <b>图片/文件已发送。</b>", force=True, parse_mode=ParseMode.HTML)
                else:
                    await editor.edit("[OK] 任务已执行完成（无文本输出内容）。", force=True)

        except asyncio.CancelledError:
            logger.info(f"Task for chat {chat_id} was cancelled by /stop")
            try:
                await editor.edit(
                    "[STOP] <b>任务已被用户手动停止。</b>",
                    force=True,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
            except Exception:
                pass
            raise
        except Exception as exc:
            logger.exception("Error executing prompt on Antigravity Agent")
            await editor.edit(f"[ERROR] *执行失败：* `{exc}`", force=True)
        finally:
            if chat_id not in self.pending_questions:
                self.active_tasks.pop(chat_id, None)
                self.active_editors.pop(chat_id, None)

    async def _dispatch_agent_prompt(
        self,
        update: Update,
        prompt: str,
        editor: Optional[ThrottledEditor] = None,
    ) -> None:
        chat_id = update.effective_chat.id
        session = self.session_mgr.get_session(chat_id)

        conv_id = session.active_conversation_id
        start_step = 0

        if not editor:
            status_msg = await update.effective_message.reply_text(
                "[CONNECT] *正在连接本地 Antigravity Agent...*",
                parse_mode=ParseMode.MARKDOWN,
            )
            editor = ThrottledEditor(status_msg, min_interval=1.2)

        current_task = asyncio.current_task()
        if current_task:
            self.active_tasks[chat_id] = current_task
        self.active_editors[chat_id] = editor

        try:
            # Auto-create conversation if none exists
            if not conv_id:
                await editor.edit("[INIT] 正在初始化新的 Antigravity 会话...")
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
                await editor.edit(f"[DISPATCH] 正在派发任务至会话 `{conv_id[:8]}...`")
                await self.agent_cli.send_message(
                    conversation_id=conv_id,
                    content=prompt,
                )

            await self._stream_turn_events(chat_id, conv_id, editor, start_step, update.effective_message)
        finally:
            self.active_tasks.pop(chat_id, None)
            self.active_editors.pop(chat_id, None)
