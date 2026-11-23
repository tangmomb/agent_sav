import json
import os
import subprocess
import sys
from queue import Queue
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_sav import model, web
from agent_sav.database import CHECKPOINT_TABLES, SHOP_TABLES, initialize_database
from agent_sav.model import confirms, understand
from agent_sav.shop import DEFAULT_DSN, Shop
from agent_sav.workflow import CUSTOMER_PAUSES, build_graph

PULL = "ITEM-DEMO-1001-1"  # Pull en laine côtelée, Vert mousse, S (ART-001; ART-021 in Noir)
TSHIRT = "ITEM-DEMO-1001-2"


@pytest.fixture(autouse=True)
def rules_only(monkeypatch):
    """Tests read messages with the rules, never with the language model."""
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")


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


@pytest.fixture
def graph(database):
    return build_graph(InMemorySaver(), Shop(database))


class Conversation:
    """A chat with the graph, as the customer CLI-001."""

    def __init__(self, graph):
        self.graph = graph
        self.config = {"configurable": {"thread_id": f"CLI-001:{uuid4()}"}}
        self.thread_id = self.config["configurable"]["thread_id"]

    def say(self, text: str) -> str:
        if self.graph.get_state(self.config).values:
            self.graph.invoke(Command(resume={"message": text}), self.config)
        else:
            self.graph.invoke({"messages": [{"type": "human", "content": text}]}, self.config)
        if self.waiting_for in CUSTOMER_PAUSES:
            return self.graph.get_state(self.config).interrupts[0].value["question"]
        return str(self.state["messages"][-1].text)

    def shop_decides(self, approved: bool, reason: str = "") -> None:
        self.graph.invoke(Command(resume={"approved": approved, "reason": reason}), self.config)

    @property
    def state(self) -> dict:
        return self.graph.get_state(self.config).values

    @property
    def waiting_for(self) -> tuple[str, ...]:
        return self.graph.get_state(self.config).next


def quantity(database, product_id, size):
    with psycopg.connect(database) as db:
        return db.execute(
            "SELECT quantity FROM shop.stock WHERE product_id = %s AND size = %s", (product_id, size),
        ).fetchone()[0]


def test_graph_shape():
    from agent_sav.studio import graph

    assert set(graph.get_input_jsonschema()["properties"]) == {"messages"}
    assert set(graph.get_output_jsonschema()["properties"]) == {
        "messages", "action", "item_id", "size", "color", "question", "status",
    }
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert edges == {
        ("__start__", "understand"),
        ("understand", "ask"), ("ask", "understand"),
        ("understand", "send_question"), ("send_question", "__end__"),
        ("understand", "cancel_request"), ("cancel_request", "__end__"),
        ("understand", "validate_request"),
        ("validate_request", "register_request"), ("validate_request", "understand"),
        ("register_request", "shop_review"), ("register_request", "__end__"),
        ("shop_review", "__end__"),
    }


@pytest.mark.parametrize(("message", "expected"), [
    ("Bonjour", {}),
    ("J'ai une question", {"action": "information"}),
    ("Où en est ma commande DEMO-1003 ?", {"action": "information", "question": "Où en est ma commande DEMO-1003 ?"}),
    ("Je veux retourner l'article", {"action": "return"}),
    ("Je veux échanger ITEM-DEMO-1001-1 en L noir", {"action": "exchange", "item_id": PULL, "size": "L", "color": "Noir"}),
    ("Il est trop petit, du M svp", {"action": "exchange", "size": "M"}),
    ("Je veux échanger ITEM-DEMO-9999-1", {"action": "exchange"}),  # not one of the customer's articles
])
def test_rules_read_the_message(message, expected):
    items = [{"item_id": PULL, "order_id": "DEMO-1001", "product_id": "ART-001", "name": "Pull en laine côtelée",
              "color": "Vert mousse", "size": "S"}]
    request = dict.fromkeys(("action", "item_id", "size", "color", "question"))
    found = understand(message, request, items, [])
    assert {key: value for key, value in found.items() if value is not None} == expected


@pytest.mark.parametrize(("message", "history", "expected"), [
    ("Non", ["recap"], None), ("annuler", [], None), ("annuler", ["recap"], "cancel"),
])
def test_the_model_cannot_give_up_for_the_customer(monkeypatch, message, history, expected):
    cancel = model.Found(**dict.fromkeys(model.Found.model_fields) | {"action": "cancel"})
    monkeypatch.setattr(model, "understand_rules", lambda *_: cancel)
    history = [{"role": "sav", "content": content} for content in history]
    assert understand(message, {}, [], history)["action"] == expected


def test_the_suggested_word_always_gives_up(monkeypatch):
    nothing = model.Found(**dict.fromkeys(model.Found.model_fields))
    monkeypatch.setattr(model, "understand_rules", lambda *_: nothing)
    assert understand("Annuler.", {}, [], [{"role": "sav", "content": "recap"}])["action"] == "cancel"


def test_rules_pick_an_article_from_the_list():
    items = [{"item_id": item_id, "order_id": "DEMO-1001", "product_id": "ART-001", "name": name,
              "color": "Écru", "size": "M"} for item_id, name in ((PULL, "Pull"), (TSHIRT, "T-shirt en coton"))]
    request = {"action": "return", "item_id": None, "size": None, "color": None, "question": None}
    history = [{"role": "sav", "content": "Quel article est concerné ?\n1. Pull\n2. T-shirt en coton"}]
    assert understand("le deuxième", request, items, history)["item_id"] == TSHIRT
    assert understand("2", request, items, history)["item_id"] == TSHIRT
    assert understand("le t-shirt en coton", request, items, [])["item_id"] == TSHIRT


@pytest.mark.parametrize(("message", "expected"), [
    ("oui", True), ("Oui !", True), ("ok merci", True), ("Oui, c’est bon", True), ("Je confirme le retour", True),
    ("non", False), ("merci", False), ("oui mais en L", False), ("oui, enfin non", False), ("le pull", False),
])
def test_only_a_plain_yes_confirms(message, expected):
    assert confirms(message) is expected


def test_information_needs_only_the_question(graph, database):
    chat = Conversation(graph)
    assert "poser une question, échanger ou retourner" in chat.say("Bonjour")
    assert chat.say("J'ai une question") == "Quelle est votre question ?"
    assert "transmise à la boutique" in chat.say("Livrez-vous en Belgique ?")
    assert chat.state["status"] == "question_sent"
    assert (chat.state["item_id"], chat.state["size"], chat.state["color"]) == (None, None, None)
    assert chat.waiting_for == ()

    shop = Shop(database)
    [inquiry] = shop.unanswered_inquiries()
    assert (inquiry["thread_id"], inquiry["customer_id"], inquiry["question"]) == (
        chat.thread_id, "CLI-001", "Livrez-vous en Belgique ?")
    assert shop.answer_inquiry(chat.thread_id, "Oui.")
    assert shop.answer_inquiry(chat.thread_id, "Oui.")  # the same answer sent twice is accepted
    assert not shop.answer_inquiry(chat.thread_id, "Non.")
    assert shop.unanswered_inquiries() == []
    [mail] = shop.mailbox("CLI-001")
    assert (mail["sender"], mail["body"]) == ("shop", "Réponse de la boutique\nOui.")


def test_return_needs_the_article_and_ends_with_the_shop_decision(graph, database):
    chat = Conversation(graph)
    question = chat.say("Je veux retourner un article")
    assert question.startswith("Quel article est concerné ?\n1. Pull en laine côtelée")
    assert chat.say("2") == (
        "Voici votre demande :\nRetour : T-shirt en coton épais (Écru · XL, commande DEMO-1001)\n"
        "Répondez « oui » pour confirmer, « annuler » pour abandonner, ou dites-moi ce qu’il faut changer."
    )
    assert chat.waiting_for == ("validate_request",)
    assert Shop(database).requests_for_shop() == []  # nothing is recorded before the customer agrees
    assert "transmise à la boutique" in chat.say("oui")
    assert (chat.state["action"], chat.state["item_id"], chat.state["status"]) == ("return", TSHIRT, "awaiting_shop")
    assert chat.waiting_for == ("shop_review",)
    assert Shop(database).requests_for_shop()[0]["thread_id"] == chat.thread_id

    chat.shop_decides(approved=True)
    assert chat.state["status"] == "completed"
    assert chat.waiting_for == ()
    [mail] = Shop(database).mailbox("CLI-001")
    assert mail["body"].startswith("Demande SAV acceptée\nCommande DEMO-1001 · Retour · T-shirt")
    assert mail["body"].endswith(f"Référence : {Shop.reference(chat.thread_id)}")


def test_exchange_needs_a_different_variant_in_stock(graph, database):
    chat = Conversation(graph)
    assert chat.say("Je voudrais échanger mon pull").startswith("Quelle taille ou quel coloris")
    assert chat.state["item_id"] == PULL
    assert chat.say("en S").startswith("C’est la variante que vous avez déjà")
    assert chat.say("en XL").startswith("Cette variante est en rupture")  # ART-001 has no XL left
    assert "Coloris : Vert mousse, Noir." in chat.say("en violet")
    assert quantity(database, "ART-021", "L") == 10

    assert "Contre : Noir · L\n" in chat.say("en L noir")
    assert quantity(database, "ART-021", "L") == 10  # not reserved yet
    assert "transmise à la boutique" in chat.say("Oui, je confirme")
    assert (chat.state["size"], chat.state["color"], chat.state["status"]) == ("L", "Noir", "awaiting_shop")
    assert quantity(database, "ART-021", "L") == 9  # reserved

    chat.shop_decides(approved=False, reason="Article abîmé")
    assert chat.state["status"] == "rejected_by_shop"
    assert quantity(database, "ART-021", "L") == 10  # back in stock
    [mail] = Shop(database).mailbox("CLI-001")
    assert (mail["sender"], mail["body"]) == ("shop", (
        "Demande SAV refusée\nCommande DEMO-1001 · Échange · Pull en laine côtelée · Noir · taille L\nMotif : Article abîmé"
    ))


def test_a_size_alone_keeps_the_bought_color(graph, database):
    chat = Conversation(graph)
    assert "Contre : Vert mousse · M\n" in chat.say(f"Je veux échanger {PULL} en M")
    chat.say("oui")
    assert (chat.state["size"], chat.state["color"], chat.state["status"]) == ("M", "Vert mousse", "awaiting_shop")
    assert quantity(database, "ART-001", "M") == 19


def test_pauses_reject_answers_in_the_wrong_shape(graph):
    from pydantic import ValidationError

    chat = Conversation(graph)
    chat.say("Je veux retourner un article")
    for wrong in ("2", {"message": "  "}, {"text": "2"}):
        with pytest.raises(ValidationError):
            graph.invoke(Command(resume=wrong), chat.config)
    assert chat.waiting_for == ("ask",)  # still waiting: the answer can be given again
    chat.say("2")
    with pytest.raises(ValidationError):
        graph.invoke(Command(resume=True), chat.config)
    assert chat.waiting_for == ("validate_request",)
    chat.say("oui")
    with pytest.raises(ValidationError):
        graph.invoke(Command(resume=True), chat.config)
    assert chat.waiting_for == ("shop_review",)


def test_the_customer_corrects_the_request_before_it_is_sent(graph, database):
    chat = Conversation(graph)
    assert "Contre : Vert mousse · M\n" in chat.say(f"Je veux échanger {PULL} en M")
    assert "Contre : Vert mousse · L\n" in chat.say("Non, plutôt en L")
    assert "Contre : Noir · L\n" in chat.say("oui mais en noir")
    assert chat.waiting_for == ("validate_request",)
    # Another article: the size and color wanted for the first one no longer apply.
    assert chat.say("Non, c'est le t-shirt").startswith("Quelle taille ou quel coloris")
    assert (chat.state["item_id"], chat.state["size"], chat.state["color"]) == (TSHIRT, None, None)
    assert "Échange : T-shirt" in chat.say("en M")
    assert Shop(database).requests_for_shop() == []
    assert "transmise à la boutique" in chat.say("oui")
    with psycopg.connect(database) as db:
        assert db.execute("SELECT item_id, size FROM shop.sav_requests").fetchall() == [(TSHIRT, "M")]
    assert quantity(database, "ART-021", "L") == 10  # the abandoned variant was never reserved


def test_a_bare_no_asks_what_to_change(graph):
    chat = Conversation(graph)
    chat.say("Je veux retourner mon pull")
    for _ in range(2):
        answer = chat.say("non")
        assert answer.startswith("Que souhaitez-vous changer ?\nRetour : Pull en laine côtelée")
    assert "Retour : T-shirt" in chat.say("le t-shirt")
    assert "transmise à la boutique" in chat.say("oui")


@pytest.mark.parametrize("first", [
    "Je veux retourner mon pull",  # waits in validate_request
    "Je veux retourner un article",  # waits in ask
    "J'ai une question",
])
def test_the_customer_gives_up(graph, database, first):
    chat = Conversation(graph)
    chat.say(first)
    assert "abandonnée" in chat.say("Non, laisse tomber")
    assert (chat.state["status"], chat.state["action"], chat.waiting_for) == ("cancelled", None, ())
    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_requests").fetchone() == (0,)


def test_a_thread_can_start_again_after_giving_up(graph):
    chat = Conversation(graph)
    chat.say("Je veux retourner mon pull")
    chat.say("annuler")
    graph.invoke({"messages": [{"type": "human", "content": "Je veux retourner un article"}]}, chat.config)
    assert chat.waiting_for == ("ask",)  # Studio keeps the thread: the old article is forgotten


def test_cancelling_an_order_is_not_giving_up(graph):
    chat = Conversation(graph)
    chat.say("J'ai une question")
    assert "transmise à la boutique" in chat.say("Puis-je annuler ma commande ?")
    assert Conversation(graph).say("annuler").startswith("Bonjour")  # nothing to give up yet


def test_a_correction_can_change_the_action(graph):
    chat = Conversation(graph)
    chat.say("Je veux retourner mon pull")
    assert "transmise à la boutique" in chat.say("Non, j'ai une question : livrez-vous en Belgique ?")
    assert (chat.state["action"], chat.state["status"]) == ("information", "question_sent")


def test_switching_between_return_and_exchange_keeps_the_article(graph):
    chat = Conversation(graph)
    chat.say("Je veux retourner mon pull")
    assert "Échange : Pull en laine côtelée" in chat.say("Non, je préfère l'échanger en L")
    assert "Retour : Pull en laine côtelée" in chat.say("Finalement un retour")
    assert (chat.state["size"], chat.state["color"]) == (None, None)


def test_variant_taken_while_the_customer_confirms(graph, database):
    chat = Conversation(graph)
    chat.say(f"Je veux échanger {PULL} en L noir")
    with psycopg.connect(database) as db:
        db.execute("UPDATE shop.stock SET quantity = 0 WHERE product_id = 'ART-021'")
    assert "vient de passer en rupture" in chat.say("oui")
    assert chat.state["status"] == "out_of_stock"


def test_changing_action_forgets_the_previous_details(graph):
    chat = Conversation(graph)
    chat.say("Je voudrais échanger mon pull")
    chat.say("Finalement j'ai une question")
    assert chat.state["action"] == "information"
    assert (chat.state["item_id"], chat.state["size"], chat.state["color"]) == (None, None, None)


def test_last_variant_taken_meanwhile(graph, database):
    chat = Conversation(graph)
    chat.say(f"Je veux échanger {PULL}")
    with psycopg.connect(database) as db:
        db.execute("UPDATE shop.stock SET quantity = 0 WHERE product_id = 'ART-021'")
    assert chat.say("en L noir").startswith("Cette variante est en rupture")


def test_database_setup_is_idempotent(database):
    initialize_database(database)
    with psycopg.connect(database) as db:
        tables = set(db.execute(
            "SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('shop', 'langgraph')"
        ).fetchall())
        assert tables == {("shop", table) for table in SHOP_TABLES} | {("langgraph", table) for table in CHECKPOINT_TABLES}
        assert db.execute("SELECT count(*) FROM shop.products").fetchone() == (40,)


def cli(database: str, *args: str, code: int = 0) -> dict | str:
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "agent_sav", *args],
        env={**os.environ, "DATABASE_URL": database, "MISTRAL_API_KEY": ""},
        capture_output=True, encoding="utf-8", check=False, timeout=60,
    )
    assert result.returncode == code, result.stderr
    return json.loads(result.stdout) if code == 0 else result.stderr


def test_cli_journey_and_recovery_after_a_lost_response(database):
    opened = cli(database, "open", "Je veux retourner un article")
    thread = opened["thread_id"]
    assert opened["next"] == ["ask"]
    assert "ce dossier n’attend pas cette étape" in cli(database, "decide", thread, "yes", code=1).casefold()

    recap = cli(database, "say", thread, "le premier")
    assert recap["next"] == ["validate_request"]
    assert "Retour : Pull en laine côtelée" in recap["question"]

    asked = cli(database, "say", thread, "oui")
    assert (asked["item_id"], asked["status"], asked["next"]) == (PULL, "awaiting_shop", ["shop_review"])
    assert "motif" in cli(database, "decide", thread, "no", code=1)
    assert "pas d'exécution à reprendre" in cli(database, "resume", thread, code=1)

    lost = cli(database, "decide", thread, "yes", "--simulate-timeout", code=2)
    assert f"resume {thread}" in lost
    resumed = cli(database, "resume", thread)
    assert (resumed["status"], resumed["reference"]) == ("completed", Shop.reference(thread))
    assert [mail["body"].split("\n")[0] for mail in Shop(database).mailbox("CLI-001")] == ["Demande SAV acceptée"]


@pytest.fixture
def boutique(database, monkeypatch):
    servers = Queue()

    class TestServer(web.ThreadingHTTPServer):
        def serve_forever(self):
            servers.put(self)
            super().serve_forever()

    monkeypatch.setattr(web, "ThreadingHTTPServer", TestServer)
    worker = Thread(target=web.serve, args=(database, 0), daemon=True)
    worker.start()
    server = servers.get(timeout=10)
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        worker.join(timeout=5)


def send(boutique, route, data, *, form=False):
    body = urlencode(data).encode() if form else json.dumps(data).encode()
    content_type = "application/x-www-form-urlencoded" if form else "application/json"
    with urlopen(Request(boutique + route, data=body, headers={"Content-Type": content_type}), timeout=30) as response:
        return response.read().decode() if form else json.load(response)


def get_page(boutique, route):
    with urlopen(boutique + route, timeout=10) as response:
        return response.read().decode()


def test_web_chat_and_shop_decision(boutique, database):
    thread = f"CLI-001:{uuid4()}"
    first = {"thread_id": thread, "message_id": str(uuid4()), "message": "Je souhaite retourner mon pull"}
    recap = send(boutique, "/api/support", first)
    assert recap["awaiting_message"] and "Retour : Pull en laine côtelée" in recap["question"]
    assert send(boutique, "/api/support", first) == recap  # a retried message is not handled twice
    assert "Aucune demande en attente" in get_page(boutique, "/shop/support")

    confirmation = {"thread_id": thread, "message_id": str(uuid4()), "message": "oui"}
    sent = send(boutique, "/api/support", confirmation)
    assert sent == {"status": "awaiting_shop", "messages": [{
        "role": "assistant", "content": "Votre demande a été transmise à la boutique. Vous recevrez la suite par mail.",
    }]}
    assert send(boutique, "/api/support", confirmation) == sent

    page = get_page(boutique, "/shop/support")
    assert "Retour · DEMO-1001" in page and f'action="/shop/support/{thread}"' in page
    assert "Indiquez un motif" in send_error(boutique, f"/shop/support/{thread}", {"decision": "no"})
    assert "Demande acceptée" in send(boutique, f"/shop/support/{thread}", {"decision": "yes"}, form=True)
    assert "Aucune demande en attente" in get_page(boutique, "/shop/support")
    assert "Demande SAV acceptée" in get_page(boutique, "/support/mailbox/CLI-001")


def test_web_chat_asks_until_complete(boutique):
    thread = f"CLI-002:{uuid4()}"
    asked = send(boutique, "/api/support", {"thread_id": thread, "message": "Bonjour"})
    assert asked["awaiting_message"] and "échanger ou retourner" in asked["question"]
    asked = send(boutique, "/api/support", {"thread_id": thread, "message": "J'ai une question"})
    assert asked == {"question": "Quelle est votre question ?", "awaiting_message": True}
    done = send(boutique, "/api/support", {"thread_id": thread, "message": "Faites-vous des cartes cadeaux ?"})
    assert done["status"] == "question_sent"

    page = get_page(boutique, "/shop/support")
    assert "Renseignement · CLI-002" in page and "Faites-vous des cartes cadeaux ?" in page
    send(boutique, f"/shop/support/inquiry/{thread}", {"answer": "Oui, en boutique."}, form=True)
    assert "Oui, en boutique." in get_page(boutique, "/support/mailbox/CLI-002")


def send_error(boutique, route, data, *, form=True) -> str:
    with pytest.raises(HTTPError) as error:
        send(boutique, route, data, form=form)
    assert error.value.code == 400
    return error.value.read().decode()


@pytest.mark.parametrize("payload", [
    {"thread_id": "pas-un-thread", "message": "Bonjour"},
    {"thread_id": f"CLI-001:{uuid4()}", "message": "   "},
    {"thread_id": f"CLI-001:{uuid4()}", "message": "Bonjour", "message_id": "pas-un-uuid"},
    {"message": "Bonjour"},
])
def test_web_chat_rejects_bad_payloads(boutique, payload):
    assert "pas pu être lue" in send_error(boutique, "/api/support", payload, form=False)
