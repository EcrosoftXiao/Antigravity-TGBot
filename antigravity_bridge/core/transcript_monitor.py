"""Monitors transcript.jsonl for real-time Agent reasoning, tools, and responses."""

import asyncio
import json
import logging
import os
import re
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
        """Get the step_index of the last step in transcript.jsonl.

        Reads from the tail of the file (last ~128KB) for efficiency. After a context
        compaction the transcript is rewritten starting from a lower step_index, so the
        true "current" baseline step is the one at the END of the file, not the
        historical all-time maximum found anywhere in the file. Using the historical
        max would cause start_step to be set too high, filtering out all new steps.
        """
        path = self.get_transcript_path(conversation_id)
        if not path.is_file():
            return -1

        chunk_size = 131072  # 128 KB — enough to contain the last dozen steps
        try:
            file_size = path.stat().st_size
            with open(path, "rb") as f:
                read_size = min(file_size, chunk_size)
                f.seek(file_size - read_size)
                raw = f.read().decode("utf-8", errors="replace")
            lines = raw.splitlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    idx = data.get("step_index", -1)
                    if idx != -1:
                        return idx
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
        return -1

    def get_global_max_step(self, conversation_id: str) -> int:
        """Get the all-time highest step_index ever seen in transcript.jsonl.

        Unlike get_current_max_step() which reads the tail, this scans the entire file
        to find the historical maximum. Use this ONLY for initializing synced_max_steps
        on bot startup, so that pre-compaction steps with high step_indexes are not
        mistakenly treated as new external turns.
        """
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
                        if req_feedback and file_basename != "walkthrough.md":
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

    def get_new_user_turns(
        self, conversation_id: str, after_step_index: int,
        since_time: Optional[str] = None,
    ) -> List[Tuple[int, str]]:
        """Get any new USER_INPUT steps that appeared after after_step_index.

        Reads only the tail of the file (last 512KB) to avoid picking up historical
        steps from before a context compaction rewrite. After compaction the transcript
        is rewritten from a lower step_index, so old high-numbered steps that were in
        the pre-compaction history will not appear in the tail window.

        Args:
            after_step_index: Only return steps with step_index > this value.
            since_time: Optional ISO 8601 timestamp string. When provided, only
                steps whose created_at >= since_time are returned. This handles the
                post-compaction case where new steps may have lower step_indexes than
                the historical maximum, by additionally filtering on wall-clock time.

        Returns list of (step_index, cleaned_prompt).
        """
        path = self.get_transcript_path(conversation_id)
        if not path.is_file():
            return []

        results: List[Tuple[int, str]] = []
        chunk_size = 524288  # 512 KB tail window
        try:
            file_size = path.stat().st_size
            with open(path, "rb") as f:
                read_size = min(file_size, chunk_size)
                f.seek(file_size - read_size)
                raw = f.read().decode("utf-8", errors="replace")
            lines = raw.splitlines()
            # Skip the first (possibly partial) line if we didn't start at offset 0
            start_idx = 1 if (file_size > chunk_size) else 0
            for line in lines[start_idx:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                idx = data.get("step_index", -1)

                # Filter by timestamp if provided
                if since_time:
                    step_ts = data.get("created_at", "")
                    if step_ts < since_time:
                        continue

                # Filter by step_index to prevent re-processing steps already synced
                if after_step_index >= 0 and idx <= after_step_index:
                    continue

                if data.get("type") == "USER_INPUT":
                    content = str(data.get("content", ""))
                    cleaned = self._clean_user_prompt(content)
                    results.append((idx, cleaned))
        except OSError:
            pass
        return results




    @staticmethod
    def _clean_user_prompt(raw: str) -> str:
        if not raw:
            return ""
        m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", raw, re.DOTALL)
        if m and m.group(1).strip():
            return m.group(1).strip()
        cleaned = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", raw, flags=re.DOTALL)
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        return cleaned.strip()

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
        last_activity_time = time.time()
        last_seen_step = start_step_index - 1

        # --- Fast-seek: position the file handle near start_step_index ---
        # For large transcripts (post-compaction), the target step is near the end.
        # We read from the tail with exponentially growing chunks until we find a
        # line whose step_index <= start_step_index, then place the file cursor just
        # before that line so readline() will re-read it in the main loop.
        initial_file_pos = 0
        try:
            file_size = path.stat().st_size
            if file_size > 65536 and start_step_index > 0:
                chunk_size = 262144  # 256 KB initial window
                while chunk_size <= file_size:
                    seek_pos = max(0, file_size - chunk_size)
                    with open(path, "rb") as probe:
                        probe.seek(seek_pos)
                        raw = probe.read(chunk_size).decode("utf-8", errors="replace")
                    probe_lines = raw.splitlines()
                    # Skip the first (possibly partial) line
                    if seek_pos > 0:
                        probe_lines = probe_lines[1:]
                    found_anchor = False
                    for probe_line in probe_lines:
                        probe_line = probe_line.strip()
                        if not probe_line:
                            continue
                        try:
                            pd = json.loads(probe_line)
                            pidx = pd.get("step_index", -1)
                            if pidx != -1 and pidx <= start_step_index:
                                found_anchor = True
                                break
                        except json.JSONDecodeError:
                            continue
                    if found_anchor:
                        initial_file_pos = seek_pos
                        break
                    if chunk_size >= file_size:
                        break
                    chunk_size = min(chunk_size * 4, file_size)
        except Exception:
            initial_file_pos = 0
        # ---------------------------------------------------------------

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if initial_file_pos > 0:
                    f.seek(initial_file_pos)
                    # Discard the first (possibly partial) line from the seek offset
                    f.readline()
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
                                        if req_feedback and file_basename != "walkthrough.md":
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

                        elif step_type == "ERROR_MESSAGE" or "interrupted" in str(step_data.get("content", "")).lower():
                            # Interruption detected (e.g. user clicked Stop in desktop IDE client)
                            err_msg = str(step_data.get("content", "")).strip() or "任务已被客户端中断 (Stop)"
                            yield ErrorEvent(
                                step_index=step_idx,
                                error_message=err_msg,
                            )
                            return

                        elif step_type == "USER_INPUT" and step_idx > start_step_index:
                            # A new user turn was entered externally while this turn was waiting
                            yield ErrorEvent(
                                step_index=step_idx,
                                error_message="已收到新指令，当前等待已中止",
                            )
                            return

                    else:
                        # No new line right now, sleep briefly
                        await asyncio.sleep(poll_interval)

        except Exception as exc:
            logger.exception(f"Error reading transcript {path}: {exc}")
            yield ErrorEvent(
                step_index=last_seen_step,
                error_message=f"Error reading transcript: {exc}",
            )
