import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from mistralai.client import Mistral

from agent_sav.grounding import ungrounded
from agent_sav.model import UnusableAnswer, is_transient, to_mistral, within


def test_a_real_network_error_is_retried():
    """Raised by the SDK itself: the kind of error it uses has changed before."""
    with Mistral(api_key="none", server_url="http://127.0.0.1:9", timeout_ms=2000) as client:
        with pytest.raises(Exception) as error:
            client.chat.complete(model="none", messages=[{"role": "user", "content": "Bonjour"}])
    assert is_transient(error.value)


@pytest.mark.parametrize(("error", "expected"), [(UnusableAnswer(), True), (TimeoutError(), True), (ValueError(), False)])
def test_other_errors(error, expected):
    assert is_transient(error) is expected


def test_messages_for_the_model():
    call = {"id": "abc", "name": "list_orders", "args": {}}
    assert to_mistral(HumanMessage("Bonjour")) == {"role": "user", "content": "Bonjour"}
    assert to_mistral(AIMessage("", tool_calls=[call]))["tool_calls"][0]["function"] == {"name": "list_orders", "arguments": "{}"}
    result = to_mistral(ToolMessage("[]", tool_call_id="abc", name="list_orders", artifact={"reply": "secret"}))
    assert result == {"role": "tool", "tool_call_id": "abc", "name": "list_orders", "content": "[]"}


def test_a_call_that_hangs_is_given_up():
    assert within(5, lambda: "ok") == "ok"
    with pytest.raises(ZeroDivisionError):
        within(5, lambda: 1 / 0)
    with pytest.raises(TimeoutError) as error:
        within(0.05, lambda: time.sleep(1))
    assert is_transient(error.value)


ORDERS = ToolMessage('[{"total": "187,00 €", "items": [{"price": "89,00 €"}, {"price": "29,00 €"}]}]', tool_call_id="a", name="list_orders")
DONE = ToolMessage("Dossier SAV-1 accepté.", tool_call_id="b", name="propose_return", artifact={"done": True})


@pytest.mark.parametrize(("answer", "seen", "grounded"), [
    ("Votre pull coûte 89 €.", [ORDERS], True),
    ("89,00 € et 29,00 €, soit 118,00 € en tout.", [ORDERS], True),
    ("Remboursement de 8,90 €.", [ORDERS], False),
    ("Une réduction de 15 %.", [ORDERS], False),
    ("Vous demandez 200 €, ce n’est pas possible.", [HumanMessage("Je veux 200 €")], True),
    ("Votre retour est enregistré.", [ORDERS], False),
    ("C’est noté, ton retour du cardigan est accepté.", [ORDERS], False),
    ("Votre demande de retour a bien été transmise à la boutique.", [ORDERS], False),
    ("J’ai transmis votre question.", [ORDERS], False),
    ("Votre retour est enregistré.", [ORDERS, DONE], True),
    ("Votre retour n’est pas accepté : le délai est dépassé.", [ORDERS], True),
    ("Votre retour n’a pas été enregistré.", [ORDERS], True),
    ("Votre commande est confirmée, pas encore expédiée.", [ORDERS], True),
    ("Jusqu’à 89 €, le retour est accepté tout de suite.", [ORDERS], True),
    ("C’est noté, vous gardez votre article.", [ORDERS], True),
])
def test_what_an_answer_may_state(answer, seen, grounded):
    assert (ungrounded(answer, seen) is None) is grounded, ungrounded(answer, seen)
