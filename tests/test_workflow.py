import json
import os
import subprocess
import sys
from queue import Queue
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_sav import web
from agent_sav.__main__ import DEFAULT_DSN
from agent_sav.database import CHECKPOINT_TABLES, SHOP_TABLES, open_checkpointer
from agent_sav.model import interpret_demo
from agent_sav.shop import Shop
from agent_sav.workflow import build_graph


@pytest.fixture
def database():
    dsn = os.getenv("SAV_TEST_DATABASE_URL", DEFAULT_DSN)
    name = f"sav_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(dsn, dbname=name)
        finally:
            db.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


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
    thread_id = opened["thread_id"]
    assert opened["status"] == "accord_requis"
    assert opened["confirmation"]["taille"] == "M"

    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_returns").fetchone() == (0,)

    cli(database, "decider", thread_id, "oui", "--simuler-timeout", code=2)
    with psycopg.connect(database) as db:
        (return_id,) = db.execute("SELECT id FROM shop.sav_returns").fetchone()

    resumed = json.loads(cli(database, "reprendre", thread_id))
    assert resumed["status"] == "termine"
    assert resumed["return_id"] == return_id
    cli(database, "decider", thread_id, "oui", code=1)

    with psycopg.connect(database) as db:
        assert db.execute("SELECT id FROM shop.sav_returns").fetchall() == [(return_id,)]
        assert db.execute(
            "SELECT quantity FROM shop.sav_stock WHERE product_id = 'ART-001' AND size = 'M'"
        ).fetchone() == (19,)


def test_stock_can_change_while_waiting_for_approval(database):
    cli(database, "init")
    opened = json.loads(cli(database, "ouvrir", "Le pull est trop petit, je voudrais du M.", "--demo", "echange"))
    with psycopg.connect(database) as db:
        db.execute("UPDATE shop.sav_stock SET quantity = 0 WHERE product_id = 'ART-001' AND size = 'M'")

    result = json.loads(cli(database, "decider", opened["thread_id"], "oui"))
    assert result["status"] == "stock_indisponible"
    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_returns").fetchone() == (0,)


@pytest.fixture
def boutique(database, monkeypatch):
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
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        worker.join(timeout=5)


def send_to_sav(boutique, **payload):
    request = Request(
        boutique + "/api/sav",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        return json.load(response)


@pytest.mark.parametrize(("status", "count", "branch", "result"), [
    ("livree", 0, "verifier_autres_commandes", "demande_a_clarifier"),
    ("livree", 2, "verifier_autres_commandes", "demande_a_clarifier"),
    ("en_livraison", 2, "suivre_livraison", "commande_en_transit"),
    ("validee", 2, "informer_preparation", "commande_validee"),
])
def test_graph_routes_by_order_status_before_interpretation(monkeypatch, status, count, branch, result):
    shop = Shop("invalid-dsn")

    def interpret(message, awaiting_size):
        assert status == "livree"
        return interpret_demo(message, awaiting_size)

    def lookup(*_args):
        assert status == "livree"
        return {"status": "livree", "size": "L", "sized": True}

    monkeypatch.setattr(shop, "order_item", lookup)
    graph = build_graph(None, shop, interpret)
    updates = list(graph.stream(
        {"active_order_count": count, "messages": [{"type": "human", "content": "Où en est ma commande ?"}]},
        {"configurable": {"thread_id": f"CLI-003:DEMO-1015:{uuid4()}"}},
        context={
            "first_name": "Inès",
            "order": {"id": "DEMO-1015", "status": status},
            "items": [{"line_number": 1, "name": "Jean droit", "color": "Indigo", "size": "L"}],
        },
        stream_mode="updates",
    ))
    assert [next(iter(update)) for update in updates[:2]] == ["verifier_statut_commande", branch]
    if status == "livree":
        assert updates[1][branch]["has_other_active_orders"] == (count > 0)
        assert "comprendre" in updates[2]
    assert next(iter(updates[-1].values()))["status"] == result


def test_boutique_clarifies_size_on_the_same_case(database, boutique):
    thread_id = f"CLI-001:DEMO-1001:{uuid4()}"

    def send(**payload):
        return send_to_sav(boutique, thread_id=thread_id, **payload)

    opened = send(open=True)
    assert "Bonjour Camille !" in opened["greeting"]
    assert "DEMO-1001 est livrée" in opened["greeting"]
    assert "Pull en laine côtelée" in opened["greeting"] and "T-shirt en coton épais" in opened["greeting"]
    assert "aucune autre commande en préparation ou en livraison" in opened["greeting"]
    assert send(open=True) == opened
    config = {"configurable": {"thread_id": thread_id}}
    with open_checkpointer(database) as checkpointer:
        assert checkpointer.get(config) is None
    first_message = {"message": "Mon pull est trop petit", "message_id": str(uuid4())}
    question = send(**first_message)
    assert question["status"] == "taille_a_preciser"
    assert question["question"] == "Quelle taille souhaitez-vous pour l’échange ?"
    assert send(**first_message) == question
    with open_checkpointer(database) as checkpointer:
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

    with open_checkpointer(database) as checkpointer:
        checkpoint = checkpointer.get(config)
    messages = checkpoint["channel_values"]["messages"]
    assert checkpoint["channel_values"]["has_other_active_orders"] is False
    assert [message.content for message in messages] == ["Mon pull est trop petit", "Du S", "Du M"]

    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_returns").fetchone() == (0,)
    completed = send(approved=True)
    assert completed["status"] == "termine"
    with psycopg.connect(database) as db:
        assert db.execute("SELECT case_id::text, size FROM shop.sav_returns").fetchall() == [
            (checkpoint["channel_values"]["case_id"], "M"),
        ]


def test_purchase_history_and_case_are_scoped_to_customer_and_order(database, boutique):
    with urlopen(boutique + "/api/clients", timeout=10) as response:
        customers = json.load(response)["customers"]
    assert len(customers) == 20
    histories = {}
    for customer in customers:
        customer_id = customer["id"]
        with urlopen(f"{boutique}/api/clients/{customer_id}/commandes", timeout=10) as response:
            histories[customer_id] = json.load(response)["orders"]
        assert 2 <= len(histories[customer_id]) <= 5
        assert all(order["customer_id"] == customer_id for order in histories[customer_id])
    all_orders = [order for orders in histories.values() for order in orders]
    assert len({item["product_id"] for order in all_orders for item in order["items"]}) == 20
    assert all(order["total_cents"] == sum(item["price_cents"] for item in order["items"]) for order in all_orders)
    validated = next(order for order in all_orders if order["status"] == "validee")
    in_transit = next(order for order in histories[validated["customer_id"]] if order["status"] == "en_livraison")
    opened = send_to_sav(
        boutique, thread_id=f"{validated['customer_id']}:{validated['id']}:{uuid4()}", open=True,
    )
    assert "Bonjour Inès !" in opened["greeting"]
    assert "est validée" in opened["greeting"]
    assert "une autre commande en préparation ou en livraison" in opened["greeting"]
    greeting = send_to_sav(
        boutique, thread_id=f"{in_transit['customer_id']}:{in_transit['id']}:{uuid4()}", open=True,
    )["greeting"]
    assert "est en livraison" in greeting
    assert "une autre commande en préparation ou en livraison" in greeting
    delivered = next(order for order in histories[validated["customer_id"]] if order["status"] == "livree")
    delivered_thread = f"{delivered['customer_id']}:{delivered['id']}:{uuid4()}"
    greeting = send_to_sav(boutique, thread_id=delivered_thread, open=True)["greeting"]
    assert "2 autres commandes en préparation ou en livraison" in greeting
    send_to_sav(boutique, thread_id=delivered_thread, message="Bonjour")
    with open_checkpointer(database) as checkpointer:
        checkpoint = checkpointer.get({"configurable": {"thread_id": delivered_thread}})["channel_values"]
        assert checkpoint["active_order_count"] == 2
        assert checkpoint["has_other_active_orders"] is True
    accessory_order, accessory = next(
        (order, item) for order in all_orders for item in order["items"]
        if order["status"] == "livree" and not item["sized"]
    )
    for order, item, status in (
        (validated, validated["items"][0], "commande_validee"),
        (in_transit, in_transit["items"][0], "commande_en_transit"),
        (accessory_order, accessory, "article_sans_taille"),
    ):
        result = send_to_sav(
            boutique, thread_id=f"{order['customer_id']}:{order['id']}:{uuid4()}",
            line_number=item["line_number"], message="Je veux un échange en L.",
        )
        assert result["status"] == status

    order, other_order = histories["CLI-001"]
    first, item, *_ = order["items"]
    nonce = uuid4()
    thread_id = f"CLI-001:{order['id']}:{nonce}"
    identity = {"thread_id": thread_id, "line_number": item["line_number"]}
    with pytest.raises(HTTPError) as wrong_owner:
        send_to_sav(boutique, thread_id=f"CLI-002:{order['id']}:{nonce}", message="Je veux un échange en L.")
    assert wrong_owner.value.code == 404
    same_size = send_to_sav(boutique, **identity, message=f"Je veux un échange en {item['size']}.")
    assert same_size["status"] == "taille_identique"
    assert f"taille {item['size']}" in same_size["question"]
    assert send_to_sav(boutique, **identity, message="Du L")["status"] == "accord_requis"

    with pytest.raises(HTTPError) as wrong_item:
        send_to_sav(boutique, **{**identity, "line_number": first["line_number"]}, approved=True)
    assert wrong_item.value.code == 404

    other_customer_order = histories["CLI-002"][0]
    for alternate in (other_order, other_customer_order):
        with pytest.raises(HTTPError) as wrong_case:
            send_to_sav(
                boutique, thread_id=f"{alternate['customer_id']}:{alternate['id']}:{nonce}", approved=True,
            )
        assert wrong_case.value.code == 404

    result = send_to_sav(boutique, **identity, approved=True)
    assert result["status"] == "termine"
    with open_checkpointer(database) as checkpointer:
        checkpoint = checkpointer.get({"configurable": {"thread_id": thread_id}})["channel_values"]
        assert (checkpoint["customer_id"], checkpoint["order_id"], checkpoint["line_number"]) == (
            "CLI-001", order["id"], item["line_number"],
        )
    with psycopg.connect(database) as db:
        assert db.execute("SELECT order_id, line_number FROM shop.sav_returns").fetchall() == [
            (order["id"], item["line_number"]),
        ]
        assert db.execute(
            "SELECT product_id, quantity FROM shop.sav_stock WHERE size = 'L' AND quantity < 20"
        ).fetchall() == [(item["product_id"], 19)]


def test_migrates_legacy_tables_and_resumes_a_committed_exchange(database):
    cli(database, "init")
    opened = json.loads(cli(database, "ouvrir", "Je veux un échange en M.", "--demo", "echange"))
    case_id = opened["case_id"]
    cli(database, "decider", opened["thread_id"], "oui", "--simuler-timeout", code=2)

    with psycopg.connect(database) as db:
        (return_id,) = db.execute("SELECT id FROM shop.sav_returns").fetchone()
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            db.execute(sql.SQL("UPDATE {} SET thread_id = %s WHERE thread_id = %s").format(
                sql.Identifier("langgraph", table),
            ), (case_id, opened["thread_id"]))
        in_transit = db.execute(
            "SELECT id, ordered_on FROM shop.sav_orders WHERE status = 'en_livraison' ORDER BY id"
        ).fetchall()
        legacy_items = db.execute("""
            SELECT order_id, product_id, size, price_cents
            FROM shop.sav_order_items WHERE line_number = 1 AND order_id IN (
                SELECT id FROM shop.sav_orders WHERE status <> 'validee'
            ) ORDER BY order_id
        """).fetchall()
        db.execute("ALTER TABLE shop.sav_returns DROP COLUMN line_number")
        db.execute("""
            ALTER TABLE shop.sav_orders ADD COLUMN product_id text REFERENCES shop.sav_products,
                ADD COLUMN size text;
            UPDATE shop.sav_orders o SET product_id = i.product_id, size = i.size
                FROM shop.sav_order_items i WHERE i.order_id = o.id AND i.line_number = 1;
            ALTER TABLE shop.sav_orders ALTER COLUMN product_id SET NOT NULL;
            DROP TABLE shop.sav_order_items;
        """)
        db.execute("ALTER TABLE shop.sav_orders DROP CONSTRAINT sav_orders_fulfillment_status_check")
        db.execute("DELETE FROM shop.sav_orders WHERE status = 'validee'")
        db.execute("UPDATE shop.sav_orders SET status = 'expediee' WHERE status = 'en_livraison'")
        db.execute("""
            ALTER TABLE shop.sav_orders ADD CONSTRAINT sav_orders_status_check
                CHECK (status IN ('livree', 'expediee'))
        """)
        for schema, tables in (("shop", SHOP_TABLES), ("langgraph", CHECKPOINT_TABLES)):
            for table in tables:
                if table == "sav_order_items":
                    continue
                db.execute(sql.SQL("ALTER TABLE {} SET SCHEMA public").format(sql.Identifier(schema, table)))
            db.execute(sql.SQL("DROP SCHEMA {}").format(sql.Identifier(schema)))
        db.execute("CREATE TABLE public.unrelated (id integer)")

    cli(database, "init")
    cli(database, "init")
    resumed = json.loads(cli(database, "reprendre", case_id))
    assert resumed["status"] == "termine"
    assert resumed["return_id"] == return_id
    with psycopg.connect(database) as db:
        tables = set(db.execute(
            "SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('shop', 'langgraph', 'public')"
        ).fetchall())
        assert tables == (
            {("shop", table) for table in SHOP_TABLES}
            | {("langgraph", table) for table in CHECKPOINT_TABLES}
            | {("public", "unrelated")}
        )
        assert db.execute("SELECT id, line_number FROM shop.sav_returns").fetchall() == [(return_id, 1)]
        assert db.execute("""
            SELECT order_id, product_id, size, price_cents
            FROM shop.sav_order_items WHERE line_number = 1 AND order_id IN (
                SELECT id FROM shop.sav_orders WHERE status <> 'validee'
            ) ORDER BY order_id
        """).fetchall() == legacy_items
        assert db.execute(
            "SELECT id, ordered_on FROM shop.sav_orders WHERE status = 'en_livraison' ORDER BY id"
        ).fetchall() == in_transit
        assert db.execute("SELECT count(*) FROM shop.sav_orders WHERE status = 'validee'").fetchone() == (5,)
        assert db.execute(
            "SELECT quantity FROM shop.sav_stock WHERE product_id = 'ART-001' AND size = 'M'"
        ).fetchone() == (19,)
