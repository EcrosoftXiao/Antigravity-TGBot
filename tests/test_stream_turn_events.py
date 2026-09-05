import asyncio
from unittest.mock import AsyncMock, MagicMock
from antigravity_bridge.core.models import (
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnCompleteEvent,
)
from antigravity_bridge.adapters.telegram.progress_tracker import TurnProgressTracker


def test_full_turn_lifecycle():
    tracker = TurnProgressTracker(conversation_id="conv-12345")

    # 1. Init
    html_init = tracker.format_status_html()
    assert "[INIT]" in html_init

    # 2. Thinking
    tracker.on_thinking(
        "**Formulating Execution Strategy**\n"
        "We should first inspect the project repository structure and then invoke subagents."
    )
    html_thinking = tracker.format_status_html()
    assert "[THINKING]" in html_thinking
    assert "Formulating Execution Strategy" in html_thinking
    assert "<blockquote expandable>" in html_thinking

    # 3. Invoke Subagent
    tracker.on_tool_call(
        tool_name="invoke_subagent",
        tool_summary="Spawning research agent",
        tool_action="Spawning research agent",
        arguments={
            "Subagents": [
                {
                    "Role": "Codebase Researcher",
                    "TypeName": "research",
                    "Model": "flash",
                },
                {
                    "Role": "Refactor Specialist",
                    "TypeName": "self",
                    "Model": "pro",
                },
            ]
        },
    )
    html_sub = tracker.format_status_html()
    assert "[SUBAGENT]" in html_sub
    assert "Codebase Researcher" in html_sub
    assert "(flash)" in html_sub
    assert "Refactor Specialist" in html_sub
    assert "(pro)" in html_sub
    assert "运行中" in html_sub

    # 4. Main agent runs tool while subagents are running
    tracker.on_tool_call(
        tool_name="view_file",
        tool_summary="Viewing main.py",
        tool_action="view_file /app/main.py",
        arguments={"AbsolutePath": "/app/main.py"},
    )
    html_composite = tracker.format_status_html()
    assert "[SUBAGENT]" in html_composite
    assert "正在执行工具 <code>view_file</code>" in html_composite
    assert "Codebase Researcher" in html_composite

    # 5. Subagents complete via tool result
    tracker.on_tool_result()
    html_res = tracker.format_status_html()
    assert "[RESULT]" in html_res
    assert "已完成" in html_res

    # 6. Subsequent standalone tool call (no subagents running)
    tracker.on_tool_call(
        tool_name="run_command",
        tool_summary="Running git status",
        tool_action="git status",
        arguments={"CommandLine": "git status"},
    )
    html_tool = tracker.format_status_html()
    assert "[TOOL]" in html_tool
    assert "<code>run_command</code>" in html_tool
    assert "git status" in html_tool


    # 6. Turn Complete
    tracker.on_turn_complete()
    assert tracker.phase.value == "COMPLETE"


if __name__ == "__main__":
    test_full_turn_lifecycle()
    print("Integration lifecycle test passed!")
