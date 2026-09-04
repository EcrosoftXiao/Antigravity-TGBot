"""Monitors transcript.jsonl for real-time Agent reasoning, tools, and responses."""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncGenerator, Dict, Optional, Set

from .models import (
    AgentEvent,
    ContentEvent,
    ErrorEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnCompleteEvent,
)

logger = logging.getLogger(__name__)


class TranscriptMonitor:
    """Watches a conversation transcript.jsonl and yields real-time agent events."""

    def __init__(self, gemini_dir: Optional[str] = None):
        self.gemini_dir = Path(
            gemini_dir or os.path.expanduser("~/.gemini/antigravity")
        ).resolve()

    def get_transcript_path(self, conversation_id: str) -> Path:
        return (
            self.gemini_dir
            / "brain"
            / conversation_id
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )

    def get_current_max_step(self, conversation_id: str) -> int:
        """Get the highest step_index currently in transcript.jsonl."""
        path = self.get_transcript_path(conversation_id)
        if not path.is_file():
            return -1

        max_step = -1
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        idx = data.get("step_index", -1)
                        if idx > max_step:
                            max_step = idx
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return max_step

    async def stream_events(
        self,
        conversation_id: str,
        start_step_index: int = 0,
        timeout: float = 300.0,
        poll_interval: float = 0.5,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Stream new events from transcript.jsonl starting after start_step_index."""
        path = self.get_transcript_path(conversation_id)
        start_time = time.time()

        # Wait for file to exist if conversation was just created
        while not path.is_file():
            if time.time() - start_time > 15.0:
                yield ErrorEvent(
                    step_index=start_step_index,
                    error_message=f"Transcript log not found for conversation {conversation_id}",
                )
                return
            await asyncio.sleep(0.3)

        seen_steps: Set[int] = set()
        file_pos = 0
        last_activity_time = time.time()
        last_seen_step = start_step_index - 1

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                while True:
                    if time.time() - last_activity_time > timeout:
                        logger.warning(f"Timeout waiting for response in {conversation_id}")
                        yield ErrorEvent(
                            step_index=last_seen_step,
                            error_message="Response timed out after 300 seconds of inactivity.",
                        )
                        return

                    line = f.readline()
                    if line:
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            step_data = json.loads(line)
                        except json.JSONDecodeError:
                            # Possible partial line write, wait and retry
                            await asyncio.sleep(0.2)
                            continue

                        step_idx = step_data.get("step_index", -1)
                        if step_idx < start_step_index or step_idx in seen_steps:
                            continue

                        seen_steps.add(step_idx)
                        last_seen_step = max(last_seen_step, step_idx)
                        last_activity_time = time.time()

                        step_type = step_data.get("type")
                        source = step_data.get("source")
                        status = step_data.get("status")

                        if step_type == "PLANNER_RESPONSE" and source == "MODEL":
                            # 1. Thinking
                            thinking = step_data.get("thinking")
                            if thinking and isinstance(thinking, str) and thinking.strip():
                                yield ThinkingEvent(
                                    step_index=step_idx,
                                    raw_step=step_data,
                                    thought=thinking.strip(),
                                )

                            # 2. Tool Calls
                            tool_calls = step_data.get("tool_calls")
                            if tool_calls and isinstance(tool_calls, list):
                                for tc in tool_calls:
                                    t_name = tc.get("name", "")
                                    args = tc.get("args", {})
                                    if isinstance(args, str):
                                        try:
                                            args = json.loads(args)
                                        except Exception:
                                            pass

                                    summary = (
                                        tc.get("toolSummary")
                                        or (args.get("toolSummary") if isinstance(args, dict) else "")
                                        or ""
                                    )
                                    action = (
                                        tc.get("toolAction")
                                        or (args.get("toolAction") if isinstance(args, dict) else "")
                                        or ""
                                    )
                                    yield ToolCallEvent(
                                        step_index=step_idx,
                                        raw_step=step_data,
                                        tool_name=t_name,
                                        tool_summary=summary,
                                        tool_action=action,
                                        arguments=args if isinstance(args, dict) else {},
                                    )

                            # 3. Content
                            content = step_data.get("content")
                            if content and isinstance(content, str) and content.strip():
                                yield ContentEvent(
                                    step_index=step_idx,
                                    raw_step=step_data,
                                    content=content.strip(),
                                )

                                # Turn complete when DONE and no tools pending
                                if status == "DONE" and not tool_calls:
                                    yield TurnCompleteEvent(
                                        step_index=step_idx,
                                        raw_step=step_data,
                                        final_content=content.strip(),
                                    )
                                    return

                        elif step_type == "GENERIC":
                            # Tool Output
                            out = step_data.get("content", "")
                            yield ToolResultEvent(
                                step_index=step_idx,
                                raw_step=step_data,
                                output_preview=str(out)[:200],
                            )

                    else:
                        # No new line right now, sleep briefly
                        await asyncio.sleep(poll_interval)

        except Exception as exc:
            logger.exception(f"Error reading transcript {path}: {exc}")
            yield ErrorEvent(
                step_index=last_seen_step,
                error_message=f"Error reading transcript: {exc}",
            )
