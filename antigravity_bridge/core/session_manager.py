"""Session state and conversation mapping manager."""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from .models import SessionState

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages active conversation sessions and batch modes per chat/user."""

    def __init__(self, persistence_file: Optional[str] = None):
        self.persistence_path = Path(
            persistence_file or Path(__file__).resolve().parent.parent.parent / ".sessions.json"
        )
        self.sessions: Dict[int, SessionState] = {}
        self.load()

    def load(self) -> None:
        """Load session state from disk if available."""
        if not self.persistence_path.is_file():
            return
        try:
            with open(self.persistence_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for chat_id_str, state_dict in data.items():
                    self.sessions[int(chat_id_str)] = SessionState.from_dict(state_dict)
            logger.info(f"Loaded {len(self.sessions)} sessions from {self.persistence_path}")
        except Exception as exc:
            logger.warning(f"Failed to load sessions from {self.persistence_path}: {exc}")

    def save(self) -> None:
        """Persist sessions to disk."""
        try:
            self.persistence_path.parent.mkdir(parents=True, exist_ok=True)
            data = {str(k): v.to_dict() for k, v in self.sessions.items()}
            with open(self.persistence_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"Failed to save sessions to {self.persistence_path}: {exc}")

    def get_session(self, chat_id: int) -> SessionState:
        """Get or initialize a session for a given chat_id."""
        if chat_id not in self.sessions:
            self.sessions[chat_id] = SessionState(chat_id=chat_id)
            self.save()
        return self.sessions[chat_id]

    def bind_conversation(self, chat_id: int, conversation_id: str) -> None:
        """Bind a specific conversation ID to this chat."""
        session = self.get_session(chat_id)
        session.active_conversation_id = conversation_id
        self.save()
        logger.info(f"Chat {chat_id} bound to conversation {conversation_id}")

    def clear_conversation(self, chat_id: int) -> None:
        """Unbind current conversation from this chat."""
        session = self.get_session(chat_id)
        session.active_conversation_id = None
        session.pending_model_switch = None
        self.save()

    def new_session(self, chat_id: int, model: Optional[str] = None) -> SessionState:
        """Reset conversation bindings and prepare for a brand new conversation."""
        session = self.get_session(chat_id)
        session.active_conversation_id = None
        session.pending_model_switch = None
        if model:
            session.model = model
        self.save()
        return session

    def set_model(self, chat_id: int, model: str, display_name: Optional[str] = None) -> None:
        """Set model in-place without breaking active conversation session."""
        session = self.get_session(chat_id)
        session.model = model
        if session.active_conversation_id:
            session.pending_model_switch = display_name or model
        else:
            session.pending_model_switch = None
        self.save()

    def set_workspace(self, chat_id: int, workspace: str) -> None:
        """Set working directory for this chat."""
        session = self.get_session(chat_id)
        session.workspace = workspace
        self.save()

    def start_batch_mode(self, chat_id: int) -> None:
        """Enable batch collection mode."""
        session = self.get_session(chat_id)
        session.batch_mode = True
        session.batch_buffer = []
        self.save()

    def add_batch_message(self, chat_id: int, message: str) -> int:
        """Add a message to the batch buffer. Returns current buffer length."""
        session = self.get_session(chat_id)
        session.batch_buffer.append(message)
        self.save()
        return len(session.batch_buffer)

    def flush_batch_mode(self, chat_id: int) -> List[str]:
        """Disable batch mode and return all accumulated messages."""
        session = self.get_session(chat_id)
        messages = list(session.batch_buffer)
        session.batch_mode = False
        session.batch_buffer = []
        self.save()
        return messages

    def cancel_batch_mode(self, chat_id: int) -> int:
        """Cancel batch mode and discard buffered messages."""
        session = self.get_session(chat_id)
        count = len(session.batch_buffer)
        session.batch_mode = False
        session.batch_buffer = []
        self.save()
        return count
