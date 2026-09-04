"""Telegram Bot Adapter implementation using python-telegram-bot."""

import logging
from typing import Any, Optional, Set
from telegram import BotCommand
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from antigravity_bridge.adapters.base import BaseBotAdapter
from antigravity_bridge.core.agent_cli import AgentCliBridge
from antigravity_bridge.core.session_manager import SessionManager
from antigravity_bridge.core.transcript_monitor import TranscriptMonitor
from .handlers import TelegramHandlers

logger = logging.getLogger(__name__)


class TelegramBotAdapter(BaseBotAdapter):
    """Integrates Telegram messaging with Antigravity Agent bridge."""

    def __init__(
        self,
        token: str,
        agent_cli: AgentCliBridge,
        monitor: TranscriptMonitor,
        session_mgr: SessionManager,
        allowed_users: Optional[Set[int]] = None,
        default_model: str = "flash",
        default_workspace: Optional[str] = None,
    ):
        super().__init__(agent_cli, monitor, session_mgr)
        self.token = token
        self.handlers = TelegramHandlers(
            agent_cli=self.agent_cli,
            monitor=self.monitor,
            session_mgr=self.session_mgr,
            allowed_users=allowed_users,
            default_model=default_model,
            default_workspace=default_workspace,
        )
        self.app: Optional[Application] = None

    def build_application(self) -> Application:
        """Create and configure the Telegram application."""
        app = ApplicationBuilder().token(self.token).build()

        # Command handlers
        app.add_handler(CommandHandler(["start"], self.handlers.cmd_start))
        app.add_handler(CommandHandler(["help"], self.handlers.cmd_help))
        app.add_handler(CommandHandler(["new"], self.handlers.cmd_new))
        app.add_handler(CommandHandler(["session", "s"], self.handlers.cmd_session))
        app.add_handler(CommandHandler(["sessions", "sessionlist", "list"], self.handlers.cmd_sessions))
        app.add_handler(CommandHandler(["history", "hist"], self.handlers.cmd_history))
        app.add_handler(CommandHandler(["status"], self.handlers.cmd_status))
        app.add_handler(CommandHandler(["models", "modellist"], self.handlers.cmd_models))
        app.add_handler(CommandHandler(["model"], self.handlers.cmd_model))
        app.add_handler(CommandHandler(["workspace", "ws"], self.handlers.cmd_workspace))
        app.add_handler(CommandHandler(["batch"], self.handlers.cmd_batch))
        app.add_handler(CommandHandler(["send"], self.handlers.cmd_send))
        app.add_handler(CommandHandler(["cancel"], self.handlers.cmd_cancel))
        app.add_handler(CommandHandler(["stop"], self.handlers.cmd_stop))

        # Plain text message handler
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handlers.handle_message)
        )

        return app

    async def start(self) -> None:
        """Start receiving Telegram updates via long polling."""
        self.app = self.build_application()
        logger.info("Initializing Telegram Bot Adapter...")
        await self.app.initialize()

        # Register command hints in Simplified Chinese for Telegram UI
        try:
            bot_commands = [
                BotCommand("new", "开启全新本地会话 (重置绑定)"),
                BotCommand("sessions", "列出本地历史会话"),
                BotCommand("history", "查看当前会话历史交互记录"),
                BotCommand("session", "绑定指定会话 ID"),
                BotCommand("status", "查看当前会话与系统状态"),
                BotCommand("models", "列出所有可用模型及序号"),
                BotCommand("model", "切换模型 (如 /model 1 或 /model sonnet)"),
                BotCommand("workspace", "查看或切换本地工程目录"),
                BotCommand("batch", "开启批量消息暂存模式"),
                BotCommand("send", "发送并执行批量暂存的消息"),
                BotCommand("cancel", "取消并清空暂存消息"),
                BotCommand("stop", "中断当前正在执行的任务"),
                BotCommand("help", "查看完整指令帮助手册"),
            ]
            await self.app.bot.set_my_commands(bot_commands)
            logger.info("Registered Simplified Chinese bot commands menu.")
        except Exception as e:
            logger.warning(f"Could not set bot commands menu: {e}")

        await self.app.start()
        logger.info("Starting Telegram update polling...")
        await self.app.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        """Gracefully stop updater and application."""
        if self.app:
            logger.info("Stopping Telegram Bot Adapter...")
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            if self.app.running:
                await self.app.stop()
            await self.app.shutdown()
            logger.info("Telegram Bot Adapter shut down cleanly.")

    async def send_message(self, recipient_id: Any, text: str, **kwargs: Any) -> Any:
        """Send message directly using Telegram Bot API."""
        if self.app and self.app.bot:
            return await self.app.bot.send_message(chat_id=recipient_id, text=text, **kwargs)
        raise RuntimeError("Telegram application is not initialized or running.")
