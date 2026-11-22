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
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import CheckpointTuple, empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_sav import model, web
from agent_sav.__main__ import DEFAULT_DSN
from agent_sav.database import CHECKPOINT_TABLES, SHOP_TABLES, initialize_database, open_checkpointer
from agent_sav.legacy_checkpoints import translate_checkpoint
from agent_sav.model import collect_demo, reply_demo, select_demo
from agent_sav.shop import Shop
from agent_sav.workflow import CollectionUpdate, build_graph, collection_update


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
        boutique + "/api/support",
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

    assert set(graph.get_input_jsonschema()["properties"]) == {"messages"}
    assert "line_number" in graph.get_output_jsonschema()["properties"]
    assert set(graph.nodes["collect"].channels) == set(CollectionUpdate.__annotations__)
    state = {"messages": [HumanMessage(content="Bonjour", id="first"), AIMessage(content="Bonjour !")]}
    for node in graph.builder.nodes.values():
        expected = {"messages": [
            {"role": "user", "content": "Bonjour", "id": "first"},
            {"role": "assistant", "content": "Bonjour !"},
        ]}
        assert node.trace_policy.process_inputs(state) == expected
        assert node.trace_policy.process_outputs(state) == expected
    assert isinstance(state["messages"][0], HumanMessage)

    labels = {
        "START": "__start__",
        "collect": "collect",
        "request_support_question": "request_support_question",
        "check_stock": "check_stock",
        "validate_intent": "validate_intent",
        "Rupture : proposer": "offer_return",
        "register_request": "register_request",
        "notify_shop": "notify_shop",
        "⏸ INTERRUPT 1": "wait_for_shop_approval",
        "Mail client : refus +": "send_rejection_email",
        "send_confirmation_email": "send_confirmation_email",
        "⏸ INTERRUPT 2": "wait_for_customer_confirmation",
        "Demande annulée": "cancel_request",
        "mail récapitulatif": "send_summary_email",
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
    assert {edge.target for edge in displayed.edges if edge.source == "collect"} == {
        "collect", "validate_intent", "request_support_question",
    }


def test_collecter_cannot_write_downstream_fields():
    assert collection_update({
        "action": "return", "line_number": 1, "approved": True, "reason": "motif",
        "return_id": "RET-1001", "demo_mode": True, "unknown": "value",
    }) == {"action": "return", "line_number": 1}


def test_legacy_checkpoint_translates_state_and_reschedules_renamed_node():
    checkpoint = empty_checkpoint()
    channel = "branch:to:attendre_confirmation_client"
    checkpoint["channel_values"] = {
        channel: None, "status": "attente_client", "action": "echange", "message_client": "Votre retour",
    }
    checkpoint["channel_versions"] = {key: 1 for key in checkpoint["channel_values"]}
    checkpoint["versions_seen"] = {"attendre_confirmation_client": {channel: 0}}
    saved = CheckpointTuple({}, checkpoint, {}, pending_writes=[("old-task", "__interrupt__", ())])

    updated = translate_checkpoint(saved)
    assert updated.checkpoint["channel_values"] == {
        "branch:to:wait_for_customer_confirmation": None,
        "status": "awaiting_customer", "action": "exchange", "customer_message": "Votre retour",
    }
    assert updated.checkpoint["versions_seen"] == {"wait_for_customer_confirmation": {
        "branch:to:wait_for_customer_confirmation": 0,
    }}
    assert not updated.pending_writes


def test_selection_trace_separates_options_from_prompt(monkeypatch):
    def parse(messages, schema, *, structured_input):
        prompt, raw_options = messages[0]["content"].split("\n")
        trace = model.trace_completion_inputs({
            "messages": messages, "schema": schema, "structured_input": structured_input,
        })
        assert trace["messages"][0]["content"] == prompt
        assert trace["messages"][1]["content"] == "Oui"
        assert trace["messages"][1]["context"]["selection_context"] == {
            "question": "Vous confirmez ?", "options": {1: "Oui", 2: "Non"},
        }
        assert json.loads(raw_options) == {"question": "Vous confirmez ?", "options": {"1": "Oui", "2": "Non"}}
        return model.Selection(number=1)

    monkeypatch.setattr(model, "parse_response", parse)
    assert model.select_mistral("Oui", "Vous confirmez ?", ["Oui", "Non"]) == 1


@pytest.mark.parametrize(("schema", "payload", "expected"), [
    (model.CollectionTurn, {"action": "return"}, {"parsed": {"action": "return"}}),
    (model.Selection, {"number": 2}, {"parsed": {"number": 2}}),
    (model.ClientReply, {"message": "Votre demande a été transmise."}, {"content": "Votre demande a été transmise."}),
])
def test_mistral_trace_outputs_are_readable(schema, payload, expected):
    raw = json.dumps(payload, ensure_ascii=False)
    result = model.ParsedChatCompletionResponse[schema].model_validate({
        "id": "test", "object": "chat.completion", "model": model.MODEL, "created": 0,
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": raw, "parsed": payload,
        }}],
    })

    trace = model.trace_completion(result)
    message = trace["choices"][0]["message"]
    assert {key: message[key] for key in ("content", "parsed") if key in message} == expected
    assert trace["usage_metadata"]["total_tokens"] == 5
    assert result.choices[0].message.content == raw


@pytest.mark.parametrize("initial_size", ["M", None])
def test_collecter_exits_when_request_fields_are_complete(database, monkeypatch, initial_size):
    initialize_database(database)
    shop = Shop(database)
    prompts = []
    reply_events = []
    order_question = "Pouvez-vous me donner la référence de votre achat ?"
    variant_question = "Dans quelle variante voulez-vous recevoir le pull ?"
    answers = iter((
        model.CollectionTurn(
            action="exchange", size=[initial_size] if initial_size else [], line=[1] if initial_size else [],
            next_question=order_question,
        ),
        model.CollectionTurn(order_id="DEMO-1001", line=[] if initial_size else [1], next_question=variant_question),
        model.CollectionTurn(size=["M"], next_question="Quel coloris souhaitez-vous ?"),
    ))

    def parse(messages, schema, *, structured_input):
        payload = json.loads(messages[-1]["content"])
        assert structured_input == payload
        trace = model.trace_completion_inputs({
            "messages": messages, "schema": schema, "structured_input": structured_input,
        })
        assert json.loads(messages[-1]["content"]) == payload
        if schema is model.ClientReply:
            assert trace["messages"][-1]["context"]["shop_facts"] == payload["shop_facts"]
            assert trace["messages"][-1]["content"] == (payload["latest_customer_message"] or "")
            event = payload["shop_facts"]
            reply_events.append(event)
            return model.ClientReply(message={
                "awaiting_shop": "La boutique a reçu votre demande.",
                "intent_confirmation_needed": "Confirmez-vous cette demande ?",
            }[event["status"]])
        assert schema is model.CollectionTurn
        assert "context" not in trace
        assert list(trace["messages"][-1]) == ["content", "context", "role"]
        assert trace["messages"][-1] == {"content": payload["user_message"], "context": payload["context"], "role": "user"}
        prompts.append(messages)
        return next(answers)

    monkeypatch.setattr(model, "parse_response", parse)
    graph = build_graph(
        InMemorySaver(), shop, model.collect_mistral, respond=model.reply_mistral, select=select_demo,
    )
    config = {"configurable": {"thread_id": f"CLI-001:{uuid4()}"}}
    message = "Je voudrais un échange en M pour la ligne 1." if initial_size else "Je voudrais un échange."
    graph.invoke({"messages": [{"type": "human", "content": message}]}, config)
    pending = graph.get_state(config)
    assert pending.next == ("collect",)
    assert pending.values["action"] == "exchange"
    assert pending.values["candidates"]["size"] == ([initial_size] if initial_size else [])
    assert pending.values["candidates"]["line"] == ([1] if initial_size else [])
    schema = pending.interrupts[0].response_schema
    assert set(schema["properties"]) == {"message"}
    assert schema["required"] == ["message"]
    assert pending.interrupts[0].value["status"] == "order_needed"
    assert pending.interrupts[0].value["question"] == order_question
    options = pending.interrupts[0].value["options"]
    assert not reply_events
    first_message = message
    assert stock(database) == 20

    message = "La commande DEMO-1001." if initial_size else "Le pull de la commande DEMO-1001."
    graph.invoke(Command(resume={"message": message}), config)
    context = json.loads(prompts[1][-1]["content"])["context"]
    assert set(context) == {"collected_details", "discussion_thread", "orders"}
    question_with_options = "\n".join([order_question, "", *(f"{i}. {option}" for i, option in enumerate(options, 1))])
    assert context["discussion_thread"] == [
        {"role": "user", "content": first_message}, {"role": "assistant", "content": question_with_options},
    ]
    collected = context["collected_details"]
    assert collected["action"] == "exchange"
    assert collected["size"] == initial_size
    assert not {"approved", "reason", "return_id", "candidates"} & collected.keys()
    if initial_size is None:
        pending = graph.get_state(config)
        assert pending.next == ("collect",)
        assert pending.interrupts[0].value["status"] == "variant_needed"
        assert pending.interrupts[0].value["question"] == variant_question
        assert not reply_events
        assert pending.values["line_number"] == 1
        assert pending.values["order_id"] == "DEMO-1001"
        assert stock(database) == 20
        graph.invoke(Command(resume={"message": "En M."}), config)

    pending = graph.get_state(config)
    assert pending.next == ("validate_intent",)
    assert pending.interrupts[0].value["question"] == "Confirmez-vous cette demande ?"
    assert pending.interrupts[0].value["request_summary"]["size"] == "M"
    assert stock(database) == 20
    graph.invoke(Command(resume={"message": "Oui, je confirme."}), config)
    saved = graph.get_state(config)
    assert saved.next == ("wait_for_shop_approval",)
    assert saved.values["customer_message"] == "La boutique a reçu votre demande."
    assert (saved.values["order_id"], saved.values["line_number"], saved.values["size"], saved.values["color"]) == (
        "DEMO-1001", 1, "M", "Vert mousse",
    )
    assert stock(database) == 19
    assert len(prompts) == (3 if initial_size is None else 2)
    assert [event["status"] for event in reply_events] == ["intent_confirmation_needed", "awaiting_shop"]


@pytest.mark.parametrize("correction", ["Le T-shirt.", "Pas le pull, un autre article.", "Le T-shirt en M."])
def test_chat_article_correction_drops_only_the_previous_variant(database, boutique, correction):
    thread = new_thread()
    initial = {"thread_id": thread, "line_number": 1, "message": "Je veux un échange en S.", "message_id": str(uuid4())}
    assert send_to_sav(boutique, **initial)["status"] == "same_variant"

    result = send_to_sav(boutique, thread_id=thread, message=correction)
    if correction == "Pas le pull, un autre article.":
        assert result["status"] == "item_needed"
        result = send_to_sav(boutique, thread_id=thread, select_line_number=2)
    if correction != "Le T-shirt en M.":
        assert (result["status"], result["line_number"]) == ("variant_needed", 2)
        with psycopg.connect(database) as db:
            assert db.execute("SELECT count(*) FROM shop.sav_requests").fetchone() == (0,)
        result = send_to_sav(boutique, thread_id=thread, message="En M.")

    assert (result["status"], result["line_number"], result["size"]) == ("intent_confirmation_needed", 2, "M")
    result = confirm_intent(boutique, thread)
    assert stock(database, "S") == 20
    assert send_to_sav(boutique, **initial) == result


def confirm_intent(boutique, thread):
    return send_to_sav(boutique, thread_id=thread, message="Oui", message_id=str(uuid4()))


def test_multiple_candidates_and_intent_correction(database, boutique):
    thread = new_thread()
    result = send_to_sav(boutique, thread_id=thread, line_number=1, message="Je veux un échange en M ou L.")
    assert result["status"] == "variant_needed"
    with open_checkpointer(database) as checkpointer:
        graph = build_graph(checkpointer, Shop(database), collect_demo, respond=reply_demo, select=select_demo)
        state = graph.get_state({"configurable": {"thread_id": thread}}).values
        assert state["candidates"]["size"] == ["M", "L"]
        assert state["size"] is None

    result = send_to_sav(boutique, thread_id=thread, message="En M.")
    assert result["request_summary"]["size"] == "M"
    assert stock(database) == 20
    result = send_to_sav(boutique, thread_id=thread, message="Non")
    assert result["status"] == "intent_needs_correction"
    result = send_to_sav(boutique, thread_id=thread, message="Je veux un retour.")
    assert result["status"] == "intent_confirmation_needed"
    assert result["request_summary"]["action"] == "return"
    assert result["request_summary"]["size"] is None
    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_requests").fetchone() == (0,)
    with open_checkpointer(database) as checkpointer:
        graph = build_graph(checkpointer, Shop(database), collect_demo, respond=reply_demo, select=select_demo)
        config = {"configurable": {"thread_id": thread}}
        steps = list(graph.stream(Command(resume={"message": "Oui"}), config, stream_mode="updates"))
        assert [node for step in steps for node in step if node in graph.nodes] == [
            "validate_intent", "register_request", "notify_shop",
        ]
        assert graph.get_state(config).values["status"] == "awaiting_shop"
    assert stock(database) == 20


def pending(boutique, *, action="Je veux un échange en M.", line=1):
    thread = new_thread()
    preview = send_to_sav(boutique, thread_id=thread, line_number=line, message=action)
    assert preview["status"] == "intent_confirmation_needed"
    state = confirm_intent(boutique, thread)
    assert state["status"] == "awaiting_shop"
    assert "transmise" in state["customer_message"]
    return thread


def shop_route(boutique):
    return re.search(r'action="(/shop/support/[^"]+)"', get_page(boutique, "/shop/support"))[1]


def mail_link(boutique, decision):
    mailbox = get_page(boutique, "/support/mailbox/CLI-001")
    return re.search(r'href="(/support/reply/[^"/]+/' + decision + r'/[a-f0-9]+)"', mailbox)[1]


def test_exchange_resumes_after_commit_without_duplicate(database):
    cli(database, "init")
    opened = json.loads(cli(database, "open", "Je veux un échange en M.", "--demo", "exchange"))
    thread = opened["thread_id"]
    assert opened["interrupt"]["status"] == "item_needed"
    opened = json.loads(cli(database, "item", thread, "1"))
    assert opened["interrupt"]["status"] == "intent_confirmation_needed"
    opened = json.loads(cli(database, "clarify", thread, "Oui"))
    assert opened["status"] == "awaiting_shop"
    assert opened["interrupt"]["size"] == "M"
    assert stock(database) == 19

    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_returns").fetchone() == (0,)

    validated = json.loads(cli(database, "decide", thread, "yes"))
    assert validated["status"] == "awaiting_customer"
    link = validated["email_links"]["confirm"]
    cli(database, "reply", link, "--simulate-timeout", code=2)

    with psycopg.connect(database) as db:
        (reference,) = db.execute("SELECT id FROM shop.sav_returns").fetchone()
    resumed = json.loads(cli(database, "resume", thread))
    assert resumed["status"] == "completed"
    assert resumed["return_id"] == reference
    assert stock(database) == 19
    with psycopg.connect(database) as db:
        assert db.execute("SELECT id FROM shop.sav_returns").fetchall() == [(reference,)]
        assert db.execute("SELECT kind FROM shop.sav_messages ORDER BY kind").fetchall() == [
            ("confirmation",), ("shop_notification",), ("summary",),
        ]


def test_chat_article_then_variant_shop_and_signed_confirmation(database, boutique):
    opened = send_to_sav(boutique, thread_id=new_thread(), open=True)
    assert "Bonjour Camille !" in opened["greeting"]
    assert "DEMO-1001 est livrée" in opened["greeting"]
    assert "Pull en laine côtelée" in opened["greeting"]

    thread = f"CLI-001:{uuid4()}"
    unclear = {"thread_id": thread, "message": "Bonjour", "message_id": str(uuid4())}
    assert send_to_sav(boutique, **unclear)["status"] == "intent_needed"
    still_unclear = {**unclear, "message": "Pouvez-vous m’aider ?", "message_id": str(uuid4())}
    assert send_to_sav(boutique, **still_unclear)["status"] == "intent_needed"
    assert send_to_sav(boutique, **still_unclear)["status"] == "intent_needed"
    request = {"thread_id": thread, "message": "Le pull est trop petit", "message_id": str(uuid4())}
    order_choices = send_to_sav(boutique, **request)
    assert order_choices["status"] == "order_needed"
    assert [order["id"] for order in order_choices["orders"]] == ["DEMO-1001", "DEMO-1002"]
    assert send_to_sav(boutique, **request) == order_choices
    assert send_to_sav(boutique, thread_id=thread, select_order_id="DEMO-1006") == order_choices
    choices = send_to_sav(boutique, thread_id=thread, message="La première commande.")
    assert choices["status"] == "item_needed"
    assert [item["line_number"] for item in choices["items"]] == [1, 2, 3]
    assert send_to_sav(boutique, **request) == choices
    assert send_to_sav(boutique, thread_id=thread, select_line_number=999)["status"] == "item_needed"
    ambiguous = {"thread_id": thread, "message": "Je ne sais pas", "message_id": str(uuid4())}
    reprompt = send_to_sav(boutique, **ambiguous)
    assert reprompt["status"] == "item_needed"
    assert reprompt["ambiguous_reply"]
    assert send_to_sav(boutique, **ambiguous) == reprompt
    variant = send_to_sav(boutique, thread_id=thread, message="Le pull vert.")
    assert variant["status"] == "variant_needed"
    assert "Noir" in variant["colors"]
    same = {"thread_id": thread, "message": "Du S", "message_id": str(uuid4())}
    assert send_to_sav(boutique, **same)["status"] == "same_variant"
    assert send_to_sav(boutique, **same)["status"] == "same_variant"

    reply = {"thread_id": thread, "message": "En coloris Noir", "message_id": str(uuid4())}
    preview = send_to_sav(boutique, **reply)
    assert preview["status"] == "intent_confirmation_needed"
    registered = confirm_intent(boutique, thread)
    assert (registered["status"], registered["size"], registered["color"]) == ("awaiting_shop", "S", "Noir")
    assert send_to_sav(boutique, **reply) == registered
    config = {"configurable": {"thread_id": thread}}
    with open_checkpointer(database) as checkpointer:
        graph = build_graph(checkpointer, Shop(database), collect_demo, respond=reply_demo, select=select_demo)
        assert graph.get_state(config).next == ("wait_for_shop_approval",)
    with pytest.raises(HTTPError) as chat_approval:
        send_to_sav(boutique, thread_id=thread, approved=True)
    assert chat_approval.value.code == 400
    assert "Aucun message" in get_page(boutique, "/support/mailbox/CLI-001")

    route = shop_route(boutique)
    assert "Demande validée" in post_form(boutique, route, decision="yes")
    with open_checkpointer(database) as checkpointer:
        graph = build_graph(checkpointer, Shop(database), collect_demo, respond=reply_demo, select=select_demo)
        assert graph.get_state(config).next == ("wait_for_customer_confirmation",)
    default_mailbox = get_page(boutique, "/support/mailbox")
    assert '<option value="CLI-001" selected>Camille Moreau</option>' in default_mailbox
    assert '<option value="CLI-002">Noah Laurent</option>' in default_mailbox
    assert "Confirmez votre demande SAV" in default_mailbox
    other_mailbox = get_page(boutique, "/support/mailbox?customer=CLI-002")
    assert '<option value="CLI-002" selected>Noah Laurent</option>' in other_mailbox
    assert "Aucun message" in other_mailbox
    assert other_mailbox == get_page(boutique, "/support/mailbox/CLI-002")
    link = mail_link(boutique, "confirm")
    changed = link[:-1] + ("0" if link[-1] != "0" else "1")
    with pytest.raises(HTTPError) as forged:
        post_form(boutique, changed)
    assert forged.value.code == 404
    assert "Confirmer la demande" in get_page(boutique, link)
    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_returns").fetchone() == (0,)
        case_id = link.split("/")[-3]
        (secret,) = db.execute("SELECT secret FROM shop.sav_requests WHERE case_id = %s", (case_id,)).fetchone()

    signature = Shop.sign_decision(secret, case_id, "confirmer")
    legacy_link = f"/sav/reponse/{case_id}/confirmer/{signature}"
    assert "Confirmer la demande" in get_page(boutique, legacy_link)
    assert "demande est confirmée" in post_form(boutique, link)
    assert "demande est confirmée" in post_form(boutique, legacy_link)
    assert "Récapitulatif" in get_page(boutique, "/support/mailbox/CLI-001")
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
            post_form(boutique, route, decision="no")
        assert missing_reason.value.code == 400
        assert "Demande refusée" in post_form(boutique, route, decision="no", reason="Délai dépassé")
        mailbox = get_page(boutique, "/support/mailbox/CLI-001")
        assert "Délai dépassé" in mailbox
        status = "rejected_by_shop"
    else:
        post_form(boutique, route, decision="yes")
        assert stock(database) == 19
        link = mail_link(boutique, "decline")
        assert "demande est annulée" in post_form(boutique, link)
        assert "demande est annulée" in post_form(boutique, link)
        status = "cancelled"
    assert stock(database) == 20
    with psycopg.connect(database) as db:
        assert db.execute("SELECT status FROM shop.sav_requests WHERE thread_id = %s", (thread,)).fetchone() == (status,)
        assert db.execute("SELECT count(*) FROM shop.sav_returns").fetchone() == (0,)


def test_stock_failure_offers_return_without_notifying_shop(database, boutique):
    thread_id = new_thread()
    send_to_sav(boutique, thread_id=thread_id, line_number=1, message="Je veux un échange en XL.")
    out = confirm_intent(boutique, thread_id)
    assert out["status"] == "out_of_stock"
    assert "retourner" in out["customer_message"]
    assert "Aucune demande" in get_page(boutique, "/shop/support")
    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_requests").fetchone() == (0,)
    declined_thread = new_thread()
    send_to_sav(boutique, thread_id=declined_thread, line_number=1, message="Je veux un échange en XL.")
    assert confirm_intent(boutique, declined_thread)["status"] == "out_of_stock"
    declined = send_to_sav(boutique, thread_id=declined_thread, message="Non, merci.")
    assert declined["status"] == "request_abandoned"
    assert "Aucune demande" in get_page(boutique, "/shop/support")
    reply = {"thread_id": thread_id, "message": "Oui, je veux bien.", "message_id": str(uuid4())}
    returned = send_to_sav(boutique, **reply)
    assert (returned["status"], returned["action"], returned["line_number"]) == ("awaiting_shop", "return", 1)
    assert send_to_sav(boutique, **reply) == returned
    post_form(boutique, shop_route(boutique), decision="yes")
    post_form(boutique, mail_link(boutique, "confirm"))
    with psycopg.connect(database) as db:
        assert db.execute("SELECT line_number, action, size FROM shop.sav_returns").fetchall() == [(1, "return", None)]


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
        send_to_sav(boutique, thread_id=thread, line_number=1, message="Je veux un échange en M.")
        return confirm_intent(boutique, thread)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [new_thread(), new_thread()]))
    assert {result["status"] for result in results} == {"awaiting_shop", "out_of_stock"}
    assert stock(database) == 0
    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_requests").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM shop.sav_messages").fetchone() == (1,)


def test_purchase_history_and_case_are_scoped_to_customer_and_order(boutique):
    with urlopen(boutique + "/api/customers", timeout=10) as response:
        customers = json.load(response)["customers"]
    assert len(customers) == 20
    histories = {}
    for customer in customers:
        customer_id = customer["id"]
        with urlopen(f"{boutique}/api/customers/{customer_id}/orders", timeout=10) as response:
            histories[customer_id] = json.load(response)["orders"]
        assert 2 <= len(histories[customer_id]) <= 5
        assert all(order["customer_id"] == customer_id for order in histories[customer_id])
    all_orders = [order for orders in histories.values() for order in orders]
    assert len({item["product_id"] for order in all_orders for item in order["items"]}) == 20
    assert all(order["total_cents"] == sum(item["price_cents"] for item in order["items"]) for order in all_orders)
    with pytest.raises(HTTPError) as wrong_owner:
        send_to_sav(boutique, thread_id=new_thread("CLI-002"), message="Je veux un retour.")
    assert wrong_owner.value.code == 404
    for status, expected in [("confirmed", "order_confirmed"), ("in_transit", "order_in_transit")]:
        order = next(order for order in all_orders if order["status"] == status)
        result = send_to_sav(boutique, thread_id=new_thread(order["customer_id"], order["id"]), message="Je veux un retour.")
        assert result["status"] == expected


def test_information_is_forwarded_once_and_boutique_reply_reaches_mailbox(database, boutique):
    thread = f"CLI-003:{uuid4()}"
    pending = send_to_sav(boutique, thread_id=thread, message="J’ai une question.")
    assert pending["status"] == "question_needed"
    assert pending["awaiting_message"]
    assert pending["question"] == "Que souhaitez vous demander au sav ?"
    assert "Aucune demande" in get_page(boutique, "/shop/support")

    inquiry = "Où en est ma commande DEMO-1011 ?"
    reply = {"thread_id": thread, "message": inquiry, "message_id": str(uuid4())}
    sent = send_to_sav(boutique, **reply)
    assert sent["status"] == "information"
    assert sent["customer_message"] == "Votre question a été transmise ! Vous recevrez une réponse par mail"
    assert not sent.get("awaiting_message")
    assert send_to_sav(boutique, **reply) == sent
    with psycopg.connect(database) as db:
        assert db.execute("SELECT customer_id, question FROM shop.sav_inquiries").fetchall() == [("CLI-003", inquiry)]
        assert db.execute("SELECT count(*) FROM shop.sav_requests").fetchone() == (0,)

    shop_page = get_page(boutique, "/shop/support")
    assert inquiry in shop_page
    route = re.search(r'action="(/shop/support/inquiry/[^"]+)"', shop_page)[1]
    answer = "Votre commande est en livraison."
    assert "Réponse disponible" in post_form(boutique, route, answer=answer)
    assert "Aucune demande" in get_page(boutique, "/shop/support")
    mailbox = get_page(boutique, "/support/mailbox/CLI-003")
    assert "Réponse du SAV" in mailbox and answer in mailbox
    assert answer not in get_page(boutique, "/support/mailbox/CLI-001")


def test_global_chat_checks_order_ownership(database, boutique):
    def ask(message, customer="CLI-001"):
        thread = f"{customer}:{uuid4()}"
        return send_to_sav(boutique, thread_id=thread, message=message)

    undelivered = ask("Je veux retourner un article de la commande DEMO-1011.", customer="CLI-003")
    assert undelivered["status"] == "order_needed"
    assert undelivered["awaiting_message"]
    assert undelivered["unavailable_order"]
    assert all(order["id"] != "DEMO-1011" for order in undelivered["orders"])
    wrong_account = ask("Je veux échanger un article de la commande DEMO-1011.")
    assert wrong_account["status"] == "order_needed"
    assert all(order["id"] != "DEMO-1011" for order in wrong_account["orders"])
    own_order = ask("Je veux retourner un article de la commande DEMO-1001.")
    assert (own_order["status"], own_order["order_id"]) == ("item_needed", "DEMO-1001")
    with psycopg.connect(database) as db:
        assert db.execute("SELECT count(*) FROM shop.sav_requests").fetchone() == (0,)


def test_migrates_legacy_tables_and_preserves_completed_exchange(database):
    cli(database, "init")
    opened = json.loads(cli(database, "open", "Je veux un échange en M.", "--line-number", "1", "--demo", "exchange"))
    thread = opened["thread_id"]
    cli(database, "clarify", thread, "Oui")
    decision = json.loads(cli(database, "decide", thread, "yes"))
    completed = json.loads(cli(database, "reply", decision["email_links"]["confirm"]))

    with psycopg.connect(database) as db:
        (return_id,) = db.execute("SELECT id FROM shop.sav_returns").fetchone()
        db.execute("DROP TABLE shop.sav_messages, shop.sav_requests, shop.sav_color_stock")
        db.execute("ALTER TABLE shop.sav_returns DROP COLUMN color")
        in_transit = db.execute(
            "SELECT id, ordered_on FROM shop.sav_orders WHERE status = 'in_transit' ORDER BY id"
        ).fetchall()
        legacy_items = db.execute("""
            SELECT order_id, product_id, size, price_cents
            FROM shop.sav_order_items WHERE line_number = 1 AND order_id IN (
                SELECT id FROM shop.sav_orders WHERE status <> 'confirmed'
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
        db.execute("DELETE FROM shop.sav_orders WHERE status = 'confirmed'")
        db.execute("""
            UPDATE shop.sav_orders SET status = CASE status WHEN 'in_transit' THEN 'expediee' ELSE 'livree' END
        """)
        db.execute("""
            ALTER TABLE shop.sav_orders ADD CONSTRAINT sav_orders_status_check
                CHECK (status IN ('livree', 'expediee'))
        """)
        db.execute("UPDATE shop.sav_returns SET action = 'echange'")
        for schema, tables in (("shop", SHOP_TABLES), ("langgraph", CHECKPOINT_TABLES)):
            for table in tables:
                if table == "sav_order_items":
                    continue
                db.execute(sql.SQL("ALTER TABLE {} SET SCHEMA public").format(sql.Identifier(schema, table)))
            db.execute(sql.SQL("DROP SCHEMA {}").format(sql.Identifier(schema)))
        db.execute("CREATE TABLE public.unrelated (id integer)")

    cli(database, "init")
    cli(database, "init")
    resumed = json.loads(cli(database, "status", thread))
    assert resumed["status"] == "completed"
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
        assert db.execute("SELECT id, line_number, action FROM shop.sav_returns").fetchall() == [(return_id, 1, "exchange")]
        assert db.execute("""
            SELECT order_id, product_id, size, price_cents
            FROM shop.sav_order_items WHERE line_number = 1 AND order_id IN (
                SELECT id FROM shop.sav_orders WHERE status <> 'confirmed'
            ) ORDER BY order_id
        """).fetchall() == legacy_items
        assert db.execute(
            "SELECT id, ordered_on FROM shop.sav_orders WHERE status = 'in_transit' ORDER BY id"
        ).fetchall() == in_transit
        assert db.execute("SELECT count(*) FROM shop.sav_orders WHERE status = 'confirmed'").fetchone() == (5,)
        assert db.execute(
            "SELECT quantity FROM shop.sav_stock WHERE product_id = 'ART-001' AND size = 'M'"
        ).fetchone() == (19,)
