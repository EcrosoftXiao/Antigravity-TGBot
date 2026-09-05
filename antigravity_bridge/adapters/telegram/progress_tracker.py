"""Real-time progress and status tracking for Antigravity Telegram turns."""

import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def format_duration(seconds: float) -> str:
    """Format duration in seconds into a human-readable string like '12s' or '1m 23s'."""
    sec = max(0, int(seconds))
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    return f"{m}m {s}s"


def clean_arg_string(val: Any) -> str:
    """Helper to unwrap extra outer quotes or raw strings from tool arguments."""
    if val is None:
        return ""
    s = str(val).strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s


def extract_thought_summary(thought: str, max_chars: int = 220) -> Tuple[Optional[str], str]:
    """Extract a concise structured title and body summary from model chain-of-thought text.

    Returns:
        (title, body) where title may be None if no explicit section header exists.
    """
    if not thought or not thought.strip():
        return None, ""

    text = thought.strip()

    # Match bold heading at start: **Title** or Markdown heading: # Title
    title: Optional[str] = None
    body_candidate = text

    heading_match = re.match(r"^(?:\*\*([^*]+)\*\*|#{1,4}\s*([^\n]+))\s*\n*", text)
    if heading_match:
        raw_title = heading_match.group(1) or heading_match.group(2)
        if raw_title and len(raw_title.strip()) < 80:
            title = raw_title.strip()
            body_candidate = text[heading_match.end() :].strip()

    # Clean body candidate
    # Remove markdown formatting like bold, backticks for preview cleanliness
    lines = [line.strip() for line in body_candidate.splitlines() if line.strip()]
    cleaned_paragraphs: List[str] = []
    for line in lines:
        if line.startswith("#"):
            continue
        cleaned_paragraphs.append(line)

    body = " ".join(cleaned_paragraphs)
    # Remove excessive backticks or double asterisks
    body = re.sub(r"\*\*([^*]+)\*\*", r"\1", body)
    body = re.sub(r"`([^`]+)`", r"\1", body)
    body = re.sub(r"\s+", " ", body).strip()

    if len(body) > max_chars:
        # Cut cleanly at last punctuation or space
        truncated = body[:max_chars]
        last_punct = max(truncated.rfind("。"), truncated.rfind("."), truncated.rfind("，"), truncated.rfind(","))
        if last_punct > int(max_chars * 0.6):
            body = truncated[: last_punct + 1] + ".."
        else:
            body = truncated.rsplit(" ", 1)[0] + "..."

    return title, body


class Phase(str, Enum):
    INIT = "INIT"
    THINKING = "THINKING"
    TOOL = "TOOL"
    SUBAGENT = "SUBAGENT"
    RESULT = "RESULT"
    COMPLETE = "COMPLETE"


@dataclass
class SubagentInfo:
    role: str
    type_name: str
    model: str = "inherit"
    status: str = "RUNNING"  # RUNNING, DONE, ERROR
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None

    @property
    def elapsed_seconds(self) -> float:
        end = self.end_time or time.time()
        return max(0.0, end - self.start_time)


class TurnProgressTracker:
    """Tracks phase, elapsed times, thoughts, tool calls, and subagents for a turn."""

    def __init__(self, conversation_id: str):
        self.conversation_id = conversation_id
        self.turn_start_time = time.time()
        self.step_start_time = time.time()
        self.phase = Phase.INIT

        # Thinking state
        self.thought_title: Optional[str] = None
        self.thought_body: str = ""

        # Tool state
        self.current_tool_name: str = ""
        self.current_tool_action: str = ""
        self.current_tool_summary: str = ""

        # Subagents state
        self.subagents: List[SubagentInfo] = []

    def on_thinking(self, raw_thought: str) -> None:
        """Update tracker with new model thinking content."""
        if self.phase != Phase.THINKING:
            self.phase = Phase.THINKING
            self.step_start_time = time.time()

        title, body = extract_thought_summary(raw_thought)
        if title:
            self.thought_title = title
        if body:
            self.thought_body = body

    def on_tool_call(
        self,
        tool_name: str,
        tool_summary: str = "",
        tool_action: str = "",
        arguments: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update tracker when a tool is called."""
        self.step_start_time = time.time()
        args = arguments or {}

        if tool_name == "invoke_subagent":
            self.phase = Phase.SUBAGENT
            self._parse_subagents(args)
        else:
            self.phase = Phase.TOOL
            self.current_tool_name = tool_name
            self.current_tool_summary = clean_arg_string(tool_summary)
            self.current_tool_action = clean_arg_string(tool_action)

    def _parse_subagents(self, args: Dict[str, Any]) -> None:
        """Parse subagents list from invoke_subagent arguments."""
        subagents_data = args.get("Subagents", [])
        if isinstance(subagents_data, str):
            try:
                subagents_data = json.loads(subagents_data)
            except Exception:
                subagents_data = []

        if isinstance(subagents_data, list):
            for item in subagents_data:
                if isinstance(item, dict):
                    role = clean_arg_string(item.get("Role") or item.get("TypeName") or "Subagent")
                    type_name = clean_arg_string(item.get("TypeName") or "self")
                    model = clean_arg_string(item.get("Model") or "inherit")

                    # Check if already tracked to avoid duplicates
                    exists = False
                    for existing in self.subagents:
                        if existing.role == role and existing.status == "RUNNING":
                            exists = True
                            break
                    if not exists:
                        self.subagents.append(
                            SubagentInfo(
                                role=role,
                                type_name=type_name,
                                model=model,
                                status="RUNNING",
                                start_time=time.time(),
                            )
                        )

    def on_tool_result(self, is_subagent_complete: bool = False) -> None:
        """Update tracker on tool result."""
        self.phase = Phase.RESULT
        self.step_start_time = time.time()
        if is_subagent_complete or self.subagents:
            # If subagents were running and tool returned, mark running subagents as DONE
            for s in self.subagents:
                if s.status == "RUNNING":
                    s.status = "DONE"
                    s.end_time = time.time()

    def on_turn_complete(self) -> None:
        """Mark turn as complete."""
        self.phase = Phase.COMPLETE
        for s in self.subagents:
            if s.status == "RUNNING":
                s.status = "DONE"
                s.end_time = time.time()

    def format_status_html(self) -> str:
        """Format the current progress status as clean, safe HTML for Telegram."""
        now = time.time()
        total_elapsed = format_duration(now - self.turn_start_time)
        step_elapsed = format_duration(now - self.step_start_time)

        lines: List[str] = []

        has_running_subagents = bool(self.subagents and any(s.status == "RUNNING" for s in self.subagents))

        # 1. If there are actively running subagents, show composite tree
        if has_running_subagents:
            lines.append(f"[SUBAGENT] <b>派发与执行子代理任务</b> (总计: {total_elapsed})")
            if self.phase == Phase.TOOL:
                tool_label = html.escape(self.current_tool_name or "tool")
                lines.append(f"├── <b>主 Agent</b>：正在执行工具 <code>{tool_label}</code> ({step_elapsed})")
            elif self.phase == Phase.THINKING:
                lines.append(f"├── <b>主 Agent</b>：正在思考分析... ({step_elapsed})")
            elif self.phase == Phase.RESULT:
                lines.append(f"├── <b>主 Agent</b>：正在分析工具结果... ({step_elapsed})")
            else:
                lines.append(f"├── <b>主 Agent</b>：正在调度协同任务... ({step_elapsed})")

            lines.append("└── <b>子代理状态树</b>：")
            total_sub = len(self.subagents)
            for idx, sub in enumerate(self.subagents):
                is_last = idx == total_sub - 1
                branch = "    └── " if is_last else "    ├── "
                sub_elapsed = format_duration(sub.elapsed_seconds)
                status_label = "运行中" if sub.status == "RUNNING" else "已完成"
                model_label = f"({html.escape(sub.model)}) " if sub.model else ""
                lines.append(
                    f"{branch}<code>{html.escape(sub.role)}</code> {model_label}[{status_label} · {sub_elapsed}]"
                )

        # 2. Phase-based rendering when no subagents are actively running
        elif self.phase == Phase.INIT:
            lines.append(f"[INIT] <b>正在建立会话与准备任务...</b> ({total_elapsed})")

        elif self.phase == Phase.THINKING:
            lines.append(f"[THINKING] <b>正在思考分析</b> (总计: {total_elapsed} · 本步: {step_elapsed})")
            if self.thought_title or self.thought_body:
                block_content = []
                if self.thought_title:
                    block_content.append(f"<b>{html.escape(self.thought_title)}</b>")
                if self.thought_body:
                    block_content.append(html.escape(self.thought_body))
                lines.append(f"<blockquote>{chr(10).join(block_content)}</blockquote>")

        elif self.phase == Phase.TOOL:
            tool_name = html.escape(self.current_tool_name or "tool")
            lines.append(f"[TOOL] <b>正在执行工具：</b> <code>{tool_name}</code> (总计: {total_elapsed} · 本步: {step_elapsed})")

            detail = self.current_tool_action or self.current_tool_summary
            if detail:
                lines.append(f"<blockquote><b>动作：</b> {html.escape(detail)}</blockquote>")

            # If there were completed subagents earlier, render them as historical summary
            if self.subagents:
                lines.append("└── <b>子任务状态</b>：")
                total_sub = len(self.subagents)
                for idx, sub in enumerate(self.subagents):
                    is_last = idx == total_sub - 1
                    branch = "    └── " if is_last else "    ├── "
                    sub_elapsed = format_duration(sub.elapsed_seconds)
                    status_label = "已完成" if sub.status == "DONE" else sub.status
                    lines.append(f"{branch}<code>{html.escape(sub.role)}</code> [{status_label} · {sub_elapsed}]")

        elif self.phase == Phase.RESULT:
            lines.append(f"[RESULT] <b>正在分析工具执行结果...</b> (总计: {total_elapsed})")
            if self.subagents:
                lines.append("└── <b>子任务状态</b>：")
                total_sub = len(self.subagents)
                for idx, sub in enumerate(self.subagents):
                    is_last = idx == total_sub - 1
                    branch = "    └── " if is_last else "    ├── "
                    sub_elapsed = format_duration(sub.elapsed_seconds)
                    status_label = "已完成" if sub.status == "DONE" else sub.status
                    lines.append(f"{branch}<code>{html.escape(sub.role)}</code> [{status_label} · {sub_elapsed}]")

        else:
            lines.append(f"[RUNNING] <b>正在处理中...</b> ({total_elapsed})")

        return "\n".join(lines)
