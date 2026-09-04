"""Abstract base class for IM platform bot adapters."""

from abc import ABC, abstractmethod
from typing import Any, Optional

from antigravity_bridge.core.agent_cli import AgentCliBridge
from antigravity_bridge.core.session_manager import SessionManager
from antigravity_bridge.core.transcript_monitor import TranscriptMonitor


class BaseBotAdapter(ABC):
    """Base interface for all chat platform adapters (Telegram, Discord, Slack, Feishu, etc.)."""

    def __init__(
        self,
        agent_cli: AgentCliBridge,
        monitor: TranscriptMonitor,
        session_mgr: SessionManager,
    ):
        self.agent_cli = agent_cli
        self.monitor = monitor
        self.session_mgr = session_mgr

    @abstractmethod
    async def start(self) -> None:
        """Start the bot adapter event loop / polling."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the bot adapter gracefully."""
        pass

    @abstractmethod
    async def send_message(self, recipient_id: Any, text: str, **kwargs: Any) -> Any:
        """Send a message to a recipient or channel."""
        pass
