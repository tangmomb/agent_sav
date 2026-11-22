from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from .shop import Shop

SHOP_TABLES = ("customers", "products", "orders", "order_items", "stock", "sav_requests", "sav_messages")
CHECKPOINT_TABLES = ("checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes")


@contextmanager
def open_checkpointer(dsn: str) -> Iterator[PostgresSaver]:
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row) as conn:
        conn.execute("SET search_path TO langgraph")
        yield PostgresSaver(conn)


def initialize_database(dsn: str) -> None:
    # The session lock also covers PostgresSaver.setup(), which builds indexes outside transactions.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended('agent_sav_database_setup', 0))")
        with conn.transaction():
            conn.execute("CREATE SCHEMA IF NOT EXISTS shop")
            conn.execute("CREATE SCHEMA IF NOT EXISTS langgraph")

        Shop(dsn).setup()
        with open_checkpointer(dsn) as checkpointer:
            checkpointer.setup()
