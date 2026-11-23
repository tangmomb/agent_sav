"""The MCP server and the operator alert, over real HTTP."""

import asyncio
import json
import threading
import time

import mcp.types as types
import pytest
import uvicorn
from conftest import PULL, refund
from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from agent_sav import alerts
from agent_sav.mcp_server import build_app


class Running:
    """An ASGI app served on a free port."""

    def __init__(self, app):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/mcp"

    def __exit__(self, *_):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture
def url(support):
    with Running(build_app(support)) as url:
        yield url


@pytest.fixture
def call(url, support):
    tokens = {customer: support.shop.create_token(customer) for customer in ("CLI-001", "CLI-002")}

    def run(name, args=None, *, customer="CLI-001", agree=None, asked=None, mode="auto"):
        async def answer(context, params):
            if asked is not None:
                asked.append(params.message)
            if agree == "decline":
                return types.ElicitResult(action="decline")
            return types.ElicitResult(action="accept", content={"confirm": agree})

        async def main():
            async with create_mcp_http_client(headers={"Authorization": f"Bearer {tokens[customer]}"}) as http:
                async with Client(streamable_http_client(url, http_client=http), mode=mode,
                                  elicitation_callback=None if agree is None else answer) as client:
                    result = await client.call_tool(name, args or {})
                    return result.is_error, result.content[0].text
        return asyncio.run(main())
    return run


RETURN = {"item_id": PULL, "reason": "changed_mind"}


def root(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    return error


@pytest.mark.parametrize("token", ["", "Bearer", "Bearer wrong", "Basic abc"])
def test_a_token_is_required(url, token):
    async def main():
        async with create_mcp_http_client(headers={"Authorization": token}) as http:
            return (await http.post(url, json={})).status_code
    assert asyncio.run(main()) == 401


def test_reading_only_shows_the_customer_of_the_token(call):
    assert "DEMO-1001" in call("list_orders")[1]
    assert "DEMO-1001" not in call("list_orders", customer="CLI-002")[1]
    assert "Article inconnu" in call("check_stock", {"item_id": PULL}, customer="CLI-002")[1]
    assert "30 jours" in call("read_policy")[1]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_the_customer_agrees_before_anything_is_written(call, query, mode):
    asked = []
    error, text = call("propose_return", RETURN, agree=True, asked=asked, mode=mode)
    assert not error and json.loads(text)["status"] == "awaiting_parcel"
    assert len(asked) == 1 and "Retour : Pull en laine" in asked[0] and "89,00 €" in asked[0]
    assert query("SELECT kind, item_id FROM shop.cases") == [("return", PULL)]


@pytest.mark.parametrize("agree", [False, "decline"])
def test_without_agreement_nothing_is_written(call, query, agree):
    error, text = call("propose_return", RETURN, agree=agree)
    assert not error and "rien n’a été enregistré" in text
    assert query("SELECT 1 FROM shop.cases") == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_a_client_that_cannot_ask_cannot_act(call, query, mode):
    with pytest.raises(BaseException) as raised:
        error, _ = call("propose_return", RETURN, mode=mode)
        assert not error  # an error result is a refusal too
    assert "elicitation" in str(root(raised.value)).lower() or isinstance(root(raised.value), AssertionError)
    assert query("SELECT 1 FROM shop.cases") == []


def test_the_rules_come_before_the_question(call, query):
    asked = []
    error, text = call("propose_return", RETURN, customer="CLI-002", agree=True, asked=asked)
    assert error and "unknown_item" in text and asked == []
    error, text = call("propose_refund", refund("DEMO-1001")[1], agree=True, asked=asked)
    assert error and "delivered" in text and asked == []
    assert query("SELECT 1 FROM shop.cases") == []


def test_the_amount_comes_from_the_rules(call, query):
    call("propose_refund", {"order_id": "DEMO-1005", "reason": "lost_parcel", "amount_euros": 999}, agree=True)
    assert query("SELECT amount_cents FROM shop.refunds") == [(4200,)]


def test_exchange_cancel_and_question(call, query):
    _, text = call("propose_exchange", {"item_id": PULL, "reason": "wrong_size", "size": "L", "color": "Noir"}, agree=True)
    case_id = json.loads(text)["case_id"]
    assert case_id in call("list_cases")[1]
    assert call("cancel_case", {"case_id": case_id}, customer="CLI-002", agree=True)[0]
    assert json.loads(call("cancel_case", {"case_id": case_id}, agree=True)[1])["status"] == "cancelled"
    assert json.loads(call("escalate", {"question": "Cartes cadeaux ?"})[1])["status"] == "awaiting_shop"
    assert call("escalate", {"question": ""})[0]


# The operator alert.

@pytest.fixture
def contact(monkeypatch):
    """A contact server like the real one."""
    server, received = MCPServer("contact"), []

    @server.tool()
    def send_telegram(subject: str, message: str, ctx: Context, urgency: str = "normal") -> str:
        """Sends a message."""
        assert ctx.headers["authorization"] == "Bearer secret"
        if subject.startswith("Agent SAV : 9"):
            raise ValueError("quota")
        received.append((subject, message))
        return "sent"

    with Running(server.streamable_http_app()) as url:
        monkeypatch.setenv("CONTACT_MCP_URL", url)
        monkeypatch.setenv("CONTACT_MCP_TOKEN", "secret")
        yield received


def ask(support, number, customer="CLI-001"):
    support.cases.open(f"q{number}", customer, "escalate", {"question": f"Secret {number}"})
    support.run_cases()


def test_the_operator_is_told_once_and_not_too_often(support, contact, clock):
    assert not alerts.alert_operator(support.cases)
    ask(support, 1)
    ask(support, 2)
    assert alerts.alert_operator(support.cases) and not alerts.alert_operator(support.cases)
    [(subject, message)] = contact
    assert subject == "Agent SAV : 2 demandes à traiter"
    assert message.count("SAV-") == 2 and "Renseignement" in message and "Secret" not in message

    ask(support, 3)
    assert not alerts.alert_operator(support.cases)  # too soon
    clock.forward(minutes=31)
    assert alerts.alert_operator(support.cases)
    assert contact[1][0] == "Agent SAV : 1 demande à traiter"
    clock.forward(hours=5)
    assert not alerts.alert_operator(support.cases)


def test_a_failed_alert_is_sent_again(support, contact, monkeypatch):
    ask(support, 1)
    url = alerts.os.environ["CONTACT_MCP_URL"]
    monkeypatch.setenv("CONTACT_MCP_URL", "http://127.0.0.1:9/mcp")
    with pytest.raises(BaseException):
        alerts.alert_operator(support.cases)
    monkeypatch.setenv("CONTACT_MCP_URL", url)
    assert alerts.alert_operator(support.cases) and len(contact) == 1


def test_no_alert_without_configuration(support, monkeypatch):
    monkeypatch.delenv("CONTACT_MCP_URL", raising=False)
    ask(support, 1)
    assert not alerts.alert_operator(support.cases)
