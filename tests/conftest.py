import os
from datetime import datetime, timedelta, timezone
from queue import Queue
from threading import Thread
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_sav import web
from agent_sav.database import initialize_database
from agent_sav.service import Support
from agent_sav.shop import DEFAULT_DSN

PULL = "ITEM-DEMO-1001-1"  # Pull en laine côtelée, Vert mousse, S (ART-001; ART-021 in Noir), 89 €
TSHIRT = "ITEM-DEMO-1001-2"
LOST = "DEMO-1005"  # a scarf at 42 €, in transit for 16 days


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests script the model, and never call it."""
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.delenv("CONTACT_MCP_URL", raising=False)


@pytest.fixture
def database():
    dsn = os.getenv("SAV_TEST_DATABASE_URL", DEFAULT_DSN)
    name = f"sav_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            database = make_conninfo(dsn, dbname=name)
            initialize_database(database)
            yield database
        finally:
            db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


class ScriptedModel:
    """Plays the model: each step is a text, a (tool, args) call, a list of calls, or an exception to raise."""

    def __init__(self):
        self.steps, self.seen = [], []

    def plays(self, *steps):
        self.steps.extend(steps)

    def __call__(self, system, messages, tools):
        self.seen.append(messages)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        usage = {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}
        if isinstance(step, str):
            return AIMessage(content=step, usage_metadata=usage)
        calls = step if isinstance(step, list) else [step]
        return AIMessage(content="", usage_metadata=usage, tool_calls=[
            {"id": uuid4().hex[:9], "name": name, "args": args} for name, args in calls])


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def forward(self, **delay):
        self.now += timedelta(**delay)


@pytest.fixture
def model():
    return ScriptedModel()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def support(database, model, clock):
    return Support(database, model=model, clock=clock)


@pytest.fixture
def serve(database, support, monkeypatch):
    """Starts the website, shared or public, and gives its address."""
    servers, running = Queue(), []

    class TestServer(web.ThreadingHTTPServer):
        def serve_forever(self):
            servers.put(self)
            super().serve_forever()

    monkeypatch.setattr(web, "ThreadingHTTPServer", TestServer)

    def start(**options):
        worker = Thread(target=web.serve, args=(database, 0, support), kwargs=options, daemon=True)
        worker.start()
        server = servers.get(timeout=10)
        running.append((server, worker))
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server, worker in running:
        server.shutdown()
        worker.join(timeout=5)


class Chat:
    def __init__(self, support, customer="CLI-001"):
        self.support, self.id = support, support.chats.open(customer)
        self.waiting = False

    def say(self, message, message_id=None):
        answer = self.support.chat(self.id, message, message_id)
        self.waiting = answer["awaiting_confirmation"]
        return "\n".join(answer["messages"])


@pytest.fixture
def chat(support):
    return Chat(support)


@pytest.fixture
def query(database):
    def run(text, *params):
        with psycopg.connect(database) as db:
            cursor = db.execute(text, params)
            return cursor.fetchall() if cursor.description else None
    return run


def propose(kind, item_id=PULL, **args):
    return (f"propose_{kind}", {"item_id": item_id, "reason": "wrong_size", **args})


def refund(order_id=LOST, reason="lost_parcel", **args):
    return ("propose_refund", {"order_id": order_id, "reason": reason, **args})
