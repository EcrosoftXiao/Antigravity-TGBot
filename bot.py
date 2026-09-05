#!/usr/bin/env python3
"""Entrypoint for Antigravity Telegram Bot Remote Control Bridge."""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from dotenv import load_dotenv

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from antigravity_bridge.adapters.telegram.bot import TelegramBotAdapter
from antigravity_bridge.core.agent_cli import AgentCliBridge
from antigravity_bridge.core.session_manager import SessionManager
from antigravity_bridge.core.transcript_monitor import TranscriptMonitor

# Setup logging
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    force=True,
)
logging.getLogger("antigravity_bridge").setLevel(logging.INFO)
logger = logging.getLogger("antigravity_bridge")



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Antigravity 本地 Agent 的 Telegram 远程控制桥接程序"
    )
    parser.add_argument("--token", type=str, help="Telegram Bot Token")
    parser.add_argument("--model", type=str, default=None, help="默认模型 (支持 1~7 序号或模型名称，如 flash, sonnet)")
    parser.add_argument("--workspace", type=str, default=None, help="默认本地工程工作区路径")
    parser.add_argument("--allowed-users", type=str, default=None, help="以逗号分隔的 Telegram 用户 ID 白名单")
    parser.add_argument("--debug", action="store_true", help="开启调试日志输出")
    return parser.parse_args()


async def main() -> None:
    # 1. Load environment
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_path)

    args = parse_args()
    if args.debug or os.getenv("DEBUG", "").lower() in ["1", "true", "yes"]:
        logging.getLogger().setLevel(logging.DEBUG)

    # 2. Extract configuration
    token = args.token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "your_telegram_bot_token_here":
        logger.error(
            "\n"
            "❌ 缺少 TELEGRAM_BOT_TOKEN 配置！\n"
            "请通过以下任一方式进行配置：\n"
            "  1. 创建 .env 文件：复制 .env.example 为 .env 并填入你的 TELEGRAM_BOT_TOKEN\n"
            "  2. 设置环境变量：export TELEGRAM_BOT_TOKEN='你的Token'\n"
            "  3. 传递启动参数：python3 bot.py --token <你的Token>\n"
        )
        sys.exit(1)

    default_model = args.model or os.getenv("DEFAULT_MODEL", "flash")
    default_ws = args.workspace or os.getenv("DEFAULT_WORKSPACE") or str(Path(__file__).resolve().parent)
    allowed_users_raw = args.allowed_users or os.getenv("ALLOWED_USERS", "")
    allowed_users = set()
    if allowed_users_raw.strip():
        for uid_str in allowed_users_raw.split(","):
            uid_str = uid_str.strip()
            if uid_str.isdigit():
                allowed_users.add(int(uid_str))

    agentapi_custom = os.getenv("AGENTAPI_PATH")

    # 3. Initialize Core Bridge & Detect Antigravity Environment
    agent_cli = AgentCliBridge(agentapi_path=agentapi_custom)
    try:
        ls_addr = agent_cli.ensure_connection()
    except Exception as exc:
        logger.error(f"❌ {exc}")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("🛸 正在启动 Antigravity Agent Telegram 远程遥控服务")
    logger.info(f"🔗 本地宿主服务:   {ls_addr}")
    logger.info(f"📁 默认工程工作区: {default_ws}")
    logger.info(f"🧠 默认使用模型:   {default_model}")
    logger.info(f"🔒 授权用户列表:   {list(allowed_users) if allowed_users else '全部放行 (公开)'}")
    logger.info("=" * 60)

    monitor = TranscriptMonitor()
    session_mgr = SessionManager()

    # 4. Initialize Telegram Adapter
    telegram_bot = TelegramBotAdapter(
        token=token,
        agent_cli=agent_cli,
        monitor=monitor,
        session_mgr=session_mgr,
        allowed_users=allowed_users,
        default_model=default_model,
        default_workspace=default_ws,
    )

    # 5. Graceful shutdown handler
    stop_event = asyncio.Event()

    def _signal_handler(sig, frame):
        logger.info(f"收到终止信号 {sig}，正在优雅关闭服务...")
        stop_event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, _signal_handler)

    # 6. Run bot
    await telegram_bot.start()
    logger.info("✅ Telegram Bot 已成功启动！按 Ctrl+C 退出。")

    try:
        await stop_event.wait()
    finally:
        await telegram_bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
