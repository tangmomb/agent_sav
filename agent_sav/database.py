from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.rows import dict_row

from .legacy_checkpoints import SupportPostgresSaver
from .shop import Shop

SHOP_TABLES = ("sav_customers", "sav_products", "sav_orders", "sav_order_items", "sav_stock", "sav_returns", "sav_inquiries")
CHECKPOINT_TABLES = ("checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes")


@contextmanager
def open_checkpointer(dsn: str) -> Iterator[PostgresSaver]:
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row) as conn:
        conn.execute("SET search_path TO langgraph")
        yield SupportPostgresSaver(conn)


def initialize_database(dsn: str) -> None:
    # The session lock also covers PostgresSaver.setup(), which builds indexes outside transactions.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended('agent_sav_database_setup', 0))")
        with conn.transaction():
            legacy_schema = conn.execute("SELECT current_schema()").fetchone()[0]
            legacy_tables = {
                name for (name,) in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = %s", (legacy_schema,),
                )
            }
            for target, tables in (("shop", SHOP_TABLES), ("langgraph", CHECKPOINT_TABLES)):
                destination = sql.Identifier(target)
                conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(destination))
                if legacy_schema != target:
                    for table in tables:
                        if table in legacy_tables:
                            conn.execute(sql.SQL("ALTER TABLE {} SET SCHEMA {}").format(
                                sql.Identifier(legacy_schema, table), destination,
                            ))

        Shop(dsn).setup()
        with open_checkpointer(dsn) as checkpointer:
            checkpointer.setup()
