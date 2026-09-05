import json
import os
import tempfile
from pathlib import Path
from antigravity_bridge.core.models import ArtifactReviewEvent
from antigravity_bridge.core.transcript_monitor import TranscriptMonitor


def test_artifact_review_event():
    event = ArtifactReviewEvent(
        step_index=42,
        artifact_path="/tmp/implementation_plan.md",
        artifact_name="implementation_plan.md",
        summary="Plan summary",
        request_feedback=True,
    )
    assert event.step_index == 42
    assert event.artifact_name == "implementation_plan.md"
    assert event.request_feedback is True


def test_get_pending_artifact_approval():
    with tempfile.TemporaryDirectory() as tmpdir:
        conv_id = "test-conv-artifacts"
        log_dir = Path(tmpdir) / "brain" / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript.jsonl"

        monitor = TranscriptMonitor(gemini_dir=tmpdir)

        # 1. No file -> None
        assert monitor.get_pending_artifact_approval(conv_id) is None

        # 2. Write a transcript with an artifact write requesting feedback
        plan_step = {
            "step_index": 10,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [
                {
                    "name": "write_to_file",
                    "args": {
                        "TargetFile": f"{tmpdir}/brain/{conv_id}/implementation_plan.md",
                        "ArtifactMetadata": {
                            "RequestFeedback": True,
                            "Summary": "Test plan summary",
                            "UserFacing": True,
                        },
                    },
                }
            ],
        }

        with open(transcript_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(plan_step) + "\n")

        # Check pending approval detected
        pending = monitor.get_pending_artifact_approval(conv_id)
        assert pending is not None
        step_idx, info = pending
        assert step_idx == 10
        assert info["artifact_name"] == "implementation_plan.md"
        assert info["summary"] == "Test plan summary"
        assert info["request_feedback"] is True

        # 3. User responds to approve the document
        user_step = {
            "step_index": 11,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": f"Comments on artifact URI: file://{tmpdir}/brain/{conv_id}/implementation_plan.md\n\nThe user has approved this document.",
        }
        with open(transcript_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(user_step) + "\n")

        # Now pending approval should be None because user responded
        assert monitor.get_pending_artifact_approval(conv_id) is None


def test_walkthrough_does_not_trigger_pending_approval():
    with tempfile.TemporaryDirectory() as tmpdir:
        conv_id = "test-conv-walkthrough"
        log_dir = Path(tmpdir) / "brain" / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript.jsonl"

        monitor = TranscriptMonitor(gemini_dir=tmpdir)

        # Write a walkthrough artifact
        walkthrough_step = {
            "step_index": 20,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [
                {
                    "name": "write_to_file",
                    "args": {
                        "TargetFile": f"{tmpdir}/brain/{conv_id}/walkthrough.md",
                        "ArtifactMetadata": {
                            "RequestFeedback": False,
                            "Summary": "Walkthrough summary",
                            "UserFacing": True,
                        },
                    },
                }
            ],
        }

        with open(transcript_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(walkthrough_step) + "\n")

        # Must NOT be detected as pending approval
        assert monitor.get_pending_artifact_approval(conv_id) is None


if __name__ == "__main__":
    test_artifact_review_event()
    test_get_pending_artifact_approval()
    test_walkthrough_does_not_trigger_pending_approval()
    print("ALL ARTIFACT APPROVAL TESTS PASSED!")
