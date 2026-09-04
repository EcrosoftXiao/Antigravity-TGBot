"""Formatting utilities, message chunking, and rate-limiting editor for Telegram."""

import asyncio
import logging
import re
import time
from typing import List, Optional
from telegram import Message
from telegram.error import BadRequest

logger = logging.getLogger(__name__)


def split_message(text: str, max_length: int = 4000) -> List[str]:
    """Split long text into Telegram-compliant chunks preserving code blocks."""
    if len(text) <= max_length:
        return [text]

    chunks: List[str] = []
    lines = text.split("\n")
    current_chunk: List[str] = []
    current_len = 0
    in_code_block = False
    current_code_lang = ""

    for line in lines:
        line_len = len(line) + 1  # newline
        if line.strip().startswith("```"):
            if in_code_block:
                in_code_block = False
                current_code_lang = ""
            else:
                in_code_block = True
                current_code_lang = line.strip()[3:].strip()

        if current_len + line_len > max_length:
            if in_code_block:
                # Close code block in current chunk
                current_chunk.append("```")
                chunks.append("\n".join(current_chunk))
                # Re-open code block in next chunk
                current_chunk = [f"```{current_code_lang}", line]
                current_len = len(current_chunk[0]) + 1 + line_len
            else:
                chunks.append("\n".join(current_chunk))
                current_chunk = [line]
                current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len

    if current_chunk:
        if in_code_block:
            current_chunk.append("```")
        chunks.append("\n".join(current_chunk))

    return chunks


class ThrottledEditor:
    """Safely updates Telegram messages with rate-limit protection and fallback."""

    def __init__(self, message: Message, min_interval: float = 1.2):
        self.message = message
        self.min_interval = min_interval
        self.last_edit_time = 0.0
        self.last_text = ""
        self._lock = asyncio.Lock()

    async def edit(self, text: str, force: bool = False, parse_mode: Optional[str] = "Markdown") -> bool:
        """Edit the target message with throttling. Returns True if edit succeeded."""
        async with self._lock:
            now = time.time()
            if not force and (now - self.last_edit_time < self.min_interval):
                return False

            if text == self.last_text:
                return False

            try:
                await self.message.edit_text(text, parse_mode=parse_mode)
                self.last_edit_time = now
                self.last_text = text
                return True
            except BadRequest as exc:
                err_msg = str(exc)
                err_msg_lower = err_msg.lower()
                if "message is not modified" in err_msg_lower:
                    return False
                if any(kw in err_msg_lower for kw in ("parse entities", "entity", "formatting", "can't find end")):
                    # Fallback to plain text without parse_mode if Telegram rejects Markdown
                    try:
                        logger.warning(
                            f"Telegram rejected formatted text ({err_msg}). Falling back to plain text edit."
                        )
                        await self.message.edit_text(text, parse_mode=None)
                        self.last_edit_time = now
                        self.last_text = text
                        return True
                    except Exception as fallback_exc:
                        logger.error(f"Fallback plain-text edit failed: {fallback_exc}")
                logger.warning(f"Failed to edit message: {exc}")
                return False
            except Exception as exc:
                logger.warning(f"Unexpected error editing message: {exc}")
                return False
