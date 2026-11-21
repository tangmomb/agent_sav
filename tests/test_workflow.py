import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from threading import Barrier, Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
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



def post_form(boutique, route, **form):
    request = Request(
        boutique + route,
        data=urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=10) as response:
        return response.read().decode()


def get_page(boutique, route):
    with urlopen(boutique + route, timeout=10) as response:
        return response.read().decode()


def new_thread(customer="CLI-001", order="DEMO-1001"):
    return f"{customer}:{order}:{uuid4()}"


def stock(database, size="M"):
    with psycopg.connect(database) as db:
        return db.execute(
            "SELECT quantity FROM shop.sav_stock WHERE product_id = 'ART-001' AND size = %s", (size,),
        ).fetchone()[0]


def test_studio_graph_matches_excalidraw():
    from agent_sav.studio import graph

    labels = {
        "START": "__start__",
        "Que souhaitez-vous": "choisir_action",
        "Quel article ?": "choisir_article",
        "Quelle taille /": "demander_taille",
        "check_stock": "check_stock",
        "Rupture : proposer": "proposer_retour",
        "enregistrer_demande": "enregistrer_demande",
        "notifier_boutique": "notifier_boutique",
        "⏸ INTERRUPT 1": "attendre_validation_boutique",
        "Mail client : refus +": "envoyer_mail_refus",
        "envoyer_mail_confirmation": "envoyer_mail_confirmation",
        "⏸ INTERRUPT 2": "attendre_confirmation_client",
        "Demande annulée": "annuler_demande",
        "mail récapitulatif": "envoyer_mail_recapitulatif",
        "END": "__end__",
    }
    source = Path(__file__).resolve().parents[1] / "schema_langgraph" / "livree_uniquement.excalidraw"
    elements = [element for element in json.loads(source.read_text(encoding="utf-8"))["elements"]
                if not element["isDeleted"]]
    texts = {element["containerId"]: element["text"].splitlines()[0]
             for element in elements if element["type"] == "text" and element["containerId"]}
    nodes = {element["id"]: labels[texts[element["id"]]]
             for element in elements if element["type"] == "rectangle"}
    arrows = {
        (nodes[element["startBinding"]["elementId"]], nodes[element["endBinding"]["elementId"]], texts.get(element["id"]))
        for element in elements if element["type"] == "arrow"
    }

    displayed = graph.get_graph()
    assert set(displayed.nodes) == set(nodes.values())
    assert {(edge.source, edge.target, edge.data) for edge in displayed.edges} == arrows


def pending(boutique, *, action="Je veux un échange en M.", line=1):
    thread = new_thread()
    state = send_to_sav(boutique, thread_id=thread, line_number=line, message=action)
    assert state["status"] == "attente_boutique"
    assert "transmise" in state["message_client"]
    return thread


def shop_route(boutique):
    return re.search(r'action="(/boutique/sav/[^"]+)"', get_page(boutique, "/boutique/sav"))[1]


def mail_link(boutique, decision):
    mailbox = get_page(boutique, "/sav/mail/CLI-001")
    return re.search(r'href="(/sav/reponse/[^"/]+/' + decision + r'/[a-f0-9]+)"', mailbox)[1]


def test_exchange_resumes_after_commit_without_duplicate(database):
    cli(database, "init")
    opened = json.loads(cli(database, "ouvrir", "Je veux un échange en M.", "--ligne", "1", "--demo", "echange"))
    thread = opened["thread_id"]
    assert opened["status"] == "attente_boutique"
    assert opened["interruption"]["taille"] == "M"
    assert stock(database) == 19

    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_returns").fetchone() == (0,)

    validated = json.loads(cli(database, "decider", thread, "oui"))
    assert validated["status"] == "attente_client"
    link = validated["liens_mail"]["confirmer"]
    cli(database, "repondre", link, "--simuler-timeout", code=2)

    with psycopg.connect(database) as db:
        (reference,) = db.execute("SELECT id FROM shop.sav_returns").fetchone()
    resumed = json.loads(cli(database, "reprendre", thread))
    assert resumed["status"] == "termine"
    assert resumed["return_id"] == reference
    assert stock(database) == 19
    with psycopg.connect(database) as db:
        assert db.execute("SELECT id FROM shop.sav_returns").fetchall() == [(reference,)]
        assert db.execute("SELECT kind FROM shop.sav_messages ORDER BY kind").fetchall() == [
            ("boutique",), ("confirmation",), ("recapitulatif",),
        ]


def test_chat_article_then_variant_shop_and_signed_confirmation(database, boutique):
    thread = new_thread()
    opened = send_to_sav(boutique, thread_id=thread, open=True)
    assert "Bonjour Camille !" in opened["greeting"]
    assert "DEMO-1001 est livrée" in opened["greeting"]
    assert "Pull en laine côtelée" in opened["greeting"]

    unclear = {"thread_id": thread, "message": "Bonjour", "message_id": str(uuid4())}
    assert send_to_sav(boutique, **unclear)["status"] == "demande_a_clarifier"
    still_unclear = {**unclear, "message": "Pouvez-vous m’aider ?", "message_id": str(uuid4())}
    assert send_to_sav(boutique, **still_unclear)["status"] == "demande_a_clarifier"
    assert send_to_sav(boutique, **still_unclear)["status"] == "demande_a_clarifier"
    request = {"thread_id": thread, "message": "Le pull est trop petit", "message_id": str(uuid4())}
    choices = send_to_sav(boutique, **request)
    assert choices["status"] == "article_a_preciser"
    assert [item["line_number"] for item in choices["articles"]] == [1, 2, 3]
    assert send_to_sav(boutique, **request) == choices
    assert send_to_sav(boutique, thread_id=thread, article=999)["status"] == "article_a_preciser"
    variant = send_to_sav(boutique, thread_id=thread, article=1)
    assert variant["status"] == "taille_a_preciser"
    assert "Noir" in variant["coloris"]
    same = {"thread_id": thread, "message": "Du S", "message_id": str(uuid4())}
    assert send_to_sav(boutique, **same)["status"] == "taille_identique"
    assert send_to_sav(boutique, **same)["status"] == "taille_identique"

    reply = {"thread_id": thread, "message": "En coloris Noir", "message_id": str(uuid4())}
    registered = send_to_sav(boutique, **reply)
    assert (registered["status"], registered["size"], registered["color"]) == ("attente_boutique", "S", "Noir")
    assert send_to_sav(boutique, **reply) == registered
    config = {"configurable": {"thread_id": thread}}
    with open_checkpointer(database) as checkpointer:
        graph = build_graph(checkpointer, Shop(database), interpret_demo)
        assert graph.get_state(config).next == ("attendre_validation_boutique",)
    with pytest.raises(HTTPError) as chat_approval:
        send_to_sav(boutique, thread_id=thread, approved=True)
    assert chat_approval.value.code == 400
    assert "Aucun message" in get_page(boutique, "/sav/mail/CLI-001")

    route = shop_route(boutique)
    assert "Demande validée" in post_form(boutique, route, decision="oui")
    with open_checkpointer(database) as checkpointer:
        graph = build_graph(checkpointer, Shop(database), interpret_demo)
        assert graph.get_state(config).next == ("attendre_confirmation_client",)
    default_mailbox = get_page(boutique, "/sav/mail")
    assert '<option value="CLI-001" selected>Camille Moreau</option>' in default_mailbox
    assert '<option value="CLI-002">Noah Laurent</option>' in default_mailbox
    assert "Confirmez votre demande SAV" in default_mailbox
    other_mailbox = get_page(boutique, "/sav/mail?client=CLI-002")
    assert '<option value="CLI-002" selected>Noah Laurent</option>' in other_mailbox
    assert "Aucun message" in other_mailbox
    assert other_mailbox == get_page(boutique, "/sav/mail/CLI-002")
    link = mail_link(boutique, "confirmer")
    changed = link[:-1] + ("0" if link[-1] != "0" else "1")
    with pytest.raises(HTTPError) as forged:
        post_form(boutique, changed)
    assert forged.value.code == 404
    assert "Confirmer la demande" in get_page(boutique, link)
    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_returns").fetchone() == (0,)

    assert "demande est confirmée" in post_form(boutique, link)
    assert "demande est confirmée" in post_form(boutique, link)
    assert "Récapitulatif" in get_page(boutique, "/sav/mail/CLI-001")
    with psycopg.connect(database) as db:
        assert db.execute("SELECT line_number, size, color FROM shop.sav_returns").fetchall() == [(1, "S", "Noir")]
        assert db.execute(
            "SELECT quantity FROM shop.sav_color_stock WHERE product_id = 'ART-001' AND size = 'S' AND color = 'Noir'"
        ).fetchone() == (9,)


@pytest.mark.parametrize("shop_refusal", [True, False])
def test_refusal_releases_exchange_stock(database, boutique, shop_refusal):
    thread = pending(boutique)
    assert stock(database) == 19
    route = shop_route(boutique)
    if shop_refusal:
        with pytest.raises(HTTPError) as missing_reason:
            post_form(boutique, route, decision="non")
        assert missing_reason.value.code == 400
        assert "Demande refusée" in post_form(boutique, route, decision="non", reason="Délai dépassé")
        mailbox = get_page(boutique, "/sav/mail/CLI-001")
        assert "Délai dépassé" in mailbox
        status = "refuse_boutique"
    else:
        post_form(boutique, route, decision="oui")
        assert stock(database) == 19
        link = mail_link(boutique, "refuser")
        assert "demande est annulée" in post_form(boutique, link)
        assert "demande est annulée" in post_form(boutique, link)
        status = "annule"
    assert stock(database) == 20
    with psycopg.connect(database) as db:
        assert db.execute("SELECT status FROM shop.sav_requests WHERE thread_id = %s", (thread,)).fetchone() == (status,)
        assert db.execute("SELECT count(*) FROM shop.sav_returns").fetchone() == (0,)


def test_stock_failure_offers_return_without_notifying_shop(database, boutique):
    out = send_to_sav(boutique, thread_id=new_thread(), line_number=1, message="Je veux un échange en XL.")
    assert out["status"] == "stock_indisponible"
    assert "retourner" in out["message_client"]
    assert "Aucune demande" in get_page(boutique, "/boutique/sav")
    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_requests").fetchone() == (0,)
    pending(boutique, action="Je veux retourner cet article.", line=out["line_number"])
    post_form(boutique, shop_route(boutique), decision="oui")
    post_form(boutique, mail_link(boutique, "confirmer"))
    with psycopg.connect(database) as db:
        assert db.execute("SELECT line_number, action, size FROM shop.sav_returns").fetchall() == [(1, "retour", None)]


def test_two_requests_cannot_reserve_the_last_unit(database, boutique, monkeypatch):
    with psycopg.connect(database) as db:
        db.execute("UPDATE shop.sav_stock SET quantity = 1 WHERE product_id = 'ART-001' AND size = 'M'")
    both_at_stock_check = Barrier(2, timeout=5)
    original = Shop.reserve_exchange

    def check_together(self, *args):
        both_at_stock_check.wait()
        return original(self, *args)

    monkeypatch.setattr(Shop, "reserve_exchange", check_together)

    def submit(thread):
        return send_to_sav(boutique, thread_id=thread, line_number=1, message="Je veux un échange en M.")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [new_thread(), new_thread()]))
    assert {result["status"] for result in results} == {"attente_boutique", "stock_indisponible"}
    assert stock(database) == 0
    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_requests").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM shop.sav_messages").fetchone() == (1,)


def test_purchase_history_and_case_are_scoped_to_customer_and_order(boutique):
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
    with pytest.raises(HTTPError) as wrong_owner:
        send_to_sav(boutique, thread_id=new_thread("CLI-002"), message="Je veux un retour.")
    assert wrong_owner.value.code == 404
    for status, expected in [("validee", "commande_validee"), ("en_livraison", "commande_en_transit")]:
        order = next(order for order in all_orders if order["status"] == status)
        result = send_to_sav(boutique, thread_id=new_thread(order["customer_id"], order["id"]), message="Je veux un retour.")
        assert result["status"] == expected


def test_migrates_legacy_tables_and_preserves_completed_exchange(database):
    cli(database, "init")
    opened = json.loads(cli(database, "ouvrir", "Je veux un échange en M.", "--ligne", "1", "--demo", "echange"))
    thread = opened["thread_id"]
    decision = json.loads(cli(database, "decider", thread, "oui"))
    completed = json.loads(cli(database, "repondre", decision["liens_mail"]["confirmer"]))

    with psycopg.connect(database) as db:
        (return_id,) = db.execute("SELECT id FROM shop.sav_returns").fetchone()
        db.execute("DROP TABLE shop.sav_messages, shop.sav_requests, shop.sav_color_stock")
        db.execute("ALTER TABLE shop.sav_returns DROP COLUMN color")
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
    resumed = json.loads(cli(database, "statut", thread))
    assert resumed["status"] == "termine"
    assert resumed["return_id"] == return_id == completed["return_id"]
    with psycopg.connect(database) as db:
        tables = set(db.execute(
            "SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('shop', 'langgraph', 'public')"
        ).fetchall())
        assert tables == (
            {("shop", table) for table in (*SHOP_TABLES, "sav_requests", "sav_messages", "sav_color_stock")}
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
