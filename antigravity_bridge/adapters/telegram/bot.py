import asyncio
import logging
import time
from typing import Any, Optional, Set
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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
        self.running: bool = False
        self._sync_task: Optional[asyncio.Task] = None

    def build_application(self) -> Application:
        """Create and configure the Telegram application."""
        app = (
            ApplicationBuilder()
            .token(self.token)
            .concurrent_updates(True)
            .build()
        )

        # Global error handler to prevent crashing on stale callback queries
        async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            err = context.error
            err_str = str(err).lower()
            if "query is too old" in err_str or "message is not modified" in err_str:
                return
            logger.warning(f"Telegram handler exception: {err}")

        app.add_error_handler(_error_handler)

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
        app.add_handler(CommandHandler(["getfile", "get"], self.handlers.cmd_getfile))

        # Plain text message handler
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handlers.handle_message)
        )

        # Photo message handler
        app.add_handler(
            MessageHandler(filters.PHOTO, self.handlers.handle_photo)
        )

        # Document/File message handler
        app.add_handler(
            MessageHandler(filters.Document.ALL & ~filters.COMMAND, self.handlers.handle_document)
        )

        # Voice and Audio message handler
        app.add_handler(
            MessageHandler((filters.VOICE | filters.AUDIO) & ~filters.COMMAND, self.handlers.handle_voice_or_audio)
        )

        # Callback query handler for inline buttons
        app.add_handler(CallbackQueryHandler(self.handlers.handle_callback_query))

        self.handlers.bot = app.bot
        return app

    async def _sync_questions_loop(self) -> None:
        """Continuously synchronize pending ask_question prompts between Antigravity and Telegram."""
        while self.running:
            try:
                await asyncio.sleep(1.0)
                if not self.app or not self.app.bot:
                    continue

                for chat_id, session in list(self.session_mgr.sessions.items()):
                    conv_id = session.active_conversation_id
                    if not conv_id:
                        continue

                    # Don't re-sync if a submission is currently in flight or was just made
                    if chat_id in self.handlers.submitting_questions:
                        continue
                    if time.time() - self.handlers.last_submitted_time.get(chat_id, 0.0) < 4.0:
                        continue

                    pending_info = self.monitor.get_pending_question(conv_id)
                    current_pending = self.handlers.pending_questions.get(chat_id)

                    if pending_info and not current_pending:
                        # New question waiting in Antigravity (e.g. from Web UI or external turn)
                        step_idx, questions = pending_info
                        try:
                            await self.handlers.send_pending_questions(
                                bot=self.app.bot,
                                chat_id=chat_id,
                                conv_id=conv_id,
                                step_index=step_idx,
                                questions=questions,
                            )
                        except Exception as send_err:
                            logger.warning(f"Failed to auto-sync pending questions to chat {chat_id}: {send_err}")

                    elif not pending_info and current_pending:
                        # Question was resolved externally (e.g. in IDE or Web UI)
                        await self.handlers.cleanup_pending_questions(chat_id)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"Question sync loop exception: {exc}")

    async def start(self) -> None:
        """Start receiving Telegram updates via long polling."""
        self.running = True
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
                BotCommand("getfile", "下载并获取本地工程文件"),
                BotCommand("help", "查看完整指令帮助手册"),
            ]
            await self.app.bot.set_my_commands(bot_commands)
            logger.info("Registered Simplified Chinese bot commands menu.")
        except Exception as e:
            logger.warning(f"Could not set bot commands menu: {e}")

        await self.app.start()
        logger.info("Starting Telegram update polling...")
        await self.app.updater.start_polling(drop_pending_updates=False)

        # Start question synchronizer task
        self._sync_task = asyncio.create_task(self._sync_questions_loop())

    async def stop(self) -> None:
        """Gracefully stop updater and application."""
        self.running = False
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()

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

