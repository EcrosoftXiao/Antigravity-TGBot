import time

from antigravity_bridge.adapters.telegram.progress_tracker import (
    TurnProgressTracker,
    format_duration,
    extract_thought_summary,
    Phase,
)


def test_format_duration():
    assert format_duration(5) == "5s"
    assert format_duration(59) == "59s"
    assert format_duration(60) == "1m 0s"
    assert format_duration(125) == "2m 5s"


def test_extract_thought_summary():
    # Test with bold header
    thought_text = (
        "**Understanding Tool Execution Structure**\n\n"
        "Observed that tool calls provide details on the tool used, a summary, "
        "the action taken, and arguments."
    )
    title, body = extract_thought_summary(thought_text)
    assert title == "Understanding Tool Execution Structure"
    assert "Observed that tool calls provide details" in body

    # Test with markdown header
    thought_header = "### Planning Subagent Execution\nWe need to invoke research subagents."
    title, body = extract_thought_summary(thought_header)
    assert title == "Planning Subagent Execution"
    assert "We need to invoke research subagents." in body

    # Test without header
    plain_thought = "Checking system environment and configuring directory paths."
    title, body = extract_thought_summary(plain_thought)
    assert title is None
    assert "Checking system environment" in body


def test_tracker_thinking_flow():
    tracker = TurnProgressTracker(conversation_id="test-conv")
    tracker.on_thinking("**Analyzing Workspace**\nExamining current files.")
    html_out = tracker.format_status_html()

    assert "[THINKING]" in html_out
    assert "<b>Analyzing Workspace</b>" in html_out
    assert "Examining current files." in html_out
    assert "<blockquote expandable>" in html_out
    assert "</blockquote>" in html_out


def test_tracker_subagents_flow():
    tracker = TurnProgressTracker(conversation_id="test-conv")
    tracker.on_tool_call(
        tool_name="invoke_subagent",
        tool_summary="Spawning subagents",
        arguments={
            "Subagents": [
                {
                    "Role": "Codebase Researcher",
                    "TypeName": "research",
                    "Model": "inherit",
                },
                {
                    "Role": "Database Debugger",
                    "TypeName": "self",
                    "Model": "pro",
                },
            ]
        },
    )

    html_out = tracker.format_status_html()
    assert "[SUBAGENT]" in html_out
    assert "├── <b>主 Agent</b>" in html_out
    assert "Codebase Researcher" in html_out
    assert "(inherit)" in html_out
    assert "Database Debugger" in html_out
    assert "(pro)" in html_out
    assert "运行中" in html_out

    # Tool result finishes subagents
    tracker.on_tool_result()
    html_res = tracker.format_status_html()
    assert "已完成" in html_res


def test_tracker_normal_tool():
    tracker = TurnProgressTracker(conversation_id="test-conv")
    tracker.on_tool_call(
        tool_name="run_command",
        tool_summary="Checking git status",
        tool_action="git status",
        arguments={"CommandLine": "git status"},
    )
    html_out = tracker.format_status_html()
    assert "[TOOL]" in html_out
    assert "<code>run_command</code>" in html_out
    assert "Ran git status" in html_out
    assert "<blockquote expandable>" in html_out


def test_format_tool_detail():
    from antigravity_bridge.adapters.telegram.progress_tracker import format_tool_detail

    # run_command
    assert format_tool_detail("run_command", {"CommandLine": "python3 -m py_compile test.py"}) == "Ran python3 -m py_compile test.py"

    # view_file with line range
    assert format_tool_detail("view_file", {"AbsolutePath": "/path/to/handlers.py", "StartLine": 10, "EndLine": 30}) == "Viewed handlers.py (L10-L30)"

    # replace_file_content
    assert format_tool_detail("replace_file_content", {"TargetFile": "/path/to/models.py", "Description": "Fix type hint"}) == "Edited models.py · Fix type hint"
