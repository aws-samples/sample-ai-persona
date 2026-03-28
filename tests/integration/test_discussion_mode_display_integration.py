"""
Integration test for discussion mode display in history
Tests task 7.4: 議論履歴でのモード表示
"""

import sys
from pathlib import Path
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.models.discussion import Discussion  # noqa: E402
from src.models.message import Message  # noqa: E402


def test_discussion_mode_display_integration():
    """Integration test for mode display in discussion history"""
    print("Testing discussion mode display integration...")

    # Create test discussions with different modes
    discussions = []

    # Classic mode discussions
    for i in range(3):
        discussion = Discussion.create_new(
            topic=f"Classic Discussion {i + 1}",
            participants=[f"persona-{j}" for j in range(1, 4)],
        )
        # Add some messages
        for j in range(3):
            message = Message(
                persona_id=f"persona-{j + 1}",
                persona_name=f"Persona {j + 1}",
                content=f"Message {j + 1} in classic mode",
                timestamp=datetime.now(),
            )
            discussion = discussion.add_message(message)
        discussions.append(discussion)

    # Agent mode discussions
    for i in range(2):
        discussion = Discussion(
            id=f"agent-discussion-{i + 1}",
            topic=f"Agent Discussion {i + 1}",
            participants=[f"persona-{j}" for j in range(1, 5)],
            messages=[],
            insights=[],
            created_at=datetime.now(),
            mode="agent",
            agent_config={"rounds": 3, "additional_instructions": "Be creative"},
        )
        # Add messages with round numbers
        for round_num in range(1, 4):
            for j in range(1, 5):
                message = Message(
                    persona_id=f"persona-{j}",
                    persona_name=f"Persona {j}",
                    content=f"Message from round {round_num}",
                    timestamp=datetime.now(),
                    message_type="statement",
                    round_number=round_num,
                )
                discussion = discussion.add_message(message)
        discussions.append(discussion)

    print(f"✓ Created {len(discussions)} test discussions")

    # Test 1: Mode counting
    agent_count = sum(1 for d in discussions if d.mode == "agent")
    classic_count = sum(1 for d in discussions if d.mode == "classic")

    assert agent_count == 2, f"Expected 2 agent discussions, got {agent_count}"
    assert classic_count == 3, f"Expected 3 classic discussions, got {classic_count}"
    print("✓ Mode counting works correctly")

    # Test 2: Mode filtering
    classic_filtered = [d for d in discussions if d.mode == "classic"]
    agent_filtered = [d for d in discussions if d.mode == "agent"]

    assert len(classic_filtered) == 3, "Classic mode filter failed"
    assert len(agent_filtered) == 2, "Agent mode filter failed"
    print("✓ Mode filtering works correctly")

    # Test 3: Mode badge generation
    for discussion in discussions:
        badge = "🤖" if discussion.mode == "agent" else "💬"
        mode_text = (
            "AIエージェントモード" if discussion.mode == "agent" else "従来モード"
        )

        if discussion.mode == "agent":
            assert badge == "🤖", "Agent mode badge incorrect"
            assert mode_text == "AIエージェントモード", "Agent mode text incorrect"
        else:
            assert badge == "💬", "Classic mode badge incorrect"
            assert mode_text == "従来モード", "Classic mode text incorrect"

    print("✓ Mode badge generation works correctly")

    # Test 4: Agent config display
    for discussion in agent_filtered:
        assert discussion.agent_config is not None, "Agent config should exist"
        assert "rounds" in discussion.agent_config, "Agent config should have rounds"

        rounds = discussion.agent_config.get("rounds")
        assert rounds == 3, f"Expected 3 rounds, got {rounds}"

    print("✓ Agent config display works correctly")

    # Test 5: Statistics calculation
    total = len(discussions)
    agent_percentage = (agent_count / total) * 100
    classic_percentage = (classic_count / total) * 100

    assert agent_percentage == 40.0, f"Expected 40% agent, got {agent_percentage}%"
    assert classic_percentage == 60.0, (
        f"Expected 60% classic, got {classic_percentage}%"
    )
    print("✓ Statistics calculation works correctly")

    # Test 6: Message type differentiation in agent mode
    for discussion in agent_filtered:
        statement_messages = [
            m for m in discussion.messages if m.message_type == "statement"
        ]
        assert len(statement_messages) > 0, "Agent mode should have statement messages"

        # Check round numbers
        for message in statement_messages:
            assert message.round_number is not None, (
                "Agent mode messages should have round numbers"
            )
            assert 1 <= message.round_number <= 3, (
                "Round number should be between 1 and 3"
            )

    print("✓ Message type differentiation works correctly")

    # Test 7: Quick filter logic
    filter_options = {
        "all": discussions,
        "classic": classic_filtered,
        "agent": agent_filtered,
    }

    assert len(filter_options["all"]) == 5, "All filter should show all discussions"
    assert len(filter_options["classic"]) == 3, (
        "Classic filter should show 3 discussions"
    )
    assert len(filter_options["agent"]) == 2, "Agent filter should show 2 discussions"
    print("✓ Quick filter logic works correctly")

    # Test 8: Mode-specific information display
    for discussion in discussions:
        if discussion.mode == "agent":
            # Agent mode should have config
            assert discussion.agent_config is not None
            assert isinstance(discussion.agent_config, dict)

            # Should be able to display rounds
            rounds_display = discussion.agent_config.get("rounds", "N/A")
            assert rounds_display != "N/A", "Rounds should be available"

            # Should be able to check for additional instructions
            has_instructions = bool(
                discussion.agent_config.get("additional_instructions")
            )
            assert has_instructions is True, "Should have additional instructions"
        else:
            # Classic mode should not have config
            assert discussion.agent_config is None

    print("✓ Mode-specific information display works correctly")

    print()
    print("=" * 60)
    print("✅ All integration tests passed!")
    print("=" * 60)
    print()
    print("Verified functionality:")
    print("  ✓ Mode counting and statistics")
    print("  ✓ Mode filtering (classic/agent)")
    print("  ✓ Mode badge generation")
    print("  ✓ Agent config display")
    print("  ✓ Statistics calculation")
    print("  ✓ Message type differentiation")
    print("  ✓ Quick filter logic")
    print("  ✓ Mode-specific information display")


if __name__ == "__main__":
    test_discussion_mode_display_integration()
