"""View saved server context documents from the database."""

import os
import sqlite3
from datetime import datetime


def view_all_contexts() -> None:
    """Display all server context documents in a readable format."""
    db_path = os.path.join(os.path.dirname(__file__), "debts.db")

    if not os.path.exists(db_path):
        print(f"Database not found at: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='server_context'"
    )
    if not cursor.fetchone():
        print("server_context table doesn't exist yet.")
        print("The bot needs to run at least once to create the table.")
        conn.close()
        return

    cursor.execute(
        """
        SELECT guild_id, context_data, updated_at
        FROM server_context
        WHERE context_data != ''
        ORDER BY guild_id
        """
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No server context saved yet.")
        print("\nTry telling the bot server-wide rules in chat, e.g.:")
        print("  - 'Everyone speak English in this server'")
        print("  - '@bot remember for this server to keep replies short'")
        return

    print("=" * 70)
    print(f"Found {len(rows)} server context document(s)")
    print("=" * 70)

    for row in rows:
        print(f"\nGuild ID: {row['guild_id']}")
        print("-" * 70)
        print(row["context_data"])
        updated = datetime.fromisoformat(row["updated_at"])
        print(f"\nUpdated: {updated.strftime('%Y-%m-%d %H:%M:%S')}")

    print("\n" + "=" * 70)


def view_guild_context(guild_id: int) -> None:
    """Display server context for a specific guild."""
    db_path = os.path.join(os.path.dirname(__file__), "debts.db")

    if not os.path.exists(db_path):
        print(f"Database not found at: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT context_data, updated_at FROM server_context WHERE guild_id = ?",
        (guild_id,),
    )
    row = cursor.fetchone()
    conn.close()

    if not row or not row["context_data"].strip():
        print(f"No server context found for guild ID: {guild_id}")
        return

    print("=" * 70)
    print(f"Server context for guild ID: {guild_id}")
    print("=" * 70)
    print(row["context_data"])
    updated = datetime.fromisoformat(row["updated_at"])
    print(f"\nUpdated: {updated.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        try:
            gid = int(sys.argv[1])
            view_guild_context(gid)
        except ValueError:
            print("Usage: python view_memory.py [guild_id]")
            print("  guild_id must be a number")
    else:
        view_all_contexts()
