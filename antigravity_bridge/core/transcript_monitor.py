"""Monitors transcript.jsonl for real-time Agent reasoning, tools, and responses."""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple

from .models import (
    AgentEvent,
    ArtifactReviewEvent,
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

    def get_pending_question(
        self, conversation_id: str
    ) -> Optional[Tuple[int, List[Dict[str, Any]]]]:
        """Check if the conversation is currently waiting on an unresolved ask_question prompt.

        Returns (step_index, questions) if a pending question is active, otherwise None.
        """
        path = self.get_transcript_path(conversation_id)
        if not path.is_file():
            return None

        lines = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        lines.append(line)
        except OSError:
            return None

        if not lines:
            return None

        recent_lines = lines[-30:] if len(lines) > 30 else lines
        ask_question_step = -1
        questions: Optional[List[Dict[str, Any]]] = None

        for line in reversed(recent_lines):
            try:
                data = json.loads(line)
            except Exception:
                continue

            step_idx = data.get("step_index", -1)
            step_type = data.get("type")

            # If there's a GENERIC or USER_INPUT after the tool call, it has been answered
            if step_type in ("GENERIC", "USER_INPUT") and ask_question_step == -1:
                return None

            if step_type == "PLANNER_RESPONSE":
                tool_calls = data.get("tool_calls", [])
                for tc in tool_calls:
                    if tc.get("name") == "ask_question":
                        ask_question_step = step_idx
                        raw_q = tc.get("args", {}).get("questions", [])
                        if isinstance(raw_q, str):
                            try:
                                questions = json.loads(raw_q)
                            except Exception:
                                questions = None
                        elif isinstance(raw_q, list):
                            questions = raw_q
                        break
                if ask_question_step != -1:
                    break

        if questions and ask_question_step != -1:
            return ask_question_step, questions
        return None

    def get_pending_artifact_approval(
        self, conversation_id: str
    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        """Check if conversation is waiting on an unresolved artifact Proceed / feedback review.

        Returns (step_index, artifact_info) if pending approval exists, otherwise None.
        """
        path = self.get_transcript_path(conversation_id)
        if not path.is_file():
            return None

        lines = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        lines.append(line)
        except OSError:
            return None

        if not lines:
            return None

        recent_lines = lines[-30:] if len(lines) > 30 else lines
        artifact_step = -1
        artifact_info: Optional[Dict[str, Any]] = None

        for line in reversed(recent_lines):
            try:
                data = json.loads(line)
            except Exception:
                continue

            step_idx = data.get("step_index", -1)
            step_type = data.get("type")

            # If there's a USER_INPUT after the tool call, it has been responded to
            if step_type == "USER_INPUT" and artifact_step == -1:
                return None

            if step_type == "PLANNER_RESPONSE":
                tool_calls = data.get("tool_calls", [])
                for tc in tool_calls:
                    if tc.get("name") in ("write_to_file", "replace_file_content"):
                        args = tc.get("args", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                args = {}

                        meta = args.get("ArtifactMetadata", {})
                        if isinstance(meta, str):
                            try:
                                meta = json.loads(meta)
                            except Exception:
                                meta = {}

                        target_file = str(args.get("TargetFile", "")).strip('"\'')
                        req_feedback = False
                        if isinstance(meta, dict):
                            req_feedback = bool(meta.get("RequestFeedback") or meta.get("request_feedback"))

                        file_basename = os.path.basename(target_file) if target_file else ""
                        if req_feedback or file_basename in ("implementation_plan.md", "walkthrough.md"):
                            summary = meta.get("Summary", "") if isinstance(meta, dict) else ""
                            artifact_info = {
                                "artifact_path": target_file,
                                "artifact_name": file_basename,
                                "summary": summary,
                                "request_feedback": req_feedback,
                            }
                            artifact_step = step_idx
                            break
                if artifact_step != -1:
                    break

        if artifact_info and artifact_step != -1:
            return artifact_step, artifact_info
        return None

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

                                    if t_name in ("write_to_file", "replace_file_content"):
                                        meta = args.get("ArtifactMetadata", {}) if isinstance(args, dict) else {}
                                        if isinstance(meta, str):
                                            try:
                                                meta = json.loads(meta)
                                            except Exception:
                                                meta = {}
                                        target_file = str(args.get("TargetFile", "") if isinstance(args, dict) else "").strip('"\'')
                                        req_feedback = False
                                        if isinstance(meta, dict):
                                            req_feedback = bool(meta.get("RequestFeedback") or meta.get("request_feedback"))
                                        file_basename = os.path.basename(target_file) if target_file else ""
                                        if req_feedback or file_basename in ("implementation_plan.md", "walkthrough.md"):
                                            summary_text = meta.get("Summary", "") if isinstance(meta, dict) else ""
                                            yield ArtifactReviewEvent(
                                                step_index=step_idx,
                                                raw_step=step_data,
                                                artifact_path=target_file,
                                                artifact_name=file_basename,
                                                summary=summary_text,
                                                request_feedback=req_feedback,
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
