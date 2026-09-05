import json
import tempfile
from pathlib import Path
from antigravity_bridge.core.transcript_monitor import TranscriptMonitor


def test_clean_user_prompt():
    raw = (
        "<USER_REQUEST>\n"
        "测试转发IDE客户端指令\n"
        "</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\n"
        "The current local time is: 2026-09-05T16:19:28+08:00.\n"
        "</ADDITIONAL_METADATA>"
    )
    cleaned = TranscriptMonitor._clean_user_prompt(raw)
    assert cleaned == "测试转发IDE客户端指令"

    plain = "直接普通提问内容"
    assert TranscriptMonitor._clean_user_prompt(plain) == "直接普通提问内容"


def test_get_new_user_turns():
    with tempfile.TemporaryDirectory() as tmpdir:
        conv_id = "test-conv-sync"
        log_dir = Path(tmpdir) / "brain" / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript.jsonl"

        monitor = TranscriptMonitor(gemini_dir=tmpdir)

        # 1. Empty -> []
        assert monitor.get_new_user_turns(conv_id, after_step_index=-1) == []

        # 2. Write an initial user input and model response
        steps = [
            {
                "step_index": 1,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\n旧指令\n</USER_REQUEST>",
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "content": "旧回复",
            },
            {
                "step_index": 3,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\n新客户端指令\n</USER_REQUEST>",
            },
        ]

        with open(transcript_file, "w", encoding="utf-8") as f:
            for s in steps:
                f.write(json.dumps(s) + "\n")

        # Query after step 2 (should find step 3)
        new_turns = monitor.get_new_user_turns(conv_id, after_step_index=2)
        assert len(new_turns) == 1
        step_idx, prompt = new_turns[0]
        assert step_idx == 3
        assert prompt == "新客户端指令"

        # Query after step 3 (should find nothing)
        assert monitor.get_new_user_turns(conv_id, after_step_index=3) == []


if __name__ == "__main__":
    test_clean_user_prompt()
    test_get_new_user_turns()
    print("ALL EXTERNAL TURN SYNC TESTS PASSED!")
