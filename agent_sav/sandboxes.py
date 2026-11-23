"""Public demo: each visitor gets a shop of their own, in a database of their own.

The cookie holds a random token; the database only keeps its hash. A visitor therefore plays the customer and
the shop without seeing, or disturbing, anybody else. Model calls share a daily budget.
"""

import hashlib
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import date

import psycopg
from langchain_core.messages import AIMessage
from psycopg import sql
from psycopg.conninfo import make_conninfo

from .database import initialize_database
from .model import Complete, complete
from .service import Support

MAX_SANDBOXES = 40
LIFETIME_HOURS = 24
DAILY_TOKENS = 1_500_000


class Full(Exception):
    pass


class BudgetSpent(Exception):
    pass


class Limit:
    """At most `count` events per key over `seconds`, in this process."""

    def __init__(self, count: int, seconds: float):
        self.count, self.seconds = count, seconds
        self.events: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            events = self.events[key]
            while events and now - events[0] > self.seconds:
                events.popleft()
            if len(events) >= self.count:
                return False
            events.append(now)
            if len(self.events) > 10000:  # forget the visitors who have left
                for other in [each for each, kept in self.events.items() if not kept or now - kept[-1] > self.seconds]:
                    del self.events[other]
            return True


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sandboxes:
    def __init__(self, dsn: str, *, model: Complete = complete, maximum: int = MAX_SANDBOXES):
        self.dsn, self.model, self.maximum = dsn, model, maximum
        self.daily_tokens = int(os.getenv("SAV_DAILY_TOKENS") or DAILY_TOKENS)
        self.lock = threading.Lock()
        self.shops: dict[str, Support] = {}
        with psycopg.connect(dsn) as conn:
            conn.execute("""
                CREATE SCHEMA IF NOT EXISTS demo;
                CREATE TABLE IF NOT EXISTS demo.sandboxes (
                    token_hash text PRIMARY KEY,
                    database text NOT NULL UNIQUE,
                    used_at timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS demo.usage (day date PRIMARY KEY, tokens bigint NOT NULL);
            """)

    def admin(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, autocommit=True)

    def drop(self, conn, database: str) -> None:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))

    def template(self, conn) -> str:
        """The shop new visitors start from. Made again each day: the demo orders are dated from today."""
        name = f"sav_demo_template_{date.today():%Y%m%d}"
        existing = [row[0] for row in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'sav\\_demo\\_template\\_%'")]
        if name not in existing:
            building = f"sav_demo_template_building_{secrets.token_hex(4)}"
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(building)))
            initialize_database(make_conninfo(self.dsn, dbname=building))
            conn.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(building), sql.Identifier(name)))
        for old in existing:
            if old != name:
                self.drop(conn, old)
        return name

    def create(self) -> str:
        """Opens a shop and returns the token of its visitor."""
        with self.lock, self.admin() as conn:
            if conn.execute("SELECT count(*) FROM demo.sandboxes").fetchone()[0] >= self.maximum:
                self.clean(conn)
                if conn.execute("SELECT count(*) FROM demo.sandboxes").fetchone()[0] >= self.maximum:
                    raise Full
            token = secrets.token_urlsafe(32)
            database = f"sav_demo_{secrets.token_hex(12)}"
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(database), sql.Identifier(self.template(conn))))
            conn.execute("INSERT INTO demo.sandboxes (token_hash, database) VALUES (%s, %s)", (token_hash(token), database))
            return token

    def open(self, token: str | None) -> Support | None:
        """The shop of a visitor, if their token is known."""
        if not token:
            return None
        key = token_hash(token)
        with self.admin() as conn:
            row = conn.execute("UPDATE demo.sandboxes SET used_at = now() WHERE token_hash = %s RETURNING database", (key,)).fetchone()
        if row is None:
            self.shops.pop(key, None)
            return None
        return self.shops.setdefault(key, self.shop(row[0]))

    def shop(self, database: str) -> Support:
        return Support(make_conninfo(self.dsn, dbname=database), model=self.metered)

    def clean(self, conn) -> None:
        """Removes the shops left alone for a day."""
        for key, database in conn.execute(
            "DELETE FROM demo.sandboxes WHERE used_at < now() - make_interval(hours => %s) RETURNING token_hash, database",
            (LIFETIME_HOURS,),
        ).fetchall():
            self.shops.pop(key, None)
            self.drop(conn, database)

    def tick(self) -> None:
        with self.lock, self.admin() as conn:
            self.clean(conn)
        for support in list(self.shops.values()):
            support.tick()

    # The daily budget.

    def spent(self) -> bool:
        with self.admin() as conn:
            row = conn.execute("SELECT tokens FROM demo.usage WHERE day = current_date").fetchone()
        return bool(row) and row[0] >= self.daily_tokens

    def metered(self, system: str, messages: list, tools: list[dict]) -> AIMessage:
        if self.spent():
            raise BudgetSpent
        answer = self.model(system, messages, tools)
        used = (answer.usage_metadata or {}).get("total_tokens", 0)
        with self.admin() as conn:
            conn.execute("""
                INSERT INTO demo.usage (day, tokens) VALUES (current_date, %s)
                ON CONFLICT (day) DO UPDATE SET tokens = demo.usage.tokens + EXCLUDED.tokens
            """, (used,))
        return answer
