"""Who opened each chat, in the sav schema. The conversation itself is kept by the graph, in the langgraph schema."""

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row


class Chats:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def setup(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('sav_chats_setup', 0))")
            conn.execute("""
                -- It was in the shop schema before, and called chats.
                ALTER TABLE IF EXISTS shop.chats RENAME TO chat_owners;
                ALTER TABLE IF EXISTS shop.chat_owners SET SCHEMA sav;
                -- A chat belongs to the customer who opened it: neither the browser nor the model says who they are.
                CREATE TABLE IF NOT EXISTS sav.chat_owners (
                    id text PRIMARY KEY,
                    customer_id text NOT NULL REFERENCES shop.customers,
                    created_at timestamptz NOT NULL DEFAULT now()
                );
                -- The tokens of the MCP server, which is gone.
                DROP TABLE IF EXISTS shop.mcp_tokens, sav.mcp_tokens;
            """)

    def open(self, customer_id: str) -> str | None:
        """Starts a chat for a signed-in customer (the demo has no password). Returns its secret id."""
        chat_id = f"chat-{uuid4()}"
        with psycopg.connect(self.dsn) as conn:
            created = conn.execute(
                "INSERT INTO sav.chat_owners (id, customer_id) SELECT %s, id FROM shop.customers WHERE id = %s",
                (chat_id, customer_id),
            ).rowcount
        return chat_id if created else None

    def customer(self, chat_id: str) -> dict | None:
        """The customer who opened the chat, or None for a chat that does not exist."""
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute("""
                SELECT c.id, c.first_name, c.name FROM sav.chat_owners o JOIN shop.customers c ON c.id = o.customer_id
                WHERE o.id = %s
            """, (chat_id,)).fetchone()
