from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from .cases import Cases
from .shop import Shop

SHOP_TABLES = ("customers", "products", "orders", "order_items", "stock", "chats", "mcp_tokens", "cases", "case_commands",
               "case_messages", "refunds")
CHECKPOINT_TABLES = ("checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes")


@contextmanager
def open_checkpointer(dsn: str) -> Iterator[PostgresSaver]:
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row) as conn:
        conn.execute("SET search_path TO langgraph")
        yield PostgresSaver(conn)


def rename_state_key(conn, thread_id: str, old: str, new: str) -> None:
    """Carries on a thread saved before a key of its state was renamed."""
    if conn.execute("SELECT 1 FROM checkpoint_blobs WHERE thread_id = %s AND channel = %s LIMIT 1", (thread_id, old)).fetchone() is None:
        return
    with conn.transaction():
        for table in ("checkpoint_blobs", "checkpoint_writes"):
            conn.execute(f"UPDATE {table} SET channel = %s WHERE thread_id = %s AND channel = %s", (new, thread_id, old))
        conn.execute("UPDATE checkpoints SET checkpoint = replace(checkpoint::text, %s, %s)::jsonb WHERE thread_id = %s",
                     (f'"{old}"', f'"{new}"', thread_id))


def initialize_database(dsn: str) -> None:
    # The session lock also covers PostgresSaver.setup(), which builds indexes outside transactions.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended('agent_sav_database_setup', 0))")
        with conn.transaction():
            conn.execute("CREATE SCHEMA IF NOT EXISTS shop")
            conn.execute("CREATE SCHEMA IF NOT EXISTS langgraph")

        Shop(dsn).setup()
        Cases(dsn).setup()
        with open_checkpointer(dsn) as checkpointer:
            checkpointer.setup()
