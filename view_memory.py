"""View all saved user memories from the database."""

import sqlite3
import os
from datetime import datetime


def view_all_memories():
    """Display all user memories in a readable format."""
    db_path = os.path.join(os.path.dirname(__file__), "debts.db")

    if not os.path.exists(db_path):
        print(f"Database not found at: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_memory'")
    if not cursor.fetchone():
        print("user_memory table doesn't exist yet.")
        print("The bot needs to run at least once to create the table.")
        conn.close()
        return

    # Fetch all memories
    cursor.execute("""
        SELECT user_id, memory_key, memory_value, context, created_at, updated_at
        FROM user_memory
        ORDER BY user_id, updated_at DESC
    """)

    memories = cursor.fetchall()
    conn.close()

    if not memories:
        print("No memories saved yet.")
        print("\nTry chatting with the bot and saying things like:")
        print("  - 'I'm a software engineer'")
        print("  - 'tandaan mo portfolio ko https://example.com'")
        print("  - 'remember my github https://github.com/username'")
        return

    print("=" * 70)
    print(f"Found {len(memories)} memory entries")
    print("=" * 70)

    current_user = None
    for mem in memories:
        if current_user != mem['user_id']:
            current_user = mem['user_id']
            print(f"\n📌 User ID: {mem['user_id']}")
            print("-" * 70)

        print(f"\n  Type: {mem['memory_key']}")
        print(f"  Value: {mem['memory_value']}")
        if mem['context']:
            print(f"  Context: {mem['context']}")

        created = datetime.fromisoformat(mem['created_at'])
        updated = datetime.fromisoformat(mem['updated_at'])
        print(f"  Created: {created.strftime('%Y-%m-%d %H:%M:%S')}")
        if created != updated:
            print(f"  Updated: {updated.strftime('%Y-%m-%d %H:%M:%S')}")

    print("\n" + "=" * 70)


def view_user_memory(user_id: int):
    """Display memories for a specific user."""
    db_path = os.path.join(os.path.dirname(__file__), "debts.db")

    if not os.path.exists(db_path):
        print(f"Database not found at: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT memory_key, memory_value, context, created_at, updated_at
        FROM user_memory
        WHERE user_id = ?
        ORDER BY updated_at DESC
    """, (user_id,))

    memories = cursor.fetchall()
    conn.close()

    if not memories:
        print(f"No memories found for user ID: {user_id}")
        return

    print("=" * 70)
    print(f"Memories for User ID: {user_id}")
    print("=" * 70)

    for mem in memories:
        print(f"\n  Type: {mem['memory_key']}")
        print(f"  Value: {mem['memory_value']}")
        if mem['context']:
            print(f"  Context: {mem['context']}")

        created = datetime.fromisoformat(mem['created_at'])
        updated = datetime.fromisoformat(mem['updated_at'])
        print(f"  Created: {created.strftime('%Y-%m-%d %H:%M:%S')}")
        if created != updated:
            print(f"  Updated: {updated.strftime('%Y-%m-%d %H:%M:%S')}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        try:
            user_id = int(sys.argv[1])
            view_user_memory(user_id)
        except ValueError:
            print("Usage: python view_memory.py [user_id]")
            print("  user_id must be a number")
    else:
        view_all_memories()
