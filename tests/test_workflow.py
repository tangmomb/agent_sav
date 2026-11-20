import json
import os
import subprocess
import sys
from queue import Queue
from threading import Thread
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_sav import web
from agent_sav.__main__ import DEFAULT_DSN
from agent_sav.model import interpret_demo
from agent_sav.shop import Shop
from agent_sav.workflow import build_graph


@pytest.fixture
def database():
    dsn = os.getenv("SAV_TEST_DATABASE_URL", DEFAULT_DSN)
    schema = f"sav_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield make_conninfo(dsn, options=f"-c search_path={schema}")
        finally:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def cli(database: str, *args: str, code: int = 0) -> str:
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "agent_sav", *args],
        env={**os.environ, "DATABASE_URL": database, "MISTRAL_API_KEY": ""},
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert result.returncode == code, result.stderr
    return result.stdout


def test_exchange_resumes_after_commit_without_duplicate(database):
    cli(database, "init")
    opened = json.loads(cli(database, "ouvrir", "Le pull est trop petit, je voudrais du M.", "--demo", "echange"))
    case_id = opened["case_id"]
    assert opened["status"] == "accord_requis"
    assert opened["confirmation"]["taille"] == "M"

    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM sav_returns").fetchone() == (0,)

    cli(database, "decider", case_id, "oui", "--simuler-timeout", code=2)
    with psycopg.connect(database) as db:
        (return_id,) = db.execute("SELECT id FROM sav_returns").fetchone()

    resumed = json.loads(cli(database, "reprendre", case_id))
    assert resumed["status"] == "termine"
    assert resumed["return_id"] == return_id
    cli(database, "decider", case_id, "oui", code=1)

    with psycopg.connect(database) as db:
        assert db.execute("SELECT id FROM sav_returns").fetchall() == [(return_id,)]
        assert db.execute("SELECT quantity FROM sav_stock WHERE size = 'M'").fetchone() == (19,)


def test_stock_can_change_while_waiting_for_approval(database):
    cli(database, "init")
    opened = json.loads(cli(database, "ouvrir", "Le pull est trop petit, je voudrais du M.", "--demo", "echange"))
    with psycopg.connect(database) as db:
        db.execute("UPDATE sav_stock SET quantity = 0 WHERE size = 'M'")

    result = json.loads(cli(database, "decider", opened["case_id"], "oui"))
    assert result["status"] == "stock_indisponible"
    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM sav_returns").fetchone() == (0,)


def test_boutique_clarifies_size_on_the_same_case(database, monkeypatch):
    servers = Queue()

    class TestServer(web.ThreadingHTTPServer):
        def serve_forever(self):
            servers.put(self)
            super().serve_forever()

    monkeypatch.setattr(web, "ThreadingHTTPServer", TestServer)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    worker = Thread(target=web.serve, args=(database, 0), kwargs={"use_mistral": False}, daemon=True)
    worker.start()
    server = servers.get(timeout=10)
    case_id = str(uuid4())

    def send(**payload):
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/sav",
            data=json.dumps({"case_id": case_id, **payload}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    try:
        first_message = {"message": "Mon pull est trop petit", "message_id": str(uuid4())}
        question = send(**first_message)
        assert question["status"] == "taille_a_preciser"
        assert question["question"] == "Quelle taille souhaitez-vous pour l’échange ?"
        assert send(**first_message) == question
        config = {"configurable": {"thread_id": case_id}}
        with PostgresSaver.from_conn_string(database) as checkpointer:
            graph = build_graph(checkpointer, Shop(database), interpret_demo)
            snapshot = graph.get_state(config)
            assert snapshot.next == ("demander_taille",)
            assert snapshot.interrupts[0].value == {"question": question["question"]}

        same_size = {"message": "Du S", "message_id": str(uuid4())}
        repeated_question = send(**same_size)
        assert repeated_question["status"] == "taille_identique"
        assert send(**same_size) == repeated_question
        approval = send(message="Du M", message_id=str(uuid4()))
        assert approval == {"status": "accord_requis", "action": "echange", "size": "M"}

        with PostgresSaver.from_conn_string(database) as checkpointer:
            checkpoint = checkpointer.get(config)
        messages = checkpoint["channel_values"]["messages"]
        assert [message.content for message in messages] == ["Mon pull est trop petit", "Du S", "Du M"]

        with psycopg.connect(database) as db:
            assert db.execute("SELECT count(*) FROM sav_returns").fetchone() == (0,)
        completed = send(approved=True)
        assert completed["status"] == "termine"
        with psycopg.connect(database) as db:
            assert db.execute("SELECT case_id::text, size FROM sav_returns").fetchall() == [(case_id, "M")]
    finally:
        server.shutdown()
        worker.join(timeout=5)
