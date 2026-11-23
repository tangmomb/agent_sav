import json
from queue import Queue
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest
from conftest import propose

from agent_sav import web


@pytest.fixture
def boutique(database, support, monkeypatch):
    servers = Queue()

    class TestServer(web.ThreadingHTTPServer):
        def serve_forever(self):
            servers.put(self)
            super().serve_forever()

    monkeypatch.setattr(web, "ThreadingHTTPServer", TestServer)
    worker = Thread(target=web.serve, args=(database, 0, support), daemon=True)
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


def send_error(boutique, route, data, *, form=False, code=400) -> str:
    with pytest.raises(HTTPError) as error:
        send(boutique, route, data, form=form)
    assert error.value.code == code
    return error.value.read().decode()


def get_page(boutique, route):
    with urlopen(boutique + route, timeout=10) as response:
        return response.read().decode()


def open_chat(boutique, customer="CLI-001"):
    return send(boutique, "/api/chats", {"customer_id": customer})["chat_id"]


def test_from_the_chat_to_the_mailbox(boutique, model):
    chat_id = open_chat(boutique)
    model.plays(propose("return", reason="damaged", details="Maille filée <b>"), "Transmis.")
    recap = send(boutique, "/api/support", {"chat_id": chat_id, "message": "Mon pull est abîmé"})
    assert recap["awaiting_confirmation"] and "soumise à la boutique" in recap["messages"][0]
    assert "Boîte vide" in get_page(boutique, "/shop/support") or "SAV-" not in get_page(boutique, "/shop/support")
    confirmation = {"chat_id": chat_id, "message": "oui", "message_id": str(uuid4())}
    sent = send(boutique, "/api/support", confirmation)
    assert sent == {"messages": ["Transmis."], "awaiting_confirmation": False}
    assert send(boutique, "/api/support", confirmation) == sent

    page = get_page(boutique, "/shop/support")
    assert "Maille filée &lt;b&gt;" in page and "89,00 €" in page and "Article abîmé" in page
    case_id = page.split('action="/shop/support/')[1].split('"')[0]
    route = f"/shop/support/{case_id}"
    assert "1 à 1 000" in send_error(boutique, route, {"action": "reject"}, form=True)
    assert "attend pas cette action" in send_error(boutique, route, {"action": "received"}, form=True)
    assert "Action inconnue" in send_error(boutique, route, {"action": "cancel"}, form=True)
    assert "Demande acceptée" in send(boutique, route, {"action": "approve"}, form=True)
    assert "Colis attendu" in get_page(boutique, "/shop/support")
    assert "Colis enregistré" in send(boutique, route, {"action": "received"}, form=True)
    mailbox = get_page(boutique, "/support/mailbox/CLI-001")
    assert "Demande SAV acceptée" in mailbox and "Remboursement : 89,00 €" in mailbox


def test_a_question_and_its_answer(boutique, model):
    model.plays(("escalate", {"question": "Cartes cadeaux ?"}), "Transmis.")
    send(boutique, "/api/support", {"chat_id": open_chat(boutique, "CLI-002"), "message": "Cartes cadeaux ?"})
    page = get_page(boutique, "/shop/support")
    assert "Renseignement · CLI-002" in page
    case_id = page.split('action="/shop/support/')[1].split('"')[0]
    send(boutique, f"/shop/support/{case_id}", {"action": "answer", "answer": "Oui, en boutique."}, form=True)
    assert "Oui, en boutique." in get_page(boutique, "/support/mailbox/CLI-002")


def test_a_model_failure_can_be_retried(boutique, model):
    message = {"chat_id": open_chat(boutique), "message": "Bonjour", "message_id": str(uuid4())}
    model.plays(RuntimeError("model down"), "Bonjour !")
    assert "momentanément indisponible" in send_error(boutique, "/api/support", message, code=503)
    assert send(boutique, "/api/support", message)["messages"] == ["Bonjour !"]


@pytest.mark.parametrize("payload", [
    {"chat_id": "CLI-001", "message": "Bonjour"},
    {"chat_id": f"chat-{uuid4()}", "message": "Bonjour"},
    {"chat_id": f"chat-{uuid4()}", "message": "   "},
    {"chat_id": f"chat-{uuid4()}", "message": "Bonjour", "message_id": "pas-un-uuid"},
    {"message": "Bonjour"}, [],
])
def test_bad_messages_are_rejected(boutique, payload):
    assert "pas pu être lue" in send_error(boutique, "/api/support", payload)


@pytest.mark.parametrize("payload", [{"customer_id": "CLI-999"}, {}, []])
def test_bad_sign_ins_are_rejected(boutique, payload):
    assert "introuvable" in send_error(boutique, "/api/chats", payload)
