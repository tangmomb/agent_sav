"""The public demo: one shop per visitor, and limits."""

import json
from http.client import HTTPConnection
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import psycopg
import pytest
from conftest import propose
from psycopg import sql

from agent_sav import sandboxes as sandboxes_module
from agent_sav import web
from agent_sav.sandboxes import BudgetSpent, Full, Limit, Sandboxes


@pytest.fixture
def sandboxes(database, model):
    made = Sandboxes(database, model=model, maximum=2)
    with psycopg.connect(database, autocommit=True) as conn:
        before = {row[0] for row in conn.execute("SELECT datname FROM pg_database")}
        yield made
        for (name,) in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'sav\\_demo\\_%'").fetchall():
            if name not in before:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


class Visitor:
    """A browser: it keeps its cookie."""

    def __init__(self, url, address="10.0.0.1"):
        self.host, self.address, self.cookie = urlsplit(url).netloc, address, None

    def request(self, method, path, body=None, form=False, **headers):
        connection = HTTPConnection(self.host, timeout=30)
        headers = {"X-Forwarded-For": f"1.2.3.4, {self.address}"} | headers
        if self.cookie:
            headers["Cookie"] = self.cookie
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            body = urlencode(body) if form else json.dumps(body)
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        if cookie := response.getheader("Set-Cookie"):
            self.cookie = cookie.split(";")[0]
            self.attributes = cookie
        return response.status, response.read().decode()

    def say(self, message, chat_id=None):
        chat_id = chat_id or json.loads(self.request("POST", "/api/chats", {"customer_id": "CLI-001"})[1])["chat_id"]
        status, text = self.request("POST", "/api/support", {"chat_id": chat_id, "message": message, "message_id": str(uuid4())})
        return status, json.loads(text), chat_id


@pytest.fixture
def public(serve, sandboxes):
    return serve(sandboxes=sandboxes)


def test_nothing_is_shown_before_a_shop_is_opened(public):
    visitor = Visitor(public)
    for path in ("/", "/orders", "/shop/support", "/support/mailbox/CLI-001"):
        status, page = visitor.request("GET", path)
        assert status == 200 and "Ouvrir ma boutique" in page and "Camille" not in page
    for path in ("/api/customers", "/api/customers/CLI-001/orders"):
        assert visitor.request("GET", path)[0] == 401
    assert visitor.request("POST", "/api/chats", {"customer_id": "CLI-001"})[0] == 401
    assert visitor.request("GET", "/chat.js")[0] == 200
    assert "Explorateur de graphes" in visitor.request("GET", "/graphs")[1]  # about the code, open to all
    assert {graph["id"] for graph in json.loads(visitor.request("GET", "/api/graphs")[1])["graphs"]} == {"conversation", "case"}
    visitor.cookie = "sav_demo=forged"
    assert "Ouvrir ma boutique" in visitor.request("GET", "/shop/support")[1]


def test_each_visitor_has_their_own_shop(public, model):
    first, second = Visitor(public), Visitor(public, "10.0.0.2")
    for visitor in (first, second):
        assert visitor.request("POST", "/demo")[0] == 303
        assert all(flag in visitor.attributes for flag in ("HttpOnly", "Secure", "SameSite=Lax"))
        assert "Maison Orme" in visitor.request("GET", "/")[1]
    model.plays(propose("return", reason="damaged"), "Transmis.")
    _, recap, chat_id = first.say("Mon pull est abîmé")
    assert recap["awaiting_confirmation"]
    assert first.say("oui", chat_id)[1]["messages"] == ["Transmis."]

    page = first.request("GET", "/shop/support")[1]
    case_id = page.split('action="/shop/support/')[1].split('"')[0]
    assert "Chronologie de l’agent" in page and "propose_return" in page
    assert case_id not in second.request("GET", "/shop/support")[1]
    assert second.request("POST", f"/shop/support/{case_id}", {"action": "approve"}, form=True)[0] == 400
    assert second.say("Bonjour", chat_id)[0] == 400  # another visitor's chat does not exist here
    assert first.request("POST", f"/shop/support/{case_id}", {"action": "approve"}, form=True)[0] == 303


def test_forms_from_another_site_are_refused(public):
    visitor = Visitor(public)
    assert visitor.request("POST", "/demo", Origin="https://evil.example")[0] == 403
    assert visitor.request("POST", "/demo", Origin=public)[0] == 303


def test_a_visitor_opens_a_few_shops_a_day(public, monkeypatch, sandboxes):
    monkeypatch.setattr(sandboxes, "create", lambda: "token")
    visitor = Visitor(public)
    assert [visitor.request("POST", "/demo")[0] for _ in range(web.SHOPS_PER_DAY + 1)] == [303] * web.SHOPS_PER_DAY + [429]
    assert Visitor(public, "10.0.0.9").request("POST", "/demo")[0] == 303


def test_the_daily_budget(public, model, sandboxes):
    sandboxes.daily_tokens = 150
    visitor = Visitor(public)
    visitor.request("POST", "/demo")
    model.plays("Bonjour !", "Encore.")
    _, _, chat_id = visitor.say("Bonjour")
    visitor.say("Bonjour", chat_id)
    status, answer, _ = visitor.say("Bonjour", chat_id)
    assert status == 429 and "budget du jour" in answer["error"]
    with pytest.raises(BudgetSpent):
        sandboxes.metered("", [], [])


def test_shops_are_limited_and_cleaned(sandboxes, query, monkeypatch):
    tokens = [sandboxes.create(), sandboxes.create()]
    with pytest.raises(Full):
        sandboxes.create()
    assert sandboxes.open(tokens[0]).shop.customers()
    query("UPDATE demo.sandboxes SET used_at = now() - interval '25 hours' WHERE token_hash = %s",
          sandboxes_module.token_hash(tokens[1]))
    third = sandboxes.create()
    assert sandboxes.open(tokens[1]) is None
    assert sandboxes.open(tokens[0]) and sandboxes.open(third)
    sandboxes.tick()
    assert tokens[0] not in str(query("SELECT * FROM demo.sandboxes"))  # only its hash is kept


def test_limit():
    limit = Limit(2, 60)
    assert [limit.allow("a"), limit.allow("a"), limit.allow("a"), limit.allow("b")] == [True, True, False, True]


def test_a_shared_shop_only_serves_this_machine(serve):
    url = serve()
    assert Visitor(url).request("GET", "/shop/support")[0] == 200
    assert Visitor(url).request("GET", "/shop/support", Host="sav.example")[0] == 403


def test_a_shared_shop_is_not_published_by_mistake(database):
    with pytest.raises(ValueError):
        web.serve(database, 0, host="0.0.0.0")
