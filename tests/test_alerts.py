"""The operator alert, sent to a contact server over real HTTP."""

import threading
import time

import pytest
import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from agent_sav import alerts


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
