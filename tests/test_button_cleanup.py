import asyncio
from unittest.mock import AsyncMock, MagicMock
from antigravity_bridge.adapters.telegram.handlers import TelegramHandlers

def test_button_cleanup_and_handled_approvals():
    agent_cli = MagicMock()
    monitor = MagicMock()
    session_mgr = MagicMock()

    handlers = TelegramHandlers(
        agent_cli=agent_cli,
        monitor=monitor,
        session_mgr=session_mgr,
        allowed_users={12345},
    )

    chat_id = 12345
    conv_id = "test-conv-123"
    artifact_path = "/tmp/plan.md"

    # 1. Simulate sending pending approval
    bot = MagicMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    sent_msg.edit_text = AsyncMock()
    bot.send_message = AsyncMock(return_value=sent_msg)

    asyncio.run(
        handlers.send_pending_approval(
            bot=bot,
            chat_id=chat_id,
            conv_id=conv_id,
            artifact_path=artifact_path,
            artifact_name="implementation_plan.md",
            summary="Test summary",
        )
    )

    assert chat_id in handlers.pending_approvals
    assert handlers.pending_approvals[chat_id]["message_id"] == 999

    # 2. Simulate cleanup_pending_approvals (e.g. externally approved)
    bot.edit_message_reply_markup = AsyncMock()
    handlers.bot = bot

    asyncio.run(handlers.cleanup_pending_approvals(chat_id))

    assert chat_id not in handlers.pending_approvals
    assert f"{conv_id}:{artifact_path}" in handlers.handled_approvals

    # 3. Simulate second attempt to send the same approval - should be blocked!
    bot.send_message.reset_mock()
    asyncio.run(
        handlers.send_pending_approval(
            bot=bot,
            chat_id=chat_id,
            conv_id=conv_id,
            artifact_path=artifact_path,
            artifact_name="implementation_plan.md",
            summary="Test summary",
        )
    )

    bot.send_message.assert_not_called()
    assert chat_id not in handlers.pending_approvals
    print("BUTTON CLEANUP TESTS PASSED!")

if __name__ == "__main__":
    test_button_cleanup_and_handled_approvals()
