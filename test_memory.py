"""Quick test for the user memory system.

Run this to verify the database schema and memory functions work correctly.
"""

import sys
import db

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

def test_memory_system():
    """Test all memory operations."""
    print("Testing user memory system...")

    # Initialize database
    db.init()
    print("✓ Database initialized")

    # Test 1: Save a nickname preference
    db.save_user_memory(
        user_id=123456789,
        memory_key="nickname_preference",
        memory_value="DOY",
        context="987654321"  # target user ID
    )
    print("✓ Saved nickname preference")

    # Test 2: Retrieve the nickname
    nickname = db.get_user_memory(123456789, "nickname_preference", "987654321")
    assert nickname == "DOY", f"Expected 'DOY', got '{nickname}'"
    print(f"✓ Retrieved nickname: {nickname}")

    # Test 3: Save language preference
    db.save_user_memory(
        user_id=123456789,
        memory_key="language_preference",
        memory_value="Tagalog"
    )
    print("✓ Saved language preference")

    # Test 4: Get all memories for a user
    memories = db.get_all_user_memories(123456789)
    assert len(memories) == 2, f"Expected 2 memories, got {len(memories)}"
    print(f"✓ Retrieved {len(memories)} memories")

    # Test 5: Build user context
    context = db.build_user_context(123456789, [987654321])
    assert "DOY" in context, "Context should contain nickname"
    assert "Tagalog" in context, "Context should contain language preference"
    print("✓ Built user context:")
    print(context)

    # Test 6: Update existing memory
    db.save_user_memory(
        user_id=123456789,
        memory_key="nickname_preference",
        memory_value="KUYA",
        context="987654321"
    )
    updated = db.get_user_memory(123456789, "nickname_preference", "987654321")
    assert updated == "KUYA", f"Expected 'KUYA', got '{updated}'"
    print("✓ Updated nickname preference")

    # Test 7: Delete a memory
    deleted = db.delete_user_memory(123456789, "nickname_preference", "987654321")
    assert deleted, "Delete should return True"
    retrieved = db.get_user_memory(123456789, "nickname_preference", "987654321")
    assert retrieved is None, "Memory should be deleted"
    print("✓ Deleted memory")

    # Test 8: Build context with no relevant mentions
    context_no_mention = db.build_user_context(123456789, [111111111])
    # Should only show language preference, not the deleted nickname
    assert "KUYA" not in context_no_mention
    print("✓ Context filtering works")

    # Cleanup
    db.delete_user_memory(123456789, "language_preference", "")
    print("\n✅ All tests passed!")
    print("\nMemory system is ready to use.")
    print("\nExample commands users can say:")
    print("  - '@bot gusto ko tawag mo kay @Doc lagi ay DOY'")
    print("  - '@bot call @user as KUYA'")
    print("  - '@bot forget about calling @user'")

if __name__ == "__main__":
    test_memory_system()
