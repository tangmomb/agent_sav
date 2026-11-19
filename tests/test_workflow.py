import json
import os
import subprocess
import sys
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_sav.__main__ import DEFAULT_DSN


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
